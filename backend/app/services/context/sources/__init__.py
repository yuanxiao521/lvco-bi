"""上下文块的数据源（Source 层）。

铁律：**Source 只取不裁**——产出"完整内容"，绝不截断、绝不感知预算、绝不拼 messages。
P0：全部转调既有实现，保证行为零变化；缓存语义与 CanvasStateProvider 一致
（请求级一次加载 + 按需 force 重读）。
"""
from __future__ import annotations

from app.services.context.sources.memory import MemorySource
from app.services.context.sources.state import (
    AssessmentSource,
    ArtifactSource,
    CanvasSource,
    DatasourceSource,
    MetricsSource,
    SubtaskSource,
)
from app.services.context.sources.window import WindowSource, select_for_answer

__all__ = [
    "AssessmentSource",
    "ArtifactSource",
    "CanvasSource",
    "DatasourceSource",
    "MemorySource",
    "MetricsSource",
    "SubtaskSource",
    "WindowSource",
    "select_for_answer",
]
