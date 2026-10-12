"""上下文预算表（token 口径）。

单位约定：**一律 token**，禁止字符数比较。
换算系数来自实测（app.services.context_utils.estimate_tokens）：
- 中文散文 ≈1.0 token/字；
- 中英混排提示 ≈0.6 token/字；
- JSON / 工具结果 ≈0.4 token/字。

阶段说明：
- **P0（当前）**：使用 EQUIVALENT_* 表（由现有 *_CHARS 配置等价换算而来），
  仅用于**台账与观测**，尚未强制生效——装配行为与原实现逐字节一致；
- **阶段 2**：配置项统一改成 *_TOKENS（等价换算，行为不变）；
- **阶段 4**：Compressor 收口后，把 ACTIVE 指向 TARGET_* 并置 enforced=True，
  由 Assembler 按目标表裁剪。

固定开销块（SYSTEM / TOOL_SCHEMA）在等价表中记为 0（即"此前从未纳入预算"）——
台账仍会记录它们的真实 token，这正是要暴露的问题。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

# ── 换算系数（实测） ────────────────────────────────────────────────────────
TOKENS_PER_CHAR_ZH = 1.0      # 中文散文
TOKENS_PER_CHAR_MIXED = 0.6   # 中英混排提示
TOKENS_PER_CHAR_JSON = 0.4    # JSON / 工具结果 / 代码


def tokens_for_chars(chars: int, tokens_per_char: float) -> int:
    """字符数 → token（按内容类型的经验系数）。"""
    return int(round(max(0, chars) * tokens_per_char))


def chars_for_tokens(tokens: int, tokens_per_char: float) -> int:
    """token → 字符数（与 tokens_for_chars 互逆，等价换算下必须精确往返）。"""
    if tokens_per_char <= 0:
        return 0
    return int(round(max(0, tokens) / tokens_per_char))


@dataclass(frozen=True)
class BlockBudget:
    """单个块的预算：上限 + 保底 + 降级优先级。"""

    kind: str
    max_tokens: int = 0        # 0 表示"本阶段未纳入预算"（仍会记账）
    min_tokens: int = 0        # 即使超支也必须留下的量（保证不整块消失）
    priority: int = 50


@dataclass(frozen=True)
class BudgetTable:
    """一次装配的完整配额表（决策 / 回答 / Worker 各一张）。"""

    purpose: str
    blocks: tuple[BlockBudget, ...] = ()
    output_tokens: int = 0
    enforced: bool = False     # P0=False：只记账不裁剪；P2 翻转
    note: str = ""

    def get(self, kind: str) -> BlockBudget | None:
        for b in self.blocks:
            if b.kind == kind or b.kind == getattr(kind, "value", kind):
                return b
        return None

    def input_budget(self) -> int:
        return sum(b.max_tokens for b in self.blocks)


# ── 等价表（P0）：由现有 *_CHARS 配置换算，保持行为不变 ─────────────────────
def equivalent_decision_table() -> BudgetTable:
    total_chars = int(getattr(settings, "LEAD_CTX_MAX_CHARS", 4000) or 4000)
    reserve_chars = int(getattr(settings, "LEAD_CTX_LIVE_RESERVE_CHARS", 800) or 800)
    return BudgetTable(
        purpose="decision",
        blocks=(
            BlockBudget("memory", tokens_for_chars(max(0, total_chars - reserve_chars), TOKENS_PER_CHAR_MIXED), priority=70),
            BlockBudget("window", tokens_for_chars(reserve_chars, TOKENS_PER_CHAR_ZH), priority=90),
        ),
        output_tokens=400,
        note="等价换算：memory=LEAD_CTX_MAX_CHARS-预留，window=预留",
    )


def equivalent_answer_table() -> BudgetTable:
    total_chars = int(getattr(settings, "LEAD_CTX_MAX_CHARS", 4000) or 4000)
    answer_chars = int(getattr(settings, "LEAD_CTX_ANSWER_HISTORY_CHARS", 2000) or 2000)
    return BudgetTable(
        purpose="answer",
        blocks=(
            BlockBudget("memory", tokens_for_chars(max(200, total_chars // 3), TOKENS_PER_CHAR_ZH), priority=70),
            BlockBudget("window", tokens_for_chars(answer_chars, TOKENS_PER_CHAR_ZH), priority=90),
        ),
        output_tokens=1200,
        note="等价换算：answer 分支 memory 上限=LEAD_CTX_MAX_CHARS//3",
    )


def equivalent_worker_table() -> BudgetTable:
    result_chars = int(getattr(settings, "RESULT_MAX_CHARS", 2000) or 2000)
    list_chars = int(getattr(settings, "CONTEXT_MAX_CHARS", 150000) or 150000)
    return BudgetTable(
        purpose="worker",
        blocks=(
            BlockBudget("tool_result", tokens_for_chars(result_chars, TOKENS_PER_CHAR_JSON), priority=80),
            BlockBudget("window", tokens_for_chars(list_chars, TOKENS_PER_CHAR_MIXED), priority=90),
        ),
        output_tokens=2000,
        note="等价换算：tool_result=RESULT_MAX_CHARS，window=CONTEXT_MAX_CHARS（实测约 9 万 token）",
    )


# ── 目标表（P2）：计划里的设计值 ────────────────────────────────────────────
TARGET_DECISION = BudgetTable(
    purpose="decision",
    blocks=(
        BlockBudget("system", 600, priority=100),
        BlockBudget("datasources", 300, priority=50),
        BlockBudget("subtask", 400, priority=10),
        BlockBudget("assessment", 150, priority=20),
        BlockBudget("canvas", 300, priority=60),
        BlockBudget("memory", 800, min_tokens=200, priority=70),
        BlockBudget("window", 1200, min_tokens=300, priority=90),
        BlockBudget("user", 300, priority=1000),
    ),
    output_tokens=400,
    enforced=False,
    note="设计目标：决策输入 ~4050 token（只用来路由一个 400 token 的 JSON）",
)

TARGET_ANSWER = BudgetTable(
    purpose="answer",
    blocks=(
        BlockBudget("system", 200, priority=100),
        BlockBudget("metrics", 400, priority=40),
        BlockBudget("memory", 600, min_tokens=150, priority=70),
        BlockBudget("canvas", 300, priority=60),
        BlockBudget("window", 800, min_tokens=200, priority=90),
        BlockBudget("user", 300, priority=1000),
    ),
    output_tokens=1200,
    enforced=False,
    note="设计目标：回答输入 ~2600 token",
)

TARGET_WORKER = BudgetTable(
    purpose="worker",
    blocks=(
        BlockBudget("system", 1500, priority=100),
        BlockBudget("tool_schema", 1947, priority=95),   # 实测值；目标 ≤1200
        BlockBudget("tool_result", 600, priority=80),    # 单次；并发上限 3
        BlockBudget("subtask", 400, priority=10),
        BlockBudget("memory", 800, min_tokens=200, priority=70),
        BlockBudget("window", 1200, min_tokens=300, priority=90),
        BlockBudget("user", 300, priority=1000),
    ),
    output_tokens=2000,
    enforced=False,
    note="设计目标：Worker 输入 ~7950 token（固定开销占 67%）",
)

# 当前生效的表。P0 → 等价表；P2 改指 TARGET_*。
ACTIVE: dict[str, BudgetTable] = {
    "decision": equivalent_decision_table(),
    "answer": equivalent_answer_table(),
    "worker": equivalent_worker_table(),
}


def active_table(purpose: str) -> BudgetTable:
    return ACTIVE.get(purpose) or BudgetTable(purpose=purpose)
