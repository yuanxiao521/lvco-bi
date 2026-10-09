# -*- coding: utf-8 -*-
"""Worker 注册与选择（表驱动）。

**为什么单独抽一层**：此前"派给哪个执行内核"是 `LeadAgent` 里的一段两级 if
（先判 entry、再判 complexity）。它有两个毛病：

1. **控制流当数据用**——"对话页复杂任务走通用编排"本是一张两行就能写完的表，
   写成 if 之后，想看全貌必须读函数体；
2. **新增助手要改函数**——加一个 worker、或调整某入口的默认内核，都得动逻辑，
   而不是加一行配置。

这里把它变成**显式的表**：`WORKERS` 描述有哪些执行内核，`ENTRY_WORKERS` 描述
"入口 × 复杂度 → 内核"的映射。新增助手 = 加一个 `WorkerSpec` + 在表里挂上，
不再触碰任何分支逻辑。

注意：worker 的 key 直接复用 `run_analysis` 的 `constraints["mode"]` 取值
（`canvas` / `orchestrator` / `react`），保持单一词汇表，避免"worker 名"与
"mode 名"两套命名互相翻译。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSpec:
    """一个执行内核的元信息。"""

    mode: str          # run_analysis 的 constraints["mode"]（同时是 worker 的唯一标识）
    label: str         # 中文名：日志 / 执行记录展示用
    canvas_only: bool = False   # 是否只在画布入口可用（通用编排在对话页才是默认）


# ── 执行内核注册表：新增助手在这里加一项 ────────────────────────────────
WORKERS: dict[str, WorkerSpec] = {
    "canvas": WorkerSpec(mode="canvas", label="画布编排", canvas_only=True),
    "orchestrator": WorkerSpec(mode="orchestrator", label="通用编排"),
    "react": WorkerSpec(mode="react", label="轻量 ReAct"),
}

# ── 派发表：入口 × 复杂度 → worker 名 ─────────────────────────────────
# 改"某入口的默认内核"= 改这里的一格；调"简单任务走哪个"= 改 simple 那一列。
ENTRY_WORKERS: dict[str, dict[str, str]] = {
    "canvas": {"complex": "canvas", "simple": "react"},
    "chat": {"complex": "orchestrator", "simple": "react"},
}

# 表里查不到时的兜底（例如未来新增入口忘了登记）
FALLBACK_WORKER = "orchestrator"


def resolve_worker(
    entry: str,
    complexity: str,
    explicit: str | None = None,
) -> WorkerSpec:
    """解析本轮该派给哪个 worker（确定性，不经额外 LLM 调用）。

    优先级：
      1. `explicit`——调用方显式指定的 mode（强制 react / orchestrator / canvas）；
      2. 「入口 × 复杂度」派发表；
      3. `FALLBACK_WORKER` 兜底。

    `entry` 未登记的入口按兜底处理（而不是抛错）——派发是主链路上的关键一步，
    宁可退化到通用编排，也不要让整轮对话失败。
    """
    table = ENTRY_WORKERS.get(entry) or ENTRY_WORKERS.get("chat") or {}
    name = explicit or table.get(complexity) or FALLBACK_WORKER
    return WORKERS.get(name) or WORKERS[FALLBACK_WORKER]
