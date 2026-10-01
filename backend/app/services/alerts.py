"""POST /alerts, GET /alerts/{id} 서비스."""
from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.jsonutil import as_json, dumps
from app.db.sqlutil import fetch_one
from app.schemas.alerts import AlertInput
from app.services.jobs import insert_job, latest_normalize_job, processing_view


def alert_status_url(alert_id: UUID) -> str:
    return f"/api/v1/alerts/{alert_id}"


async def active_release(session: AsyncSession, deployment: str) -> dict[str, Any] | None:
    return await fetch_one(
        session,
        """
        SELECT kr.id, kr.version
        FROM rsw.knowledge_deployments kd
        JOIN rsw.knowledge_releases kr ON kr.id = kd.release_id AND kr.status = 'published'
        WHERE kd.name = :name
        """,
        name=deployment,
    )


async def _existing_alert(session: AsyncSession, producer_id: UUID, event_id: str, submitted_json: str) -> dict[str, Any] | None:
    return await fetch_one(
        session,
        """
        SELECT id, received_at, (parsed_payload = CAST(:submitted AS jsonb)) AS same_payload
        FROM rsw.alert_events
        WHERE producer_id = :producer_id AND source_event_id = :event_id
        """,
        producer_id=producer_id, event_id=event_id, submitted=submitted_json,
    )


async def accept_alert(
    session: AsyncSession,
    principal: Principal,
    raw: bytes,
    submitted: dict[str, Any],
    alert: AlertInput,
    cfg: Settings,
) -> tuple[int, dict[str, Any]]:
    """경보 원본과 정규화 작업을 한 트랜잭션에 저장한다. (HTTP 상태, AlertAccepted) 반환."""
    if principal.producer_key != alert.producer_key:
        raise ApiError(403, "PRODUCER_NOT_ALLOWED", "인증된 생산자와 producer_key가 일치하지 않습니다.")
    assert principal.producer_id is not None
    submitted_json = dumps(submitted)

    async with session.begin():
        existing = await _existing_alert(session, principal.producer_id, alert.event_id, submitted_json)
        if existing is None:
            release = await active_release(session, cfg.KNOWLEDGE_DEPLOYMENT)
            if release is None:
                raise ApiError(
                    503, "KNOWLEDGE_NOT_READY", "활성화된 published 지식 배포본이 없어 신규 경보를 접수할 수 없습니다.",
                    headers={"Retry-After": str(cfg.RETRY_AFTER_SECONDS)},
                )
            inserted = await fetch_one(
                session,
                """
                INSERT INTO rsw.alert_events
                  (producer_id, source_event_id, reported_asset_key, occurred_at, producer_schema_version,
                   raw_payload, parsed_payload, payload_sha256)
                VALUES (:producer_id, :event_id, :asset_key, :occurred_at, :psv,
                        :raw, CAST(:submitted AS jsonb), :sha)
                ON CONFLICT (producer_id, source_event_id) DO NOTHING
                RETURNING id, received_at
                """,
                producer_id=principal.producer_id, event_id=alert.event_id, asset_key=alert.asset_key,
                occurred_at=alert.occurred_at, psv=alert.producer_schema_version, raw=raw,
                submitted=submitted_json, sha=hashlib.sha256(raw).hexdigest(),
            )
            if inserted is not None:
                job = await insert_job(
                    session, kind="normalize_alert", alert_id=inserted["id"], release_id=release["id"],
                    max_attempts=cfg.JOB_MAX_ATTEMPTS,
                    config={
                        "knowledge_deployment": cfg.KNOWLEDGE_DEPLOYMENT,
                        "knowledge_version": release["version"],
                        "normalizer_version": cfg.NORMALIZER_VERSION,
                        "matcher_version": cfg.MATCHER_VERSION,
                    },
                )
                return 201, _accepted(inserted["id"], alert, inserted["received_at"], job, duplicate=False)
            # 동시에 같은 사건이 먼저 커밋되었다. 커밋된 행을 다시 읽어 비교한다.
            existing = await _existing_alert(session, principal.producer_id, alert.event_id, submitted_json)
            assert existing is not None

        if not existing["same_payload"]:
            raise ApiError(409, "EVENT_ID_CONFLICT", "같은 생산자의 사건 ID에 다른 내용이 이미 등록되어 있습니다.")
        job = await latest_normalize_job(session, existing["id"])
        assert job is not None, "every stored alert has a normalization job"
        return 200, _accepted(existing["id"], alert, existing["received_at"], job, duplicate=True)


