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
    logger = structlog.get_logger("main")
    cache_repo = get_cache_repository()
    status = "redis" if getattr(cache_repo, "_use_redis", False) else "fallback_to_dict"
    logger.info("redis ping", status=status)


@app.on_event("shutdown")
async def shutdown():
    logger = structlog.get_logger("main")
    logger.info("app_shutdown_complete")
