"""두 가지 시연 시나리오를 실행 중인 API에 그대로 호출한다 (python -m app.cli mvp-demo).

1) ML 오류 코드 기반: ML 핸드오프 경보(E012 + E01 + P1) → 정규화·시나리오 매칭 → 작업자 질문 → 절차·인용
2) 자연어 질의 기반: ML은 분류 없는 이상 점수만 보냄 → 작업자가 본 상황을 문장으로 질문 → 벡터 검색으로 절·시나리오 연결
표준 라이브러리 urllib만 사용한다.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from app.mvp.scenarios import BY_KEY, alert_payload


class DemoError(RuntimeError):
    pass


def _call(method: str, url: str, token: str, body: dict[str, Any] | None = None,
          headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _poll(fn, done, timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if done(value):
            return value
        if time.monotonic() > deadline:
            raise DemoError(f"{what} 대기 시간 초과 (워커가 실행 중인지 확인하세요)")
        time.sleep(0.5)


def run_case(base_url: str, ml_token: str, operator_token: str, case_key: str, timeout: float = 60.0) -> dict[str, Any]:
    case = BY_KEY[case_key]
    payload = alert_payload(case)
    status, accepted = _call("POST", f"{base_url}/api/v1/alerts", ml_token, payload)
    if status not in (200, 201):
        raise DemoError(f"경보 등록 실패 {status}: {accepted}")
    alert_id = accepted["alert_id"]
    alert = _poll(lambda: _call("GET", f"{base_url}/api/v1/alerts/{alert_id}", operator_token)[1],
                  lambda a: a["normalization"]["status"] in ("succeeded", "failed"), timeout, "정규화")
    if alert["context"] is None:
        raise DemoError(f"정규화 실패: {alert['normalization']}")
    context_id = alert["context"]["context_id"]
    status, queued = _call("POST", f"{base_url}/api/v1/alerts/{alert_id}/questions", operator_token,
                           {"context_id": context_id, "question": case.question},
                           {"Idempotency-Key": f"demo-{uuid.uuid4().hex}"})
    if status != 202:
        raise DemoError(f"질문 접수 실패 {status}: {queued}")
    answer = _poll(lambda: _call("GET", f"{base_url}/api/v1/answers/{queued['answer_id']}", operator_token)[1],
                   lambda a: a["status"] not in ("pending", "running"), timeout, "답변")
    return {"case": case, "payload": payload, "alert": alert, "answer": answer}


def describe(result: dict[str, Any], candidates: list[dict[str, Any]] | None = None) -> str:
    case, alert, answer = result["case"], result["alert"], result["answer"]
    context = alert["context"]
    lines = [
        f"=== [{case.key}] {'ML 오류 코드 기반' if case.kind == 'ml_error_code' else '작업자 자연어 질의'} ===",
        f"경보: codes={[c['code'] for c in result['payload']['observed_codes']]} "
        f"ml={[(m['kind'], (m.get('target') or {}).get('code')) for m in result['payload']['ml_assessments']]}",
        f"alert_id={alert['alert_id']} context={context['context_id']} status={context['status']} "
        f"asset={context['asset']['verification_status']}",
        f"시나리오 매칭: {alert['related_scenarios']['status']} "
        + ", ".join(f"{i['code']}({'+'.join(i['match_methods'])})" for i in alert["related_scenarios"]["items"]),
        f"질문: {case.question}",
        f"답변 상태: {answer['status']}  answer_id={answer['answer_id']}",
    ]
    res = answer.get("result") or {}
    lines.append(f"요약: {res.get('summary')}")
    for claim in res.get("claims", []):
        if claim["type"] == "hypothesis" and claim["claim_key"].startswith("scenario"):
            lines.append(f"  - {claim['text']}")
    for procedure in res.get("procedures", []):
        lines.append(f"절차: {procedure['title']} (역할 {procedure['required_role']})")
        for step in procedure["steps"]:
            lines.append(f"  {step['step_number']}. {step['instruction']}")
    cites = {c["citation_id"]: c for c in res.get("citations", [])}
    shown = 0
    for claim in res.get("claims", []):
        if claim["type"] != "manual_fact" or shown >= 6:
            continue
        for cid in claim["citation_ids"]:
            c = cites[cid]
            loc = c["locator"]
            page = loc.get("printed_page_label") or loc.get("page_number")
            lines.append(f"  [근거 {c['reference_scope']}] {c['document_title'][:40]} §{c['section_number'] or ''} "
                         f"p.{page}: {c['quote'][:90].replace(chr(10), ' ')}")
            shown += 1
    for notice in res.get("notices", []):
        lines.append(f"  (알림 {notice['code']}) {notice['message'][:120]}")
    if candidates:
        lines.append("검색 후보 (방식별 상위 3):")
        for method in ("code", "graph", "keyword", "vector", "fusion"):
            top = [c for c in candidates if c["method"] == method][:3]
            if top:
                lines.append(f"  {method:7s} " + " | ".join(
                    f"#{c['rank']} {c['section_key']}" + (f" ({c['score']:.3f})" if c["score"] is not None else "") for c in top))
    return "\n".join(lines)
