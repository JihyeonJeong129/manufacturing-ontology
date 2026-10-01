"""질문 접수 → 답변 생성 → 추가 질문 응답 → 절차 안내, 그리고 저장된 추적 기록."""
import uuid

from app.core.jsonutil import as_json
from app.db.sqlutil import fetch_all, fetch_one
from app.demo.seed import sample_alert
from tests.contract import assert_contract
from tests.integration.helpers import answer, ask, ask_and_answer, drain, post_alert, ready_alert


async def test_needs_context_then_followup_yields_grounded_procedure(client, tokens, worker, database, seed):
    alert = await ready_alert(client, tokens, worker)
    accepted = await ask(client, tokens, alert, key="q-flow-1")
    assert accepted.status_code == 202
    assert accepted.headers["location"] == accepted.json()["status_url"]
    assert accepted.headers["idempotency-replayed"] == "false"
    pending = await answer(client, tokens, accepted.json()["answer_id"])
    assert pending["status"] == "pending" and pending["result"] is None

    await drain(worker)
    first = await answer(client, tokens, accepted.json()["answer_id"])
    assert first["status"] == "needs_context"
    result = first["result"]
    assert result["procedures"] == []
    assert [q["question_key"] for q in result["follow_up_questions"]] == ["cycle_state"]
    assert {c["reference_scope"] for c in result["citations"]} == {"applicable", "analogous"}
    assert "ANALOGOUS_REFERENCE" in {n["code"] for n in result["notices"]}
    ml_claim = next(c for c in result["claims"] if c["claim_key"] == "ml-1")
    assert "실제 발생 확률이 아닙니다" in ml_claim["text"] and ml_claim["event_evidence"][0]["kind"] == "ml_assessment"

    followup = await ask(client, tokens, alert, question="비용접 상태에서 발생했습니다. 이 정보를 반영해 설명해주세요.",
                         reply_to_answer_id=first["answer_id"],
                         context_answers=[{"question_key": "cycle_state", "value": "non_welding", "unit": None}])
    assert followup.status_code == 202, followup.text
    await drain(worker)
    second = await answer(client, tokens, followup.json()["answer_id"])
    assert second["status"] == "answered" and second["reply_to_answer_id"] == first["answer_id"]
    procedure = second["result"]["procedures"][0]
    assert procedure["procedure_id"] == seed["entities"]["P01"]
    assert [(s["step_number"], s["condition_state"]) for s in procedure["steps"]] == [(1, "unconditional"), (2, "unconditional"), (3, "satisfied")]
    assert procedure["steps"][2]["condition_summary"] == "운전 상태 = 비용접 상태"
    citations = {c["citation_id"]: c for c in second["result"]["citations"]}
    for step in procedure["steps"]:
        assert all(citations[cid]["reference_scope"] == "applicable" for cid in step["citation_ids"])
    step3 = citations[procedure["steps"][2]["citation_ids"][0]]
    assert step3["quote"].startswith("Step 3:") and step3["locator"]["page_number"] == 2
    assert step3["document_url"] == f"/api/v1/documents/{seed['document_revision_id']}"
    assert "OPERATOR_REPORTED_CONTEXT" in {n["code"] for n in second["result"]["notices"]}

    welding = await ask_and_answer(client, tokens, worker, alert, question="용접 중이었습니다.", reply_to_answer_id=first["answer_id"],
                                   context_answers=[{"question_key": "cycle_state", "value": "welding"}])
    assert [s["step_number"] for s in welding["result"]["procedures"][0]["steps"]] == [1, 2], "조건이 거짓인 분기는 제외한다"

    # 저장된 실행 추적: 검색 후보, 인용, 실제 입력 snapshot
    async with database.session() as s:
        run = await fetch_one(s, "SELECT answer, input_snapshot, status, finished_at FROM rsw.assistant_runs WHERE id = :id",
                              id=second["answer_id"])
        stored_citations = await fetch_all(s, """
            SELECT ac.id, ac.claim_key, ac.cited_text, sp.excerpt FROM rsw.answer_citations ac
            JOIN rsw.section_spans sp ON sp.id = ac.span_id WHERE ac.run_id = :id""", id=second["answer_id"])
        candidates = await fetch_all(s, "SELECT method, rank, selected FROM rsw.retrieval_candidates WHERE run_id = :id",
                                     id=second["answer_id"])
    assert as_json(run["answer"]) == second["result"]
    assert {str(c["id"]) for c in stored_citations} == set(citations)
    assert all(c["cited_text"] == c["excerpt"] for c in stored_citations), "인용문은 span 원문에서 서버가 추출한다"
    methods = {c["method"] for c in candidates}
    assert {"code", "graph", "fusion"} <= methods
    assert all(not c["selected"] for c in candidates if c["method"] != "fusion")
    assert any(c["selected"] for c in candidates if c["method"] == "fusion")
    snapshot = as_json(run["input_snapshot"])
    assert snapshot["facts"]["operating_context.cycle_state"] == "non_welding"
    assert snapshot["fact_provenance"]["operating_context.cycle_state"] == "operator_reported"
    assert snapshot["generator"]["messages"][0]["role"] == "system"


