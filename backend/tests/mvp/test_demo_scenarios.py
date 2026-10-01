"""시연 시나리오 동작 검증: 실제 API(ASGI) + 워커 + 실제 지식 배포본.

1) ML 오류 코드 기반 (S01): ML 핸드오프 경보(E012 + E01 분류 + P1 규칙) → 시나리오 매칭 → 질문 → 절차·인용
2) 작업자 자연어 질의 (NL-S09): 분류 없는 이상 점수 경보 + 자연어 질문 → 벡터 검색 → 질문 기반 시나리오 연결
그리고 10개 상황 경보가 모두 기대 상황에 매칭되는지, 벡터 검색 품질이 기준 이상인지 확인한다.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.jsonutil import as_json, dumps
from app.db.sqlutil import execute, fetch_all, fetch_one
from app.mvp.scenarios import BY_KEY, CASES, DEMO_ML_CASE, DEMO_NL_CASE, alert_payload
from app.services.answering.embedding import find_model_row, local_embedder
from tests.contract import assert_contract
from tests.integration.helpers import ask_and_answer, post_alert, ready_alert

pytestmark = pytest.mark.asyncio


async def run_case(client, tokens, worker, case_key: str, question: str | None = None) -> tuple[dict, dict]:
    case = BY_KEY[case_key]
    alert = await ready_alert(client, tokens, worker, alert_payload(case, event_id=f"t-{case_key}-{uuid.uuid4().hex[:8]}"))
    answer = await ask_and_answer(client, tokens, worker, alert, question=question or case.question)
    return alert, answer


async def run_trace(session, answer_id: str) -> dict[str, Any]:
    run = await fetch_one(session, "SELECT * FROM rsw.assistant_runs WHERE id = :id", id=answer_id)
    candidates = await fetch_all(session, """
        SELECT rc.method, rc.rank, rc.score, rc.selected, ds.section_key
        FROM rsw.retrieval_candidates rc JOIN rsw.retrieval_chunks c ON c.id = rc.chunk_id
        JOIN rsw.document_sections ds ON ds.id = c.section_id
        WHERE rc.run_id = :id ORDER BY rc.method, rc.rank""", id=answer_id)
    by_method: dict[str, list[str]] = {}
    for c in candidates:
        by_method.setdefault(c["method"], []).append(c["section_key"])
    return {"run": run, "snapshot": as_json(run["input_snapshot"]), "by_method": by_method, "candidates": candidates}


async def record_evaluation(database, case_key: str, answer: dict, trace: dict) -> dict[str, Any]:
    """평가 케이스(봉인됨)에 대한 실행 결과를 evaluation_results에 남긴다 (별도 트랜잭션)."""
    async with database.session() as session, session.begin():
        return await _record(session, case_key, answer, trace)


async def _record(session, case_key: str, answer: dict, trace: dict) -> dict[str, Any]:
    case = await fetch_one(session, "SELECT id, release_id FROM rsw.evaluation_cases WHERE case_key = :k",
                           k=f"rsw-mvp-{case_key}")
    expected = {r["section_key"] for r in await fetch_all(session, """
        SELECT ds.section_key FROM rsw.evaluation_expected_sections e JOIN rsw.document_sections ds ON ds.id = e.section_id
        WHERE e.case_id = :c""", c=case["id"])}
    cited = {c["section_id"] for c in answer["result"]["citations"]}
    cited_keys = {r["section_key"] for r in await fetch_all(session, "SELECT section_key FROM rsw.document_sections WHERE id = ANY(:ids)",
                                                             ids=[uuid.UUID(x) for x in cited])}
    metrics = {"status": answer["status"], "expected_status": "answered",
               "expected_section_recall": round(len(expected & cited_keys) / len(expected), 3),
               "cited_expected_sections": sorted(expected & cited_keys),
               "procedures": [p["title"] for p in answer["result"]["procedures"]],
               "vector_status": trace["snapshot"]["retrieval"]["vector_status"]}
    await execute(session, """
        INSERT INTO rsw.evaluation_results (case_id, run_id, release_id, evaluator_version, metrics)
        VALUES (:c, :r, :rel, 'rsw-mvp-acceptance-v1', CAST(:m AS jsonb))""",
        c=case["id"], r=answer["answer_id"], rel=case["release_id"], m=dumps(metrics))
    return metrics


def citations_by_claim(result: dict) -> dict[str, list[dict]]:
    by_id = {c["citation_id"]: c for c in result["citations"]}
    return {claim["claim_key"]: [by_id[i] for i in claim["citation_ids"]] for claim in result["claims"]}


async def test_scenario_1_ml_error_code_e012(client, tokens, worker, session, database, model_ready):
    """ML이 보낸 오류 코드 E012 + 고장 분류 E01 + 센서 규칙 P1 → S01 절차와 원문 근거."""
    alert, answer = await run_case(client, tokens, worker, DEMO_ML_CASE)

    context = alert["context"]
    assert context["status"] == "mapped" and context["asset"]["verification_status"] == "verified"
    assert [m["status"] for m in context["code_mappings"]] == ["mapped"]
    assert all(m["target_entity_id"] for m in context["ml_mappings"])
    matches = alert["related_scenarios"]
    assert matches["status"] == "matched"
    assert matches["items"][0]["code"] == "S01"
    assert set(matches["items"][0]["match_methods"]) == {"observed_code", "ml_classification", "rule_trigger"}

    assert answer["status"] == "answered", answer
    result = answer["result"]
    [procedure] = result["procedures"]
    assert procedure["title"].startswith("S01 보정 압력 도달 지연")
    assert [s["step_number"] for s in procedure["steps"]] == [1, 2, 3, 4, 5]
    cites = citations_by_claim(result)
    for step in procedure["steps"]:
        assert [c["reference_scope"] for c in cites[step["claim_key"]]] == ["applicable"]
    step1 = cites[procedure["steps"][0]["claim_key"]][0]
    assert step1["section_number"] == "10.1/12" and "5 bar" in step1["quote"]
    assert step1["document_url"] == f"/api/v1/documents/{step1['document_revision_id']}"
    assert step1["locator"]["page_number"] == 90
    # 원문 근거: 정의서 S01(데이터 신호)과 Festo 오류 12
    sections = {(c["document_title"][:9], c["section_number"]) for c in result["citations"]}
    assert ("RSW 용접건 M", "S01") in sections
    assert any(c["section_number"] == "10.1/12" for c in result["citations"])
    # 절차가 쓰는 수치 기준 (uses_spec)
    spec_claims = [c["text"] for c in result["claims"] if c["claim_key"].startswith("spec-")]
    assert any("5 bar" in t for t in spec_claims) and any("1800 ms" in t for t in spec_claims)
    # 원인 후보는 확정이 아닌 후보로만
    hypotheses = [c for c in result["claims"] if c["claim_key"].startswith("hypothesis-")]
    assert len(hypotheses) == 4 and all("확정 원인이 아닙니다" in c["text"] for c in hypotheses)

    trace = await run_trace(session, answer["answer_id"])
    assert trace["by_method"]["code"][0] == "D-S01"
    assert trace["snapshot"]["graph"]["question_links"] == {}
    if model_ready:
        assert trace["run"]["embedding_model_id"] is not None
        assert trace["snapshot"]["retrieval"]["vector_status"] == "searched" and trace["by_method"]["vector"]
    metrics = await record_evaluation(database, DEMO_ML_CASE, answer, trace)
    assert metrics["expected_section_recall"] >= 0.5


async def test_scenario_2_natural_language_vector_search(client, tokens, worker, session, database, model_ready):
    """분류 없는 이상 경보에서 작업자의 자연어 질문 → 벡터 검색으로 S09 설명 절 → 질문 기반 시나리오·절차."""
    if not model_ready:
        pytest.skip("임베딩 모델 파일이 없습니다")
    alert, answer = await run_case(client, tokens, worker, DEMO_NL_CASE)
    assert alert["related_scenarios"]["status"] == "no_match"
    assert [m["target_entity_id"] for m in alert["context"]["ml_mappings"]] == [None]

    trace = await run_trace(session, answer["answer_id"])
    assert trace["snapshot"]["retrieval"]["vector_status"] == "searched"
    assert trace["by_method"]["vector"][0] == "D-S09", trace["by_method"]["vector"][:5]
    vector_rows = [c for c in trace["candidates"] if c["method"] == "vector"]
    assert vector_rows[0]["score"] < vector_rows[-1]["score"]            # 코사인 거리 오름차순
    links = trace["snapshot"]["graph"]["question_links"]
    assert len(links) == 1 and next(iter(links.values()))["agreement"] == ["keyword", "vector"]

    assert answer["status"] == "answered", answer
    result = answer["result"]
    [procedure] = result["procedures"]
    assert procedure["title"].startswith("S09 주변 설비 이상")
    assert len(procedure["steps"]) == 3
    codes = [n["code"] for n in result["notices"]]
    assert "QUESTION_LINKED_SCENARIO" in codes
    assert "ANALOGOUS_REFERENCE" in codes                                # MILCO 냉각수 수치는 유사 장비 참고
    scenario_claim = next(c for c in result["claims"] if c["claim_key"].startswith("scenario-"))
    assert "질문 내용과 가장 가까운 시나리오 후보" in scenario_claim["text"] and scenario_claim["citation_ids"]
    quote = citations_by_claim(result)[scenario_claim["claim_key"]][0]
    assert quote["section_number"] == "S09" and "건 자체는 정상으로 움직인다" in quote["quote"]
    assert quote["document_url"] is None and quote["locator"]["kind"] == "logical_block"   # Word 정의서
    spec_scopes = {c["reference_scope"] for k, v in citations_by_claim(result).items() if k.startswith("spec-") for c in v}
    assert spec_scopes == {"analogous"}
    metrics = await record_evaluation(database, DEMO_NL_CASE, answer, trace)
    assert "D-S09" in metrics["cited_expected_sections"]


async def test_ambiguous_question_does_not_guess_a_procedure(client, tokens, worker, session, model_ready):
    """키워드와 벡터 검색이 다른 시나리오를 가리키면 절차를 내지 않고 후보만 알린다 (알려진 한계: 드리프트 구어 표현)."""
    if not model_ready:
        pytest.skip("임베딩 모델 파일이 없습니다")
    _, answer = await run_case(client, tokens, worker, DEMO_NL_CASE,
                               question="용접건이 멈춰 있는데도 전극이 조금씩 밀려 내려가요. 어디부터 점검해야 하나요?")
    result = answer["result"]
    assert result["procedures"] == []
    notice = next(n for n in result["notices"] if n["code"] == "QUESTION_SCENARIO_UNCERTAIN")
    assert "S04 드리프트" in notice["message"]
    assert any(c["section_number"] == "S04" for c in result["citations"])


@pytest.mark.parametrize("case", [c for c in CASES if c.kind == "ml_error_code"], ids=lambda c: c.key)
async def test_ten_situation_alerts_match_expected_scenario(client, tokens, worker, case):
    alert = await ready_alert(client, tokens, worker, alert_payload(case, event_id=f"m-{case.key}-{uuid.uuid4().hex[:8]}"))
    assert alert["context"]["status"] == "mapped", alert["context"]["warnings"]
    items = {i["code"]: i for i in alert["related_scenarios"]["items"]}
    assert case.scenario in items, items.keys()
    assert set(items[case.scenario]["match_methods"]) == set(case.expected_methods)
    assert all(m["status"] == "mapped" for m in alert["context"]["observation_mappings"])


async def test_alert_payloads_follow_the_api_contract(client, tokens):
    for case in CASES:
        payload = alert_payload(case)
        assert_contract(payload, "AlertInput")
    response = await post_alert(client, tokens["ml"], alert_payload(BY_KEY["S03"], event_id="contract-s03"))
    assert response.status_code == 201


VECTOR_QUERIES = [
    ("보정 압력이 설정값까지 안 올라가서 라인이 섰어요", {"D-S01", "F-10.1-12", "F-5.3"}),
    ("용접 힘이 약해졌고 배관 쪽에서 바람 새는 소리가 나요", {"D-S05", "M-4"}),
    ("건이 뻑뻑하게 움직이고 끼익 소리가 나요", {"D-S06", "M-4", "F-8.6"}),
    ("캡이 평소보다 너무 빨리 닳아요 왜 그런가요", {"D-S07", "F-10.1-29", "F-8.8"}),
    ("정비하고 캡 바꾼 뒤로 위치가 조금씩 안 맞아요", {"D-S08", "F-9.14.8", "F-8.5", "D-S02"}),
    ("라인은 멈췄는데 건 자체는 정상으로 움직여요", {"D-S09", "R-ABB", "R-JUDGEMENT"}),
    ("냉각수는 분당 얼마나 흘러야 정상인가요", {"M-9-1", "D-S09"}),
    ("공기 필터는 언제 교체하나요", {"F-6", "D-S05", "F-5.2.1"}),
    ("자잘한 에러 코드가 며칠째 떠 있는데 무시해도 되나요", {"D-S10", "A-2.1.2"}),
    ("전극 캡이 깨져서 빠졌어요", {"D-S02", "F-10.1-31"}),
    ("건이 엉뚱한 위치로 가서 지그에 부딪힐 뻔했어요", {"D-S03", "F-10.1-9", "F-10.1-4"}),
    ("윤활은 어떤 그리스로 얼마마다 해야 돼요", {"F-6", "D-S06"}),
    ("변압기가 과열됐다는 경보가 떠요", {"M-8", "Y-1.6.6", "F-10.1-17", "D-S09"}),
    ("잠금 상태인데도 실린더가 밀려요", {"D-S04", "F-9.14.10", "F-11.3", "F-10.1-11"}),
    # 알려진 한계: 드리프트의 구어 표현은 e5-small이 S05/S08과 구분하지 못한다 (하이브리드 단계에서 절차를 보류).
    ("용접건이 멈춰 있는데도 전극이 조금씩 밀려 내려가요", {"D-S04", "F-9.14.10", "F-11.3"}),
    ("건을 세워 뒀는데 위치가 계속 조금씩 움직여요", {"D-S04", "F-9.14.10", "F-11.3"}),
]


async def test_vector_search_quality(session, cfg, model_ready):
    """벡터 검색만으로 한국어 자연어 질문이 맞는 원문 절을 찾는 비율 (고정 모델 리비전 기준)."""
    if not model_ready:
        pytest.skip("임베딩 모델 파일이 없습니다")
    embedder = local_embedder(cfg.EMBEDDING_MODEL_DIR)
    model_id = await find_model_row(session)
    release_id = (await fetch_one(session, "SELECT release_id FROM rsw.knowledge_deployments WHERE name = :n",
                                  n=cfg.KNOWLEDGE_DEPLOYMENT))["release_id"]
    ranks = []
    for question, expected in VECTOR_QUERIES:
        vector = await embedder.embed(question)
        rows = await fetch_all(session, """
            SELECT ds.section_key FROM rsw.release_chunks rc
            JOIN rsw.chunk_embeddings ce ON ce.chunk_id = rc.chunk_id AND ce.embedding_model_id = :m
            JOIN rsw.document_sections ds ON ds.id = rc.section_id
            WHERE rc.release_id = :r ORDER BY ce.embedding <=> CAST(:v AS public.vector), rc.chunk_id LIMIT 30""",
            m=model_id, r=release_id, v="[" + ",".join(map(repr, vector)) + "]")
        order = list(dict.fromkeys(r["section_key"] for r in rows))
        ranks.append(next((i + 1 for i, key in enumerate(order) if key in expected), 99))
    core = ranks[:-2]
    assert sum(r == 1 for r in core) >= 13, list(zip([q for q, _ in VECTOR_QUERIES], ranks))
    assert sum(r <= 3 for r in core) == len(core)
