"""v1 路由装配。

P3-B：这里额外承担一件"装配期策略"——给 AI 的**额度消耗型**入口挂编辑权限闸。

为什么不在 ai.py 里用 dependencies= 挂：ai.py 正由另一个 agent 修改，本任务不动该文件；
且 AI 路由里既有"消耗 LLM 额度"的 POST（chat/stream、query、canvas/chat…），
也有**只读** GET（列会话、读消息）。整体挂会把 viewer 的"看历史会话"也挡掉，
所以只挑出显式清单逐条追加依赖。
"""
from fastapi import APIRouter, Depends

from app.api.deps import require_editor
from app.api.v1.ai import router as ai_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.canvases import router as canvases_router
from app.api.v1.connections import router as connections_router
from app.api.v1.dashboards import router as dashboards_router
from app.api.v1.datasources import router as datasources_router
from app.api.v1.insights import router as insights_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.permissions import router as permissions_router
from app.api.v1.public import public_router
from app.api.v1.reports import router as reports_router
from app.api.v1.statistics import statistics_router
from app.api.v1.trash import router as trash_router

# ── AI 额度消耗型入口（viewer 只读 → 不允许跑 AI 烧额度）────────────────────
# 判据：该 POST 会真正调用 LLM（生成 SQL / 分析 / 清洗建议 / 润色 / 对话）。
# 不含：GET 会话列表、GET 消息、（创建/重命名/删除会话、取消任务）——这些不烧额度。
# 也不含 POST /ai/sessions（只建一条空会话，不调模型）。
AI_EDITOR_ONLY_PATHS = frozenset({
    "/ai/chat/stream",
    "/ai/canvas/chat",
    "/ai/query",
    "/ai/insights",
    "/ai/polish",
    "/ai/clean",
    "/ai/clean/apply",
    "/ai/sessions/{session_id}/messages",
})

# FastAPI 的 include_router 在 include 那一刻读取 route.dependencies
# （fastapi/routing.py: dependencies=[*include_context.dependencies, *route.dependencies]），
# 因此这一步必须发生在下面 include_router(ai_router) **之前**。
# "not _route.dependencies" 让本段幂等：重复 import（如测试里重复加载 app）不会重复追加。
for _route in ai_router.routes:
    if getattr(_route, "path", None) in AI_EDITOR_ONLY_PATHS and not _route.dependencies:
        _route.dependencies.append(Depends(require_editor))


api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth_router)
api_v1_router.include_router(datasources_router)
api_v1_router.include_router(connections_router)
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
api_v1_router.include_router(metrics_router)
