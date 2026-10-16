"""TurnContextProvider：上下文装配的唯一门面。

职责边界（四件套）：
- 持有本轮全部 Source（请求级，warmup 一次、全轮复用）；
- 暴露 for_decision / for_answer / for_worker 三个出口；
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
from app.services.context.types import Assembled, Block, BlockKind, Ledger


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

    def for_agent_loop(self, user_msg: str, *, step: int = 0) -> Assembled:
        """单 Agent 循环的装配出口（**P0 空实现**：内部先复用 for_decision）。

        为什么要先占住这个出口：P1 会把 Lead 从「四个工位」合并成「一个 system + 循环」，
        P4 再把它变成「按循环步装配」（step0 全量；后续步只追加工具结果增量，
        并把决策路径手拼的四块全部搬进 Source）。先把出口立起来，
        调用方就能提前切过去而**不改行为**，把风险留在一个可回滚的小提交里。

        P0 契约：行为与 for_decision **完全一致**（step 参数暂被忽略）——
        由 tests/test_lead_p0_capabilities.py 对拍钉住。
        """
        return self.for_decision(user_msg)

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
