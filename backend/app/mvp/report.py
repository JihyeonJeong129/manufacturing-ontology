"""10개 상황별 적재 결과 요약 (DB에서 다시 읽어 계산한다)."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sqlutil import fetch_all, fetch_one, fetch_value
from app.mvp.catalog_knowledge import NS_SCENARIO


async def active_release_id(session: AsyncSession, deployment: str) -> UUID | None:
    return await fetch_value(session, "SELECT release_id FROM rsw.knowledge_deployments WHERE name = :n", n=deployment)


async def _objects(session: AsyncSession, rid: UUID, predicate: str, subjects: list[UUID]) -> list[dict[str, Any]]:
    if not subjects:
        return []
    return await fetch_all(session, """
        SELECT r.id, r.subject_id, r.object_id, r.object_kind, ke.namespace, ke.local_code
        FROM rsw.knowledge_relations r JOIN rsw.knowledge_entities ke ON ke.id = r.object_id
        WHERE r.release_id = :rid AND r.predicate = :p AND r.subject_id = ANY(:ids) AND r.review_status = 'reviewed'
        ORDER BY ke.local_code""", rid=rid, p=predicate, ids=subjects)


async def _subjects(session: AsyncSession, rid: UUID, predicate: str, objects: list[UUID]) -> list[dict[str, Any]]:
    if not objects:
        return []
    return await fetch_all(session, """
        SELECT r.id, r.subject_id, r.object_id, ke.namespace, ke.local_code
        FROM rsw.knowledge_relations r JOIN rsw.knowledge_entities ke ON ke.id = r.subject_id
        WHERE r.release_id = :rid AND r.predicate = :p AND r.object_id = ANY(:ids) AND r.review_status = 'reviewed'
        ORDER BY ke.local_code""", rid=rid, p=predicate, ids=objects)


async def scenario_report(session: AsyncSession, release_id: UUID) -> list[dict[str, Any]]:
    """상황마다 연결된 코드·규칙·원인·절차·수치·원문 절·검색 청크·임베딩 수를 센다."""
    scenarios = await fetch_all(session, """
        SELECT ke.id, ke.local_code, ev.label_ko, sc.scenario_kind, jsonb_array_length(sc.question_examples) AS questions
        FROM rsw.scenarios sc
        JOIN rsw.entity_versions ev ON ev.release_id = sc.release_id AND ev.entity_id = sc.entity_id
        JOIN rsw.knowledge_entities ke ON ke.id = sc.entity_id
        WHERE sc.release_id = :rid AND ke.namespace = :ns ORDER BY ke.local_code""", rid=release_id, ns=NS_SCENARIO)
    model = await fetch_one(session, "SELECT id FROM rsw.embedding_models ORDER BY id LIMIT 1")
    rows = []
    for s in scenarios:
        sid = s["id"]
        faults = await _objects(session, release_id, "related_fault", [sid])
        codes = await _subjects(session, release_id, "maps_to", [f["object_id"] for f in faults])
        rules = await _subjects(session, release_id, "triggers", [sid])
        causes = await _objects(session, release_id, "raises_hypothesis", [sid])
        cause_ids = [c["object_id"] for c in causes]
        patterns = await _subjects(session, release_id, "suggests", cause_ids)
        contributes = await _objects(session, release_id, "may_contribute_to", cause_ids)
        procedures = await _objects(session, release_id, "has_procedure", [sid])
        proc_ids = [p["object_id"] for p in procedures]
        components = await _objects(session, release_id, "concerns_component", cause_ids + proc_ids)
        specs = await _objects(session, release_id, "uses_spec", proc_ids)
        documented = await _objects(session, release_id, "documented_in", [sid] + cause_ids + proc_ids)
        section_ids = list({d["object_id"] for d in documented})
        steps = await fetch_value(session, """
            SELECT count(*) FROM rsw.procedure_steps WHERE release_id = :rid AND procedure_id = ANY(:ids)""",
            rid=release_id, ids=proc_ids) if proc_ids else 0
        docs = await fetch_all(session, """
            SELECT dr.document_key, count(*) AS n FROM rsw.document_sections ds
            JOIN rsw.document_revisions dr ON dr.id = ds.document_revision_id
            WHERE ds.id = ANY(:ids) GROUP BY dr.document_key ORDER BY dr.document_key""", ids=section_ids) if section_ids else []
        chunks = await fetch_value(session, """
            SELECT count(*) FROM rsw.release_chunks WHERE release_id = :rid AND section_id = ANY(:ids)""",
            rid=release_id, ids=section_ids) if section_ids else 0
        embedded = await fetch_value(session, """
            SELECT count(*) FROM rsw.release_chunks rc JOIN rsw.chunk_embeddings ce ON ce.chunk_id = rc.chunk_id
            WHERE rc.release_id = :rid AND rc.section_id = ANY(:ids) AND ce.embedding_model_id = :m""",
            rid=release_id, ids=section_ids, m=model["id"]) if section_ids and model else 0
        relation_ids = [r["id"] for r in faults + codes + rules + causes + patterns + contributes + procedures
                        + components + specs + documented]
        evidence = await fetch_value(session, """
            SELECT count(*) FROM rsw.relation_evidence WHERE release_id = :rid AND relation_id = ANY(:ids)""",
            rid=release_id, ids=relation_ids) if relation_ids else 0
        case = await fetch_one(session, """
            SELECT case_key, sealed_at IS NOT NULL AS sealed FROM rsw.evaluation_cases
            WHERE release_id = :rid AND case_key = :k""", rid=release_id, k=f"rsw-mvp-{s['local_code']}")
        rows.append({
            "scenario": s["local_code"], "label": s["label_ko"], "kind": s["scenario_kind"],
            "question_examples": s["questions"],
            "faults": [f["local_code"] for f in faults], "error_codes": sorted({c["local_code"] for c in codes}),
            "ml_rules": [r["local_code"] for r in rules], "causes": len(causes),
            "patterns": sorted({p["local_code"] for p in patterns}),
            "contributes_to": sorted({c["local_code"] for c in contributes}),
            "components": len({c["object_id"] for c in components}),
            "procedure_steps": steps, "specs": len(specs), "sections": len(section_ids),
            "sections_by_document": {d["document_key"]: d["n"] for d in docs},
            "chunks": chunks, "embedded_chunks": embedded, "relations": len(relation_ids),
            "relation_evidence": evidence, "evaluation_case": case["case_key"] if case and case["sealed"] else None,
        })
    return rows


async def release_totals(session: AsyncSession, release_id: UUID) -> dict[str, Any]:
    queries = {
        "entities": "SELECT count(*) FROM rsw.entity_versions WHERE release_id = :rid",
        "relations": "SELECT count(*) FROM rsw.knowledge_relations WHERE release_id = :rid",
        "relation_evidence": "SELECT count(*) FROM rsw.relation_evidence WHERE release_id = :rid",
        "applicability": "SELECT count(*) FROM rsw.applicability WHERE release_id = :rid",
        "procedure_steps": "SELECT count(*) FROM rsw.procedure_steps WHERE release_id = :rid",
        "numeric_specs": "SELECT count(*) FROM rsw.numeric_specs WHERE release_id = :rid",
        "feature_bindings": "SELECT count(*) FROM rsw.feature_bindings WHERE release_id = :rid",
        "release_chunks": "SELECT count(*) FROM rsw.release_chunks WHERE release_id = :rid",
        "knowledge_conflicts": "SELECT count(*) FROM rsw.knowledge_conflicts WHERE release_id = :rid",
        "evaluation_cases": "SELECT count(*) FROM rsw.evaluation_cases WHERE release_id = :rid",
    }
    totals = {k: await fetch_value(session, q, rid=release_id) for k, q in queries.items()}
    by_kind = await fetch_all(session, """
        SELECT entity_kind, count(*) AS n FROM rsw.entity_versions WHERE release_id = :rid GROUP BY entity_kind ORDER BY 1""",
        rid=release_id)
    by_predicate = await fetch_all(session, """
        SELECT predicate, count(*) AS n FROM rsw.knowledge_relations WHERE release_id = :rid GROUP BY predicate ORDER BY 1""",
        rid=release_id)
    totals["entities_by_kind"] = {r["entity_kind"]: r["n"] for r in by_kind}
    totals["relations_by_predicate"] = {r["predicate"]: r["n"] for r in by_predicate}
    totals["document_pages"] = await fetch_value(session, "SELECT count(*) FROM rsw.document_pages")
    totals["chunk_embeddings"] = await fetch_value(session, """
        SELECT count(*) FROM rsw.chunk_embeddings ce JOIN rsw.release_chunks rc ON rc.chunk_id = ce.chunk_id
        WHERE rc.release_id = :rid""", rid=release_id)
    return totals


def format_table(rows: list[dict[str, Any]]) -> str:
    header = ["상황", "고장", "에러코드", "ML규칙", "원인", "패턴", "부품", "절차단계", "수치", "원문절", "청크", "임베딩", "관계", "근거", "평가케이스"]
    lines = [" | ".join(header), " | ".join("---" for _ in header)]
    for r in rows:
        lines.append(" | ".join(str(x) for x in [
            r["scenario"], ",".join(r["faults"]) or "-", ",".join(r["error_codes"]) or "-", ",".join(r["ml_rules"]),
            r["causes"], ",".join(r["patterns"]), r["components"], r["procedure_steps"], r["specs"], r["sections"],
            r["chunks"], r["embedded_chunks"], r["relations"], r["relation_evidence"], "O" if r["evaluation_case"] else "-"]))
    return "\n".join(lines)
