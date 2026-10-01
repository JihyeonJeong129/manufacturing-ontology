"""엄격한 JSON 처리.

- 중복 키, NaN/Infinity, 표현 범위를 넘는 수(1e400 등)는 파싱 오류(400)로 거절한다.
- PostgreSQL jsonb가 저장할 수 없는 문자열(NUL, 짝 없는 surrogate)은 422로 거절한다.
- 직렬화는 UTC 'Z' 시각을 사용한다.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID


class JsonParseError(ValueError):
    """JSON 문법/중복 키/비유한 수 오류 (HTTP 400)."""


class JsonContentError(ValueError):
    """JSON으로는 읽혔으나 저장할 수 없는 문자열 (HTTP 422)."""

    def __init__(self, pointer: str, message: str) -> None:
        super().__init__(message)
        self.pointer = pointer


def _reject_constant(name: str) -> Any:
    raise JsonParseError(f"JSON 숫자로 {name}은(는) 허용되지 않습니다.")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise JsonParseError("유한하지 않은 JSON 숫자는 허용되지 않습니다.")
    return value


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonParseError(f"중복된 JSON 키가 있습니다: {key!r}")
        result[key] = value
    return result


def strict_loads(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonParseError("요청 본문이 올바른 UTF-8이 아닙니다.") from exc
    if text.startswith("﻿"):
        raise JsonParseError("UTF-8 BOM은 허용되지 않습니다.")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except JsonParseError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise JsonParseError("요청 본문을 JSON으로 해석할 수 없습니다.") from exc
    check_storable(value)
    return value


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def check_storable(value: Any, pointer: str = "") -> None:
    """PostgreSQL jsonb에 저장할 수 없는 문자열을 찾는다."""
    if isinstance(value, str):
        _check_string(value, pointer)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_string(key, pointer + "/" + _escape(key))
            check_storable(item, pointer + "/" + _escape(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_storable(item, f"{pointer}/{index}")


def _check_string(text: str, pointer: str) -> None:
    if "\x00" in text:
        raise JsonContentError(pointer, "NUL 문자(\\u0000)는 저장할 수 없습니다.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JsonContentError(pointer, "짝이 맞지 않는 UTF-16 surrogate 문자는 저장할 수 없습니다.") from exc


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    text = value.isoformat()
    return text.replace("+00:00", "Z")


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value) if value != value.to_integral_value() else int(value)
    if isinstance(value, (bytes, bytearray)):
        raise TypeError("bytes are not JSON serializable")
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def dumps(value: Any) -> str:
    """DB 바인딩/저장용 결정적 JSON 문자열."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_default, allow_nan=False)


def as_json(value: Any) -> Any:
    """asyncpg가 문자열로 돌려준 json/jsonb 값을 파이썬 객체로 변환한다."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value
