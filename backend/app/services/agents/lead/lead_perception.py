"""感知与汇报：把确定性编排器的事件流旁路翻译为「用户能看懂的进度」。

设计要点（对齐 `lead-agent-upgrade-design.md` §4.3）：
- 输入是编排器现有的 emit 事件流（status / plan / tool_call / tool_result / chart / ...），
  **不改编排器**，只做旁路翻译。
- 输出 `StepProgress` 交给 LeadAgent 决定「是否对用户说一句」。
- 汇报策略：默认「关键节点汇报」（每步开始/结束各一次，失败必报），避免刷屏。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

logger = logging.getLogger("lvco.lead.perception")

_STEP_RESULT_NAME_LEN = 20  # 编排器以 goal[:20] 作为步骤结果事件的 name


@dataclass
class StepProgress:
    """单个步骤的进度快照。"""

    index: int              # 第几步（1-based）
    total: int              # 共几步（来自 PlanOutput；未知为 0）
    step_id: str = ""       # 步骤 id（字符串化）
    title: str = ""         # 步骤目标（人话）
    status: str = "start"   # "start"|"ok"|"fail"|"skip"
    tool: str | None = None # 当前工具名
    latency_ms: int = 0     # 耗时（毫秒）
    note: str = ""          # 人话描述


def render_progress_text(p: StepProgress) -> str:
    """把 StepProgress 渲染成用户可见的一句话。

    例：
        '【3/7】正在查 2024 年华东区销售额 ...'
        '【3/7】✓ 已拿到 1.2 万行'
        '【3/7】✗ 查询失败：字段不存在'
    """
    counter = f"【{p.index}/{p.total}】" if p.total else (f"【{p.index}】" if p.index else "")
    body = (p.note or p.title or "").strip()
    if p.status == "ok":
        return f"{counter}✓ {body}" if body else f"{counter}✓"
    if p.status == "fail":
        return f"{counter}✗ {body}" if body else f"{counter}✗"
    if p.status == "skip":
        return f"{counter}· 跳过 {body}".rstrip()
    return f"{counter}{body} ..." if body else f"{counter}执行中 ..."


def _parse_plan_steps(plan: dict) -> list[dict]:
    if not isinstance(plan, dict):
        return []
    steps = plan.get("steps")
    return steps if isinstance(steps, list) else []


def _result_is_error(result_str: str) -> bool:
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


def _short(text: str, limit: int = 40) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t if len(t) <= limit else t[:limit] + "…"


async def perceive_stream(
    raw_events: AsyncIterator[dict],
    *,
    total_hint: int = 0,
) -> AsyncIterator[StepProgress]:
    """把编排器事件流翻译为连续的 StepProgress。

    Args:
        raw_events: 编排器 emit 出来的原始事件流。
        total_hint: 若上游已知总步数，可先行给出（plan 事件到达后会被真实值覆盖）。

    Yields:
        StepProgress（关键节点：计划就绪 / 工具开始 / 工具结束 / 步骤完成 / 错误）。
    """
    steps: list[dict] = []
    total = total_hint
    idx = 0
    cur_title = ""
    cur_step_id = ""

    def _cur() -> StepProgress:
        return StepProgress(
            index=idx, total=total, step_id=cur_step_id, title=cur_title, status="start",
        )

    async for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")

        if etype == "plan":
            steps = _parse_plan_steps(ev.get("plan") or {})
            if steps:
                total = len(steps)
                idx = 1
                first = steps[0]
                cur_step_id = str(first.get("step_id", ""))
                cur_title = str(first.get("goal") or first.get("purpose") or "")
                p = _cur()
                p.status = "start"
                p.note = _short(cur_title, 60)
                yield p
            continue

        if etype == "status":
            msg = str(ev.get("message") or "")
            if msg:
                p = _cur()
                p.status = "start"
                p.note = _short(msg, 60)
                yield p
            continue

        if etype == "tool_call":
            p = _cur()
            p.status = "start"
            p.tool = str(ev.get("name") or "")
            p.note = f"正在执行 {p.tool}" if p.tool else "正在执行工具"
            yield p
            continue

        if etype == "chart":
            p = _cur()
            p.status = "ok"
            p.note = f"已生成 {ev.get('chart_type', '')} 图表".strip()
            yield p
            continue

        if etype == "tool_result":
            name = str(ev.get("name") or "")
            ok = not _result_is_error(str(ev.get("result") or ""))
            is_step_result = bool(name) and name in (cur_title[:_STEP_RESULT_NAME_LEN],)
            p = _cur()
            p.status = "ok" if ok else "fail"
            if is_step_result:
                p.note = _short(cur_title, 50) if ok else f"步骤失败：{_short(cur_title, 40)}"
                yield p
                if idx < len(steps):
                    idx += 1
                    nxt = steps[idx - 1]
                    cur_step_id = str(nxt.get("step_id", ""))
                    cur_title = str(nxt.get("goal") or nxt.get("purpose") or "")
            else:
                p.tool = name
                p.note = f"{name} 完成" if ok else f"{name} 失败"
                yield p
            continue

        if etype == "error":
            p = _cur()
            p.status = "fail"
            p.note = _short(str(ev.get("message") or "执行出错"), 60)
            yield p
            continue
