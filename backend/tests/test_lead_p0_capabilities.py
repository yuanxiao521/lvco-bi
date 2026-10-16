# -*- coding: utf-8 -*-
"""P0 能力接口：评审结构化（HARD/SOFT 分级）。

【背景】P0 的目标是"把将来要变成工具的能力抽成显式函数，**行为零变化**"。
评审这一项要求：`assess_subtask` 的返回从"一段文本"扩为 `{text, hard[], soft[]}`，
但**文本格式必须保持兼容**（旧通道写入 turn_summaries，既有断言直接对字符串做 in 判断）。

【本次做法】不改旧函数签名，新增 `assess_subtask_detail` 返回 `SubtaskAssessment`；
`assess_subtask` 变成取 `.text` 的薄封装。逐字节等价已用"从 git HEAD 取旧实现
对拍 320 组输入"验证（scripts/_verify_p0_review_equiv.py）。

【为什么现在就要分级】P2 的评审阶段：HARD 是硬闸（非空则 verdict 强制 fail，
Lead 不得翻案，零 LLM）；只有 SOFT 非空时才触发一次裁量调用。
"""
from __future__ import annotations

from app.services.agents.lead.lead_review import review_subtask_detail
from app.services.agents.lead.lead_tools import (
    RunAnalysisResult,
    assess_subtask,
    assess_subtask_detail,
)


def _res(**kw) -> RunAnalysisResult:
    base = dict(success=True, report="r", steps=[], blocks_added=1,
                error=None, verified=True, tool_chain=[{"name": "run_query", "ok": True}])
    base.update(kw)
    return RunAnalysisResult(**base)


def test_text_is_identical_to_legacy_string_api():
    """薄封装：detail.text 必须与旧 assess_subtask 的返回值完全一致。"""
    cases = [
        (_res(), "chat", ""),
        (_res(success=False, error="超时"), "canvas", ""),
        (_res(verified=False), "chat", ""),
        (_res(blocks_added=0, tool_chain=[]), "canvas", ""),
        (_res(tool_chain=[{"name": "a", "ok": False}]), "chat", ""),
    ]
    for r, entry, goal in cases:
        d = assess_subtask_detail(r, entry=entry, user_goal=goal)
        assert d.text == assess_subtask(r, entry=entry, user_goal=goal)


def test_hard_facts_are_classified_hard():
    """HARD = 事实性失败：执行失败 / 工具链失败 / 画布零落块。"""
    d = assess_subtask_detail(
        _res(success=False, error="查询超时",
             tool_chain=[{"name": "run_query", "ok": False}]),
        entry="canvas", user_goal="",
    )
    assert any("执行失败" in h for h in d.hard)
    assert any("存在失败工具" in h for h in d.hard)
    assert d.soft == []
    assert d.passed is False

    zero = assess_subtask_detail(_res(blocks_added=0, tool_chain=[]), entry="canvas", user_goal="")
    assert any("未在画布上产出内容块" in h for h in zero.hard)
    assert zero.passed is False


def test_soft_items_are_classified_soft():
    """SOFT = 存疑，需质量判断：报告数字未过后验 / 维度覆盖不全。"""
    unverified = assess_subtask_detail(_res(verified=False), entry="chat", user_goal="")
    assert unverified.hard == []
    assert any("报告数字存疑" in s for s in unverified.soft)
    assert unverified.passed is False

    # 注意：_count_named_dimensions 只认"…三个维度"这类说法，
    # "按品类和品牌分别分析" 会被它判为 0 个维度（实测），所以这里用能触发的措辞。
    partial = assess_subtask_detail(
        _res(blocks_added=2,
             tool_chain=[{"name": "add_chart_block", "ok": True,
                          "args": '{"dimensions": ["category"]}'}]),
        entry="canvas", user_goal="按品类、品牌、季节三个维度分析",
    )
    assert partial.hard == []
    assert any("维度覆盖不全" in s for s in partial.soft)


