"""RFC 9457 Problem Details 오류 응답.

모든 오류는 application/problem+json 으로 응답하고, 내부 stack trace·인증 정보·
원문 저장 경로는 노출하지 않는다.
"""
from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ApiError(Exception):
    """애플리케이션 오류. HTTP 상태와 안정된 오류 code를 가진다."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        errors: list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.errors = errors or []
        self.headers = headers or {}


def field_error(location: str, path: str, message: str) -> dict[str, str]:
    return {"location": location, "path": path, "message": message}


def validation_error(errors: list[dict[str, str]], detail: str = "요청 값이 계약을 만족하지 않습니다.") -> ApiError:
    return ApiError(422, "VALIDATION_ERROR", detail, errors=errors)


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", None) or "00000000-0000-0000-0000-000000000000"


def problem_response(
    request: Request,
    status: int,
    code: str,
    detail: str,
    *,
    errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Error"
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "request_id": request_id_of(request),
        "errors": errors or [],
    }
    response_headers = {"Cache-Control": "no-store", **(headers or {})}
    if status == 401:
        response_headers.setdefault("WWW-Authenticate", "Bearer")
    return JSONResponse(body, status_code=status, headers=response_headers, media_type=PROBLEM_MEDIA_TYPE)


_UNION_TAGS = {"str", "int", "float", "bool", "none", "dict", "list", "json-or-python[json=str,python=str]"}


def pydantic_loc_to_pointer(loc: tuple[Any, ...] | list[Any]) -> str:
    """pydantic 오류 위치를 JSON Pointer로 변환한다. union/validator 태그는 제거한다."""
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(str(item))
            continue
        text = str(item)
        if text in _UNION_TAGS or "[" in text or text.startswith("function-") or text == "tagged-union":
            continue
        parts.append(text.replace("~", "~0").replace("/", "~1"))
    return "/" + "/".join(parts) if parts else ""


def pydantic_errors_to_fields(errors: list[dict[str, Any]], location: str = "body") -> list[dict[str, str]]:
    result = []
    seen = set()
    for err in errors:
        loc = list(err.get("loc", ()))
        loc_location = location
        if loc and loc[0] in {"body", "query", "path", "header"}:
            loc_location = loc[0]
            loc = loc[1:]
        pointer = pydantic_loc_to_pointer(loc)
        message = str(err.get("msg", "invalid value"))
        key = (loc_location, pointer, message)
        if key in seen:
            continue
        seen.add(key)
        result.append(field_error(loc_location, pointer, message))
    return result


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem_response(request, exc.status, exc.code, exc.detail, errors=exc.errors, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            request, 422, "VALIDATION_ERROR", "요청 경로·쿼리·헤더 형식이 올바르지 않습니다.",
            errors=pydantic_errors_to_fields(list(exc.errors())),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        detail = exc.detail if isinstance(exc.detail, str) else "요청을 처리할 수 없습니다."
        if exc.status_code == 404:
            detail = "요청한 리소스를 찾을 수 없습니다."
        return problem_response(request, exc.status_code, code, detail, headers=dict(exc.headers or {}))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error request_id=%s", request_id_of(request))
        return problem_response(request, 500, "INTERNAL_ERROR", "요청 처리 중 예기치 않은 오류가 발생했습니다.")
