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

import json

from app.config import settings
from app.services.context.budgets import TOKENS_PER_CHAR_JSON, active_table, chars_for_tokens
from app.services.context.compress import DEGRADE_TOOL_COMPACTED, Compressor
from app.services.context.sources.window import select_for_answer
from app.services.context.types import Assembled, Block, BlockKind, Ledger
from app.services.context_utils import compact_result_json, estimate_tokens


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


# 单 Agent 循环的上下文块标题。
# 与决策路径**共用同一批渲染函数**（sources/state.py）——统一装配的意义就在于
# "同一事实只有一处渲染"，各出口只决定"注入哪些块、怎么拼"，不各写一份文案。
AGENT_LOOP_HEADERS: dict[BlockKind, str] = {
    BlockKind.MEMORY: "历史记忆",
    BlockKind.METRICS: "受治理指标清单",
    BlockKind.DATASOURCES: "可用数据源",
    BlockKind.CANVAS: "当前画布布局（已落盘，以此为准）",
    BlockKind.SUBTASK: "已完成子任务摘要",
    BlockKind.ASSESSMENT: "最近一次子任务评估",
    BlockKind.ARTIFACT: "上一轮已产出内容",
}


# ── 工具结果压缩（P4 / §5.3）────────────────────────────────────────────────

def _tool_result_budget(table) -> int:
    """单条工具消息的 token 预算（与执行层 TOOL_RESULT_MAX_TOKENS 同口径）。"""
    budget = table.get(BlockKind.TOOL_RESULT)
    if budget is not None and budget.max_tokens > 0:
        return int(budget.max_tokens)
    return int(getattr(settings, "TOOL_RESULT_MAX_TOKENS", 800) or 800)


def compress_tool_message(content: str, max_tokens: int) -> tuple[str, bool]:
    """压缩**一条** role=tool 消息的 content，返回 (新内容, 是否压缩过)。

    只动 content 字符串：role / tool_call_id / assistant.tool_calls 原样保留——
    工具协议的配对关系不可破坏（成对消息一旦错位，下一次调用直接 400）。

    复用执行层的 compact_result_json（同口径、同铁律）：error 结果完整保留、
    rows 只留前几行、非 JSON 走字符截断。不在这里另写一套截断规则。
    """
    if not content:
        return content, False
    max_chars = chars_for_tokens(max_tokens, TOKENS_PER_CHAR_JSON)
    if len(content) <= max_chars:
        return content, False
    compacted = compact_result_json(content, max_chars=max_chars)
    return compacted, compacted != content


# ── 超窗守卫（P1-A）────────────────────────────────────────────────────────
#
# 为什么必须有：模型窗口是**硬边界**，超了上游直接 400。而 400 在调用方只会被
# 当成"LLM 不可用"（lead_agent_unified 的 except 一律归为"服务暂时不可用"），
# 结果是一整轮对话报废、画布什么都不动。所以"超窗"必须在装配出口变成"裁剪"，
# 而不是把命运交给上游返回什么。
#
# 裁剪顺序（先丢"用完即弃"的，再丢"越旧越不值钱"的，最后才动任务产物）：
#   ① 最旧的 TOOL_RESULT 组（整组丢：assistant.tool_calls 与 role=tool 必须同生共死，
#      只丢一半会破坏工具协议，下一次调用直接 400）
#   ② WINDOW 最旧的消息（近端原文，越旧越不重要；不是"近端才重要"的倒置）
#   ③ SUBTASK 最旧的条目（派发产物，模型真正依赖的是最新一条评估）
#   ④ 兜底硬截断：仍超预算就按体积从大到小截断剩余文本块——保证"绝不静默超窗"

TRIM_TOOL_RESULT = "context_trimmed:tool_result"
TRIM_WINDOW = "context_trimmed:window"
TRIM_SUBTASK = "context_trimmed:subtask"
TRIM_HARD_CAP = "context_trimmed:hard_cap"
OVER_BUDGET_UNSATISFIABLE = "context_over_budget_unsatisfiable"


