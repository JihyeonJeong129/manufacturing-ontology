"""PostgreSQL 작업 테이블 소비 워커 (API_SPEC.md 10.2).

- FOR UPDATE SKIP LOCKED로 작업을 잡고 lease_token/lease_expires_at으로 소유권을 표시한다.
- 긴 답변 생성 동안 DB 트랜잭션을 열어 두지 않고 heartbeat로 lease를 연장한다.
- 결과 저장 트랜잭션은 lease_token이 여전히 유효한지 먼저 확인한다(늦게 끝난 이전 워커 차단).
- 재시도를 소진했을 때만 failed로 확정한다. 답변 run은 재시도 동안 running을 유지한다.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import traceback
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.jsonutil import as_json, dumps
from app.db.session import Database
from app.db.sqlutil import execute, fetch_one
from app.services.answering.invariants import AnswerInvariantError
from app.services.answering.embedding import local_embedder
from app.services.answering.pipeline import RunNotAnswerable, prepare_answer
from app.services.normalizer import NormalizationError, normalize_alert

logger = logging.getLogger("app.worker")


class LeaseLost(Exception):
    """다른 워커가 작업을 회수했거나 작업이 이미 끝났다."""


@dataclass
class Job:
    id: UUID
    kind: str
    alert_id: UUID | None
    run_id: UUID | None
    release_id: UUID
    config: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_token: UUID

    def row(self) -> dict[str, Any]:
        return {"id": self.id, "alert_id": self.alert_id, "run_id": self.run_id,
                "release_id": self.release_id, "config": self.config}


class Worker:
    def __init__(self, database: Database, cfg: Settings, worker_id: str | None = None) -> None:
        self.db = database
        self.cfg = cfg
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ claim

    async def claim(self) -> Job | None:
        async with self.db.session() as session, session.begin():
            while True:
                row = await fetch_one(
                    session,
                    """
                    SELECT id, kind, alert_id, run_id, release_id, config, attempts, max_attempts, status
                    FROM rsw.background_jobs
                    WHERE (status = 'queued' AND available_at <= now())
                       OR (status = 'running' AND lease_expires_at < now())
                    ORDER BY available_at, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                )
                if row is None:
                    return None
                if row["attempts"] >= row["max_attempts"]:
                    # 마지막 시도 중 워커가 사라져 lease가 만료되었다. 영원히 running으로 남기지 않는다.
                    await self._fail_in_tx(session, row, "WORKER_LEASE_EXPIRED",
                                           "처리 워커가 제한 시간 안에 작업을 완료하지 못했습니다.", retryable=True)
                    continue
                token = uuid.uuid4()
                await execute(
                    session,
                    """
                    UPDATE rsw.background_jobs
                    SET status = 'running', attempts = attempts + 1, lease_token = :token,
                        lease_expires_at = now() + make_interval(secs => :lease), heartbeat_at = now()
                    WHERE id = :id
                    """,
                    id=row["id"], token=token, lease=self.cfg.JOB_LEASE_SECONDS,
                )
                if row["kind"] == "generate_answer":
                    await execute(session, "UPDATE rsw.assistant_runs SET status = 'running' WHERE id = :id AND status = 'pending'",
                                  id=row["run_id"])
                return Job(row["id"], row["kind"], row["alert_id"], row["run_id"], row["release_id"],
                           as_json(row["config"]) or {}, row["attempts"] + 1, row["max_attempts"], token)

    async def _lock_lease(self, session: AsyncSession, job: Job) -> None:
        row = await fetch_one(
            session,
            "SELECT id FROM rsw.background_jobs WHERE id = :id AND lease_token = :token AND status = 'running' FOR UPDATE",
            id=job.id, token=job.lease_token,
        )
        if row is None:
            raise LeaseLost(str(job.id))

    async def heartbeat(self, job: Job) -> bool:
        async with self.db.session() as session, session.begin():
            count = await execute(
                session,
                """
                UPDATE rsw.background_jobs
                SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => :lease)
                WHERE id = :id AND lease_token = :token AND status = 'running'
                """,
                id=job.id, token=job.lease_token, lease=self.cfg.JOB_LEASE_SECONDS,
            )
        return count == 1

    async def _heartbeat_loop(self, job: Job) -> None:
        interval = max(1.0, self.cfg.JOB_LEASE_SECONDS / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                if not await self.heartbeat(job):
                    return
            except Exception:  # noqa: BLE001
                logger.warning("heartbeat failed job=%s", job.id, exc_info=True)

    # ------------------------------------------------------------ process

    async def run_once(self) -> bool:
        job = await self.claim()
        if job is None:
            return False
        await self.process(job)
        return True

    async def process(self, job: Job) -> None:
        beat = asyncio.create_task(self._heartbeat_loop(job))
        try:
            if job.kind == "normalize_alert":
                await self._normalize(job)
            else:
                await self._answer(job)
            logger.info("job succeeded kind=%s id=%s", job.kind, job.id)
        except LeaseLost:
            logger.warning("lease lost job=%s; result discarded", job.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("job failed kind=%s id=%s attempt=%s: %s", job.kind, job.id, job.attempts, exc)
            await self._handle_failure(job, exc)
        finally:
            beat.cancel()

    async def _normalize(self, job: Job) -> None:
        async with self.db.session() as session, session.begin():
            await self._lock_lease(session, job)
            context_id = await normalize_alert(session, job.row(), self.cfg.CLOCK_SKEW_SECONDS)
            await execute(
                session,
                """
                UPDATE rsw.background_jobs
                SET status = 'succeeded', result_context_id = :cid, finished_at = now(), error = NULL
                WHERE id = :id
                """,
                id=job.id, cid=context_id,
            )

    async def _answer(self, job: Job) -> None:
        # 근거 수집·생성은 짧은 읽기 트랜잭션에서 수행하고, 결과는 한 트랜잭션으로 확정한다.
        async with self.db.session() as session:
            prepared = await prepare_answer(session, job.run_id, self.cfg)
            await session.rollback()
        async with self.db.session() as session, session.begin():
            await self._lock_lease(session, job)
            for row in prepared.candidates:
                await execute(
                    session,
                    """
                    INSERT INTO rsw.retrieval_candidates
                      (run_id, release_id, chunk_id, method, rank, score, selected, applicability_result)
                    VALUES (:run_id, :release_id, :chunk_id, :method, :rank, :score, :selected,
                            CAST(:applicability AS jsonb))
                    """,
                    run_id=row["run_id"], release_id=row["release_id"], chunk_id=row["chunk_id"],
                    method=row["method"], rank=row["rank"], score=row["score"], selected=row["selected"],
                    applicability=dumps(row["applicability_result"]),
                )
            for row in prepared.citations:
                await execute(
                    session,
                    """
                    INSERT INTO rsw.answer_citations (id, run_id, release_id, claim_key, section_id, span_id, cited_text)
                    VALUES (:id, :run_id, :release_id, :claim_key, :section_id, :span_id, :cited_text)
                    """,
                    **row,
                )
            updated = await execute(
                session,
                """
                UPDATE rsw.assistant_runs
                SET status = :status, answer = CAST(:answer AS jsonb), input_snapshot = CAST(:snapshot AS jsonb),
                    finished_at = now()
                WHERE id = :id AND status IN ('pending', 'running')
                """,
                id=job.run_id, status=prepared.status, answer=dumps(prepared.result),
                snapshot=dumps(prepared.input_snapshot),
            )
            if updated != 1:
                raise LeaseLost(f"run {job.run_id} already completed")
            await execute(session, "UPDATE rsw.background_jobs SET status = 'succeeded', finished_at = now(), error = NULL WHERE id = :id",
                          id=job.id)

    # ------------------------------------------------------------ failure

    def _classify(self, exc: BaseException) -> tuple[str, str, bool]:
        if isinstance(exc, NormalizationError):
            return exc.code, exc.message, exc.retryable
        if isinstance(exc, RunNotAnswerable):
            return exc.code, exc.message, False
        if isinstance(exc, AnswerInvariantError):
            return "ANSWER_CONTRACT_VIOLATION", "생성된 답변이 근거 계약을 만족하지 않아 저장하지 않았습니다.", False
        return "PROCESSING_FAILED", "처리 중 일시적인 오류가 발생했습니다.", True

    async def _handle_failure(self, job: Job, exc: BaseException) -> None:
        code, message, retryable = self._classify(exc)
        internal = {"exception": type(exc).__name__, "detail": str(exc)[:2000],
                    "traceback": traceback.format_exception_only(type(exc), exc)[-1][:2000],
                    "attempt": job.attempts, "worker": self.worker_id}
        try:
            async with self.db.session() as session, session.begin():
                await self._lock_lease(session, job)
                if retryable and job.attempts < job.max_attempts:
                    delay = self.cfg.JOB_RETRY_BASE_SECONDS * (2 ** (job.attempts - 1))
                    await execute(
                        session,
                        """
                        UPDATE rsw.background_jobs
                        SET status = 'queued', lease_token = NULL, lease_expires_at = NULL,
                            available_at = now() + make_interval(secs => :delay), error = CAST(:error AS jsonb)
                        WHERE id = :id
                        """,
                        id=job.id, delay=delay,
                        error=dumps({"code": code, "message": message, "retryable": True, "internal": internal}),
                    )
                    return
                row = {"id": job.id, "kind": job.kind, "run_id": job.run_id}
                await self._fail_in_tx(session, row, code, message, retryable=False, internal=internal)
        except LeaseLost:
            logger.warning("lease lost while recording failure job=%s", job.id)

    async def _fail_in_tx(self, session: AsyncSession, row: dict[str, Any], code: str, message: str, *,
                          retryable: bool, internal: dict[str, Any] | None = None) -> None:
        error = {"code": code, "message": message, "retryable": retryable, "internal": internal or {}}
        await execute(
            session,
            """
            UPDATE rsw.background_jobs SET status = 'failed', error = CAST(:error AS jsonb), finished_at = now()
            WHERE id = :id
            """,
            id=row["id"], error=dumps(error),
        )
        if row["kind"] == "generate_answer" and row["run_id"] is not None:
            # 실패로 완료된 run도 기존 트리거 계약상 input_snapshot이 필요하다.
            snapshot = {"snapshot_version": 1, "snapshot_kind": "failure", "error_code": code,
                        "note": "답변 생성 전 또는 생성 중 실패하여 실제 모델 입력이 확정되지 않았습니다."}
            await execute(
                session,
                """
                UPDATE rsw.assistant_runs
                SET status = 'failed', error = CAST(:error AS jsonb), input_snapshot = COALESCE(input_snapshot, CAST(:snapshot AS jsonb)),
                    finished_at = now()
                WHERE id = :id AND status IN ('pending', 'running')
                """,
                id=row["run_id"], error=dumps({"code": code, "message": message, "retryable": retryable}),
                snapshot=dumps(snapshot),
            )

    # ------------------------------------------------------------ loop

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        logger.info("worker %s started", self.worker_id)
        await self._warm_up_embedder()
        while not self._stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("worker loop error")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.WORKER_POLL_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
            await self._purge_idempotency()
        logger.info("worker %s stopped", self.worker_id)

    async def _warm_up_embedder(self) -> None:
        """로컬 임베딩 모델을 미리 읽어 첫 질문의 지연을 줄인다. 실패해도 워커는 계속 동작한다."""
        if self.cfg.EMBEDDING_BACKEND != "local":
            return
        embedder = local_embedder(self.cfg.EMBEDDING_MODEL_DIR)
        if embedder is None:
            logger.warning("embedding model not found at %s; vector search disabled", self.cfg.EMBEDDING_MODEL_DIR)
            return
        try:
            await asyncio.to_thread(embedder.warm_up)
            logger.info("embedding model loaded: %s", embedder.spec.model_name)
        except Exception:  # noqa: BLE001
            logger.exception("embedding model warm-up failed")

    _last_purge = 0.0

    async def _purge_idempotency(self) -> None:
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_purge < 3600:
            return
        self._last_purge = loop.time()
        try:
            async with self.db.session() as session, session.begin():
                await execute(session, "DELETE FROM rsw.api_idempotency_requests WHERE expires_at <= now()")
        except Exception:  # noqa: BLE001
            logger.warning("idempotency purge failed", exc_info=True)


async def main(cfg: Settings | None = None) -> None:
    from app.core.config import settings

    cfg = cfg or settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    database = Database.from_settings(cfg)
    worker = Worker(database, cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        await worker.run_forever()
    finally:
        await database.dispose()
