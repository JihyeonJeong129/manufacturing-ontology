from fastapi import APIRouter

from app.api.v1.endpoints import alerts, answers, documents, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(alerts.router)
api_router.include_router(answers.router)
api_router.include_router(documents.router)
