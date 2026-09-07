import logging
import structlog
import traceback

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.api.v1.router import api_v1_router
from app.config import settings
from app.core.limiter import limiter
from app.core.middleware import OperationLogMiddleware, RequestTimingMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lvco")

app = FastAPI(title="Lvco BI", version="1.0.0")

app.state.limiter = limiter


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    retry_after = exc.retry_after if hasattr(exc, "retry_after") else 60
    return JSONResponse(
        status_code=429,
        content={"code": "RATE_LIMITED", "message": "操作过于频繁，请稍后再试"},
        headers={"Retry-After": str(retry_after)},
    )


app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(RequestTimingMiddleware)
app.add_middleware(OperationLogMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    log.error(traceback.format_exc())
    origin = request.headers.get("origin", "")
    headers = {}
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": {"code": "INTERNAL_ERROR", "message": str(exc)}},
        headers=headers,
    )


app.include_router(api_v1_router)


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
async def startup():
    from app.api.deps import get_cache_repository
    from app.core.database import async_session_factory
    from app.services.insight_engine.scheduler import InsightScheduler
    logger = structlog.get_logger("main")
    cache_repo = get_cache_repository()
    status = "redis" if getattr(cache_repo, "_use_redis", False) else "fallback_to_dict"
    logger.info("redis ping", status=status)
    # 幂等写入内置指标模板（指标语义层）；失败不影响应用启动
    try:
        from app.services.metric_service import ensure_default_metrics
        async with async_session_factory() as db:
            await ensure_default_metrics(db)
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("seed_default_metrics_failed")

    if settings.INSIGHT_ENABLED:
        try:
            insight_scheduler = InsightScheduler(
                interval_minutes=settings.INSIGHT_INTERVAL_MINUTES
            )
            insight_scheduler.start()
            app.state.insight_scheduler = insight_scheduler
            logger.info(
                "insight_scheduler_started",
                interval_minutes=settings.INSIGHT_INTERVAL_MINUTES,
            )
        except Exception:  # noqa: BLE001
            logger.warning("insight_scheduler_start_failed")

    # 启动 DashboardScheduler
    from app.services.dashboard_scheduler import DashboardScheduler
    dashboard_scheduler = DashboardScheduler()
    dashboard_scheduler.start()
    app.state.dashboard_scheduler = dashboard_scheduler
    logger.info("dashboard_scheduler_started")

    # 加载已开启刷新的仪表盘
    from app.core.database import async_session_factory
    from app.models.dashboard import Dashboard
    from sqlalchemy import select
    async with async_session_factory() as session:
        result = await session.execute(
            select(Dashboard).where(Dashboard.refresh_enabled == True, Dashboard.deleted_at.is_(None))
        )
        dashboards = result.scalars().all()
        for d in dashboards:
            if d.refresh_cron:
                try:
                    dashboard_scheduler.add_job(d.id, d.refresh_cron)
                except Exception as e:
                    logger.warning(f"dashboard_schedule_load_failed: {d.id} {e}")


@app.on_event("shutdown")
async def shutdown():
    logger = structlog.get_logger("main")
    insight_scheduler = getattr(app.state, "insight_scheduler", None)
    if insight_scheduler is not None:
        try:
            insight_scheduler.shutdown()
        except Exception:  # noqa: BLE001
            logger.warning("insight_scheduler_stop_failed")
    dashboard_scheduler = getattr(app.state, "dashboard_scheduler", None)
    if dashboard_scheduler:
        dashboard_scheduler.stop()
    logger.info("app_shutdown_complete")
