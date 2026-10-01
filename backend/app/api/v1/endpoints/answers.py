from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_cfg, Principal, idempotency_key, read_json_request, require_user
from app.api.v1.endpoints.openapi_docs import request_body_doc, responses_doc
from app.core.config import Settings
from app.db.session import get_db
from app.schemas.answers import AnswerDetails, FeedbackCreated, FeedbackInput
from app.services import feedback as feedback_service
from app.services import questions as question_service

router = APIRouter(prefix="/answers", tags=["Answers"])


@router.get(
    "/{id}",
    summary="비동기 답변 상태·결과 조회",
    response_model=AnswerDetails,
    responses=responses_doc({}, 401, 403, 404, 422),
)
async def get_answer(
    id: UUID,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    cfg: Settings = Depends(get_cfg),
) -> JSONResponse:
    payload = await question_service.get_answer(db, id)
    headers = {}
    if payload["status"] in ("pending", "running"):
        headers["Retry-After"] = str(cfg.RETRY_AFTER_SECONDS)
    body = AnswerDetails.model_validate(payload).model_dump(mode="json")
    return JSONResponse(body, headers=headers)


@router.post(
    "/{id}/feedback",
    summary="답변 적절성 피드백 추가",
    status_code=201,
    response_model=FeedbackCreated,
    tags=["Feedback"],
    openapi_extra=request_body_doc(FeedbackInput, idempotent=True),
    responses=responses_doc({}, 400, 401, 403, 404, 409, 413, 415, 422),
)
async def create_feedback(
    id: UUID,
    request: Request,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    cfg: Settings = Depends(get_cfg),
) -> JSONResponse:
    key = idempotency_key(request)
    _, submitted, data = await read_json_request(request, FeedbackInput, cfg.FEEDBACK_MAX_BYTES)
    outcome = await feedback_service.create_feedback(db, principal, cfg, id, key, submitted, data)
    body = FeedbackCreated.model_validate(outcome.body).model_dump(mode="json")
    return JSONResponse(body, status_code=outcome.status,
                        headers={"Idempotency-Replayed": "true" if outcome.replayed else "false"})
