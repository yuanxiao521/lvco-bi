import time

import structlog
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.database import async_session_factory as SessionLocal
from app.core.operation_log import parse_action, should_skip
from app.core.security import decode_token
from app.models.operation_log import OperationLog

logger = structlog.get_logger("api")


class RequestTimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response


def _extract_user_id(request: Request) -> str | None:
    """从 Authorization 头里轻量解析 user_id（不查库）。"""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    return payload.get("sub")


def _extract_resource_id(path: str) -> str | None:
    """从路径里提取 {resource}/{uuid} 中的 uuid。"""
    # /api/v1/canvases/{uuid}/... → 取第一段 uuid
    parts = path.split("/")
    for p in parts:
        # 简单判定：含 - 且长度 >= 32 视为 uuid
        if len(p) >= 32 and "-" in p and all(c.isalnum() or c == "-" for c in p):
            return p
    return None


class OperationLogMiddleware(BaseHTTPMiddleware):
    """操作审计中间件：响应返回后写入 operation_logs。

    设计：
    - 仅记录 /api/v1/ 下的请求
    - 跳过健康检查、Swagger、静态资源
    - 通过 BackgroundTask 异步写入，不阻塞响应
    - 写入失败仅记 warning，不影响业务
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if should_skip(path) or not path.startswith("/api/"):
            return await call_next(request)

        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)

        try:
            user_id = _extract_user_id(request)
            resource_type, action = parse_action(request.method, path)
            resource_id = _extract_resource_id(path)
            ip = request.client.host if request.client else None
            ua = request.headers.get("user-agent")

            log_entry = {
                "user_id": user_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "method": request.method,
                "path": path[:500],
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "ip_address": ip,
                "user_agent": ua[:1000] if ua else None,
            }

            # 用 BackgroundTask 在响应关闭前执行写入
            response.background = BackgroundTask(_write_log, log_entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("operation_log_prepare_failed", error=str(exc), path=path)

        return response


async def _write_log(payload: dict) -> None:
    """独立数据库会话写入审计日志。"""
    try:
        async with SessionLocal() as session:
            session.add(OperationLog(**payload))
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("operation_log_write_failed", error=str(exc))


structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