def input_token_budget() -> int:
    """本次装配允许的**输入** token 上限 = 窗口 × (1 − 输出预留比)。

    配置：`LLM_CONTEXT_WINDOW_TOKENS`（默认 64000）、
    `LLM_CONTEXT_OUTPUT_RESERVE_RATIO`（默认 0.25 → 48000）。
    两处都做防御：配置写错（0 / 负数 / 比例 >0.9）时回落到可用值，
    绝不把预算算成 0 或负数（那会把整轮上下文裁空）。
    """
    window = int(getattr(settings, "LLM_CONTEXT_WINDOW_TOKENS", 64000) or 64000)
    if window <= 0:
        window = 64000
    ratio = float(getattr(settings, "LLM_CONTEXT_OUTPUT_RESERVE_RATIO", 0.25) or 0.25)
    ratio = min(max(ratio, 0.0), 0.9)
    # round 而不是 int：1.0-0.9 在浮点里是 0.0999…，直接 int 会少 1 token
    return max(1, int(round(window * (1.0 - ratio))))


def message_tokens(msg: dict) -> int:
    """一条消息的估算 token（含 tool_calls 的 JSON 体积）。

    只算 content 会低估工具轮：assistant 的 tool_calls（name + arguments JSON）
    本身也是输入 token，漏算就会"以为没超、实际超了"。
    """
    n = estimate_tokens(str(msg.get("content") or ""))
    calls = msg.get("tool_calls")
    if calls:
        n += estimate_tokens(json.dumps(calls, ensure_ascii=False, default=str))
    return n


def messages_tokens(messages: list[dict]) -> int:
    """整组 messages 的估算 token（守卫的唯一度量口径）。"""
    return sum(message_tokens(m) for m in messages)


