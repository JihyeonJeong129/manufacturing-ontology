"""요청 추적 UUID(X-Request-ID)와 JSON 캐시 헤더를 모든 응답에 붙이는 ASGI 미들웨어."""
from __future__ import annotations

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode()))
                names = {k.lower() for k, _ in headers}
                content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if b"cache-control" not in names and b"json" in content_type:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