async def test_question_idempotency(client, tokens, worker, database):
    alert = await ready_alert(client, tokens, worker)
    key = f"q-{uuid.uuid4().hex}"
    first = await ask(client, tokens, alert, key=key)
    replay = await client.post(f"/api/v1/alerts/{alert['alert_id']}/questions",
                               json={"question": "이 경보가 의미하는 것과 먼저 확인할 정보를 알려주세요.",
                                     "context_id": alert["context"]["context_id"]},  # 키 순서만 다름
                               headers={**tokens["operator"], "Idempotency-Key": key})
    assert replay.status_code == 202 and replay.json() == first.json()
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.headers["x-request-id"] != first.headers["x-request-id"]
    reused = await ask(client, tokens, alert, question="다른 질문", key=key)
    assert reused.status_code == 409 and reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    # 다른 사용자는 같은 키를 독립적으로 쓸 수 있다
    other_user = await client.post(f"/api/v1/alerts/{alert['alert_id']}/questions",
                                   json={"context_id": alert["context"]["context_id"], "question": "검토자 질문"},
                                   headers={**tokens["reviewer"], "Idempotency-Key": key})
    assert other_user.status_code == 202 and other_user.json()["answer_id"] != first.json()["answer_id"]
    async with database.session() as s:
        runs = await fetch_all(s, "SELECT id FROM rsw.assistant_runs WHERE id = :id", id=first.json()["answer_id"])
    assert len(runs) == 1


async def test_question_state_errors(client, tokens, worker):
    queued = (await post_alert(client, tokens["ml"])).json()
    not_ready = await client.post(f"/api/v1/alerts/{queued['alert_id']}/questions",
                                  json={"context_id": str(uuid.uuid4()), "question": "q"},
                                  headers={**tokens["operator"], "Idempotency-Key": f"k-{uuid.uuid4().hex}"})
    assert not_ready.status_code == 409 and not_ready.json()["code"] == "ALERT_NOT_READY"

    alert_a = await ready_alert(client, tokens, worker)
    alert_b = await ready_alert(client, tokens, worker)
    mismatch = await ask(client, tokens, {**alert_a, "context": alert_b["context"]})
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "CONTEXT_ALERT_MISMATCH"
    missing = await ask(client, tokens, {**alert_a, "context": {"context_id": str(uuid.uuid4())}})
    assert missing.status_code == 404
    unknown_alert = await ask(client, tokens, {**alert_a, "alert_id": str(uuid.uuid4())})
    assert unknown_alert.status_code == 404 and unknown_alert.json()["code"] == "ALERT_NOT_FOUND"

    pending = await ask(client, tokens, alert_a)
    not_done = await ask(client, tokens, alert_a, reply_to_answer_id=pending.json()["answer_id"])
    assert not_done.status_code == 409 and not_done.json()["code"] == "ANSWER_NOT_READY"
    await drain(worker)
    other_context = await ask(client, tokens, alert_b, reply_to_answer_id=pending.json()["answer_id"])
    assert other_context.status_code == 409 and other_context.json()["code"] == "REPLY_CONTEXT_MISMATCH"
    undefined = await ask(client, tokens, alert_a, reply_to_answer_id=pending.json()["answer_id"],
                          context_answers=[{"question_key": "controller_model", "value": "X"}])
    assert undefined.status_code == 422 and undefined.json()["errors"][0]["path"] == "/context_answers/0/question_key"
    wrong_type = await ask(client, tokens, alert_a, reply_to_answer_id=pending.json()["answer_id"],
                           context_answers=[{"question_key": "cycle_state", "value": True}])
    assert wrong_type.status_code == 422
    no_parent = await ask(client, tokens, alert_a, context_answers=[{"question_key": "cycle_state", "value": "welding"}])
    assert no_parent.status_code == 422


