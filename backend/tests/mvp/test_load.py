"""실제 지식 적재 결과 검증 (DB). 10개 상황별 삽입 결과와 연결 경로를 DB에서 다시 읽어 확인한다."""
from __future__ import annotations

import pytest

from app.db.sqlutil import fetch_all, fetch_one, fetch_value
from app.mvp import catalog_knowledge as K
from app.mvp.loader import RELEASE_VERSION, load_mvp
from app.mvp.report import release_totals, scenario_report
from app.mvp.scenarios import CASES

pytestmark = pytest.mark.asyncio

# 상황별 기대 삽입 결과 (카탈로그 = 정의서 기준): 고장, 에러 코드, ML 규칙, 원인 수, 절차 단계 수, 수치 수
EXPECTED = {
    "S01": (["E01"], ["E012"], ["P1"], 4, 5, 3),
    "S02": (["E02"], ["E016"], ["P5", "P6"], 4, 4, 1),
    "S03": (["E03"], ["E028"], ["P5"], 4, 4, 1),
    "S04": (["E04"], ["E029"], ["P4"], 2, 4, 1),
    "S05": ([], [], ["P2"], 4, 4, 2),
    "S06": ([], [], ["P3"], 4, 4, 2),
    "S07": ([], [], ["P6"], 3, 4, 2),
    "S08": ([], [], ["P5"], 3, 3, 0),
    "S09": ([], [], ["P9"], 4, 3, 4),
    "S10": ([], [], ["P7", "P8"], 3, 3, 0),
}


async def _release(session) -> dict:
    return await fetch_one(session, "SELECT * FROM rsw.knowledge_releases WHERE version = :v", v=RELEASE_VERSION)


async def test_release_published_validated_and_active(session, load_report, cfg):
    release = await _release(session)
    assert release["status"] == "published"
    assert release["shacl_conforms"] is True and release["publish_checks_passed"] is True
    assert release["domain_reviewed_by"] is not None
    report = release["validation_report"]
    assert report["result"] == "published" and report["graph"]["triples"] > 5000
    assert all(c["passed"] for c in report["domain_checks"])
    active = await fetch_value(session, "SELECT release_id FROM rsw.knowledge_deployments WHERE name = :n",
                               n=cfg.KNOWLEDGE_DEPLOYMENT)
    assert active == release["id"]
    assert load_report["created"] is True and load_report["embeddings"]["status"] in ("ok", "model_missing")
    assert (cfg.STORAGE_ROOT / report["graph"]["uri"].removeprefix("storage:")).is_file()


async def test_release_totals(session):
    release = await _release(session)
    totals = await release_totals(session, release["id"])
    assert totals["entities_by_kind"] == {
        "alarm_code": 20, "component_type": 15, "diagnostic_rule": 9, "document_section": 88, "evidence_pattern": 9,
        "fault_type": 4, "feature": 18, "hypothesis": 29, "numeric_spec": 14, "procedure": 10, "scenario": 10}
    assert totals["relations"] == len(K.RELATIONS)
    assert totals["relation_evidence"] >= totals["relations"]
    assert totals["procedure_steps"] == sum(len(p.steps) for p in K.PROCEDURES)
    assert totals["feature_bindings"] == 18 and totals["knowledge_conflicts"] == 1
    assert totals["evaluation_cases"] == len(CASES)
    assert totals["document_pages"] == 21 + 9 + 101 + 49 + 56 + 266 + 450


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
async def test_insertion_result_per_scenario(session, scenario, model_ready):
    release = await _release(session)
    row = next(r for r in await scenario_report(session, release["id"]) if r["scenario"] == scenario)
    faults, codes, rules, causes, steps, specs = EXPECTED[scenario]
    assert row["faults"] == faults
    assert row["error_codes"] == codes
    assert row["ml_rules"] == rules
    assert row["causes"] == causes
    assert row["procedure_steps"] == steps
    assert row["specs"] == specs
    assert row["patterns"], "원인 후보를 가리키는 증상 패턴이 있어야 한다"
    assert row["components"] >= 1
    assert "rsw-mvp-situations" in row["sections_by_document"]
    assert row["sections"] >= 6 and row["chunks"] >= row["sections"]
    assert row["relation_evidence"] >= row["relations"]
    assert row["evaluation_case"] == f"rsw-mvp-{scenario}"
    if model_ready:
        assert row["embedded_chunks"] == row["chunks"]