def _trim_oldest_entry(text: str) -> str | None:
    """删掉文本里**最旧**的一条 `- ` 条目；没有可删的结构时返回 None。

    子任务摘要的渲染顺序是"旧→新"（sources/state.py:render_subtask_summaries
    取 recent[-keep:]，顺序保持原样），所以第一条 `- ` 就是最旧的那条。
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("- "):
            rest = "\n".join(lines[:i] + lines[i + 1:]).strip()
            return rest or None
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

    # ── 单 Agent 循环（P4）：唯一出口 ───────────────────────────────────────

    def for_agent_loop(self, blocks: list[Block], *, user_msg: str) -> Assembled:
        """单 Agent 循环的装配：system（含上下文块）+ 窗口 + 本轮用户消息 + 工具结果。

        **每步重建一次完整 messages，而不是让调用方手工维护消息列表**：
          · LLM 无状态，每步本来就要重发完整 messages，重建不增加任何 token；
          · 状态块（记忆/指标/数据源/画布/子任务/评估）由 Source 恒定持有，
            每步重建出的 system 逐字节相同，不会出现两份互相矛盾的快照；
          · 工具结果块是**唯一**会增长的部分（每步追加一组），这就是"增量"的来源。
            派发后的新画布/评审结论由工具结果承载（build_execution_summary 内含最新布局），
            所以循环中途不需要刷新 Source。

        顺序：system → 窗口(历史) → 本轮 user → assistant(tool_calls)/tool 结果。
        TOOL_RESULT 块承载结构化消息，顺序与 tool_call_id **原样保留**——
        这是 OpenAI 工具协议的要求，压缩不得破坏。
        """
        table = active_table("agent_loop")
        ledger = Ledger(purpose="agent_loop", budget_tokens=table.input_budget())
        parts: dict[str, str] = {}
        system_prompt = ""
        # 状态块按"种类 + 渲染文本"存放（而不是直接拼成 ctx_lines）：
        # 超窗守卫要能按种类单独裁剪（如 SUBTASK 丢最旧条目），拼成字符串就回不去了。
        ctx_items: list[list] = []          # [[BlockKind, text], ...]
        window_msgs: list[dict] = []
        # 每个 TOOL_RESULT 块 = 一组（assistant.tool_calls + 其 role=tool 结果）。
        # 整组增删——只丢一半会破坏工具协议配对，下一次调用直接 400。
        tool_groups: list[list[dict]] = []

        for b in blocks:
            if b.is_empty:
                continue
            if b.kind is BlockKind.TOOL_RESULT:
                # §5.3：工具结果是循环里**唯一随步数增长**的块（状态块恒定不变），
                # 它的上限必须在这里收口——不做上限就是"步数越多上下文越无界"。
                # 与状态块的 enforced 开关无关：那个开关治的是"要不要改变渲染结果"的
                # 取舍（改了必须配评测），而工具结果不做上限是缺陷，不是取舍。
                # 逐条各算预算（确定性）：同一条结果在任何一步压出的文本都相同，
                # 不会因为后面又来了两条结果而改变已发出去的内容——前缀缓存不吃亏。
                tool_budget = _tool_result_budget(table)
                added_tokens = 0
                group: list[dict] = []
                for m in b.messages:
                    msg = dict(m)
                    if msg.get("role") == "tool":
                        text, compacted = compress_tool_message(
                            str(msg.get("content") or ""), tool_budget
                        )
                        if compacted:
                            msg["content"] = text
                            ledger.degrade(DEGRADE_TOOL_COMPACTED)
                    group.append(msg)
                    added_tokens += estimate_tokens(str(msg.get("content") or ""))
                tool_groups.append(group)
                ledger.record(BlockKind.TOOL_RESULT.value, added_tokens)
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

            if b.kind is BlockKind.SYSTEM:
                system_prompt = text
                ledger.record(BlockKind.SYSTEM.value, estimate_tokens(text))
            elif b.kind is BlockKind.WINDOW:
                window_block = b
                budget_block = table.get(BlockKind.WINDOW)
                window_budget = budget_block.max_tokens if budget_block else 0
                if window_budget > 0:
                    # P1-A：窗口块现在 compressible=True，让 compress.MessageListPolicy
                    # 真正可达（超过 CONTEXT_KEEP 条才折叠；Source 上限 30 条 → no-op）。
                    # 这里**不受 table.enforced 约束**：enforced 治的是"要不要按配额改渲染
                    # 结果"（改了要配评测），"条数无界"则是缺陷兜底——与工具结果 800 token
                    # 收口同一取舍（见上面那段注释）。
                    w2, reason = self._compressor.compress(b, window_budget)
                    if reason:
                        ledger.degrade(reason)
                    window_block = w2
                window_msgs = [dict(m) for m in window_block.messages]
                ledger.record(BlockKind.WINDOW.value, window_block.tokens())
            else:
                ledger.record(b.kind.value, estimate_tokens(text))
                ctx_items.append([b.kind, text])

        messages, trimmed = self._fit_to_budget(
            system_prompt=system_prompt,
            ctx_items=ctx_items,
            window_msgs=window_msgs,
            tool_groups=tool_groups,
            user_msg=user_msg,
            ledger=ledger,
        )
        # parts 只承载"进了 system 的状态块"：裁剪后重建一次，保证与 messages 一致
        parts.update({kind.value: text for kind, text in ctx_items})

        ledger.record("user", estimate_tokens(user_msg))
        return Assembled(messages=messages, ledger=ledger, parts=parts, trimmed=trimmed)

    # ── 超窗守卫：把"超窗"降级为"裁剪" ──────────────────────────────────────

    def _fit_to_budget(
        self,
        *,
        system_prompt: str,
        ctx_items: list[list],
        window_msgs: list[dict],
        tool_groups: list[list[dict]],
        user_msg: str,
        ledger: Ledger,
    ) -> tuple[list[dict], bool]:
        """按预算裁剪这一次装配；返回 (messages, trimmed)。

        未超窗 → 原样返回（逐字节不变，测试锁住）。
        超窗 → 依次丢最旧的工具结果组 / 最旧的窗口消息 / 最旧的子任务条目；
        仍超再按体积硬截断文本。每次裁剪都写 `context_trimmed:*` 到台账，
        绝不"静默超窗"——即便裁到无可再裁，也留一条
        `context_over_budget_unsatisfiable`，让上游看得见。

        实现要点：**滚动记账**。裁剪只做"删除/缩短"，被删块自己的体积是确定值，
        所以每轮只需减去增量（`total -= 被删块的 token`），不必重算整组 messages。
        这一步很关键：守卫落在每步的装配出口上，重算全量会让"超窗"从"一次报错"
        变成"每步多几十毫秒"，大租户上尤其明显（system 段动辄上万 token）。
        """

        def render() -> list[dict]:
            system_text = system_prompt
            if ctx_items:
                lines = [
                    f"【{AGENT_LOOP_HEADERS.get(kind, getattr(kind, 'value', kind))}】\n{text}"
                    for kind, text in ctx_items
                ]
                system_text = (
                    system_text.rstrip() + "\n\n" + "\n\n".join(lines)
                ).strip()
            msgs: list[dict] = []
            if system_text:
                msgs.append({"role": "system", "content": system_text})
            msgs.extend(t for t in window_msgs if t.get("role") in ("user", "assistant"))
            # 本轮用户消息必须排在工具结果**之前**：工具结果是本轮用户诉求引发的动作，
            # 顺序反了就是"先看到工具结果再看到问题"，模型会误判因果。
            msgs.append({"role": "user", "content": user_msg})
            for group in tool_groups:
                msgs.extend(group)
            return msgs

        budget = input_token_budget()
        messages = render()
        total = messages_tokens(messages)
        if total <= budget:
            return messages, False

        trimmed = False
        # ① 最旧的工具结果组（L0 热层：用完即弃）
        while tool_groups and total > budget:
            total -= sum(message_tokens(m) for m in tool_groups.pop(0))
            ledger.degrade(TRIM_TOOL_RESULT)
            trimmed = True
        # ② 最旧的窗口消息（L1：越旧越不值钱；至少留一条，别把"最近的对话"整段抽干）
        while len(window_msgs) > 1 and total > budget:
            total -= message_tokens(window_msgs.pop(0))
            ledger.degrade(TRIM_WINDOW)
            trimmed = True
        # ③ 子任务摘要里最旧的条目（任务产物；模型真正依赖的是最新一条评估）
        while total > budget:
            idx = next(
                (i for i, (kind, _t) in enumerate(ctx_items) if kind == BlockKind.SUBTASK),
                None,
            )
            if idx is None:
                break
            old_text = ctx_items[idx][1]
            new_text = _trim_oldest_entry(old_text)
            if new_text is None:
                ctx_items.pop(idx)
                total -= estimate_tokens(old_text)
            else:
                ctx_items[idx][1] = new_text
                total -= estimate_tokens(old_text) - estimate_tokens(new_text)
            ledger.degrade(TRIM_SUBTASK)
            trimmed = True
        # ④ 兜底硬截断：结构性块都丢完了仍超 → 截文本本体。
        #    每轮至少切掉一段（有界循环 400 次），防止异常配置下死循环。
        guard = 0
        while total > budget and guard < 400:
            guard += 1
            saved = _truncate_largest_ctx_item(ctx_items)
            if saved is None:
                saved = _truncate_oldest_window(window_msgs)
            if saved is None:
                saved = _truncate_oldest_tool(tool_groups)
            if saved is None:
                break
            total -= saved
            ledger.degrade(TRIM_HARD_CAP)
            trimmed = True
        messages = render()
        # 滚动记账是精确的；这里再做一次独立核对，防止"记账漂移"导致悄悄超窗
        if messages_tokens(messages) > budget:
            # 连 system 提示词本身都超预算：无可再裁。**必须留痕**，
            # 不允许"看起来正常、实际超窗"。
            ledger.degrade(OVER_BUDGET_UNSATISFIABLE)
            trimmed = True
        return messages, trimmed


# ── 兜底硬截断的三个原语（确定性 + 返回"省下多少 token"）─────────────────────

_MIN_KEEP_CHARS = 40   # 单块最少保留字符数：再小就只剩残句，不如整块丢掉


def _truncate_largest_ctx_item(ctx_items: list[list]) -> int | None:
    """截断体积最大的 system 文本块；返回省下的 token（None = 无可截）。"""
    if not ctx_items:
        return None
    idx = max(range(len(ctx_items)), key=lambda i: len(ctx_items[i][1]))
    text = ctx_items[idx][1]
    before = estimate_tokens(text)
    if len(text) <= _MIN_KEEP_CHARS:
        ctx_items.pop(idx)
        return before
    cut = max(_MIN_KEEP_CHARS, len(text) // 2)
    new_text = text[: len(text) - cut].rstrip() + "…"
    ctx_items[idx][1] = new_text
    return before - estimate_tokens(new_text)


def _truncate_oldest_window(window_msgs: list[dict]) -> int | None:
    """把最旧的窗口消息截半（从旧到新找第一条够长的）；返回省下的 token。"""
    for msg in window_msgs:
        text = str(msg.get("content") or "")
        if len(text) <= _MIN_KEEP_CHARS:
            continue
        before = estimate_tokens(text)
        msg["content"] = text[: len(text) // 2].rstrip() + "…"
        return before - estimate_tokens(str(msg["content"]))
    return None


def _truncate_oldest_tool(tool_groups: list[list[dict]]) -> int | None:
    """把最旧工具结果里的 role=tool 内容截半；返回省下的 token。

    只动 content（协议字段 tool_call_id / assistant.tool_calls 一字不改），
    与 compress_tool_message 的铁律一致。
    """
    for group in tool_groups:
        for msg in group:
            if msg.get("role") != "tool":
                continue
            text = str(msg.get("content") or "")
            if len(text) <= _MIN_KEEP_CHARS:
                continue
            before = estimate_tokens(text)
            msg["content"] = text[: len(text) // 2].rstrip() + "…"
            return before - estimate_tokens(str(msg["content"]))
    return None

