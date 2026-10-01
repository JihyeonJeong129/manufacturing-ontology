"""통합 테스트: 실제 PostgreSQL 18 + pgvector에 마이그레이션을 적용하고 합성 데모 배포본을 발행한다.

- 대상 DB: 환경변수 POSTGRES_* 서버의 "<POSTGRES_DB>_test" (TEST_POSTGRES_DB로 변경 가능).
  세션 시작 시 이 테스트 DB만 DROP/CREATE 한다. 운영/개발 DB는 건드리지 않는다.
- PostgreSQL에 연결할 수 없으면 통합 테스트 전체를 skip 한다(단위 테스트는 그대로 실행).
"""
from __future__ import annotations

import asyncio
import os

import asyncpg
import httpx
import pytest

from app.core.config import Settings
from app.db.migrate import upgrade_head
from app.db.session import Database
from app.demo.seed import seed_demo
from app.main import create_app
from app.worker.runner import Worker


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/integration/" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def integration_env(tmp_path_factory):
    base = Settings()
    test_db = os.environ.get("TEST_POSTGRES_DB") or f"{base.POSTGRES_DB}_test"
    if test_db == base.POSTGRES_DB:
        pytest.exit("TEST_POSTGRES_DB must differ from POSTGRES_DB (the test database is dropped and recreated)")
    storage = tmp_path_factory.mktemp("rsw-storage")
    cfg = Settings(
        POSTGRES_DB=test_db, STORAGE_ROOT=storage, JOB_RETRY_BASE_SECONDS=0, WORKER_POLL_INTERVAL_SECONDS=0.05,
        IDEMPOTENCY_WAIT_SECONDS=3, DB_ECHO=False,
    )
    admin = f"postgresql://{base.POSTGRES_USER}:{base.POSTGRES_PASSWORD}@{base.POSTGRES_HOST}:{base.POSTGRES_PORT}/postgres"

    async def recreate() -> Exception | None:
        try:
            conn = await asyncpg.connect(admin, timeout=5)
        except Exception as exc:  # noqa: BLE001
            return exc
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{test_db}"')
        finally:
            await conn.close()
        return None

    error = asyncio.run(recreate())
    if error is not None:
        pytest.skip(f"PostgreSQL unavailable for integration tests: {error!r}")
    upgrade_head(cfg.DATABASE_URL)

    async def seed():
        database = Database(cfg.DATABASE_URL, null_pool=True)
        try:
            async with database.session() as session, session.begin():
                return await seed_demo(session, cfg)
        finally:
            await database.dispose()

    return {"cfg": cfg, "seed": asyncio.run(seed())}


@pytest.fixture
def cfg(integration_env) -> Settings:
    return integration_env["cfg"]


@pytest.fixture
def seed(integration_env) -> dict:
    return integration_env["seed"]


@pytest.fixture
async def database(cfg):
    db = Database(cfg.DATABASE_URL, null_pool=True)
    yield db
    await db.dispose()


@pytest.fixture
async def client(cfg, database):
    app = create_app(cfg, database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def worker(cfg, database) -> Worker:
    return Worker(database, cfg, worker_id="test-worker")


@pytest.fixture
def tokens(cfg):
    return {
        "ml": {"Authorization": f"Bearer {cfg.DEMO_ML_TOKEN}"},
        "operator": {"Authorization": f"Bearer {cfg.DEMO_OPERATOR_TOKEN}"},
        "reviewer": {"Authorization": f"Bearer {cfg.DEMO_REVIEWER_TOKEN}"},
    }