def _accepted(alert_id: UUID, alert: AlertInput, received_at: Any, job: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
    return {
        "alert_id": alert_id,
        "event_id": alert.event_id,
        "producer_key": alert.producer_key,
        "received_at": received_at,
        "duplicate": duplicate,
        "normalization": processing_view(job),
        "status_url": alert_status_url(alert_id),
    }


NOT_EVALUATED = {"status": "not_evaluated", "matcher_version": None, "items": [], "error": None}


async def get_alert_details(
    session: AsyncSession, principal: Principal, alert_id: UUID, context_id: UUID | None
) -> dict[str, Any]:
    alert = await fetch_one(
        session,
        "SELECT id, producer_id, received_at, parsed_payload FROM rsw.alert_events WHERE id = :id",
        id=alert_id,
    )
    # 생산자 서비스는 자기가 보낸 경보만 조회할 수 있다. 다른 경보는 존재를 숨긴다.
    if alert is None or (not principal.is_user and alert["producer_id"] != principal.producer_id):
        raise ApiError(404, "ALERT_NOT_FOUND", "경보를 찾을 수 없습니다.")

    if context_id is not None:
        job = await fetch_one(
            session,
            """
            SELECT id, status, updated_at, error, result_context_id FROM rsw.background_jobs
            WHERE alert_id = :alert_id AND kind = 'normalize_alert' AND status = 'succeeded'
              AND result_context_id = :context_id
            """,
            alert_id=alert_id, context_id=context_id,
        )
        if job is None:
            raise ApiError(404, "CONTEXT_NOT_FOUND", "이 경보에 속한 해당 context를 찾을 수 없습니다.")
    else:
        job = await latest_normalize_job(session, alert_id)
        if job is None:
            raise ApiError(500, "INTERNAL_ERROR", "경보의 정규화 작업 기록이 없습니다.")

    details: dict[str, Any] = {
        "alert_id": alert["id"],
        "received_at": alert["received_at"],
        "submitted_event": as_json(alert["parsed_payload"]),
        "normalization": processing_view(job),
        "context": None,
        "related_scenarios": dict(NOT_EVALUATED),
    }
    if job["status"] == "succeeded":
        context, scenarios = await load_context_view(session, job["result_context_id"])
        details["context"] = context
        details["related_scenarios"] = scenarios
    return details


async def load_context_view(session: AsyncSession, context_id: UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    row = await fetch_one(
        session,
        """
        SELECT ac.id, ac.release_id, kr.version AS knowledge_version, ac.normalizer_version, ac.status,
               ac.operating_context, ac.warnings, ac.sealed_at
        FROM rsw.alert_contexts ac
        JOIN rsw.knowledge_releases kr ON kr.id = ac.release_id
        WHERE ac.id = :id AND ac.sealed_at IS NOT NULL
        """,
        id=context_id,
    )
    if row is None:
        raise ApiError(404, "CONTEXT_NOT_FOUND", "봉인된 context를 찾을 수 없습니다.")
    integration = (as_json(row["operating_context"]) or {}).get("_integration", {})
    context = {
        "context_id": row["id"],
        "knowledge_release_id": row["release_id"],
        "knowledge_version": row["knowledge_version"],
        "normalizer_version": row["normalizer_version"],
        "status": row["status"],
        "asset": integration.get("asset"),
        "code_mappings": integration.get("code_mappings", []),
        "observation_mappings": integration.get("observation_mappings", []),
        "ml_mappings": integration.get("ml_mappings", []),
        "warnings": as_json(row["warnings"]) or [],
        "sealed_at": row["sealed_at"],
    }
    scenarios = integration.get("related_scenarios") or dict(NOT_EVALUATED)
    return context, scenarios
