# -*- coding: utf-8 -*-
"""防「同一事实写两处」的漂移守卫。

本项目有两类双份声明，都是"改一处忘一处就静默劣化"的隐患：

1. **默认值双份**：`getattr(settings, "X", D)` 的 D 与 `config.X` 的定义值。
   实测踩过：lead_agent 写 `getattr(..., "LEAD_MAX_SUPERVISOR_ROUNDS", 4)`，
   而 config 是 3——当前有配置所以是 3，**一旦配置被删就悄悄变 4 轮**。

2. **提示词双份**：`prompts/*.yaml`（运行时生效）与 ai_prompts 的 Python fallback
   常量（yaml 加载失败才用）。实测踩过：fallback 声明了 `ask_kind`，
   而两份 yaml 都漏了——于是运行时 LLM 从不输出该字段，解析端只能拿到默认值
   `clarify`，**confirm 确认卡片的生产链路永远不触发**。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings


# ── 1. 默认值双份 ───────────────────────────────────────────────────────────

_GETATTR_RE = re.compile(r'getattr\(\s*settings\s*,\s*"([A-Z_][A-Z0-9_]*)"\s*,\s*([^),]+?)\s*\)')


def test_getattr_defaults_match_config():
    """扫描全仓 `getattr(settings, "NAME", DEFAULT)`，DEFAULT 必须等于 config 的定义值。"""
    declared = {name: f.default for name, f in Settings.model_fields.items()}
    root = Path(__file__).resolve().parents[1] / "app"

    mismatches: list[str] = []
    checked = 0
    for path in root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for m in _GETATTR_RE.finditer(src):
            name, raw = m.group(1), m.group(2).strip()
            if name not in declared:
                continue          # 不是 config 字段（如第三方 settings），跳过
            literal: object
            if raw == "None":
                literal = None
            elif raw == "True":
                literal = True
            elif raw == "False":
                literal = False
            elif raw.isdigit():
                literal = int(raw)
            else:
                continue          # 非字面量（变量/表达式）无法静态比对
            checked += 1
            if literal != declared[name]:
                mismatches.append(
                    f"{path.relative_to(root)}: getattr({name}, {raw}) "
                    f"与 config 的 {declared[name]!r} 不一致"
                )

    assert checked > 0, "没扫到任何 getattr 默认值，正则可能失效（守卫空转）"
    assert not mismatches, "默认值与 config 漂移：\n" + "\n".join(mismatches)


# ── 2. 提示词双份 ───────────────────────────────────────────────────────────

def test_decision_prompts_declare_ask_kind():
    """决策提示词必须声明 ask_kind。

    解析端（lead_decider）读该字段并默认 `clarify`；lead_agent 只在
    `ask_kind == "confirm"` 时发 confirm_request 事件。**提示词不声明 → 模型不输出
    → 永远走 clarify → 前端确认卡片从不出现**（HITL 链路静默失效）。
    """
    from app.services.ai_prompts import (
        LEAD_DECISION_SYSTEM,
        LEAD_MERGED_SYSTEM,
        LEAD_MERGED_SYSTEM_CANVAS,
    )

    for name, txt in (
        ("LEAD_DECISION_SYSTEM", LEAD_DECISION_SYSTEM),
        ("LEAD_MERGED_SYSTEM", LEAD_MERGED_SYSTEM),
        ("LEAD_MERGED_SYSTEM_CANVAS", LEAD_MERGED_SYSTEM_CANVAS),
    ):
        assert "ask_kind" in txt, f"{name} 未声明 ask_kind（confirm 卡片链路会失效）"
        assert "confirm" in txt, f"{name} 未说明 confirm 语义"
        assert "clarify" in txt, f"{name} 未说明 clarify 语义"


def test_prompts_loaded_from_yaml_not_fallback():
    """yaml 必须真的被加载到（否则一直在用 Python fallback，yaml 改了也不生效）。"""
    from app.services.ai_prompts import LEAD_MERGED_SYSTEM
    # yaml 版本包含"输出 Schema"标题；fallback 版式不同——用长度做粗判（yaml > 3000 字）
    assert len(LEAD_MERGED_SYSTEM) > 1000, "疑似加载到了空的/异常的提示词"
