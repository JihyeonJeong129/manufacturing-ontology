"""인증, 요청 본문 읽기(크기·Content-Type·엄격한 JSON), 멱등 키 처리."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

from fastapi import Depends, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, field_error, pydantic_errors_to_fields, validation_error
from app.core.jsonutil import JsonContentError, JsonParseError, strict_loads
from app.db.session import get_db
from app.db.sqlutil import fetch_one

USER_ROLES = frozenset({"operator", "domain_reviewer", "developer"})
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class Principal:
    actor_id: UUID
    role: str
    display_name: str
    credential_id: UUID
    producer_id: UUID | None
    producer_key: str | None

    @property
    def is_user(self) -> bool:
        return self.role in USER_ROLES


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _unauthorized(detail: str = "유효한 Bearer 토큰이 필요합니다.") -> ApiError:
    return ApiError(401, "UNAUTHORIZED", detail)


async def get_principal(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    header = request.headers.get("authorization")
    if not header:
        raise _unauthorized()
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()
    row = await fetch_one(
        db,
        """
        SELECT c.id AS credential_id, c.producer_id, a.id AS actor_id, a.role, a.display_name,
               p.producer_key
        FROM rsw.api_credentials c
        JOIN rsw.actors a ON a.id = c.actor_id
        LEFT JOIN rsw.event_producers p ON p.id = c.producer_id
        WHERE c.token_sha256 = :digest AND c.active AND a.active
          AND (c.expires_at IS NULL OR c.expires_at > now())
        """,
        digest=token_sha256(token),
    )
    # 인증 조회만 한 읽기 트랜잭션은 바로 종료해 이후 서비스 트랜잭션과 분리한다.
    await db.rollback()
    if row is None:
        raise _unauthorized()
    return Principal(
        actor_id=row["actor_id"], role=row["role"], display_name=row["display_name"],
        credential_id=row["credential_id"], producer_id=row["producer_id"], producer_key=row["producer_key"],
    )


async def require_user(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_user:
        raise ApiError(403, "FORBIDDEN", "이 작업은 작업자/검토자 사용자만 수행할 수 있습니다.")
    return principal


async def require_producer(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role != "service" or principal.producer_id is None:
        raise ApiError(403, "PRODUCER_NOT_ALLOWED", "경보 등록 권한이 있는 생산자 서비스 토큰이 아닙니다.")
    return principal


async def read_limited_body(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ApiError(413, "PAYLOAD_TOO_LARGE", f"요청 본문은 최대 {max_bytes} 바이트입니다.")
        except ValueError as exc:
            raise ApiError(400, "BAD_REQUEST", "Content-Length 헤더가 올바르지 않습니다.") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", f"요청 본문은 최대 {max_bytes} 바이트입니다.")
        chunks.append(chunk)
    return b"".join(chunks)


def check_json_headers(request: Request) -> None:
    encoding = (request.headers.get("content-encoding") or "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "지원하지 않는 Content-Encoding입니다.")
    content_type = request.headers.get("content-type")
    if content_type is None:
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type: application/json이 필요합니다.")
    media, *params = [part.strip() for part in content_type.split(";")]
    if media.lower() != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type: application/json이 필요합니다.")
    for param in params:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset" and value.strip().strip('"').lower() not in ("utf-8", "utf8"):
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "JSON 본문은 UTF-8이어야 합니다.")


async def read_json_request(request: Request, model: type[M], max_bytes: int) -> tuple[bytes, dict[str, Any], M]:
    """본문을 읽고 (원본 바이트, 제출된 JSON 객체, 검증된 DTO)를 돌려준다."""
    check_json_headers(request)
    raw = await read_limited_body(request, max_bytes)
    try:
        submitted = strict_loads(raw)
    except JsonParseError as exc:
        raise ApiError(400, "INVALID_JSON", str(exc)) from exc
    except JsonContentError as exc:
        raise validation_error([field_error("body", exc.pointer, str(exc))]) from exc
    if not isinstance(submitted, dict):
        raise validation_error([field_error("body", "", "요청 본문은 JSON 객체여야 합니다.")])
    try:
        parsed = model.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        raise validation_error(pydantic_errors_to_fields(exc.errors(include_url=False))) from exc
    return raw, submitted, parsed


def idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key")
    if key is None:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key 헤더가 필요합니다.")
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise ApiError(
            422, "VALIDATION_ERROR", "Idempotency-Key 형식이 올바르지 않습니다.",
            errors=[field_error("header", "Idempotency-Key", "영문·숫자·.·_·:·- 1~128자")],
        )
    return key


def get_cfg(request: Request):
    """앱에 주입된 Settings (테스트에서 교체 가능)."""
    from app.core.config import settings

    return getattr(request.app.state, "settings", settings)
