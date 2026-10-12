"""装配层：算预算、定顺序、产出消息——唯一允许拼 messages 的地方。

分工铁律（四件套）：
- Source 产出"完整内容"（Block）；
- Compressor 负责把块压进预算；
- Assembler 只负责：排序 → 降级决策 → 渲染 → 台账。

P0（行为零变化）：
- 决策路径：渲染窗口的算法从 LeadContext.digest **原样迁出**（render_window_for_decision），
  user_content 的最终拼装仍留在 lead_decider（消费这里的 parts），等价性由测试锁住；
- 回答路径：messages 拼装整体迁入（for_answer），legacy 的 system/记忆/指标/画布/窗口顺序不变。
"""
from __future__ import annotations

from app.config import settings
from app.services.context.budgets import active_table
from app.services.context.compress import Compressor
from app.services.context.sources.window import select_for_answer
from app.services.context.types import Assembled, Block, BlockKind, Ledger
from app.services.context_utils import estimate_tokens


# ── legacy 渲染（逐字节等价层，测试锁定） ───────────────────────────────────


def render_window_for_decision(
    turns: list[dict],
    *,
    extra_context: str = "",
    history_summary: str = "",
    max_chars: int | None = None,
) -> str:
    """决策窗口渲染：从 LeadContext.digest 原样迁移（逐字节等价）。

    装配规则：不切句子（整条消息从新到旧累加，装不下就停；单条自身超预算才截它）、
    长期记忆有上限（预算 - 活记忆预留）、成对对齐丢孤立回答、兜底硬上限。
    """
    # P0：limit 语义仍是"字符"（与 legacy 逐字节等价）；阶段 4 由 Compressor 接管后
    # 切换为 token 语义。配置项先行统一读 token（等价换算见 config 别名注释）。
    limit = int(max_chars or getattr(settings, "LEAD_CTX_MAX_TOKENS", 2400) or 2400)
    reserve = min(
        max(0, int(getattr(settings, "LEAD_CTX_LIVE_RESERVE_TOKENS", 800) or 0)),
        limit // 2,
    )

    extra = (extra_context or "").strip()
    if extra:
        extra = extra[: max(0, limit // 4)]

    memory = ""
    if history_summary.strip():
        memory = f"【长期记忆】{history_summary.strip()}"
        max_memory = limit - reserve
        if 0 < max_memory < len(memory):
            memory = memory[:max_memory] + "…"

    window_turns = [
        {"role": t.get("role"), "content": str(t.get("content", ""))}
        for t in turns[-max(1, settings.LEAD_MAX_TURNS_IN_CTX):]
    ]
    used = len(memory) + len(extra)
    picked: list[str] = []
    for t in reversed(window_turns):
        line = f"{'用户' if t.get('role') == 'user' else '助手'}: {t['content']}"
        if used + len(line) + 1 > limit:
            remain = limit - used - 1
            if remain > 40 and not picked:
                picked.append(line[:remain] + "…")
            break
        picked.append(line)
        used += len(line) + 1
    picked.reverse()

    if any(p.startswith("用户:") for p in picked):
        head = 0
        while head < len(picked) and picked[head].startswith("助手:"):
            head += 1
        picked = picked[head:]

    parts = [p for p in (extra, memory, "\n".join(picked)) if p]
    out = "\n".join(parts)
    if len(out) > limit:
        out = out[:limit]
    return out


def build_worker_messages(
    *,
    system_text: str,
    history_summary: str,
    turns: list[dict],
    max_messages: int,
    keep: int | None = None,
    max_chars: int | None = None,
) -> list[dict]:
    """Worker（执行层）的消息装配——legacy lead_tools 三段式的唯一实现。

    结构（ReAct 需要结构化消息，与 for_answer 的纯问答不同）：
      system → 【压缩摘要】【历史记忆】（长期记忆，COMPRESSION_MARKER 标记沿用）→ 窗口原文
    最后过 MessageListPolicy 兜底折叠（keep 条 + 字符上限）。

    窗口口径：max_messages 由调用方按 WINDOW_MAX_ROUNDS*2 传入（阶段 3 统一，
    消除 worker 漏网的 history[-30:] 条数特例）。
    """
    from app.services.context_utils import COMPRESSION_MARKER, compress_history

    messages: list[dict] = [{"role": "system", "content": system_text}]
    if (history_summary or "").strip():
        messages.append({
            "role": "assistant",
            "content": f"{COMPRESSION_MARKER}【历史记忆】{history_summary.strip()}",
        })
    for h in turns[-max(1, max_messages):]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
            messages.append({"role": h["role"], "content": str(h.get("content", ""))})
    return compress_history(messages, keep=keep or 40, max_chars=max_chars or 150000)


def first_block(blocks: list[Block], kind: BlockKind) -> Block | None:
    """取第一个非空的同种块；空内容块视为不存在（不注入占位）。"""
    for b in blocks:
        if b.kind == kind and not b.is_empty:
            return b
    return None


class Assembler:
    """按用途装配上下文（P0：委托模式，输出与旧路径逐字节等价）。"""

    def __init__(self, compressor: Compressor | None = None) -> None:
        self._compressor = compressor or Compressor()

    # ── 决策：产出各块文本（digest 的窗口渲染 + 状态块），最终拼装仍在 decider ──

    def for_decision(self, blocks: list[Block], *, user_msg: str) -> Assembled:
        table = active_table("decision")
        ledger = Ledger(purpose="decision", budget_tokens=table.input_budget())
        parts: dict[str, str] = {}

        window = first_block(blocks, BlockKind.WINDOW) or Block(kind=BlockKind.WINDOW)
        memory = first_block(blocks, BlockKind.MEMORY)
        artifact = first_block(blocks, BlockKind.ARTIFACT)

        digest_text = render_window_for_decision(
            list(window.messages),
            extra_context=artifact.text if artifact else "",
            history_summary=memory.text if memory else "",
        )
        parts["history_summary"] = digest_text
        ledger.record("history_summary", estimate_tokens(digest_text))

        for b in blocks:
            if b.is_empty or b.kind in (BlockKind.WINDOW, BlockKind.MEMORY, BlockKind.ARTIFACT):
                continue
            text = b.text
            if table.enforced:
                budget_block = table.get(b.kind)
                budget = budget_block.max_tokens if budget_block else 0
                if budget > 0:
                    b2, reason = self._compressor.compress(b, budget)
                    text = b2.text
                    if reason:
                        ledger.degrade(reason)
            parts[b.kind.value] = text
            ledger.record(b.kind.value, estimate_tokens(text))

        parts["user"] = user_msg
        return Assembled(messages=[], ledger=ledger, parts=parts)

    # ── 回答：完整拼装 messages（legacy 顺序不变） ─────────────────────────────

    def for_answer(self, blocks: list[Block], *, user_msg: str) -> Assembled:
        table = active_table("answer")
        ledger = Ledger(purpose="answer", budget_tokens=table.input_budget())
        messages: list[dict] = []
        parts: dict[str, str] = {}

        for b in blocks:
            if b.is_empty:
                continue
            text = b.text
            if table.enforced:
                budget_block = table.get(b.kind)
                budget = budget_block.max_tokens if budget_block else 0
                if budget > 0:
                    b2, reason = self._compressor.compress(b, budget)
                    text = b2.text
                    if reason:
                        ledger.degrade(reason)
            parts[b.kind.value] = text
            ledger.record(b.kind.value, b.tokens())   # WINDOW 是 messages 型，必须用 b.tokens()

        if parts.get(BlockKind.SYSTEM.value):
            messages.append({"role": "system", "content": parts[BlockKind.SYSTEM.value]})
        if parts.get(BlockKind.MEMORY.value):
            # legacy 等价（lead_agent._answer_branch）：长期记忆也要给上限，
            # 否则 legacy 摘要可能上千字，原样注入会挤占追问所需的空间。
            # P0：精确复刻 legacy 的字符截断；阶段 4 由 Compressor 的记忆策略接管。
            mem = parts[BlockKind.MEMORY.value]
            # legacy: LEAD_CTX_MAX_CHARS(4000字)//3 = 1333 字（中文 1:1 → 1333 token）。
            # 等价派生：LEAD_CTX_MAX_TOKENS(2400, 混排0.6) * 5 / 9 ≈ 1333。
            mem_cap = max(200, int(getattr(settings, "LEAD_CTX_MAX_TOKENS", 2400) or 2400) * 5 // 9)
            if len(mem) > mem_cap:
                mem = mem[:mem_cap] + "…"
                ledger.degrade("memory_capped_for_answer")
            messages.append({"role": "assistant", "content": f"【历史记忆】{mem}"})
        if parts.get(BlockKind.METRICS.value):
            messages.append({"role": "assistant", "content": f"【受治理指标清单】\n{parts[BlockKind.METRICS.value]}"})
        if parts.get(BlockKind.CANVAS.value):
            messages.append({
                "role": "assistant",
                "content": f"【当前画布布局（已落盘，回答涉及画布时以此为准）】\n{parts[BlockKind.CANVAS.value]}",
            })
        window = first_block(blocks, BlockKind.WINDOW)
        for t in select_for_answer(list(window.messages) if window else []):
            messages.append({"role": t["role"], "content": t["content"]})
        messages.append({"role": "user", "content": user_msg})

        ledger.record("user", estimate_tokens(user_msg))
        return Assembled(messages=messages, ledger=ledger, parts=parts)
