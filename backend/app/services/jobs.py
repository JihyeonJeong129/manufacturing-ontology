"""background_jobs 조회/생성 보조."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jsonutil import as_json, dumps
from app.db.sqlutil import fetch_one


def public_work_error(error: Any) -> dict[str, Any] | None:
    error = as_json(error)
    if not error:
        return None
    return {
        "code": str(error.get("code", "PROCESSING_FAILED")),
        "message": str(error.get("message", "처리 중 오류가 발생했습니다.")),
        "retryable": bool(error.get("retryable", False)),
    }


def processing_view(job: dict[str, Any]) -> dict[str, Any]:
    status = job["status"]
    return {
        "status": status,
        "updated_at": job["updated_at"],
        "error": public_work_error(job["error"]) if status == "failed" else None,
    }


async def latest_normalize_job(session: AsyncSession, alert_id: UUID) -> dict[str, Any] | None:
    return await fetch_one(
        session,
        """
        SELECT id, status, updated_at, error, result_context_id, release_id, config
        FROM rsw.background_jobs
        WHERE alert_id = :alert_id AND kind = 'normalize_alert'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        alert_id=alert_id,
    )


async def insert_job(
    session: AsyncSession,
    *,
    kind: str,
    release_id: UUID,
    config: dict[str, Any],
    max_attempts: int,
    alert_id: UUID | None = None,
    run_id: UUID | None = None,
) -> dict[str, Any]:
    row = await fetch_one(
        session,
        """
        INSERT INTO rsw.background_jobs (kind, alert_id, run_id, release_id, config, max_attempts)
        VALUES (:kind, :alert_id, :run_id, :release_id, CAST(:config AS jsonb), :max_attempts)
        RETURNING id, status, updated_at, error
        """,
        kind=kind, alert_id=alert_id, run_id=run_id, release_id=release_id,
        config=dumps(config), max_attempts=max_attempts,
    )
    assert row is not None
    return row
