"""동시 요청: 같은 사건/같은 멱등 키/같은 피드백 정정은 하나만 생성된다."""
import asyncio
import json
import uuid

from app.db.sqlutil import fetch_value
from app.demo.seed import sample_alert
from tests.integration.helpers import ask_and_answer, ready_alert


async def test_concurrent_duplicate_alerts_create_one_event_and_job(client, tokens, database):
    payload = sample_alert(f"it-{uuid.uuid4().hex}")
    body = json.dumps(payload).encode()
    responses = await asyncio.gather(*[
        client.post("/api/v1/alerts", content=body, headers={**tokens["ml"], "Content-Type": "application/json"})
        for _ in range(6)
    ])
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 200, 200, 200, 200, 201], [r.text for r in responses]
    assert len({r.json()["alert_id"] for r in responses}) == 1
    async with database.session() as s:
        events = await fetch_value(s, "SELECT count(*) FROM rsw.alert_events WHERE source_event_id = :e", e=payload["event_id"])
        jobs = await fetch_value(s, """SELECT count(*) FROM rsw.background_jobs j JOIN rsw.alert_events a ON a.id = j.alert_id
                                       WHERE a.source_event_id = :e""", e=payload["event_id"])
    assert events == 1 and jobs == 1


async def test_concurrent_same_idempotency_key_creates_one_run(client, tokens, worker, database):
    alert = await ready_alert(client, tokens, worker)
    key = f"q-{uuid.uuid4().hex}"
    body = {"context_id": alert["context"]["context_id"], "question": "동시 질문"}
    responses = await asyncio.gather(*[
        client.post(f"/api/v1/alerts/{alert['alert_id']}/questions", json=body,
                    headers={**tokens["operator"], "Idempotency-Key": key})
        for _ in range(5)
    ])
    assert all(r.status_code == 202 for r in responses), [r.text for r in responses]
    assert len({r.json()["answer_id"] for r in responses}) == 1
    assert sorted(r.headers["idempotency-replayed"] for r in responses) == ["false"] + ["true"] * 4
    async with database.session() as s:
        runs = await fetch_value(s, "SELECT count(*) FROM rsw.assistant_runs WHERE context_id = :c AND question = '동시 질문'",
                                 c=alert["context"]["context_id"])
    assert runs == 1


async def test_concurrent_feedback_correction_creates_one_successor(client, tokens, worker, database):
    alert = await ready_alert(client, tokens, worker)
    done = await ask_and_answer(client, tokens, worker, alert)
    url = f"/api/v1/answers/{done['answer_id']}/feedback"
    first = await client.post(url, json={"verdict": "incorrect", "error_categories": ["wrong_source"]},
                              headers={**tokens["operator"], "Idempotency-Key": f"f-{uuid.uuid4().hex}"})
    fix = {"verdict": "acceptable", "error_categories": [], "supersedes_id": first.json()["feedback_id"]}
    responses = await asyncio.gather(*[
        client.post(url, json=fix, headers={**tokens["operator"], "Idempotency-Key": f"f-{uuid.uuid4().hex}"})
        for _ in range(4)
    ])
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 409, 409, 409], [r.text for r in responses]
    assert all(r.json()["code"] == "FEEDBACK_ALREADY_SUPERSEDED" for r in responses if r.status_code == 409)
    async with database.session() as s:
        successors = await fetch_value(s, "SELECT count(*) FROM rsw.feedback WHERE supersedes_id = :id", id=first.json()["feedback_id"])
    assert successors == 1
