"""状态类块的数据源：数据源清单 / 画布 / 指标 / 子任务 / 评估 / 产物。

P0：渲染函数从 lead_decider._build_context_blocks **原样迁出**（逐字节等价），
lead_decider 改为转调这里；DB 读取仍由调用方完成（阶段 3 收归 Source 内部，
届时 warmup 一次、全轮复用）。
"""

from __future__ import annotations

from app.services.context.types import Block, BlockKind, Priority

# legacy 常量（lead_decider.MAX_SUMMARY_CHARS 等），迁出后在此固化
SUBTASK_SUMMARY_CHARS = 1000   # 每条子任务摘要的截断上限
SUBTASK_KEEP = 2               # 已完成子任务摘要保留最近 N 条
ASSESSMENT_KEEP = 1            # 子任务评估取最近 N 条


# ── 渲染（纯函数，逐字节等价于 legacy） ─────────────────────────────────────

def render_datasources(datasources: list[dict] | None) -> str:
    """可用数据源清单（供 LLM 引用真实 id，禁止编造）。"""
    ds_lines: list[str] = []
    for d in datasources or []:
        if not isinstance(d, dict):
            continue
        ds_lines.append(f"- id={d.get('id')} name={d.get('name')} type={d.get('type')}")
    return "\n".join(ds_lines) or "（无）"


def render_canvas(layout_text: str) -> str:
    """画布布局快照；空则给占位说明。"""
    block = (layout_text or "").strip()
    return block or "（非画布入口或画布状态不可用）"


def render_assessment(subtask_summaries: list[str] | None) -> str:
    """最近一条规则评审（单独注入、不随摘要截断丢弃）。"""
    lines = [s for s in (subtask_summaries or []) if "[子任务评估]" in s]
    return lines[-1] if lines else "（本轮无子任务评估）"


def render_subtask_summaries(
    subtask_summaries: list[str] | None,
    max_chars: int = SUBTASK_SUMMARY_CHARS,
    keep: int = SUBTASK_KEEP,
) -> str:
    """已完成子任务摘要：最近 keep 条、每条截断（防报告全文撑爆决策 prompt）。"""
    recent = list(subtask_summaries or [])[-keep:]
    lines = [f"- {s[:max_chars]}" for s in recent]
    return "\n".join(lines) or "（本轮无已完成子任务）"


# ── Source（只取不裁） ──────────────────────────────────────────────────────

class DatasourceSource:
    def __init__(self, datasources: list[dict] | None) -> None:
        self._datasources = datasources

    def load(self, *, force: bool = False) -> Block:
        return Block(
            kind=BlockKind.DATASOURCES,
            text=render_datasources(self._datasources),
            priority=Priority.DATASOURCES,
            compressible=False,
        )


class CanvasSource:
    """画布快照源。

    Args:
        placeholder: 空画布时是否注入占位说明——决策 prompt 需要（legacy
            _build_context_blocks 行为），回答分支不要（legacy _answer_branch
            对空 layout 直接不注入，避免噪声）。
    """

    def __init__(self, layout_text: str, *, placeholder: bool = True) -> None:
        self._layout = layout_text or ""
        self._placeholder = placeholder

    def load(self, *, force: bool = False) -> Block:
        if not self._layout.strip() and not self._placeholder:
            return Block(kind=BlockKind.CANVAS)
        return Block(
            kind=BlockKind.CANVAS,
            text=render_canvas(self._layout),
            priority=Priority.CANVAS,
            compressible=True,
        )


class MetricsSource:
    """受治理指标清单（口径的权威源；空清单视为无块）。"""

    def __init__(self, metrics_ctx: str) -> None:
        self._metrics_ctx = (metrics_ctx or "").strip()

    def load(self, *, force: bool = False) -> Block:
        if not self._metrics_ctx:
            return Block(kind=BlockKind.METRICS)
        return Block(
            kind=BlockKind.METRICS,
            text=self._metrics_ctx,
            priority=Priority.METRICS,
            compressible=True,
        )


class SubtaskSource:
    def __init__(self, subtask_summaries: list[str] | None) -> None:
        self._summaries = subtask_summaries

    def load(self, *, force: bool = False) -> Block:
        return Block(
            kind=BlockKind.SUBTASK,
            text=render_subtask_summaries(self._summaries),
            priority=Priority.SUBTASK,
            compressible=True,
        )


class AssessmentSource:
    def __init__(self, subtask_summaries: list[str] | None) -> None:
        self._summaries = subtask_summaries

    def load(self, *, force: bool = False) -> Block:
        return Block(
            kind=BlockKind.ASSESSMENT,
            text=render_assessment(self._summaries),
            priority=Priority.ASSESSMENT,
            compressible=False,   # 评估是决策依据（重做/收尾），不压
        )


class ArtifactSource:
    """上一轮已生成图表的摘要（防重复生成同类型图表）。"""

    def __init__(self, note: str) -> None:
        self._note = (note or "").strip()

    def load(self, *, force: bool = False) -> Block:
        if not self._note:
            return Block(kind=BlockKind.ARTIFACT)
        return Block(
            kind=BlockKind.ARTIFACT,
            text=self._note,
            priority=Priority.ARTIFACT,
            compressible=True,
        )
