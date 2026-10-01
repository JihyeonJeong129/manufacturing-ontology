"""지식 배포본 수명주기: 승인 → 동결 → RDF/SHACL/도메인 검사 → 발행 → 활성화."""
import hashlib
import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.jsonutil import as_json
from app.db.sqlutil import execute, fetch_one
from app.ontology import builder, publisher


async def test_demo_release_was_published_with_real_validation(database, seed, cfg):
    async with database.session() as s:
        release = await fetch_one(s, "SELECT * FROM rsw.knowledge_releases WHERE id = :id", id=seed["release_id"])
        deployment = await fetch_one(s, "SELECT release_id FROM rsw.knowledge_deployments WHERE name = :n", n=cfg.KNOWLEDGE_DEPLOYMENT)
    assert release["status"] == "published" and release["shacl_conforms"] and release["publish_checks_passed"]
    assert deployment["release_id"] == release["id"]
    report = as_json(release["validation_report"])
    assert report["shacl"]["conforms"] is True and report["result"] == "published"
    assert report["contract"]["ontology_sha256"] == builder.ontology_sha256()
    assert {c["check"] for c in report["domain_checks"]} >= {"no_draft_content", "procedure_steps_applicable"}
    graph_file = cfg.STORAGE_ROOT / release["graph_uri"].removeprefix("storage:")
    assert hashlib.sha256(graph_file.read_bytes()).hexdigest() == release["graph_sha256"]


async def test_graph_is_reproducible_from_published_release(database, seed):
    async with database.session() as s:
        release = await fetch_one(s, "SELECT graph_sha256 FROM rsw.knowledge_releases WHERE id = :id", id=seed["release_id"])
        snapshot = await builder.load_snapshot(s, seed["release_id"])
    # 발행 시에는 frozen 상태에서 빌드했으므로 같은 조건으로 다시 만든다.
    snapshot.release["status"] = "frozen"
    data = builder.canonical_ntriples(builder.build_graph(snapshot))
    assert hashlib.sha256(data).hexdigest() == release["graph_sha256"]


async def test_published_release_and_its_content_are_immutable(database, seed):
    for sql in ("UPDATE rsw.knowledge_releases SET builder_version = 'x' WHERE id = :id",
                "UPDATE rsw.entity_versions SET description = 'late edit' WHERE release_id = :id"):
        async with database.session() as s:
            with pytest.raises(DBAPIError):
                async with s.begin():
                    await execute(s, sql, id=seed["release_id"])


async def _draft_with_relation(s, *, with_evidence: bool, review_status: str = "reviewed") -> uuid.UUID:
    developer = await publisher.actor_id(s, "demo:developer")
    release_id = await publisher.create_draft(s, version=f"test-{uuid.uuid4().hex[:8]}", created_by=developer)
    ns = f"test.{uuid.uuid4().hex[:6]}"
    ids = {}
    for kind, code in (("alarm_code", "E1"), ("fault_type", "F1")):
        row = await fetch_one(s, "INSERT INTO rsw.knowledge_entities (kind, namespace, local_code) VALUES (:k, :n, :c) RETURNING id",
                              k=kind, n=ns, c=code)
        ids[kind] = row["id"]
        await execute(s, """INSERT INTO rsw.entity_versions (release_id, entity_id, entity_kind, label_ko, description, review_status)
                            VALUES (:r, :e, :k, '시험', '시험', :rs)""", r=release_id, e=row["id"], k=kind, rs=review_status)
    section = await fetch_one(s, "SELECT id FROM rsw.document_sections WHERE section_key = 'sec-2.1'")
    await execute(s, """INSERT INTO rsw.entity_versions (release_id, entity_id, entity_kind, label_ko, description, review_status)
                        VALUES (:r, :e, 'document_section', '절', '절', 'reviewed')""", r=release_id, e=section["id"])
    rel = await fetch_one(s, """
        INSERT INTO rsw.knowledge_relations (release_id, subject_id, subject_kind, predicate, object_id, object_kind, evidence_kind, review_status)
        VALUES (:r, :s, 'alarm_code', 'maps_to', :o, 'fault_type', 'manual', :rs) RETURNING id""",
        r=release_id, s=ids["alarm_code"], o=ids["fault_type"], rs=review_status)
    if with_evidence:
        await execute(s, """INSERT INTO rsw.relation_evidence (release_id, relation_id, section_id, evidence_role, explanation)
                            VALUES (:r, :rel, :sec, 'supports', '시험')""", r=release_id, rel=rel["id"], sec=section["id"])
    return release_id


async def test_freeze_requires_whole_release_approval(database):
    async with database.session() as s:
        with pytest.raises(DBAPIError) as info:
            async with s.begin():
                release_id = await _draft_with_relation(s, with_evidence=True)
                await publisher.freeze(s, release_id)
    assert "Whole-release domain review" in str(info.value)


async def test_only_domain_reviewers_can_approve(database):
    async with database.session() as s:
        await s.begin()
        try:
            release_id = await _draft_with_relation(s, with_evidence=True)
            with pytest.raises(publisher.ReleaseError):
                await publisher.approve(s, release_id, await publisher.actor_id(s, "demo:operator"))
        finally:
            await s.rollback()


async def test_shacl_failure_keeps_release_frozen_with_report(database, cfg):
    async with database.session() as s, s.begin():
        release_id = await _draft_with_relation(s, with_evidence=False)
        await publisher.approve(s, release_id, await publisher.actor_id(s, "demo:reviewer"))
        await publisher.freeze(s, release_id)
        with pytest.raises(publisher.ReleaseError):
            await publisher.publish(s, uuid.uuid4(), cfg.STORAGE_ROOT)
        report = await publisher.publish(s, release_id, cfg.STORAGE_ROOT)
        row = await fetch_one(s, "SELECT status, shacl_conforms, publish_checks_passed, validation_report FROM rsw.knowledge_releases WHERE id = :id", id=release_id)
    assert report["result"] == "rejected" and report["shacl"]["conforms"] is False
    assert "지지(supports) 근거" in report["shacl"]["report_text"]
    assert row["status"] == "frozen" and row["shacl_conforms"] is False
    assert as_json(row["validation_report"])["result"] == "rejected"


async def test_domain_checks_block_draft_content(database, cfg):
    async with database.session() as s, s.begin():
        release_id = await _draft_with_relation(s, with_evidence=True, review_status="draft")
        await publisher.approve(s, release_id, await publisher.actor_id(s, "demo:reviewer"))
        await publisher.freeze(s, release_id)
        report = await publisher.publish(s, release_id, cfg.STORAGE_ROOT)
        status = (await fetch_one(s, "SELECT status FROM rsw.knowledge_releases WHERE id = :id", id=release_id))["status"]
    failed = {c["check"] for c in report["domain_checks"] if not c["passed"]}
    assert "no_draft_content" in failed
    assert report["result"] == "rejected" and status == "frozen"


async def test_publish_rejects_ontology_contract_drift(database, cfg):
    async with database.session() as s, s.begin():
        release_id = await _draft_with_relation(s, with_evidence=True)
        await execute(s, "UPDATE rsw.knowledge_releases SET shapes_sha256 = :h WHERE id = :id", h="0" * 64, id=release_id)
        await publisher.approve(s, release_id, await publisher.actor_id(s, "demo:reviewer"))
        await publisher.freeze(s, release_id)
        report = await publisher.publish(s, release_id, cfg.STORAGE_ROOT)
    assert report["result"] == "rejected"
    assert report["contract"]["errors"]
