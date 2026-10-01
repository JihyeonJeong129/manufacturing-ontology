"""질문·피드백 멱등 처리 (API_SPEC.md 2.3, 10.3).

(인증 주체, 메서드, 정규화 경로, Idempotency-Key)가 유일하다. 같은 키·같은 JSON이면 최초 응답을
재생하고, 다른 JSON이면 409 IDEMPOTENCY_KEY_REUSED. 같은 키의 동시 요청은 DB UNIQUE로 하나만
생성되며, 먼저 들어온 트랜잭션이 짧은 시간 안에 끝나지 않으면 409 REQUEST_IN_PROGRESS.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.jsonutil import as_json, dumps
from app.db.sqlutil import LOCK_NOT_AVAILABLE, UNIQUE_VIOLATION, constraint_name, execute, fetch_one, sqlstate

IDEMPOTENCY_CONSTRAINT = "api_idempotency_requests_principal_id_method_resource_path_i_key"


@dataclass
class IdempotentResult:
    status: int
    body: dict[str, Any]
    replayed: bool


async def _lookup(session: AsyncSession, principal_id: UUID, path: str, key: str, body_json: str) -> IdempotentResult | None:
    row = await fetch_one(
        session,
        """
        SELECT response_status, response_body, (request_body = CAST(:body AS jsonb)) AS same_body
        FROM rsw.api_idempotency_requests
        WHERE principal_id = :pid AND method = 'POST' AND resource_path = :path AND idempotency_key = :key
          AND expires_at > now()
        """,
        pid=principal_id, path=path, key=key, body=body_json,
    )
    if row is None:
        return None
    if not row["same_body"]:
        raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "같은 Idempotency-Key가 다른 요청 본문에 이미 사용되었습니다.")
    return IdempotentResult(row["response_status"], as_json(row["response_body"]), True)


def _is_idempotency_conflict(exc: BaseException) -> bool:
    name = constraint_name(exc) or ""
    return sqlstate(exc) == UNIQUE_VIOLATION and (name.startswith("api_idempotency_requests") or not name)


async def run_idempotent(
    session: AsyncSession,
    cfg: Settings,
    *,
    principal_id: UUID,
    path: str,
    key: str,
    submitted: dict[str, Any],
    create: Callable[[AsyncSession], Awaitable[tuple[int, dict[str, Any], dict[str, UUID]]]],
) -> IdempotentResult:
    """create()는 같은 트랜잭션에서 리소스를 만들고 (HTTP 상태, 응답 body, {run_id|feedback_id})를 돌려준다."""
    body_json = dumps(submitted)
    body_sha = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
    ttl = timedelta(hours=max(24, cfg.IDEMPOTENCY_TTL_HOURS))
    wait_ms = max(1, int(cfg.IDEMPOTENCY_WAIT_SECONDS * 1000))
    try:
        async with session.begin():
            await execute(session, f"SET LOCAL lock_timeout = '{wait_ms}ms'")
            # 보관 기간이 지난 기록은 더 이상 중복 방지를 보장하지 않는다.
            await execute(
                session,
                """
                DELETE FROM rsw.api_idempotency_requests
                WHERE principal_id = :pid AND method = 'POST' AND resource_path = :path
                  AND idempotency_key = :key AND expires_at <= now()
                """,
                pid=principal_id, path=path, key=key,
            )
            existing = await _lookup(session, principal_id, path, key, body_json)
            if existing is not None:
                return existing
            status, body, links = await create(session)
            await execute(
                session,
                """
                INSERT INTO rsw.api_idempotency_requests
                  (principal_id, method, resource_path, idempotency_key, request_body, request_sha256,
                   response_status, response_body, run_id, feedback_id, expires_at)
                VALUES (:pid, 'POST', :path, :key, CAST(:body AS jsonb), :sha, :status, CAST(:response AS jsonb),
                        :run_id, :feedback_id, now() + CAST(:ttl AS interval))
                """,
                pid=principal_id, path=path, key=key, body=body_json, sha=body_sha, status=status,
                response=dumps(body), run_id=links.get("run_id"), feedback_id=links.get("feedback_id"),
                ttl=ttl,
            )
            return IdempotentResult(status, body, False)
    except DBAPIError as exc:
        code = sqlstate(exc)
        if code == LOCK_NOT_AVAILABLE:
            raise ApiError(409, "REQUEST_IN_PROGRESS", "같은 Idempotency-Key의 이전 요청이 아직 처리 중입니다.",
                           headers={"Retry-After": str(cfg.RETRY_AFTER_SECONDS)}) from exc
        if _is_idempotency_conflict(exc):
            # 동시에 들어온 같은 키 요청이 먼저 커밋되었다. 기록을 다시 읽어 재생한다.
            async with session.begin():
                replay = await _lookup(session, principal_id, path, key, body_json)
            if replay is not None:
                return replay
        raise
