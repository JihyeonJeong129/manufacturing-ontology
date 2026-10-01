"""워커 신뢰성: lease 만료 회수, 늦게 끝난 워커 결과 폐기, 재시도 소진 후 실패 확정."""
import uuid

import pytest

from app.core.jsonutil import as_json
from app.db.sqlutil import execute, fetch_one, fetch_value
from app.services import normalizer
from app.services.answering import pipeline
from app.worker.runner import LeaseLost, Worker
from tests.contract import assert_contract
from tests.integration.helpers import answer, ask, drain, post_alert, ready_alert


async def test_expired_lease_is_reclaimed_and_stale_worker_cannot_commit(client, tokens, cfg, database):
    await drain(Worker(database, cfg))
    created = (await post_alert(client, tokens["ml"])).json()
    first, second = Worker(database, cfg, "w1"), Worker(database, cfg, "w2")
    stale_job = await first.claim()
    assert stale_job is not None and stale_job.alert_id is not None and str(stale_job.alert_id) == created["alert_id"]
    assert await second.claim() is None, "유효한 lease가 있는 작업은 다른 워커가 잡지 않는다"

    async with database.session() as s, s.begin():
        await execute(s, "UPDATE rsw.background_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id", id=stale_job.id)
    fresh_job = await second.claim()
    assert fresh_job.id == stale_job.id and fresh_job.attempts == 2 and fresh_job.lease_token != stale_job.lease_token

    with pytest.raises(LeaseLost):
        await first._normalize(stale_job)
    assert not await first.heartbeat(stale_job)
    await second.process(fresh_job)
    async with database.session() as s:
        contexts = await fetch_value(s, "SELECT count(*) FROM rsw.alert_contexts WHERE alert_id = :a", a=created["alert_id"])
    assert contexts == 1, "이전 워커의 늦은 결과는 저장되지 않는다"


async def test_normalization_retries_then_fails_and_keeps_raw_event(client, tokens, cfg, database, monkeypatch):
    worker = Worker(database, cfg)
    await drain(worker)
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("temporary database hiccup")

    monkeypatch.setattr("app.worker.runner.normalize_alert", boom)
    created = (await post_alert(client, tokens["ml"])).json()
    await drain(worker)
    assert calls["n"] == cfg.JOB_MAX_ATTEMPTS
    details = (await client.get(f"/api/v1/alerts/{created['alert_id']}", headers=tokens["operator"])).json()
    assert_contract(details, "AlertDetails")
    assert details["normalization"]["status"] == "failed"
    assert details["normalization"]["error"]["code"] == "PROCESSING_FAILED"
    assert details["context"] is None and details["submitted_event"]["event_id"] == created["event_id"]
    assert "temporary database hiccup" not in str(details), "내부 예외 내용은 공개하지 않는다"


async def test_non_retryable_normalization_error_fails_immediately(client, tokens, cfg, database, monkeypatch):
    worker = Worker(database, cfg)
    await drain(worker)

    async def reject(*args, **kwargs):
        raise normalizer.NormalizationError("UNSUPPORTED_PAYLOAD", "지원하지 않는 송신 형식입니다.")

    monkeypatch.setattr("app.worker.runner.normalize_alert", reject)
    created = (await post_alert(client, tokens["ml"])).json()
    await drain(worker)
    async with database.session() as s:
        job = await fetch_one(s, "SELECT status, attempts, error FROM rsw.background_jobs WHERE alert_id = :a", a=created["alert_id"])
    assert job["status"] == "failed" and job["attempts"] == 1
    assert as_json(job["error"])["code"] == "UNSUPPORTED_PAYLOAD"


async def test_answer_failure_is_recorded_with_snapshot(client, tokens, cfg, database, monkeypatch):
    worker = Worker(database, cfg)
    alert = await ready_alert(client, tokens, worker)

    async def broken(*args, **kwargs):
        raise pipeline.RunNotAnswerable("LLM_TIMEOUT", "답변 생성 시간이 초과되었습니다.")

    monkeypatch.setattr("app.worker.runner.prepare_answer", broken)
    accepted = await ask(client, tokens, alert)
    await drain(worker)
    failed = await answer(client, tokens, accepted.json()["answer_id"])
    assert failed["status"] == "failed" and failed["result"] is None
    assert failed["error"] == {"code": "LLM_TIMEOUT", "message": "답변 생성 시간이 초과되었습니다.", "retryable": False}
    assert failed["finished_at"] is not None
    async with database.session() as s:
        snapshot = await fetch_value(s, "SELECT input_snapshot FROM rsw.assistant_runs WHERE id = :id", id=accepted.json()["answer_id"])
    assert as_json(snapshot)["snapshot_kind"] == "failure"

    reply = await ask(client, tokens, alert, reply_to_answer_id=failed["answer_id"])
    assert reply.status_code == 409 and reply.json()["code"] == "REPLY_ANSWER_FAILED"
    feedback = await client.post(f"/api/v1/answers/{failed['answer_id']}/feedback",
                                 json={"verdict": "acceptable", "error_categories": []},
                                 headers={**tokens["operator"], "Idempotency-Key": f"f-{uuid.uuid4().hex}"})
    assert feedback.status_code == 409 and feedback.json()["code"] == "ANSWER_NOT_REVIEWABLE"


async def test_lease_expired_on_last_attempt_fails_run_instead_of_hanging(client, tokens, cfg, database):
    worker = Worker(database, cfg)
    alert = await ready_alert(client, tokens, worker)
    accepted = await ask(client, tokens, alert)
    async with database.session() as s, s.begin():
        await execute(s, """UPDATE rsw.background_jobs SET status = 'running', attempts = max_attempts, lease_token = gen_random_uuid(),
                            lease_expires_at = now() - interval '1 second' WHERE run_id = :r""", r=accepted.json()["answer_id"])
        await execute(s, "UPDATE rsw.assistant_runs SET status = 'running' WHERE id = :r", r=accepted.json()["answer_id"])
    await drain(worker)
    failed = await answer(client, tokens, accepted.json()["answer_id"])
    assert failed["status"] == "failed" and failed["error"]["code"] == "WORKER_LEASE_EXPIRED"
