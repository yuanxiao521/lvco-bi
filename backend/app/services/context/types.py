"""上下文装配的公共类型：Block / Ledger / Assembled。

三者是四件套之间的唯一通信协议——Source 产出 Block，Compressor 消费并返回 Block，
Assembler 汇总成 Ledger + Assembled。除此之外不传别的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.services.context_utils import estimate_tokens


class BlockKind(str, Enum):
    """上下文块的种类。

    内容分三层（按"丢得起吗"）：L0 丢得起 / L1 丢不起 / L2 必须丢。
    其余是固定开销与状态类块，按各自的时效性挂在同一张配额表上。
    """

    # ── 固定开销（不衰减，但必须计入预算）──
    SYSTEM = "system"              # 系统提示词
    TOOL_SCHEMA = "tool_schema"    # 工具 JSON schema（实测 1947 token，此前从未计入预算）

    # ── L0 热层：当轮工具结果，用完即弃 ──
    TOOL_RESULT = "tool_result"

    # ── L1 温层：最近对话原文，丢不起 ──
    WINDOW = "window"

    # ── L2 冷层：长期记忆，必须压 ──
    MEMORY = "memory"

    # ── 状态类：按需注入、可失效重读 ──
    CANVAS = "canvas"
    DATASOURCES = "datasources"
    METRICS = "metrics"

    # ── 任务类：一次派发的产物，优先级最低 ──
    SUBTASK = "subtask"
    ASSESSMENT = "assessment"
    ARTIFACT = "artifact"

    # ── 永不可丢 ──
    USER = "user"


class Priority:
    """降级优先级：**数值越小越先被丢**。

    顺序体现"越近越贵"：先丢一次性产物与状态载荷，再压记忆，最后才动近端原文。
    USER / SYSTEM 视为不可丢（远大于其他值）。
    """

    SUBTASK = 10
    ASSESSMENT = 20
    ARTIFACT = 30
    METRICS = 40
    DATASOURCES = 50
    CANVAS = 60
    MEMORY = 70
    TOOL_RESULT = 80
    WINDOW = 90
    TOOL_SCHEMA = 95
    SYSTEM = 100
    USER = 1000


@dataclass(frozen=True)
class Block:
    """一个上下文块。

    Args:
        kind: 块种类（决定用哪个压缩策略）。
        text: 渲染后的文本（大多数块只填这个）。
        messages: 结构化消息列表（仅 WINDOW 这类需要保留问答结构的块使用）。
        priority: 降级优先级，越小越先丢。
        min_tokens: 该块的预算下限——即使超支也要留下这么多（如口径节不能整块消失）。
        compressible: False 表示该块不允许压缩（如 error 结果、用户原话）。
    """

    kind: BlockKind
    text: str = ""
    messages: tuple[dict[str, Any], ...] = ()
    priority: int = Priority.METRICS
    min_tokens: int = 0
    compressible: bool = True

    @property
    def is_empty(self) -> bool:
        return not (self.text or "").strip() and not self.messages

    def tokens(self) -> int:
        """估算本块 token 数（结构化消息按各条 content 累加）。"""
        if self.text:
            return estimate_tokens(self.text)
        return sum(estimate_tokens(str(m.get("content", ""))) for m in self.messages)


@dataclass
class Ledger:
    """一次装配的预算台账——回答"这个 prompt 为什么这么大"。

    落 trace 后可以直接看到每个块占了多少 token、丢了谁、为什么降级。
    """

    purpose: str
    budget_tokens: int = 0
    entries: dict[str, int] = field(default_factory=dict)  # 块名 → token
    dropped: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    @property
    def used_tokens(self) -> int:
        return sum(self.entries.values())

    @property
    def over_budget(self) -> bool:
        return self.budget_tokens > 0 and self.used_tokens > self.budget_tokens

    def record(self, name: str, tokens: int) -> None:
        """记账：同名块累加（如多次工具结果）。"""
        self.entries[name] = self.entries.get(name, 0) + int(tokens)

    def drop(self, name: str) -> None:
        if name not in self.dropped:
            self.dropped.append(name)

    def degrade(self, reason: str) -> None:
        if reason and reason not in self.degraded:
            self.degraded.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "budget_tokens": self.budget_tokens,
            "used_tokens": self.used_tokens,
            "over_budget": self.over_budget,
            "entries": dict(self.entries),
            "dropped": list(self.dropped),
            "degraded": list(self.degraded),
        }

    def summary(self) -> str:
        """一行摘要，用于日志。"""
        top = sorted(self.entries.items(), key=lambda kv: -kv[1])[:6]
        parts = ", ".join(f"{k}={v}" for k, v in top)
        flag = " OVER" if self.over_budget else ""
        return (
            f"[ctx] {self.purpose} used={self.used_tokens}/{self.budget_tokens}{flag} "
            f"({parts}) dropped={self.dropped} degraded={self.degraded}"
        )


@dataclass
class Assembled:
    """装配产物：直接下发给 LLM 的消息列表 + 台账 + 各块最终文本。"""

    messages: list[dict[str, Any]]
    ledger: Ledger
    parts: dict[str, str] = field(default_factory=dict)

    def history_summary(self) -> str:
        """兼容旧调用点：决策/回答仍需要一个 `history_summary` 字符串。

        注意 key：决策路径的窗口渲染（extra+记忆+近端）存在 "history_summary" 下；
        这里同时兼容未来按块拆分后的 "window"。
        """
        return self.parts.get("history_summary") or self.parts.get(BlockKind.WINDOW.value, "")
