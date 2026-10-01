"""FastAPI /docs 표시용 OpenAPI 보조. 요청 본문은 엄격한 JSON 파서로 직접 읽으므로 스키마를 수동 연결한다."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.schemas.common import Problem

ERROR_TEXT = {
    400: "잘못된 JSON/중복 JSON 키/필수 헤더 누락", 401: "인증 필요", 403: "권한 부족", 404: "리소스 없음",
    409: "처리 상태·멱등 키·정정 관계 충돌", 413: "요청 크기 초과", 415: "지원하지 않는 Content-Type/Encoding",
    416: "RANGE_NOT_SATISFIABLE", 422: "필드 검증 실패", 503: "일시적으로 사용 불가",
}


def request_body_doc(model: type[BaseModel], *, idempotent: bool = False) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema(ref_template="#/components/schemas/{model}")}},
        }
    }
    if idempotent:
        extra["parameters"] = [{
            "name": "Idempotency-Key", "in": "header", "required": True,
            "schema": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": "^[A-Za-z0-9._:-]+$"},
        }]
    return extra


def responses_doc(extra: dict[int, tuple[type[BaseModel], str]], *codes: int) -> dict[int | str, Any]:
    result: dict[int | str, Any] = {code: {"model": model, "description": text} for code, (model, text) in extra.items()}
    for code in codes:
        result[code] = {"model": Problem, "description": ERROR_TEXT.get(code, "오류"),
                        "content": {"application/problem+json": {}}}
    return result
