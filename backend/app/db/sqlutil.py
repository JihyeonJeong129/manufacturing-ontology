"""text() SQL 실행 보조. rsw 스키마는 SQL 파일이 기준이므로 ORM 대신 명시적 SQL을 사용한다.

JSON 값은 dumps()로 문자열화해 CAST(:x AS jsonb)로 바인딩하고, 조회 결과의 json/jsonb는
asyncpg가 문자열로 돌려주므로 as_json()으로 복원한다.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


async def fetch_all(session: AsyncSession, sql: str, **params: Any) -> list[dict[str, Any]]:
    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings().all()]


async def fetch_one(session: AsyncSession, sql: str, **params: Any) -> dict[str, Any] | None:
    result = await session.execute(text(sql), params)
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_value(session: AsyncSession, sql: str, **params: Any) -> Any:
    result = await session.execute(text(sql), params)
    return result.scalar()


async def execute(session: AsyncSession, sql: str, **params: Any) -> int:
    result = await session.execute(text(sql), params)
    return result.rowcount if result.rowcount is not None else 0


def sqlstate(exc: BaseException) -> str | None:
    """SQLAlchemy/asyncpg 예외에서 SQLSTATE 코드를 꺼낸다."""
    current: BaseException | None = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if isinstance(code, str):
            return code
        orig = getattr(current, "orig", None)
        if orig is not None and orig is not current:
            current = orig
            continue
        current = current.__cause__ or current.__context__
    return None


def constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        orig = getattr(current, "orig", None)
        if orig is not None and orig is not current:
            current = orig
            continue
        current = current.__cause__ or current.__context__
    return None


UNIQUE_VIOLATION = "23505"
LOCK_NOT_AVAILABLE = "55P03"
SERIALIZATION_FAILURE = "40001"
DEADLOCK_DETECTED = "40P01"
RAISE_EXCEPTION = "P0001"


def is_db_error(exc: BaseException) -> bool:
    return isinstance(exc, DBAPIError)
