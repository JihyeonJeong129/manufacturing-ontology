from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, settings

# 벡터 연산자(<=>)와 rsw 도메인 타입을 해석하도록 public을 포함한 search_path를 고정한다.
SEARCH_PATH = "rsw,public"


def make_engine(url: str, *, echo: bool = False, null_pool: bool = False) -> AsyncEngine:
    kwargs: dict = {
        "echo": echo,
        "pool_pre_ping": True,
        "connect_args": {"server_settings": {"search_path": SEARCH_PATH, "timezone": "UTC"}},
    }
    if null_pool:
        kwargs["poolclass"] = NullPool
    return create_async_engine(url, **kwargs)


class Database:
    """엔진과 세션 팩토리 묶음. API 앱, 워커, CLI, 테스트가 각각 인스턴스를 만든다."""

    def __init__(self, url: str, *, echo: bool = False, null_pool: bool = False) -> None:
        self.engine = make_engine(url, echo=echo, null_pool=null_pool)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, cfg: Settings | None = None, *, null_pool: bool = False) -> "Database":
        cfg = cfg or settings
        return cls(cfg.DATABASE_URL, echo=cfg.DB_ECHO, null_pool=null_pool)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        async with self.sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()


# 기존 코드 호환용 기본 인스턴스 (health 엔드포인트 등)
default_database = Database.from_settings()
engine = default_database.engine
AsyncSessionLocal = default_database.sessionmaker


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    database: Database = getattr(request.app.state, "database", default_database)
    async with database.sessionmaker() as session:
        yield session
