from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, StrictBool, StrictFloat, StrictInt, StrictStr


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# 응답 시각은 항상 UTC 'Z'
UtcDatetime = Annotated[datetime, PlainSerializer(_utc, return_type=str, when_used="json")]


class StrictInput(BaseModel):
    """요청 DTO: 알 수 없는 필드 거절, 타입 자동 변환 금지."""

    model_config = ConfigDict(extra="forbid", strict=True)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr]


def finite(value: float | int | None, name: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name}은(는) 유한한 수여야 합니다.")


class FieldError(Output):
    location: Literal["body", "query", "path", "header"]
    path: str
    message: str


class Problem(Output):
    type: Literal["about:blank"] = "about:blank"
    title: str
    status: int
    detail: str
    instance: str
    code: str
    request_id: UUID
    errors: list[FieldError]


class WorkError(Output):
    code: str
    message: str
    retryable: bool


class Notice(Output):
    code: str
    message: str
    field: str | None


def notice(code: str, message: str, field: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "field": field}


AwareTimestamp = AwareDatetime
NonEmpty = Field(min_length=1)
