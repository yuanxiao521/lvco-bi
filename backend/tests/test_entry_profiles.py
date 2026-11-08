# -*- coding: utf-8 -*-
"""P1-D：能力画像（EntryProfile）与子任务回执。

本文件守三件事：
  1. **入口差异只有一处映射**：工具子集 / 配额 / 派发内核 / 验收口径都从画像来，
     对话页看不见画布观察工具（`get_canvas_layout` 在 chat 恒返回空串，是死工具）；
  2. **第三个入口能跑通**：新增入口 = 加一个 EntryProfile，不改任何 if/elif
     （真加一次给测试看：假 profile 的工具表 / 提示词 / 派发 / 评审全部可用）；
  3. **回执带执行摘要**：派发回来给模型的 role=tool 必须是"评审结论 + 执行摘要"，
     只回一句话就是"第 3 次派发重复同一目标"的根因（真机问题）。
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.services.agents.lead.capability import (
    CANVAS,
    CANVAS_PROMPT_KEY,
    CHAT,
    CHAT_PROMPT_KEY,
    CORE_PROMPT_KEY,
    DEFAULT_PROFILE,
    PROFILES,
    ReviewPolicy,
    all_profiles,
    get_profile,
    register_profile,
)
from app.services.agents.lead.lead_agent_tools import (
    DISPATCH_TOOL_NAMES,
    OBSERVE_TOOL_NAMES,
    QUOTA,
    TERMINAL_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    lead_tool_specs,
    tool_class,
)
from app.services.agents.lead.lead_worker import ENTRY_WORKERS, FALLBACK_WORKER, WORKERS


# ── 1. 工具子集：入口差集可枚举，写工具永不出现 ────────────────────────────

def test_chat_and_canvas_tool_sets_differ_only_by_canvas_observer():
    """两个入口的工具差集**恰好**是 get_canvas_layout（不多不少）。

    差集写成"恰好等于"而不是"包含"：将来谁悄悄把画布工具漏给 chat、或把
    对话工具从 canvas 摘掉，这条会立刻变红。
    """
    assert CANVAS.tool_names - CHAT.tool_names == {"get_canvas_layout"}
    assert CHAT.tool_names - CANVAS.tool_names == set()


def test_chat_has_no_dead_canvas_tool():
    """chat 入口不得暴露 get_canvas_layout。

    依据：CanvasStateProvider.enabled 要求 entry=canvas（canvas_state.py:69-71），
    于是 chat 下该方法恒返回空串——留着它只会让模型白调一次并拿到"（空）"。
    """
    assert "get_canvas_layout" not in CHAT.observe_tools
    assert "get_canvas_layout" in CANVAS.observe_tools
    chat_specs = {s["function"]["name"] for s in lead_tool_specs(CHAT)}
    assert "get_canvas_layout" not in chat_specs


def test_profile_tool_sets_are_subsets_of_catalog_and_exclude_write_tools():
    """画像是**子集**：不得凭空造工具名，也不得让写工具进 LLM 调用面。"""
    catalog = OBSERVE_TOOL_NAMES | DISPATCH_TOOL_NAMES | TERMINAL_TOOL_NAMES
    for profile in all_profiles():
        assert profile.tool_names <= catalog, f"{profile.entry} 引用了未登记的工具"
        assert profile.tool_names & WRITE_TOOL_NAMES == set(), f"{profile.entry} 混入写工具"
        # 终止动作三件套必须齐全（否则循环收不了尾）
        assert profile.terminal_tools == TERMINAL_TOOL_NAMES
        # 每个名字都能被判类（循环靠它分流）
        for name in profile.tool_names:
            assert tool_class(name) is not None, f"{name} 不属于任何工具类别"


def test_profile_datasource_visible_in_both_entries():
    """数据画像工具是"数据产品 vs 聊天机器人"的分界线：两入口都要能看见。

    （画布也必须有——选图表前要知道维度基数与时间列范围。）
    """
    for profile in (CHAT, CANVAS):
        assert "profile_datasource" in profile.observe_tools
        names = {s["function"]["name"] for s in lead_tool_specs(profile)}
        assert "profile_datasource" in names


def test_lead_tool_specs_full_catalog_kept_for_backward_compat():
    """不传画像时返回**全目录**（审计脚本/旧测试按目录断言，不能悄悄变窄）。"""
    names = {s["function"]["name"] for s in lead_tool_specs()}
    assert names == (OBSERVE_TOOL_NAMES | DISPATCH_TOOL_NAMES | TERMINAL_TOOL_NAMES)


# ── 2. 配额 / 派发内核：单向引用既有表单，不复制 ───────────────────────────

def test_quota_and_worker_row_are_single_sourced():
    """画像里的配额与派发表必须是**同一对象**（引用），不是复制品。"""
    assert CHAT.quota is QUOTA and CANVAS.quota is QUOTA
    assert CHAT.worker_row is ENTRY_WORKERS["chat"]
    assert CANVAS.worker_row is ENTRY_WORKERS["canvas"]
    assert CHAT.fallback_worker == CANVAS.fallback_worker == FALLBACK_WORKER


def test_profile_resolve_worker_matches_entry_table():
    """画像的派发解析结果必须与 lead_worker 的派发表一致（防两处漂移）。"""
    for profile in (CHAT, CANVAS):
        for complexity in ("simple", "complex", ""):
            got = profile.resolve_worker(complexity).mode
            expect = WORKERS[profile.worker_row.get(complexity or "complex")
                             or profile.fallback_worker].mode
            assert got == expect
    # 显式内核优先于派发表
    assert CHAT.resolve_worker("complex", explicit="react").mode == "react"
    assert CANVAS.resolve_worker("simple", explicit="canvas").mode == "canvas"


# ── 3. 提示词按画像取：core + profile，chat 不含画布小节 ────────────────────

def test_profile_prompt_keys_point_to_three_segments():
    assert CHAT.prompt_keys() == (CORE_PROMPT_KEY, CHAT_PROMPT_KEY)
    assert CANVAS.prompt_keys() == (CORE_PROMPT_KEY, CANVAS_PROMPT_KEY)
    assert CHAT.core_prompt_key == CANVAS.core_prompt_key == CORE_PROMPT_KEY


def test_chat_prompt_has_no_canvas_section():
    """对话页的 **profile 段**不得含画布专属小节；画布段必须含（判据自证）。

    注意"落块 / add_chart_block"不能当判据：它们在 core 段的「写工具对你不可见」里
    作为**反例**被点名（两入口都必须知道"这个工具我调不到"）。判据取画布**独有**的
    动作语义：画布观察工具、文本块工具、布局整理、KPI 卡片、canvas_action 事件。
    """
    from app.services.ai_prompts import CANVAS_PROFILE, CHAT_PROFILE

    canvas_only = (
        "get_canvas_layout", "add_text_block", "arrange_layout",
        "KPI 卡片", "canvas_action",
    )
    for token in canvas_only:
        assert token in CANVAS_PROFILE, f"判据失效：canvas 段里也没有 {token!r}"
        assert token not in CHAT_PROFILE, f"chat 段混入画布专属内容：{token!r}"
    # 组合后同样不得出现画布观察工具
    assert "get_canvas_layout" not in CHAT.system_prompt()


def test_canvas_prompt_declares_block_delivery():
    """画布提示词必须讲清落块交付与破坏性操作确认（否则模型当纯问答干）。"""
    text = CANVAS.system_prompt()
    for token in ("get_canvas_layout", "add_chart_block", "落块", "confirm"):
        assert token in text, f"canvas 提示词缺少关键约定：{token}"


def test_both_prompts_share_the_core_segment():
    """两入口的共性约束必须来自**同一段 core**（写边界/ask_kind 不能各写一份）。"""
    core_markers = ("写工具对你不可见", "ask_kind", "破坏性操作")
    for text in (CHAT.system_prompt(), CANVAS.system_prompt()):
        for token in core_markers:
            assert token in text, f"core 段未进入 {token}"


# ── 4. 第三入口：加一个画像就能跑通（不改 if/elif）──────────────────────────

def test_third_entry_profile_runs_end_to_end():
    """"报告页"假入口：工具表 / 提示词 / 派发 / 评审四件事全部可用。"""
    third = replace(
        CHAT,
        entry="report",
        profile_prompt_key=CHAT_PROMPT_KEY,   # 借用现有段，重点是"不再有 if 分支"
        observe_tools=CHAT.observe_tools,
        artifacts=frozenset({"report"}),
        review_policy=ReviewPolicy(numbers_mode="always"),
    )
    register_profile(third)
    try:
        assert get_profile("report") is third
        # 工具表：按画像过滤后仍是合法目录子集，且不含画布观察工具
        specs = lead_tool_specs(third)
        names = {s["function"]["name"] for s in specs}
        assert names == set(third.tool_names)
        assert "get_canvas_layout" not in names
        # 提示词：core + profile 组合成功
        assert third.system_prompt().strip()
        # 派发：走自己的派发表（这里沿用 chat 的那一行）
        assert third.resolve_worker("simple").mode in WORKERS
        # 评审：策略生效（对话口径：零块不算硬失败）
        from app.services.agents.lead.lead_tools import RunAnalysisResult, assess_subtask_detail

        res = RunAnalysisResult(success=True, report="x", blocks_added=0)
        assert assess_subtask_detail(res, entry="report", user_goal="").hard == []
    finally:
        PROFILES.pop("report", None)


def test_unknown_entry_falls_back_to_default_profile():
    """未登记入口 → 默认画像，不抛错（派发/装配不该让整轮失败）。"""
    assert get_profile("no_such_entry") is DEFAULT_PROFILE
    assert get_profile(None) is DEFAULT_PROFILE
    assert get_profile("") is DEFAULT_PROFILE
    assert get_profile("CHAT  ") is CHAT          # 归一化：大小写/空格


# ── 5. 验收口径：policy 与旧行为逐条等价（文本兼容是硬要求）───────────────

def _result(**kw):
    from app.services.agents.lead.lead_tools import RunAnalysisResult

    base = dict(
        success=True, report="r", verified=True, blocks_added=0,
        tool_chain=[{"name": "query_sql", "ok": True}],
    )
    base.update(kw)
    return RunAnalysisResult(**base)


def test_review_policy_matches_legacy_behavior():
    """四条判定的入口差异必须与重构前**完全一致**（否则评测口径会漂）。"""
    from app.services.agents.lead.lead_tools import assess_subtask_detail

    # chat：零块不是失败；数字未验证一律 SOFT
    chat_zero = assess_subtask_detail(_result(blocks_added=0), entry="chat")
    assert chat_zero.hard == [] and chat_zero.soft == []
    chat_unverified = assess_subtask_detail(_result(verified=False), entry="chat")
    assert chat_unverified.soft == ["报告数字存疑"]
    # canvas：成功却零块且无变更 = HARD
    canvas_zero = assess_subtask_detail(_result(blocks_added=0), entry="canvas")
    assert canvas_zero.hard == ["未在画布上产出内容块"]
    # canvas：清空类（有变更操作）→ 豁免
    canvas_clear = assess_subtask_detail(
        _result(blocks_added=0, tool_chain=[{"name": "clear_canvas", "ok": True}]),
        entry="canvas",
    )
    assert canvas_clear.hard == []
    # canvas：已落块时数字存疑只算 SOFT（不敲成失败）
    canvas_unverified = assess_subtask_detail(
        _result(blocks_added=2, verified=False), entry="canvas"
    )
    assert canvas_unverified.hard == [] and canvas_unverified.soft == []


def test_review_policy_flags_when_artifacts_expect_blocks():
    """产物白名单与"零产物是否 HARD"必须自洽（配置错了测试先红）。"""
    assert "block" in CANVAS.artifacts and CANVAS.review_policy.hard_zero_artifact
    assert "block" not in CHAT.artifacts and not CHAT.review_policy.hard_zero_artifact


# ── 6. 子任务回执：执行摘要必须回给模型 ───────────────────────────────────

def test_dispatch_receipt_carries_execution_summary():
    from app.services.agents.lead.lead_agent_unified import build_dispatch_receipt

    note = "子任务评审未通过：存在失败工具"
    summary = "[子任务执行记录]\n  动作：分析完成"
    got = build_dispatch_receipt(note, summary)
    assert got.startswith(note)
    assert "[子任务执行记录]" in got
    assert "分析完成" in got


def test_dispatch_receipt_truncates_to_tool_result_budget():
    from app.services.agents.lead.lead_agent_unified import build_dispatch_receipt

    got = build_dispatch_receipt("note", "x" * 5000, max_chars=100)
    assert len(got) <= len("note") + 2 + 100
    assert got.endswith("…"), "超预算必须截断并留省略号（可观测的降级痕迹）"


def test_dispatch_receipt_without_summary_keeps_note_only():
    from app.services.agents.lead.lead_agent_unified import build_dispatch_receipt

    assert build_dispatch_receipt("only note", "") == "only note"
    assert build_dispatch_receipt("", "") == ""


# ── 7. profile_datasource：SQL 构造 + 分派（不连真库）──────────────────────

def test_profile_sql_is_single_scan_and_column_typed():
    """画像 SQL：一次扫描、按类型选聚合、别名用下标（不受中文/空格列名影响）。"""
    from app.services.agents.lead.lead_observe import profile_sql

    sql = profile_sql('"sch"."data"', [
        {"name": "订单日期", "data_type": "DATE", "category": "dimension"},
        {"name": "销售额", "data_type": "DOUBLE", "category": "measure"},
        {"name": "备注", "data_type": "VARCHAR", "category": "dimension"},
    ])
    assert sql.count("FROM") == 1, "画像必须一次扫描算完，不能每列一条查询"
    assert 'COUNT(*) AS "total_rows"' in sql
    assert 'COUNT(DISTINCT "订单日期") AS "c0__nd"' in sql
    assert 'MIN("订单日期") AS "c0__min"' in sql          # 时间列要 min/max
    assert 'AVG(TRY_CAST("销售额" AS DOUBLE)) AS "c1__avg"' in sql  # 数值列要均值
    assert '"c2__min"' not in sql, "文本列不该做 MIN/MAX（比较无意义且可能抛错）"
    assert "SELECT *" not in sql.upper()


def test_profile_sql_empty_input_returns_empty():
    from app.services.agents.lead.lead_observe import profile_sql

    assert profile_sql("", [{"name": "a", "data_type": "INT"}]) == ""
    assert profile_sql('"s"."data"', []) == ""


async def test_recon_tool_routes_profile_datasource(monkeypatch):
    """recon_tool 的分派把 profile_datasource 接到真实实现并组装成画像 payload。"""
    from app.services import agent_tools
    from app.services.agents.lead import lead_observe

    calls: list[dict] = []

    class _FakeTool:
        async def execute(self, **kwargs):
            calls.append(kwargs)
            return json.dumps({
                "columns": [
                    "total_rows",
                    "c0__nd", "c0__nulls", "c0__min", "c0__max",
                    "c1__nd", "c1__nulls", "c1__min", "c1__max", "c1__avg",
                ],
                "rows": [[1000, 12, 0, "2023-01-01", "2024-12-31",
                          50, 100, 0.0, 90000.0, 4321.5]],
            })

    async def _fake_datasources(*_a, **_kw):
        return [{
            "id": "ds-1", "name": "销售库", "table_ref": '"sch"."data"',
            "fields": [
                {"name": "order_date", "data_type": "DATE", "category": "dimension"},
                {"name": "amount", "data_type": "DOUBLE", "category": "measure"},
            ],
        }]

    monkeypatch.setattr(agent_tools, "QueryDatasourceTool", _FakeTool)
    monkeypatch.setattr(lead_observe, "list_datasources", _fake_datasources)

    class _Ctx:
        entry = "chat"
        user_id = "u1"
        datasource_id = "ds-1"
        canvas_state = None
        metrics_ctx = ""

    degradation: list[str] = []
    got = await lead_observe.recon_tool(
        "profile_datasource", ctx=_Ctx(), args={}, db_session=object(),
        degradation=degradation,
    )
    assert got is not None
    payload = json.loads(got)
    assert payload["ok"] is True
    assert payload["row_count"] == 1000
    assert payload["columns"][0]["name"] == "order_date"
    assert payload["columns"][0]["min"] == "2023-01-01"
    assert payload["columns"][1]["distinct"] == 50
    assert payload["columns"][1]["null_rate"] == 0.1
    assert payload["columns"][1]["avg"] == 4321.5
    assert payload["highlights"], "画像必须给出可读要点（时间范围/低基数列/空值列）"
    assert "不是样本" in payload["note"]
    assert calls, "必须真的走 QueryDatasourceTool（复用 guard/归属校验/超时）"


async def test_profile_datasource_degrades_when_datasource_missing(monkeypatch):
    """找不到数据源 → 返回 None + 降级标记（绝不伪造统计）。"""
    from app.services.agents.lead import lead_observe

    async def _empty(*_a, **_kw):
        return []

    monkeypatch.setattr(lead_observe, "list_datasources", _empty)
    degradation: list[str] = []
    got = await lead_observe.profile_datasource(None, "u1", "ds-x", degradation=degradation)
    assert got is None
    assert "lead_agent_observe_profile_datasource_not_found" in degradation
