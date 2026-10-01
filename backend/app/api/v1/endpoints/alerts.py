from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_cfg, Principal, get_principal, idempotency_key, read_json_request, require_producer, require_user
from app.api.v1.endpoints.openapi_docs import request_body_doc, responses_doc
from app.core.config import Settings
from app.db.session import get_db
from app.schemas.alerts import AlertAccepted, AlertDetails, AlertInput
from app.schemas.answers import QuestionAccepted, QuestionInput
from app.services import alerts as alert_service
from app.services import questions as question_service

router = APIRouter(prefix="/alerts", tags=["Alerts"])


def _json(model, payload: dict, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    body = model.model_validate(payload).model_dump(mode="json")
    return JSONResponse(body, status_code=status, headers=headers or {})


@router.post(
    "",
    summary="ML 경보 원본 접수",
    status_code=201,
    response_model=AlertAccepted,
    openapi_extra=request_body_doc(AlertInput),
    responses=responses_doc({200: (AlertAccepted, "같은 사건·같은 내용 재수신")}, 400, 401, 403, 409, 413, 415, 422, 503),
)
async def create_alert(
    request: Request,
    principal: Principal = Depends(require_producer),
    db: AsyncSession = Depends(get_db),
    cfg: Settings = Depends(get_cfg),
) -> JSONResponse:
    raw, submitted, alert = await read_json_request(request, AlertInput, cfg.ALERT_MAX_BYTES)
    status, payload = await alert_service.accept_alert(db, principal, raw, submitted, alert, cfg)
    headers = {}
    if status == 201:
        headers["Location"] = payload["status_url"]
    if payload["normalization"]["status"] in ("queued", "running"):
        headers["Retry-After"] = str(cfg.RETRY_AFTER_SECONDS)
    return _json(AlertAccepted, payload, status, headers)


@router.get(
    "/{id}",
    summary="경보와 정규화 결과·관련 시나리오 조회",
    response_model=AlertDetails,
    responses=responses_doc({}, 401, 403, 404, 422),
)
async def get_alert(
    id: UUID,
    context_id: UUID | None = Query(default=None, description="이 경보에 속한 특정 봉인 context"),
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
    cfg: Settings = Depends(get_cfg),
) -> JSONResponse:
    payload = await alert_service.get_alert_details(db, principal, id, context_id)
    headers = {}
    if payload["normalization"]["status"] in ("queued", "running"):
        headers["Retry-After"] = str(cfg.RETRY_AFTER_SECONDS)
    return _json(AlertDetails, payload, 200, headers)


@router.post(
    "/{id}/questions",
    summary="작업자 질문 접수 및 답변 실행 생성",
    status_code=202,
    response_model=QuestionAccepted,
    openapi_extra=request_body_doc(QuestionInput, idempotent=True),
    responses=responses_doc({}, 400, 401, 403, 404, 409, 413, 415, 422),
)
async def create_question(
    id: UUID,
    request: Request,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    cfg: Settings = Depends(get_cfg),
) -> JSONResponse:
    key = idempotency_key(request)
    _, submitted, question = await read_json_request(request, QuestionInput, cfg.QUESTION_MAX_BYTES)
    outcome = await question_service.accept_question(db, principal, cfg, id, key, submitted, question)
    headers = {
        "Location": outcome.body["status_url"],
        "Retry-After": str(cfg.RETRY_AFTER_SECONDS),
        "Idempotency-Replayed": "true" if outcome.replayed else "false",
    }
    return _json(QuestionAccepted, outcome.body, outcome.status, headers)
