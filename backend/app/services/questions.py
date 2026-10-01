"""POST /alerts/{id}/questions, GET /answers/{id} 서비스."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.core.config import Settings
from app.core.errors import ApiError, field_error, validation_error
from app.core.jsonutil import as_json, dumps, utc_iso
from app.db.sqlutil import fetch_one
from app.schemas.answers import QuestionInput
from app.services.answering.embedding import resolve_run_model_id
from app.services.followups import check_answer_value
from app.services.idempotency import IdempotentResult, run_idempotent
from app.services.jobs import insert_job, public_work_error

REPLYABLE = ("answered", "needs_context", "insufficient_evidence")


def answer_status_url(answer_id: UUID) -> str:
    return f"/api/v1/answers/{answer_id}"


def question_path(alert_id: UUID) -> str:
    return f"/api/v1/alerts/{alert_id}/questions"


async def accept_question(
    session: AsyncSession, principal: Principal, cfg: Settings, alert_id: UUID, key: str,
    submitted: dict[str, Any], question: QuestionInput,
) -> IdempotentResult:
    async def create(tx: AsyncSession) -> tuple[int, dict[str, Any], dict[str, UUID]]:
        alert = await fetch_one(tx, "SELECT id FROM rsw.alert_events WHERE id = :id", id=alert_id)
        if alert is None:
            raise ApiError(404, "ALERT_NOT_FOUND", "경보를 찾을 수 없습니다.")
        context = await fetch_one(
            tx,
            """
            SELECT ac.id, ac.alert_id, ac.release_id, ac.status, ac.sealed_at,
                   EXISTS (SELECT 1 FROM rsw.background_jobs j WHERE j.result_context_id = ac.id
                           AND j.status = 'succeeded') AS published_result
            FROM rsw.alert_contexts ac WHERE ac.id = :id
            """,
            id=question.context_id,
        )
        if context is None or context["sealed_at"] is None or not context["published_result"]:
            ready = await fetch_one(
                tx,
                """
                SELECT 1 FROM rsw.background_jobs
                WHERE alert_id = :alert_id AND kind = 'normalize_alert' AND status = 'succeeded' LIMIT 1
                """,
                alert_id=alert_id,
            )
            if ready is None:
                raise ApiError(409, "ALERT_NOT_READY", "경보 정규화와 봉인이 아직 완료되지 않았습니다.",
                               headers={"Retry-After": str(cfg.RETRY_AFTER_SECONDS)})
            raise ApiError(404, "CONTEXT_NOT_FOUND", "봉인된 context를 찾을 수 없습니다.")
        if context["alert_id"] != alert_id:
            raise ApiError(409, "CONTEXT_ALERT_MISMATCH", "context가 URL의 경보에 속하지 않습니다.")
        if context["status"] == "invalid":
            raise ApiError(409, "CONTEXT_NOT_USABLE", "invalid로 판정된 context로는 질문을 실행할 수 없습니다.")

        depth = 1
        operator_inputs: dict[str, Any] = {}
        run_id = uuid.uuid4()
        if question.reply_to_answer_id is not None:
            parent = await fetch_one(
                tx,
                "SELECT id, context_id, release_id, status, answer, parameters FROM rsw.assistant_runs WHERE id = :id",
                id=question.reply_to_answer_id,
            )
            if parent is None:
                raise ApiError(404, "ANSWER_NOT_FOUND", "이전 답변을 찾을 수 없습니다.")
            if parent["context_id"] != context["id"] or parent["release_id"] != context["release_id"]:
                raise ApiError(409, "REPLY_CONTEXT_MISMATCH", "이전 답변의 context/지식 배포본이 요청과 다릅니다.")
            if parent["status"] in ("pending", "running"):
                raise ApiError(409, "ANSWER_NOT_READY", "이전 답변이 아직 완료되지 않았습니다.",
                               headers={"Retry-After": str(cfg.RETRY_AFTER_SECONDS)})
            if parent["status"] not in REPLYABLE:
                raise ApiError(409, "REPLY_ANSWER_FAILED", "실패한 답변에는 후속 질문을 연결할 수 없습니다. 새 질문을 보내세요.")
            parameters = as_json(parent["parameters"]) or {}
            depth = int(parameters.get("conversation_depth", 1)) + 1
            if depth > cfg.CONVERSATION_MAX_DEPTH:
                raise ApiError(422, "CONVERSATION_LIMIT_EXCEEDED",
                               f"후속 질문 체인은 최대 {cfg.CONVERSATION_MAX_DEPTH}단계입니다.")
            operator_inputs = dict(parameters.get("operator_inputs") or {})
            follow_ups = {q["question_key"]: q for q in ((as_json(parent["answer"]) or {}).get("follow_up_questions") or [])}
            errors = []
            now = utc_iso(datetime.now(timezone.utc))
            for index, answer in enumerate(question.context_answers):
                spec = follow_ups.get(answer.question_key)
                pointer = f"/context_answers/{index}"
                if spec is None:
                    errors.append(field_error("body", pointer + "/question_key", "이전 답변이 요청하지 않은 질문입니다."))
                    continue
                problem = check_answer_value(spec, answer.value, answer.unit)
                if problem:
                    errors.append(field_error("body", pointer + "/value", problem))
                    continue
                operator_inputs[spec["field"]] = {
                    "value": answer.value, "unit": answer.unit, "question_key": answer.question_key,
                    "source": "operator_reported", "provided_at": now,
                    "provided_by": str(principal.actor_id), "provided_in_answer_id": str(run_id),
                }
            if errors:
                raise validation_error(errors, "추가 질문 응답이 이전 답변의 질문 계약과 맞지 않습니다.")

        parameters = {
            "api_request": submitted,
            "alert_id": str(alert_id),
            "reply_to_answer_id": str(question.reply_to_answer_id) if question.reply_to_answer_id else None,
            "conversation_depth": depth,
            "operator_inputs": operator_inputs,
            "retrieval": {"top_k": cfg.RETRIEVAL_TOP_K, "candidate_limit": cfg.RETRIEVAL_CANDIDATE_LIMIT},
            "validation_contract_version": cfg.VALIDATION_CONTRACT_VERSION,
        }
        embedding_model_id = await resolve_run_model_id(tx, cfg)
        run = await fetch_one(
            tx,
            """
            INSERT INTO rsw.assistant_runs
              (id, context_id, release_id, requested_by, question, model_provider, model_name,
               prompt_version, retriever_version, embedding_model_id, parameters)
            VALUES (:id, :cid, :rid, :actor, :question, :provider, :model, :prompt, :retriever, :embedding_model,
                    CAST(:parameters AS jsonb))
            RETURNING id, started_at
            """,
            id=run_id, cid=context["id"], rid=context["release_id"], actor=principal.actor_id,
            question=question.question, provider=cfg.ANSWER_MODEL_PROVIDER, model=cfg.ANSWER_MODEL_NAME,
            prompt=cfg.PROMPT_VERSION, retriever=cfg.RETRIEVER_VERSION, parameters=dumps(parameters),
            embedding_model=embedding_model_id,
        )
        assert run is not None
        await insert_job(
            tx, kind="generate_answer", run_id=run_id, release_id=context["release_id"],
            max_attempts=cfg.JOB_MAX_ATTEMPTS,
            config={"retriever_version": cfg.RETRIEVER_VERSION, "prompt_version": cfg.PROMPT_VERSION,
                    "model_provider": cfg.ANSWER_MODEL_PROVIDER, "model_name": cfg.ANSWER_MODEL_NAME},
        )
        body = {
            "answer_id": str(run_id), "alert_id": str(alert_id), "context_id": str(context["id"]),
            "knowledge_release_id": str(context["release_id"]), "status": "pending",
            "status_url": answer_status_url(run_id), "created_at": utc_iso(run["started_at"]),
        }
        return 202, body, {"run_id": run_id}

    return await run_idempotent(
        session, cfg, principal_id=principal.actor_id, path=question_path(alert_id), key=key,
        submitted=submitted, create=create,
    )


async def get_answer(session: AsyncSession, answer_id: UUID) -> dict[str, Any]:
    row = await fetch_one(
        session,
        """
        SELECT r.id, r.context_id, r.release_id, r.status, r.question, r.started_at, r.finished_at,
               r.answer, r.error, r.parameters, ac.alert_id
        FROM rsw.assistant_runs r JOIN rsw.alert_contexts ac ON ac.id = r.context_id
        WHERE r.id = :id
        """,
        id=answer_id,
    )
    if row is None:
        raise ApiError(404, "ANSWER_NOT_FOUND", "답변 실행을 찾을 수 없습니다.")
    parameters = as_json(row["parameters"]) or {}
    status = row["status"]
    return {
        "answer_id": row["id"],
        "alert_id": row["alert_id"],
        "context_id": row["context_id"],
        "knowledge_release_id": row["release_id"],
        "reply_to_answer_id": parameters.get("reply_to_answer_id"),
        "status": status,
        "question": row["question"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"] if status not in ("pending", "running") else None,
        "result": as_json(row["answer"]) if status in ("answered", "needs_context", "insufficient_evidence") else None,
        "error": public_work_error(row["error"]) if status == "failed" else None,
    }
