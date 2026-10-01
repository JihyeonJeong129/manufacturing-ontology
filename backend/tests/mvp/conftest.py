"""RSW MVP 동작 검증 (tests/mvp). 기존 단위·통합 테스트와 분리해서 실행한다.

  docker compose --profile test run --rm mvp-tests      (= pytest -q tests/mvp)

- 원본 문서: RAW_DATA_DIR (compose에서 ../RAW Data → /data/raw 읽기 전용). 없으면 전체 skip.
- 임베딩 모델: EMBEDDING_MODEL_DIR (이미지에 포함). 없으면 벡터 관련 검사만 skip.
- DB: "<POSTGRES_DB>_mvp_test"(MVP_TEST_POSTGRES_DB로 변경 가능)를 DROP/CREATE한 뒤 마이그레이션하고
  실제 지식을 한 번 적재한다. 운영/개발 DB와 통합 테스트 DB는 건드리지 않는다.
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
from app.main import create_app
from app.mvp import build
from app.mvp.loader import load_mvp
from app.mvp.sources import SOURCES, SourceError, locate
from app.services.answering.embedding import model_available
from app.worker.runner import Worker


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/mvp/" in str(item.fspath):
            item.add_marker(pytest.mark.mvp)


@pytest.fixture(scope="session")
def raw_dir():
    path = Settings().RAW_DATA_DIR
    try:
        for source in SOURCES.values():
            locate(path, source)
    except SourceError as exc:
        pytest.skip(f"원본 문서가 없어 MVP 동작 검증을 건너뜁니다: {exc}")
    return path


@pytest.fixture(scope="session")
def pages(raw_dir):
    return build.load_pages(raw_dir)


@pytest.fixture(scope="session")
def resolved(pages):
    return build.resolve_sections(pages)


@pytest.fixture(scope="session")
def model_ready():
    return model_available(Settings().EMBEDDING_MODEL_DIR)


@pytest.fixture(scope="session")
def mvp_env(raw_dir, tmp_path_factory):
    base = Settings()
    test_db = os.environ.get("MVP_TEST_POSTGRES_DB") or f"{base.POSTGRES_DB}_mvp_test"
    if test_db == base.POSTGRES_DB:
        pytest.exit("MVP_TEST_POSTGRES_DB must differ from POSTGRES_DB (the test database is dropped and recreated)")
    cfg = Settings(POSTGRES_DB=test_db, STORAGE_ROOT=tmp_path_factory.mktemp("rsw-mvp-storage"), JOB_RETRY_BASE_SECONDS=0,
                   WORKER_POLL_INTERVAL_SECONDS=0.05, IDEMPOTENCY_WAIT_SECONDS=3, DB_ECHO=False, RAW_DATA_DIR=raw_dir)
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
        pytest.skip(f"PostgreSQL unavailable for MVP tests: {error!r}")
    upgrade_head(cfg.DATABASE_URL)

    async def load():
        database = Database(cfg.DATABASE_URL, null_pool=True)
        try:
            async with database.session() as session, session.begin():
                return await load_mvp(session, cfg)
        finally:
            await database.dispose()

    return {"cfg": cfg, "report": asyncio.run(load())}


@pytest.fixture
def cfg(mvp_env) -> Settings:
    return mvp_env["cfg"]


@pytest.fixture
def load_report(mvp_env) -> dict:
    return mvp_env["report"]


@pytest.fixture
async def database(cfg):
    db = Database(cfg.DATABASE_URL, null_pool=True)
    yield db
    await db.dispose()


@pytest.fixture
async def session(database):
    async with database.session() as s:
        yield s


@pytest.fixture
async def client(cfg, database):
    app = create_app(cfg, database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def worker(cfg, database) -> Worker:
    return Worker(database, cfg, worker_id="mvp-test-worker")


@pytest.fixture
def tokens(cfg):
    return {
        "ml": {"Authorization": f"Bearer {cfg.MVP_ML_TOKEN}"},
        "operator": {"Authorization": f"Bearer {cfg.MVP_OPERATOR_TOKEN}"},
        "reviewer": {"Authorization": f"Bearer {cfg.MVP_REVIEWER_TOKEN}"},
    }
