from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from app.demo.seed import sample_alert
from app.worker.runner import Worker
from tests.contract import assert_contract


async def drain(worker: Worker, limit: int = 100) -> int:
    processed = 0
    while await worker.run_once():
        processed += 1
        assert processed < limit, "job loop did not terminate"
    return processed


async def post_alert(client: httpx.AsyncClient, headers: dict, payload: dict | None = None, raw: bytes | None = None,
                     **overrides: Any) -> httpx.Response:
    payload = payload or sample_alert(f"it-{uuid.uuid4().hex}")
    payload.update(overrides)
    body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode()
    response = await client.post("/api/v1/alerts", content=body, headers={**headers, "Content-Type": "application/json"})
    if response.status_code in (200, 201):
        assert_contract(response.json(), "AlertAccepted")
    return response


async def ready_alert(client, tokens, worker, payload: dict | None = None) -> dict:
    response = await post_alert(client, tokens["ml"], payload)
    assert response.status_code == 201, response.text
    await drain(worker)
    details = await client.get(f"/api/v1/alerts/{response.json()['alert_id']}", headers=tokens["operator"])
    assert details.status_code == 200, details.text
    assert_contract(details.json(), "AlertDetails")
    return details.json()


async def ask(client, tokens, alert: dict, question: str = "이 경보가 의미하는 것과 먼저 확인할 정보를 알려주세요.",
              key: str | None = None, **extra: Any) -> httpx.Response:
    body = {"context_id": alert["context"]["context_id"], "question": question, **extra}
    response = await client.post(
        f"/api/v1/alerts/{alert['alert_id']}/questions", json=body,
        headers={**tokens["operator"], "Idempotency-Key": key or f"q-{uuid.uuid4().hex}"},
    )
    if response.status_code == 202:
        assert_contract(response.json(), "QuestionAccepted")
    return response


async def answer(client, tokens, answer_id: str) -> dict:
    response = await client.get(f"/api/v1/answers/{answer_id}", headers=tokens["operator"])
    assert response.status_code == 200, response.text
    assert_contract(response.json(), "AnswerDetails")
    return response.json()


async def ask_and_answer(client, tokens, worker, alert, **kwargs) -> dict:
    response = await ask(client, tokens, alert, **kwargs)
    assert response.status_code == 202, response.text
    await drain(worker)
    return await answer(client, tokens, response.json()["answer_id"])
