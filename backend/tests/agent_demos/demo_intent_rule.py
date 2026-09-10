"""D1.1 意图识别 · 纯规则分类器基线。

目标：不接 LLM，只用关键词/规则把用户消息归类到五类意图，跑 10 条样本，
看纯规则路线的准确率基线，以及哪类样本必挂。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_intent_rule.py

复用真实兜底逻辑 `lead_intent._rule_intent`——它正是 LLM 不可用时的降级分类器。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.lead.lead_intent import IntentType, _rule_intent  # noqa: E402

SAMPLES = [
    ("你好，介绍一下你自己", IntentType.CHAT, ""),
    ("这个系统怎么用", IntentType.CHAT, ""),
    ("本月总销售额是多少", IntentType.DATA_QA, ""),
    ("按地区统计订单数量", IntentType.DATA_QA, ""),
    ("在画布上新增一个图表块", IntentType.CANVAS_EDIT, ""),
    ("把销售看板删掉重做", IntentType.CANVAS_EDIT, ""),
    ("帮我做一份完整的销售分析报告", IntentType.ANALYSIS, ""),
    ("对华东区做同比归因诊断", IntentType.ANALYSIS, ""),
    ("那再继续看下华北区", IntentType.FOLLOWUP, "用户此前问过华东区销售"),
    ("换成按月的口径", IntentType.FOLLOWUP, "用户此前问过销售数据"),
]


def main() -> None:
    hits = 0
    print(f"{'用户消息':<26} {'期望':<12} {'规则判定':<12} {'命中'}")
    print("-" * 66)
    for msg, expected, hist in SAMPLES:
        intent, conf = _rule_intent(msg, hist)
        ok = intent == expected
        hits += int(ok)
        print(f"{msg:<26} {expected.value:<12} {intent.value:<12} {'✅' if ok else '❌'}  conf={conf}")
    total = len(SAMPLES)
    print("-" * 66)
    print(f"规则基线准确率：{hits}/{total} = {hits / total:.0%}")
    print("观察点：FOLLOWUP 依赖 history_summary；一旦措辞不含关键词，规则必挂。")


if __name__ == "__main__":
    main()
