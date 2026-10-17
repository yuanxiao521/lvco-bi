"""TurnContextProvider：上下文装配的唯一门面。

职责边界（四件套）：
- 持有本轮全部 Source（请求级，warmup 一次、全轮复用）；
- 暴露四个出口：for_agent_loop（单 Agent 循环，主路径）/ for_decision /
  for_answer / for_worker；
- refresh(kind) 失效重读（等价于旧 CanvasStateProvider 的 force=True）；
- **不做**压缩、不做格式化——那是 Compressor / Assembler 的事。

分层约束：本包不 import agents/*（反向依赖）。系统提示等调用方私有内容
一律作为参数传入（system_text），而不是在这里引用常量。

P0（行为零变化）：
- 数据仍由调用方带进来（turns 来自 API 层旧契约、layout 来自 CanvasStateProvider），
  窗口所有权收归服务端是阶段 3 的事；
- 台账（Ledger）随 Assembled 返回，调用方决定是否落 trace。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import settings
from app.services.context.assembler import Assembler
from app.services.context.sources.memory import MemorySource
from app.services.context.sources.state import (
    AssessmentSource,
    ArtifactSource,
    CanvasSource,
    DatasourceSource,
    MetricsSource,
    SubtaskSource,
)
from app.services.context.sources.window import WindowSource
from app.services.context.types import Assembled, Block, BlockKind, Ledger, Priority


@dataclass
class TurnInputs:
    """一轮对话的原始输入（P0 由调用方收集；阶段 3 起逐步收归 Source 内部）。"""

    user_id: str = ""
    session_id: str = ""
    entry: str = "chat"
    turns: list[dict] = field(default_factory=list)
    history_summary: str = ""
    subtask_summaries: list[str] | None = None
    datasources: list[dict] | None = None
    metrics_ctx: str = ""
    canvas_layout: str = ""
    artifact_note: str = ""


class TurnContextProvider:
    """一轮对话的上下文门面。用法：

        provider = TurnContextProvider.for_turn(TurnInputs(...))
        assembled = provider.for_decision(user_msg)
        history_summary = assembled.parts["history_summary"]   # 与旧 digest() 逐字节等价
        ledger = assembled.ledger                              # 各块 token 台账
    """

    def __init__(self, inputs: TurnInputs, *, assembler: Assembler | None = None) -> None:
        self._inputs = inputs
        self._assembler = assembler or Assembler()
        self._window = WindowSource(
            list(inputs.turns),
            int(getattr(settings, "LEAD_MAX_TURNS_IN_CTX", 30) or 30),
        )
        self._memory = MemorySource(inputs.history_summary)
        # P4：循环内的工具结果（L0 热层）。每步追加一条（本轮 assistant 的 tool_calls
        # + 各 role=tool 结果），顺序与 tool_call_id 必须原样保留。
        self._tool_results: list[list[dict]] = []
        self._sources: dict[BlockKind, object] = {
            BlockKind.DATASOURCES: DatasourceSource(inputs.datasources),
            BlockKind.CANVAS: CanvasSource(inputs.canvas_layout),
            BlockKind.METRICS: MetricsSource(inputs.metrics_ctx),
            BlockKind.SUBTASK: SubtaskSource(inputs.subtask_summaries),
            BlockKind.ASSESSMENT: AssessmentSource(inputs.subtask_summaries),
            BlockKind.ARTIFACT: ArtifactSource(inputs.artifact_note),
        }

    @classmethod
    def for_turn(cls, inputs: TurnInputs, **kw) -> "TurnContextProvider":
        return cls(inputs, **kw)

    def warmup(self) -> None:
        """P0：所有 Source 都是内存装配，无需预热。阶段 3（DB 取数）在此并发加载。"""
        return None

    def add_tool_result(self, messages: list[dict]) -> None:
        """追加一步的工具结果（assistant 的 tool_calls + role=tool 结果）。"""
        if messages:
            self._tool_results.append([dict(m) for m in messages])

    def refresh_canvas(self, layout_text: str) -> None:
        """落块后刷新画布快照（等价于旧 CanvasStateProvider 的 force=True 语义）。"""
        self._sources[BlockKind.CANVAS] = CanvasSource(layout_text)

    # ── 出口 ────────────────────────────────────────────────────────────────

    def _base_blocks(self, *, canvas_placeholder: bool = True) -> list[Block]:
        blocks: list[Block] = [self._memory.load(), self._window.load()]
        for kind, source in self._sources.items():
            if kind is BlockKind.CANVAS and not canvas_placeholder:
                # 回答分支沿用 legacy 语义：空 layout 不注入占位
                if not self._inputs.canvas_layout.strip():
                    continue
            blocks.append(source.load())   # type: ignore[attr-defined]
        return blocks

    def for_decision(self, user_msg: str) -> Assembled:
        """决策上下文：parts["history_summary"] 与旧 ctx.digest() 逐字节等价。"""
        return self._assembler.for_decision(self._base_blocks(), user_msg=user_msg)

    def for_agent_loop(self, user_msg: str, *, system_text: str = "") -> Assembled:
        """**单 Agent 循环的唯一装配出口**：每次调用返回该步要发的**完整** messages。

        循环里的用法（每步一次）：

            messages = provider.for_agent_loop(user_msg, system_text=SYS).messages
            ... 调 LLM、执行工具 ...
            provider.add_tool_result([assistant 消息, *各 role=tool 结果])

        调用方**不手工维护消息列表**：块的增加只通过 add_tool_result 提交，
        消息的重建只通过本方法发生——tool_call_id 配对、块顺序、台账三件事
        因此只有一处代码在管。

        P0 时它是 for_decision 的空壳别名（先占住出口、行为不变）；P4 起为真装配。
        原 step 参数已删除：增量不是靠步数判断，而是**块集合本身在长**
        （工具结果块每步追加，状态块恒定），见 Assembler.for_agent_loop。
        """
        blocks: list[Block] = []
        if system_text:
            blocks.append(Block(
                kind=BlockKind.SYSTEM, text=system_text,
                priority=Priority.SYSTEM, compressible=False,
            ))
        blocks.extend(self._base_blocks())          # 记忆 + 窗口 + 状态源
        for msgs in self._tool_results:
            blocks.append(Block(
                kind=BlockKind.TOOL_RESULT, messages=tuple(msgs),
                priority=Priority.TOOL_RESULT, compressible=False,
            ))
        return self._assembler.for_agent_loop(blocks, user_msg=user_msg)

    def for_answer(
        self,
        user_msg: str,
        *,
        system_text: str = "",
    ) -> Assembled:
        """回答上下文：完整 messages（system 由调用方传入，避免反向依赖）。"""
        blocks = self._base_blocks(canvas_placeholder=False)
        if system_text:
            blocks.append(Block(
                kind=BlockKind.SYSTEM, text=system_text,
                priority=100, compressible=False,
            ))
        return self._assembler.for_answer(blocks, user_msg=user_msg)

    def for_worker(
        self,
        goal: str,
        *,
        system_text: str,
        max_messages: int,
    ) -> Assembled:
        """Worker（执行层）上下文：结构化 messages（system + 记忆 + 窗口 + 折叠兜底）。

        goal 本身不进 messages——Worker 把 goal 作为任务指令单独传给编排器，
        这里只供它"知道之前聊过什么、确立了什么口径"。
        """
        from app.services.context.assembler import build_worker_messages

        ledger = Ledger(purpose="worker")
        window = self._window.load()
        memory = self._memory.load()
        messages = build_worker_messages(
            system_text=system_text,
            history_summary=memory.text,
            turns=list(window.messages),
            max_messages=max_messages,
        )
        # 台账速记：Worker 消息混合 tool 结构，字符口径足够定位大头；
        # 精确 token 计量在 Worker 侧接入 LLM usage 后自然补齐。
        ledger.record("worker_messages_chars", sum(len(str(m.get("content", ""))) for m in messages))
        ledger.record("window_messages", len(window.messages))
        parts = {"goal": goal, "history_summary": memory.text}
        return Assembled(messages=messages, ledger=ledger, parts=parts)
