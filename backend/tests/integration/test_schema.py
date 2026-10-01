"""설계 스키마(schema.sql)와 API 보조 마이그레이션을 실제 PostgreSQL에서 검증한다."""
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from app.db.sqlutil import execute, fetch_one, fetch_value

CHECKS_SQL = Path(__file__).parent / "sql" / "checks.sql"


def asyncpg_dsn(cfg) -> str:
    return f"postgresql://{cfg.POSTGRES_USER}:{cfg.POSTGRES_PASSWORD}@{cfg.POSTGRES_HOST}:{cfg.POSTGRES_PORT}/{cfg.POSTGRES_DB}"


async def test_design_checks_sql_passes_all_30_checks(cfg):
    """docs/design/schema/checks.sql을 수정 없이 실행한다 (BEGIN … ROLLBACK, 합성 데이터는 남지 않음)."""
    conn = await asyncpg.connect(asyncpg_dsn(cfg))
    notices: list[str] = []
    conn.add_log_listener(lambda _c, message: notices.append(message.message))
    try:
        await conn.execute(CHECKS_SQL.read_text(encoding="utf-8"))
    finally:
        await conn.close()
    assert any(n.startswith("PASS: 30 single-session schema checks") for n in notices), notices


async def test_schema_shape(database):
    async with database.session() as s:
        counts = await fetch_one(s, """
            SELECT (SELECT count(*) FROM information_schema.tables WHERE table_schema = 'rsw') AS tables,
                   (SELECT count(*) FROM information_schema.table_constraints
                     WHERE table_schema = 'rsw' AND constraint_type = 'FOREIGN KEY') AS fks,
                   (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS vector,
                   current_setting('server_version_num')::int AS pg
        """)
    assert counts["tables"] == 43 + 3  # 설계 43개 + background_jobs, api_credentials, api_idempotency_requests
    assert counts["fks"] >= 96
    assert counts["pg"] >= 180000
    assert tuple(int(x) for x in counts["vector"].split(".")[:2]) >= (0, 8)


async def _expect_db_error(database, sql: str, fragment: str, **params):
    async with database.session() as s:
        with pytest.raises(DBAPIError) as info:
            async with s.begin():
                await execute(s, sql, **params)
    assert fragment in str(info.value)


async def test_background_job_constraints(database, seed):
    release = seed["release_id"]
    await _expect_db_error(database, "INSERT INTO rsw.background_jobs (kind, release_id) VALUES ('normalize_alert', :r)",
                           "check constraint", r=release)
    await _expect_db_error(database, "INSERT INTO rsw.background_jobs (kind, release_id, status) VALUES ('generate_answer', :r, 'bogus')",
                           "check constraint", r=release)


async def test_finished_job_is_immutable(database, client, tokens, worker):
    from tests.integration.helpers import drain, post_alert

    response = await post_alert(client, tokens["ml"])
    await drain(worker)
    async with database.session() as s:
        job_id = await fetch_value(s, "SELECT id FROM rsw.background_jobs WHERE alert_id = :a", a=response.json()["alert_id"])
    await _expect_db_error(database, "UPDATE rsw.background_jobs SET status = 'queued' WHERE id = :id",
                           "Finished background job is immutable", id=job_id)
    await _expect_db_error(database, "DELETE FROM rsw.background_jobs WHERE id = :id", "cannot be deleted", id=job_id)


async def test_idempotency_and_feedback_index_exist(database):
    async with database.session() as s:
        index = await fetch_value(s, "SELECT indexdef FROM pg_indexes WHERE schemaname = 'rsw' AND indexname = 'feedback_one_successor'")
    assert "UNIQUE" in index and "supersedes_id IS NOT NULL" in index
    await _expect_db_error(database, """
        INSERT INTO rsw.api_idempotency_requests (principal_id, method, resource_path, idempotency_key, request_body,
          request_sha256, response_status, response_body, expires_at)
        SELECT id, 'POST', '/x', 'bad key', '{}', repeat('a', 64), 202, '{}', now() + interval '1 day' FROM rsw.actors LIMIT 1
    """, "check constraint")