async def test_question_type_paths(session):
    """정의서 '작업자 질문 유형' 6가지 경로가 DB 관계로 이어지는지 확인한다."""
    rid = (await _release(session))["id"]

    async def objects(subject_ns: str, subject_code: str, predicate: str) -> list[dict]:
        return await fetch_all(session, """
            SELECT o.namespace, o.local_code, r.object_id FROM rsw.knowledge_relations r
            JOIN rsw.knowledge_entities s ON s.id = r.subject_id JOIN rsw.knowledge_entities o ON o.id = r.object_id
            WHERE r.release_id = :rid AND s.namespace = :ns AND s.local_code = :c AND r.predicate = :p
            ORDER BY o.local_code""", rid=rid, ns=subject_ns, c=subject_code, p=predicate)

    # 1) 상황 파악: 에러 코드 E028 → 고장 E03 → 정의 원문
    fault = await objects(K.NS_ERROR, "E028", "maps_to")
    assert [f["local_code"] for f in fault] == ["E03"]
    definitions = await fetch_all(session, """
        SELECT sp.excerpt FROM rsw.knowledge_relations r JOIN rsw.relation_evidence e ON e.relation_id = r.id
        JOIN rsw.section_spans sp ON sp.id = e.span_id
        WHERE r.release_id = :rid AND r.subject_id = :f AND r.predicate = 'documented_in'""", rid=rid, f=fault[0]["object_id"])
    assert any("스트로크의 6.5%" in d["excerpt"] for d in definitions)
    # 2) 원인: 증상 P1(보정 압력↓ 힘 형성 시간↑) → 원인 후보
    assert {c["local_code"] for c in await objects(K.NS_PATTERN, "P1", "suggests")} == {"H01", "H02", "H03", "H04"}
    # 3) 점검 절차: S04 → 절차 → 단계 근거가 Festo 9.14.10 잠금 진단
    steps = await fetch_all(session, """
        SELECT ps.step_number, ds.section_key, sp.excerpt FROM rsw.procedure_steps ps
        JOIN rsw.document_sections ds ON ds.id = ps.source_section_id JOIN rsw.section_spans sp ON sp.id = ps.source_span_id
        JOIN rsw.knowledge_entities p ON p.id = ps.procedure_id
        WHERE ps.release_id = :rid AND p.local_code = 'S04-CHECK' ORDER BY ps.step_number""", rid=rid)
    assert [s["section_key"] for s in steps] == ["F-9.14.10", "F-9.14.10", "F-10.1-11", "D-S04"]
    assert "blocking valves 1V2, 1V3, 1V4" in steps[0]["excerpt"]
    # 4) 수치·사양: S09 절차 → 냉각수 기준 (단위·원문 표기 포함)
    specs = await fetch_all(session, """
        SELECT ns.value_low, ns.value_high, ns.unit, ns.comparison, ds.section_key FROM rsw.knowledge_relations r
        JOIN rsw.numeric_specs ns ON ns.release_id = r.release_id AND ns.entity_id = r.object_id
        JOIN rsw.document_sections ds ON ds.id = ns.source_section_id JOIN rsw.knowledge_entities p ON p.id = r.subject_id
        WHERE r.release_id = :rid AND r.predicate = 'uses_spec' AND p.local_code = 'S09-CHECK'""", rid=rid)
    assert {(float(s["value_low"]), s["unit"], s["comparison"]) for s in specs} >= {(4.0, "L/min", "ge"), (8.0, "L/min", "ge"), (18.0, "°C", "range")}
    assert {s["section_key"] for s in specs} == {"M-9-1", "M-3-1"}
    # 5) 조치·예방: 필터 교체 주기 100만 용접점 (Festo 6장)
    spec = await fetch_one(session, """
        SELECT ns.value_low, ns.unit FROM rsw.numeric_specs ns JOIN rsw.knowledge_entities e ON e.id = ns.entity_id
        WHERE ns.release_id = :rid AND e.local_code = 'FESTO-FILTER'""", rid=rid)
    assert float(spec["value_low"]) == 1_000_000 and spec["unit"] == "welding points"
    # 6) 경보 판단: ML 규칙 P7·P8 → S10 (오경보 구분 상황)
    assert {s["local_code"] for s in await objects(K.NS_RULE, "P8", "triggers")} == {"S10"}
    # 원인 → 부품 (발생 위치)
    assert {c["local_code"] for c in await objects(K.NS_CAUSE, "H16", "concerns_component")} == {"blocking_valves"}


