"""传输层强制加密（A 方案）：REQUIRE_HTTPS=true 时拒绝明文 API 请求。

为什么后端还要自己做一层
------------------------
正常部署里 TLS 由 nginx 终止（443），HTTP 请求在 nginx 就 301 到 HTTPS 了。
但如果：反向代理配错、有人绕过 nginx 直连 8000、或内网有人手动 curl——
数据库密码就会以明文过网。这一层是纵深防御：**只要没走 HTTPS，一律拒绝**，
错误信息里明确说明"请用 https 访问"，避免用户以为是自己密码错了。

信任边界
--------
只信任反向代理写入的 X-Forwarded-Proto（生产由 nginx 设置）；直连时没有该头，
用 request.url.scheme 判断。因此本中间件不要求客户端自己伪造头——它伪造了也只会
让自己能访问，不构成越权（真正的越权风险在认证，不在协议）。
"""
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

# 健康检查豁免：容器编排/LB 的内网探活通常走 http，且不携带任何业务数据
_EXEMPT_PATHS = {"/health"}


class HttpsEnforcementMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not getattr(settings, "REQUIRE_HTTPS", False):
            return await call_next(request)
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        forwarded = request.headers.get("x-forwarded-proto", "")
        proto = (forwarded.split(",")[0].strip().lower() if forwarded else request.url.scheme)
        if proto != "https":
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "detail": {
                        "code": "HTTPS_REQUIRED",
                        "message": "本服务已强制 HTTPS，请使用 https:// 访问（数据库密码不接受明文传输）",
                    }
                },
            )
        return await call_next(request)
