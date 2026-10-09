"""轻量图编排引擎（LangGraph 模式，零依赖）。

核心概念（对齐 LangGraph）：
- Node: 执行单元，函数签名 `async def node(state: dict, **shared) -> dict`，返回部分状态更新
- Edge: 顺序边，节点完成后无条件流转到下一节点
- Conditional Edge: 条件路由，router(state, **shared) -> route_name，按映射表跳转
- State: 图执行期间的共享状态字典（节点间传递数据）
- Entry / Finish: 入口节点与终结点集合
- steps_log: 每步记录（节点/状态/路由/耗时），Checkpointer 的轻量替代，用于观测审计

用法：
    g = Graph("my_flow")
    g.add_node("a", node_a).add_node("b", node_b)
    g.add_edge("a", "b")
    g.add_conditional_edges("a", router, {"x": "b", "y": "c"})
    g.set_entry_point("a").set_finish_point("b", "c")
    state = await g.invoke({"k": "v"}, emit=emit_fn)
"""
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# 节点函数：接收共享 state 与注入参数，返回部分状态更新（dict）
NodeFunc = Callable[..., Awaitable[dict]]
# 条件路由函数：接收 state 与注入参数，返回路由名（str）
RouterFunc = Callable[..., Awaitable[str]]

_MAX_STEPS = 100  # 防死循环上限


class Graph:
    """有向图编排引擎：按边/条件路由从入口流转到终结点，共享 State。"""

    def __init__(self, name: str = "graph"):
        self.name = name
        self._nodes: dict[str, NodeFunc] = {}
        self._edges: dict[str, str] = {}  # src -> dst（顺序边）
        self._conditional: dict[str, tuple[RouterFunc, dict[str, str]]] = {}
        self._entry: str | None = None
        self._finish: set[str] = set()

    def add_node(self, name: str, func: NodeFunc) -> "Graph":
        """注册节点。func 接收 (state, **shared)，返回部分状态更新。"""
        self._nodes[name] = func
        return self

    def add_edge(self, src: str, dst: str) -> "Graph":
        """顺序边：src 完成后无条件流转到 dst。"""
        self._edges[src] = dst
        return self

    def add_conditional_edges(self, src: str, router: RouterFunc, mapping: dict[str, str]) -> "Graph":
        """条件路由：src 完成后调用 router 决定去向，按 mapping 映射节点名。"""
        self._conditional[src] = (router, mapping)
        return self

    def set_entry_point(self, name: str) -> "Graph":
        """设置入口节点。"""
        self._entry = name
        return self

    def set_finish_point(self, *names: str) -> "Graph":
        """设置终结点集合。"""
        self._finish.update(names)
        return self

    async def invoke(self, initial_state: dict | None = None, **shared) -> dict:
        """执行图：从入口开始按边/条件路由流转，直到终结点或异常。

        参数：
            initial_state: 初始共享状态。
            **shared: 注入给所有节点与路由函数的额外参数（如 emit 回调、db_session）。

        返回：
            最终 state（含 __steps__ 步骤日志；异常时含 __error__）。
        """
        state: dict[str, Any] = dict(initial_state or {})
        steps_log: list[dict] = []
        current = self._entry
        guard = 0

        while current is not None and guard < _MAX_STEPS:
            guard += 1
            node = self._nodes.get(current)
            if node is None:
                if current in self._finish:
                    # 纯终结点（无函数，如 END）：正常结束，不报错
                    logger.debug(f"[graph:{self.name}] 到达终结点 {current}")
                    break
                logger.error(f"[graph:{self.name}] 节点不存在: {current}")
                state["__error__"] = f"节点不存在: {current}"
                steps_log.append({"node": current, "status": "error", "error": state["__error__"]})
                break

            started = time.time()
            try:
                update = await node(state, **shared)
            except Exception as e:
                logger.exception(f"[graph:{self.name}] 节点 {current} 异常: {e}")
                state["__error__"] = str(e)
                steps_log.append({
                    "node": current,
                    "status": "error",
                    "error": str(e),
                    "elapsed_ms": int((time.time() - started) * 1000),
                })
                break

            if isinstance(update, dict):
                state.update(update)

            # 终结点：执行完本节点后停止（LangGraph 语义：END 节点执行完即结束）
            if current in self._finish:
                logger.debug(f"[graph:{self.name}] 到达终结点 {current}")
                steps_log.append({
                    "node": current,
                    "status": "ok",
                    "route": None,
                    "elapsed_ms": int((time.time() - started) * 1000),
                })
                break

            # 路由：条件边优先，否则顺序边
            route_name: str | None = None
            if current in self._conditional:
                router, mapping = self._conditional[current]
                try:
                    route = await router(state, **shared)
                except Exception as e:
                    logger.exception(f"[graph:{self.name}] 路由异常: {e}")
                    state["__error__"] = f"路由异常: {e}"
                    break
                route_name = mapping.get(route)
                if route_name is None:
                    logger.error(f"[graph:{self.name}] 路由结果未映射: {route}")
                    state["__error__"] = f"路由结果未映射: {route}"
                    break
            else:
                route_name = self._edges.get(current)

            steps_log.append({
                "node": current,
                "status": "ok",
                "route": route_name,
                "elapsed_ms": int((time.time() - started) * 1000),
            })
            current = route_name

        if guard >= _MAX_STEPS:
            logger.warning(f"[graph:{self.name}] 达到最大步骤上限 {_MAX_STEPS}，强制终止")
            state["__error__"] = state.get("__error__") or f"超过最大步骤上限 {_MAX_STEPS}"

        state["__steps__"] = steps_log
        return state