async def test_applicability_tiers_and_drive_type(session):
    rid = (await _release(session))["id"]
    rows = await fetch_all(session, """
        SELECT dr.document_key, a.reference_scope, count(*) AS n FROM rsw.applicability a
        JOIN rsw.document_sections ds ON ds.id = a.content_entity_id
        JOIN rsw.document_revisions dr ON dr.id = ds.document_revision_id
        WHERE a.release_id = :rid AND a.content_kind = 'document_section'
        GROUP BY dr.document_key, a.reference_scope""", rid=rid)
    scopes = {r["document_key"]: r["reference_scope"] for r in rows}
    assert scopes["festo-cpx-sb-lt-weld-sa"] == "applicable"
    assert scopes["milco-modular-weld-guns"] == "analogous"
    assert scopes["fanuc-r30ib-plus-servo-gun"] == "comparison"
    meta = await fetch_all(session, """
        SELECT DISTINCT ev.metadata->>'drive_type' AS drive FROM rsw.entity_versions ev
        WHERE ev.release_id = :rid AND ev.entity_kind = 'document_section' AND ev.metadata->>'manual_id' IS NOT NULL""", rid=rid)
    assert {m["drive"] for m in meta} == {"servo_pneumatic", "pneumatic", "control_software", "servo_electric"}
    config = await fetch_one(session, "SELECT * FROM rsw.asset_configs ac JOIN rsw.assets a ON a.id = ac.asset_id WHERE a.asset_code = 'RSW-GUN-01'")
    assert config["verification_status"] == "verified" and config["drive_type"] == "servo_pneumatic"
    assert "가정" in config["verification_basis"]


async def test_every_span_is_the_sealed_page_text(session):
    mismatches = await fetch_value(session, """
        SELECT count(*) FROM rsw.section_spans sp
        JOIN rsw.document_pages dp ON dp.document_revision_id = sp.document_revision_id AND dp.page_number = sp.page_number
        WHERE substr(dp.page_text, sp.char_start + 1, sp.char_end - sp.char_start) <> sp.excerpt""")
    assert mismatches == 0
    unsealed = await fetch_value(session, "SELECT count(*) FROM rsw.document_revisions WHERE sealed_at IS NULL")
    assert unsealed == 0
    labels = await fetch_all(session, """
        SELECT dr.document_key, dp.page_number, dp.printed_page_label FROM rsw.document_pages dp
        JOIN rsw.document_revisions dr ON dr.id = dp.document_revision_id
        WHERE (dr.document_key, dp.page_number) IN (('fanuc-r30ib-plus-servo-gun', 310), ('yaskawa-yrc1000-air-gun', 48),
                                                    ('festo-cpx-sb-lt-weld-sa', 90), ('milco-modular-weld-guns', 27))""")
    assert {(r["document_key"], r["printed_page_label"]) for r in labels} == {
        ("fanuc-r30ib-plus-servo-gun", "294"), ("yaskawa-yrc1000-air-gun", "1-40"),
        ("festo-cpx-sb-lt-weld-sa", "90"), ("milco-modular-weld-guns", "27")}


async def test_embeddings_cover_every_release_chunk(session, model_ready):
    if not model_ready:
        pytest.skip("임베딩 모델 파일이 없습니다")
    rid = (await _release(session))["id"]
    model = await fetch_one(session, "SELECT * FROM rsw.embedding_models")
    assert model["model_name"] == "intfloat/multilingual-e5-small" and model["dimensions"] == 384
    missing = await fetch_value(session, """
        SELECT count(*) FROM rsw.release_chunks rc LEFT JOIN rsw.chunk_embeddings ce
          ON ce.chunk_id = rc.chunk_id AND ce.embedding_model_id = :m
        WHERE rc.release_id = :rid AND ce.chunk_id IS NULL""", rid=rid, m=model["id"])
    assert missing == 0


async def test_reload_is_idempotent(database, cfg):
    async def counts(session):
        return {t: await fetch_value(session, f"SELECT count(*) FROM rsw.{t}")
                for t in ("document_revisions", "document_sections", "section_spans", "retrieval_chunks",
                          "chunk_embeddings", "knowledge_releases", "knowledge_relations", "evaluation_cases")}

    async with database.session() as session:
        before = await counts(session)
    async with database.session() as session, session.begin():
        report = await load_mvp(session, cfg)
    async with database.session() as session:
        after = await counts(session)
    assert report["created"] is False and report["release_status"] == "published"
    assert before == after
