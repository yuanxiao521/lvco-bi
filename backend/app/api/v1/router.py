from fastapi import APIRouter

from app.api.v1.ai import router as ai_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.canvases import router as canvases_router
from app.api.v1.dashboards import router as dashboards_router
from app.api.v1.datasources import router as datasources_router
from app.api.v1.insights import router as insights_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.permissions import router as permissions_router
from app.api.v1.public import public_router
from app.api.v1.reports import router as reports_router
from app.api.v1.statistics import statistics_router
from app.api.v1.trash import router as trash_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth_router)
api_v1_router.include_router(datasources_router)
api_v1_router.include_router(canvases_router)
api_v1_router.include_router(dashboards_router)
api_v1_router.include_router(reports_router)
api_v1_router.include_router(ai_router)
api_v1_router.include_router(public_router)
api_v1_router.include_router(statistics_router)
api_v1_router.include_router(trash_router)
api_v1_router.include_router(notifications_router)
api_v1_router.include_router(permissions_router)
api_v1_router.include_router(audit_router)
api_v1_router.include_router(insights_router)