async def test_invalid_context_is_not_usable(client, tokens, worker):
    payload = sample_alert(f"it-{uuid.uuid4().hex}")
    payload["observations"][0]["value"] = "fast"
    alert = await ready_alert(client, tokens, worker, payload)
    response = await ask(client, tokens, alert)
    assert response.status_code == 409 and response.json()["code"] == "CONTEXT_NOT_USABLE"


async def test_conversation_depth_limit(cfg, database, client, tokens, worker):
    import httpx

    from app.main import create_app

    app = create_app(cfg.model_copy(update={"CONVERSATION_MAX_DEPTH": 2}), database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as limited:
        alert = await ready_alert(limited, tokens, worker)
        first = await ask_and_answer(limited, tokens, worker, alert)
        second = await ask_and_answer(limited, tokens, worker, alert, reply_to_answer_id=first["answer_id"])
        third = await ask(limited, tokens, alert, reply_to_answer_id=second["answer_id"])
    assert third.status_code == 422 and third.json()["code"] == "CONVERSATION_LIMIT_EXCEEDED"


async def test_unverified_asset_never_gets_procedures(client, tokens, worker):
    alert = await ready_alert(client, tokens, worker, sample_alert(f"it-{uuid.uuid4().hex}", asset_key="RSW-DEMO-002",
                                                                   cycle_state="non_welding"))
    result = (await ask_and_answer(client, tokens, worker, alert))["result"]
    assert result["procedures"] == []
    assert any(c["claim_key"] == "limitation-asset" for c in result["claims"])
    assert all(c["reference_scope"] == "unknown" for c in result["citations"])
    assert "APPLICABILITY_UNKNOWN" in {n["code"] for n in result["notices"]}


async def test_unmapped_event_gives_insufficient_evidence(client, tokens, worker):
    payload = sample_alert(f"it-{uuid.uuid4().hex}", asset_key="NO-SUCH-ASSET")
    payload.update(observed_codes=[{"namespace": "x.vendor", "code": "ZZZ"}], observations=[], ml_assessments=[])
    alert = await ready_alert(client, tokens, worker, payload)
    result = await ask_and_answer(client, tokens, worker, alert, question="qqq")
    assert result["status"] == "insufficient_evidence"
    assert result["result"]["procedures"] == [] and result["result"]["citations"] == []
    assert_contract(result, "AnswerDetails")


async def test_known_event_context_needs_no_follow_up(client, tokens, worker):
    alert = await ready_alert(client, tokens, worker, sample_alert(f"it-{uuid.uuid4().hex}", cycle_state="welding"))
    first = await ask_and_answer(client, tokens, worker, alert)
    assert first["status"] == "answered"  # 장비 데이터에 운전 상태가 있으므로 바로 절차 제시
    assert first["result"]["follow_up_questions"] == []
    assert [s["step_number"] for s in first["result"]["procedures"][0]["steps"]] == [1, 2]
    # 이전 답변이 묻지 않은 필드는 작업자 답변으로 덮어쓸 수 없다
    override = await ask(client, tokens, alert, reply_to_answer_id=first["answer_id"],
                         context_answers=[{"question_key": "cycle_state", "value": "non_welding"}])
    assert override.status_code == 422
