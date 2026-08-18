"""根据 HTTP method + path 推断操作审计标签。

约定：
- 业务路径为 /api/v1/{resource}/{id?}/{sub_action?}
- 资源类型集合：auth, canvas, datasource, dashboard, report, ai, user, system
- action 格式：{resource}.{verb}   例如 canvas.create / canvas.delete / ai.query

注意：
- 只覆盖核心写操作与高频读操作；纯 GET 列表等会归到 {resource}.list 以减少噪音
- 未匹配到业务前缀的请求会得到 action='other'，仍然会被记录但不出现在默认筛选里
"""

from __future__ import annotations

# 业务前缀 → 资源类型
_RESOURCE_PREFIX = {
    "auth": "auth",
    "users": "user",
    "datasources": "datasource",
    "canvases": "canvas",
    "dashboards": "dashboard",
    "dashboard-charts": "dashboard_chart",
    "reports": "report",
    "ai": "ai",
    "statistics": "statistics",
    "trash": "trash",
    "insights": "insight",
    "notifications": "notification",
    "permissions": "permission",
    "audit": "audit",
    "share": "share",
}


def _strip_api_prefix(path: str) -> str:
    """去掉 /api/v1 前缀。"""
    if path.startswith("/api/"):
        parts = path.split("/", 3)
        # /api/v1/foo/bar → ['api','v1','foo','bar']
        return parts[3] if len(parts) > 3 else ""
    return path.lstrip("/")


def parse_action(method: str, path: str) -> tuple[str, str]:
    """根据 method + path 推断 (resource_type, action)。

    action 形如 'auth.login' / 'canvas.create' / 'ai.query' / 'other'
    """
    method = (method or "GET").upper()
    rel = _strip_api_prefix(path)
    if not rel:
        return ("other", "other")

    head, *rest = rel.split("/", 1)
    resource = _RESOURCE_PREFIX.get(head, head or "other")
    sub = rest[0] if rest else ""

    # auth 子路径单独处理（/auth/login 等）
    if resource == "auth":
        verb = sub.split("?", 1)[0] if sub else "unknown"
        return ("auth", f"auth.{verb}" if verb else "auth.unknown")

    # 标准 CRUD 动作
    if method == "GET":
        if not sub:
            return (resource, f"{resource}.list")
        # /canvases/{id}?foo=bar → canvas.read
        if "?" in sub:
            return (resource, f"{resource}.read")
        # /canvases/{id}/restore → canvas.restore
        if "/" in sub:
            sub_action = sub.split("/", 1)[1].split("?", 1)[0]
            return (resource, f"{resource}.{sub_action or 'read'}")
        return (resource, f"{resource}.read")
    elif method == "POST":
        return (resource, f"{resource}.create")
    elif method in ("PUT", "PATCH"):
        return (resource, f"{resource}.update")
    elif method == "DELETE":
        return (resource, f"{resource}.delete")
    return (resource, f"{resource}.{method.lower()}")


def should_skip(path: str) -> bool:
    """是否跳过日志记录（健康检查、OPTIONS 预检、Swagger 等）。"""
    if not path:
        return True
    if path == "/health":
        return True
    if path.startswith("/docs") or path.startswith("/openapi") or path.startswith("/redoc"):
        return True
    if path == "/favicon.ico":
        return True
    return False
