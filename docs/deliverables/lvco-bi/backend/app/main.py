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
    from app.services.cache_service import cache
    logger = structlog.get_logger("main")
    logger.info("redis ping", status="ok" if cache._redis else "fallback_to_dict")

    # 启动智能洞察调度器（失败不阻断应用启动）
    try:
        from app.services.insight_engine.scheduler import insight_scheduler
        await insight_scheduler.start()
    except Exception as e:
        logger.error("insight_scheduler_start_failed", error=str(e))


@app.on_event("shutdown")
async def shutdown():
    logger = structlog.get_logger("main")
    # 优雅关闭智能洞察调度器
    try:
        from app.services.insight_engine.scheduler import insight_scheduler
        await insight_scheduler.shutdown(wait=False)
    except Exception as e:
        logger.warning("insight_scheduler_shutdown_failed", error=str(e))
    logger.info("app_shutdown_complete")
