from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import Settings, settings
from app.core.errors import install_exception_handlers
from app.core.request_context import RequestContextMiddleware
from app.db.session import Database, default_database


def create_app(cfg: Settings | None = None, database: Database | None = None) -> FastAPI:
    cfg = cfg or settings
    database = database or default_database

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await database.dispose()

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("app").setLevel(logging.DEBUG if cfg.DEBUG else logging.INFO)
    # debug=True면 Starlette가 500 응답에 stack trace를 노출하므로 항상 False로 둔다.
    app = FastAPI(title=cfg.APP_NAME, debug=False, lifespan=lifespan,
                  description="RSW 정비 안내 MVP 연동 API (계약 1.0.0-draft)")
    app.state.database = database
    app.state.settings = cfg
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/")
    async def root() -> dict:
        return {"app": cfg.APP_NAME, "env": cfg.APP_ENV}

    return app


app = create_app()
