"""지식 배포본 수명주기 서비스 (SCHEMA.md 4.2).

draft 작성 → approve(전체 도메인 검토) → freeze → publish(RDF 빌드 + pySHACL + 도메인 검사) → activate.
publish는 실제로 검사를 실행한 결과만 validation_report/shacl_conforms/publish_checks_passed에 기록한다.
검사에 실패하면 배포본은 frozen으로 남고 보고서만 기록된다(동결 해제 없음). 수정은 새 draft로 한다.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from rdflib import Graph
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jsonutil import dumps
from app.db.sqlutil import execute, fetch_one
from app.ontology import builder
from app.ontology.checks import domain_checks_passed, run_domain_checks


class ReleaseError(Exception):
    pass


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def load_shapes() -> tuple[Graph, Graph]:
    shapes = Graph().parse(builder.SHAPES_FILE, format="turtle")
    ontology = Graph().parse(builder.ONTOLOGY_FILE, format="turtle")
    return shapes, ontology


def validate_graph(data: Graph) -> tuple[bool, str, int]:
    from pyshacl import validate

    shapes, ontology = load_shapes()
    conforms, results_graph, results_text = validate(
        data, shacl_graph=shapes, ont_graph=ontology, inference="none",
        abort_on_first=False, allow_warnings=False, meta_shacl=False, advanced=False, debug=False,
    )
    from rdflib.namespace import SH

    if not isinstance(results_graph, Graph):
        # shapes 자체 오류 등으로 검사가 수행되지 않았다. 통과로 취급하지 않는다.
        return False, f"SHACL validation could not run: {results_text}", -1
    violations = len(list(results_graph.subjects(predicate=SH.resultSeverity)))
    return bool(conforms), str(results_text), violations


async def release_by_version(session: AsyncSession, version: str) -> dict[str, Any]:
    row = await fetch_one(session, "SELECT * FROM rsw.knowledge_releases WHERE version = :v", v=version)
    if row is None:
        raise ReleaseError(f"release {version!r} not found")
    return row


async def actor_id(session: AsyncSession, external_key: str) -> UUID:
    row = await fetch_one(session, "SELECT id FROM rsw.actors WHERE external_key = :k AND active", k=external_key)
    if row is None:
        raise ReleaseError(f"actor {external_key!r} not found")
    return row["id"]


async def create_draft(session: AsyncSession, *, version: str, created_by: UUID, parent_release_id: UUID | None = None,
                       validation_contract_version: str = "rsw-conditions-v1") -> UUID:
    row = await fetch_one(
        session,
        """
        INSERT INTO rsw.knowledge_releases
          (version, parent_release_id, ontology_version, ontology_sha256, shapes_version, shapes_sha256,
           builder_version, validation_contract_version, created_by)
        VALUES (:version, :parent, :ov, :osha, :sv, :ssha, :bv, :vcv, :created_by)
        RETURNING id
        """,
        version=version, parent=parent_release_id, ov=builder.ONTOLOGY_VERSION, osha=builder.ontology_sha256(),
        sv=builder.SHAPES_VERSION, ssha=builder.shapes_sha256(), bv=builder.BUILDER_VERSION,
        vcv=validation_contract_version, created_by=created_by,
    )
    assert row is not None
    return row["id"]


async def approve(session: AsyncSession, release_id: UUID, reviewer_id: UUID) -> None:
    role = await fetch_one(session, "SELECT role FROM rsw.actors WHERE id = :id", id=reviewer_id)
    if role is None or role["role"] != "domain_reviewer":
        raise ReleaseError("전체 배포본 승인은 domain_reviewer만 할 수 있습니다.")
    count = await execute(
        session,
        """
        UPDATE rsw.knowledge_releases SET domain_reviewed_by = :rid_actor, domain_reviewed_at = now()
        WHERE id = :id AND status = 'draft'
        """,
        id=release_id, rid_actor=reviewer_id,
    )
    if count != 1:
        raise ReleaseError("draft 상태의 배포본만 승인할 수 있습니다.")


async def freeze(session: AsyncSession, release_id: UUID) -> None:
    # 트리거가 승인·원문 봉인 여부를 검사하고 frozen_at을 설정한다.
    await execute(session, "UPDATE rsw.knowledge_releases SET status = 'frozen' WHERE id = :id", id=release_id)


async def publish(session: AsyncSession, release_id: UUID, storage_root: Path) -> dict[str, Any]:
    """frozen 배포본의 RDF를 만들고 SHACL/도메인 검사를 실행한다. 통과하면 published로 전환한다."""
    release = await fetch_one(session, "SELECT * FROM rsw.knowledge_releases WHERE id = :id FOR UPDATE", id=release_id)
    if release is None:
        raise ReleaseError("release not found")
    if release["status"] != "frozen":
        raise ReleaseError(f"frozen 배포본만 발행할 수 있습니다 (현재 {release['status']}).")

    contract_errors = []
    if release["ontology_sha256"] != builder.ontology_sha256():
        contract_errors.append("ontology_sha256가 현재 rsw.ttl과 다릅니다.")
    if release["shapes_sha256"] != builder.shapes_sha256():
        contract_errors.append("shapes_sha256가 현재 shapes.ttl과 다릅니다.")
    if release["builder_version"] != builder.BUILDER_VERSION:
        contract_errors.append(f"builder_version {release['builder_version']} != {builder.BUILDER_VERSION}")

    snapshot = await builder.load_snapshot(session, release_id)
    graph = builder.build_graph(snapshot)
    data = builder.canonical_ntriples(graph)
    graph_sha = hashlib.sha256(data).hexdigest()
    conforms, shacl_text, violation_count = validate_graph(graph)
    domain = await run_domain_checks(session, release_id)
    checks_ok = domain_checks_passed(domain) and not contract_errors

    relative = f"graphs/{release['version']}-{graph_sha[:16]}.nt"
    target = storage_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    report = {
        "report_version": 1,
        "executed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tools": {"rdflib": _version("rdflib"), "pyshacl": _version("pyshacl")},
        "contract": {
            "ontology_version": release["ontology_version"], "ontology_sha256": release["ontology_sha256"],
            "shapes_version": release["shapes_version"], "shapes_sha256": release["shapes_sha256"],
            "builder_version": release["builder_version"],
            "validation_contract_version": release["validation_contract_version"],
            "errors": contract_errors,
        },
        "graph": {"uri": f"storage:{relative}", "sha256": graph_sha, "triples": len(graph), "format": "sorted N-Triples"},
        "shacl": {"conforms": conforms, "violation_count": violation_count, "inference": "none",
                  "report_text": shacl_text[:20000]},
        "domain_checks": domain,
        "result": "published" if (conforms and checks_ok) else "rejected",
    }
    params = dict(id=release_id, uri=f"storage:{relative}", sha=graph_sha, report=dumps(report),
                  conforms=conforms, checks=checks_ok)
    if conforms and checks_ok:
        await execute(
            session,
            """
            UPDATE rsw.knowledge_releases
            SET status = 'published', graph_uri = :uri, graph_sha256 = :sha, validation_report = CAST(:report AS jsonb),
                shacl_conforms = :conforms, publish_checks_passed = :checks
            WHERE id = :id
            """,
            **params,
        )
    else:
        await execute(
            session,
            """
            UPDATE rsw.knowledge_releases
            SET graph_uri = :uri, graph_sha256 = :sha, validation_report = CAST(:report AS jsonb),
                shacl_conforms = :conforms, publish_checks_passed = :checks
            WHERE id = :id
            """,
            **params,
        )
    return report


async def activate(session: AsyncSession, release_id: UUID, deployment: str, actor: UUID) -> None:
    await execute(
        session,
        """
        INSERT INTO rsw.knowledge_deployments (name, release_id, activated_by)
        VALUES (:name, :rid, :actor)
        ON CONFLICT (name) DO UPDATE
          SET release_id = EXCLUDED.release_id, activated_by = EXCLUDED.activated_by, activated_at = now()
        """,
        name=deployment, rid=release_id, actor=actor,
    )