def test_canvas_exemption_unchanged():
    """画布豁免照旧：画布已产出内容块时，"数字未过后验"不算问题（硬软都不算）。"""
    d = assess_subtask_detail(_res(verified=False, blocks_added=3), entry="canvas", user_goal="")
    assert d.hard == [] and d.soft == []
    assert d.passed is True


def test_passed_semantics():
    """达标 = 无 HARD 且无 SOFT。"""
    ok = assess_subtask_detail(_res(), entry="chat", user_goal="")
    assert ok.hard == [] and ok.soft == []
    assert ok.passed is True
    assert "上轮达标" in ok.text


async def test_get_canvas_layout_degrades_to_empty():
    """观察能力：无画布上下文 / provider 抛错 → 空串。

    §6 不变量 4：每个新环节都要有确定性降级——观察失败绝不能把主循环带崩。
    """
    from app.services.agents.lead.lead_observe import get_canvas_layout

    class _NoCanvas:
        canvas_state = None

    class _Boom:
        async def layout(self, force: bool = False):
            raise RuntimeError("db down")

    class _Ok:
        async def layout(self, force: bool = False):
            return "画布：1 个图表块"

    assert await get_canvas_layout(_NoCanvas()) == ""
    assert await get_canvas_layout(type("C", (), {"canvas_state": _Boom()})()) == ""
    assert await get_canvas_layout(type("C", (), {"canvas_state": _Ok()})()) == "画布：1 个图表块"


async def test_list_datasources_and_fields_degrade_to_empty():
    """数据源/字段观察：底层失败 → 空列表（不抛）。"""
    from app.services.agents.lead.lead_observe import list_datasources, list_fields

    # user_id 不是合法 UUID → 底层抛错 → 必须被吞掉并返回空
    assert await list_datasources(object(), "not-a-uuid") == []
    assert await list_fields(object(), "not-a-uuid", 1) == []


def test_run_worker_uses_the_single_dispatch_core():
    """派发路径唯一性守卫。

    P0 把派发内核抽成 dispatch_analysis；run_worker（流式路径）必须复用它，
    而不是自己再调一次 run_analysis——两份派发实现必然跑偏
    （本项目已在"画布事件表"上踩过一次同样的坑）。
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app/services/agents/lead/lead_dispatch.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run_worker")
    body = ast.unparse(fn)
    assert "dispatch_analysis(" in body, "run_worker 必须复用派发内核"
    assert "run_analysis(" not in body, "run_worker 不得绕过 dispatch_analysis 直接调 run_analysis"


def test_for_agent_loop_is_a_noop_alias_of_for_decision():
    """P0 装配出口：for_agent_loop(step) 的行为必须与 for_decision 完全一致。

    P4 才会让它「按循环步装配」；在那之前它只是占位出口——任何行为差异都说明
    有人在 P0 阶段偷偷改了装配语义（这正是本用例要拦住的事）。
    """
    from app.services.context.provider import TurnContextProvider, TurnInputs

    inputs = TurnInputs(
        user_id="u", session_id="s", entry="canvas",
        turns=[{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}],
        history_summary="历史摘要",
        subtask_summaries=["[子任务评估] 上轮达标：工具调用全部成功。"],
        datasources=[{"id": "d1", "name": "DS"}],
        metrics_ctx="指标口径",
        canvas_layout="画布：1 个图表块",
        artifact_note="产物说明",
    )
    p = TurnContextProvider.for_turn(inputs)
    base = p.for_decision("按品类分析")

    same = p.for_agent_loop("按品类分析")
    assert same.messages == base.messages
    assert same.parts == base.parts
    assert same.ledger == base.ledger

    # step 在 P0 被忽略：换 step 也必须给出同一装配结果
    stepped = p.for_agent_loop("按品类分析", step=3)
    assert stepped.messages == base.messages
    assert stepped.parts == base.parts


def test_review_subtask_detail_is_exported_from_review_module():
    """P2 会从评审模块取结构化结果，这里钉住出口存在且与底层一致。"""
    r = _res(verified=False)
    d = review_subtask_detail(r, entry="chat", user_goal="")
    assert d.text == assess_subtask_detail(r, entry="chat", user_goal="").text
    assert d.soft and not d.hard
