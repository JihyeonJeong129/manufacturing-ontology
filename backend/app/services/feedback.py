"""POST /answers/{id}/feedback 서비스. 피드백은 답변 품질 의견이며 원인 확정이 아니다."""
from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.jsonutil import dumps, utc_iso
from app.db.sqlutil import fetch_one
from app.schemas.answers import FeedbackInput
from app.services.idempotency import IdempotentResult, run_idempotent

REVIEWABLE = ("answered", "needs_context", "insufficient_evidence")


def feedback_path(answer_id: UUID) -> str:
    return f"/api/v1/answers/{answer_id}/feedback"


async def create_feedback(
    session: AsyncSession, principal: Principal, cfg: Settings, answer_id: UUID, key: str,
    submitted: dict[str, Any], data: FeedbackInput,
) -> IdempotentResult:
    async def create(tx: AsyncSession) -> tuple[int, dict[str, Any], dict[str, UUID]]:
        run = await fetch_one(tx, "SELECT id, status FROM rsw.assistant_runs WHERE id = :id", id=answer_id)
        if run is None:
            raise ApiError(404, "ANSWER_NOT_FOUND", "답변 실행을 찾을 수 없습니다.")
        if run["status"] not in REVIEWABLE:
            raise ApiError(409, "ANSWER_NOT_REVIEWABLE", "완료된 답변(answered/needs_context/insufficient_evidence)만 평가할 수 있습니다.")
        if data.supersedes_id is not None:
            # 같은 이전 의견의 동시 정정을 직렬화한다. (feedback_one_successor 인덱스로도 차단)
            previous = await fetch_one(
                tx, "SELECT id, run_id, reviewer_id FROM rsw.feedback WHERE id = :id FOR UPDATE", id=data.supersedes_id)
            if previous is None:
                raise ApiError(404, "FEEDBACK_NOT_FOUND", "정정 대상 피드백을 찾을 수 없습니다.")
            if previous["run_id"] != answer_id:
                raise ApiError(409, "FEEDBACK_TARGET_MISMATCH", "정정 대상 피드백이 다른 답변에 속합니다.")
            if previous["reviewer_id"] != principal.actor_id:
                raise ApiError(403, "FEEDBACK_NOT_OWNED", "다른 작성자의 피드백은 정정할 수 없습니다.")
            successor = await fetch_one(tx, "SELECT id FROM rsw.feedback WHERE supersedes_id = :id", id=data.supersedes_id)
            if successor is not None:
                raise ApiError(409, "FEEDBACK_ALREADY_SUPERSEDED", "이미 정정된 피드백입니다. 최신 정정 행을 대상으로 다시 제출하세요.")
        feedback_id = uuid.uuid4()
        row = await fetch_one(
            tx,
            """
            INSERT INTO rsw.feedback (id, run_id, reviewer_id, verdict, error_categories, comment, supersedes_id)
            VALUES (:id, :run_id, :reviewer, :verdict, CAST(:categories AS jsonb), :comment, :supersedes)
            RETURNING created_at
            """,
            id=feedback_id, run_id=answer_id, reviewer=principal.actor_id, verdict=data.verdict,
            categories=dumps(list(data.error_categories)), comment=data.comment, supersedes=data.supersedes_id,
        )
        assert row is not None
        body = {
            "feedback_id": str(feedback_id), "answer_id": str(answer_id), "reviewer_id": str(principal.actor_id),
            "verdict": data.verdict, "error_categories": list(data.error_categories), "comment": data.comment,
            "supersedes_id": str(data.supersedes_id) if data.supersedes_id else None,
            "created_at": utc_iso(row["created_at"]),
        }
        return 201, body, {"feedback_id": feedback_id}

    try:
        return await run_idempotent(
            session, cfg, principal_id=principal.actor_id, path=feedback_path(answer_id), key=key,
            submitted=submitted, create=create,
        )
    except Exception as exc:
        from app.db.sqlutil import UNIQUE_VIOLATION, constraint_name, sqlstate

        if sqlstate(exc) == UNIQUE_VIOLATION and constraint_name(exc) == "feedback_one_successor":
            raise ApiError(409, "FEEDBACK_ALREADY_SUPERSEDED", "이미 정정된 피드백입니다.") from exc
        raise
