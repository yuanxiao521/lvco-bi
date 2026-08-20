"""工具白名单标记化（P2-10 Task 10）单测。

覆盖 BaseTool.orchestrator_safe + ToolRegistry.orchestrator_safe_tools +
planner_agent.get_orchestrator_tools / _get_cached_orchestrator_tools 协作：

- 默认 orchestrator_safe=False 的工具不出现在白名单中；
- 注册时标记 orchestrator_safe=True 的新工具，Planner 自动可见；
- 未标记 orchestrator_safe=True 的新工具，不进入白名单；
- get_orchestrator_tools 返回类型为 set[str]；
- _ORCHESTRATOR_TOOLS 旧名已被替换为 _ORCHESTRATOR_TOOLS_FALLBACK，
  且新增 _ORCHESTRATOR_TOOLS_CACHE 进程级缓存；
- _get_cached_orchestrator_tools 进程内复用缓存，缓存清空后能拉取最新白名单。
"""
from __future__ import annotations

from app.services.agent_tools import BaseTool, ToolRegistry
from app.services.agents import planner_agent as pa
from app.services.agents.planner_agent import (
    _get_cached_orchestrator_tools,
    get_orchestrator_tools,
)


# 默认白名单（启动时由 agent_tools.py 底部模块级 register 注册产生的 10 个工具）。
EXPECTED_SAFE_TOOLS = (
    "list_datasources", "query_datasource", "render_chart",
    "validate_chart", "query_engine", "data_quality",
    "insight", "clean_suggest", "recommend_charts", "polish_text",
)


def _reset_orchestrator_cache() -> None:
    """清空进程级缓存，确保下次调用重新从 ToolRegistry 派生。"""
    pa._ORCHESTRATOR_TOOLS_CACHE = None


# ======================================================================
# 默认白名单：orchestrator_safe=True 工具应全部出现在白名单中
# ======================================================================


def test_default_tool_not_in_orchestrator_whitelist():
    """默认白名单包含全部已注册的 orchestrator_safe=True 工具。"""
    tools = get_orchestrator_tools()
    assert isinstance(tools, set)
    assert len(tools) >= 10
    for expected in EXPECTED_SAFE_TOOLS:
        assert expected in tools, f"缺少 {expected}"


# ======================================================================
# 动态注册：标记 orchestrator_safe=True 的工具自动进入白名单
# ======================================================================


def test_register_new_orchestrator_safe_tool_visible_to_planner():
    """注册时标记 orchestrator_safe=True 的新工具，Planner 自动可见。"""

    class _CustomTool(BaseTool):
        name = "custom_test_tool"
        description = "测试工具"
        orchestrator_safe = True  # 关键：标记为编排器安全

        def schema(self) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }

        async def execute(self, **kwargs) -> str:
            return "{}"

    try:
        ToolRegistry.register(_CustomTool())
        _reset_orchestrator_cache()
        tools = get_orchestrator_tools()
        assert "custom_test_tool" in tools
    finally:
        ToolRegistry._tools.pop("custom_test_tool", None)
        _reset_orchestrator_cache()


# ======================================================================
# 动态注册：未标记 orchestrator_safe=True 的工具不进入白名单
# ======================================================================


def test_register_tool_without_orchestrator_safe_excluded():
    """未标记 orchestrator_safe=True 的新工具，不进入白名单。"""

    class _UnsafeTool(BaseTool):
        name = "unsafe_test_tool"
        description = "未标记安全的工具"
        # 不设 orchestrator_safe，继承 BaseTool 默认 False

        def schema(self) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }

        async def execute(self, **kwargs) -> str:
            return "{}"

    try:
        ToolRegistry.register(_UnsafeTool())
        _reset_orchestrator_cache()
        tools = get_orchestrator_tools()
        assert "unsafe_test_tool" not in tools
    finally:
        ToolRegistry._tools.pop("unsafe_test_tool", None)
        _reset_orchestrator_cache()


# ======================================================================
# 返回类型契约
# ======================================================================


def test_get_orchestrator_tools_returns_set():
    """get_orchestrator_tools 返回类型为 set[str]，元素全部为字符串。"""
    tools = get_orchestrator_tools()
    assert isinstance(tools, set)
    assert all(isinstance(t, str) for t in tools)
    # 非空
    assert len(tools) > 0


# ======================================================================
# 兼容：兜底常量保留，旧名清理
# ======================================================================


def test_existing_hardcoded_fallback_is_removed_or_aliased():
    """_ORCHESTRATOR_TOOLS_FALLBACK 常量保留；旧名 _ORCHESTRATOR_TOOLS 不再独立存在。"""
    assert hasattr(pa, "_ORCHESTRATOR_TOOLS_FALLBACK")
    # 旧名不应作为模块顶层常量（仅可能以 _FALLBACK / _CACHE 前缀出现）
    legacy = getattr(pa, "_ORCHESTRATOR_TOOLS", None)
    assert legacy is None or (
        isinstance(legacy, str) and "_ORCHESTRATOR_TOOLS_FALLBACK" in legacy
    )


# ======================================================================
# 缓存：进程内复用 + 失效后重新派生
# ======================================================================


def test_cached_orchestrator_tools_returns_frozenset():
    """_get_cached_orchestrator_tools 返回 frozenset，便于 hash/比较。"""
    _reset_orchestrator_cache()
    cached = _get_cached_orchestrator_tools()
    assert isinstance(cached, frozenset)
    # 包含全部期望工具
    for expected in EXPECTED_SAFE_TOOLS:
        assert expected in cached


def test_cached_orchestrator_tools_hits_cache(monkeypatch):
    """缓存命中：第二次调用不应再次调用 get_orchestrator_tools() 派生函数。"""
    _reset_orchestrator_cache()
    call_counter = {"n": 0}
    original = pa.get_orchestrator_tools

    def counting():
        call_counter["n"] += 1
        return original()

    monkeypatch.setattr(pa, "get_orchestrator_tools", counting)

    first = _get_cached_orchestrator_tools()
    second = _get_cached_orchestrator_tools()
    third = _get_cached_orchestrator_tools()

    assert first == second == third
    # 派生函数只在缓存为 None 时执行一次
    assert call_counter["n"] == 1


def test_cache_invalidation_picks_up_new_tool():
    """缓存清空后，新注册的工具能被白名单感知到。"""

    class _LateTool(BaseTool):
        name = "late_registered_tool"
        description = "缓存失效后才注册的工具"
        orchestrator_safe = True

        def schema(self) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }

        async def execute(self, **kwargs) -> str:
            return "{}"

    try:
        # 缓存首次建立
        _reset_orchestrator_cache()
        cached_before = _get_cached_orchestrator_tools()
        assert "late_registered_tool" not in cached_before

        # 注册新工具 + 清缓存
        ToolRegistry.register(_LateTool())
        _reset_orchestrator_cache()
        cached_after = _get_cached_orchestrator_tools()
        assert "late_registered_tool" in cached_after
    finally:
        ToolRegistry._tools.pop("late_registered_tool", None)
        _reset_orchestrator_cache()


# ======================================================================
# planner_agent.py 中的硬编码常量与缓存变量必须导出
# ======================================================================


def test_planner_agent_exports_required_symbols():
    """planner_agent 模块对外暴露必要的常量与缓存变量。"""
    assert hasattr(pa, "_ORCHESTRATOR_TOOLS_FALLBACK")
    assert hasattr(pa, "_ORCHESTRATOR_TOOLS_CACHE")
    assert callable(_get_cached_orchestrator_tools)
    assert callable(get_orchestrator_tools)