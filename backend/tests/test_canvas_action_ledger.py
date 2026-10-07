"""画布动作台账 + 工具防重复/存在性校验的回归测试。

对应线上反馈："模型删完了又反问用户要删哪些块，还重复写同样的文本"。
根因：画布工具不写库，get_canvas_layout 读到的库内快照落后于前端自动保存，
模型看不到自己的动作 → 重复删/重复加/回头反问。
"""
import json

import pytest

from app.services import canvas_tools as ct


H1 = {"type": "h1", "blockId": "h1_a", "content": "Ecommerce Orders 数据概览", "x": 20, "y": 32, "width": 980, "height": 58}
CHART = {"type": "chart", "blockId": "chart_a", "title": "每日订单量趋势", "chartType": "line", "x": 20, "y": 120, "width": 480, "height": 320}
TEXT = {"type": "text", "blockId": "text_a", "content": "整体呈上升趋势", "x": 20, "y": 500, "width": 980, "height": 62}


@pytest.fixture
def canvas(monkeypatch):
    """把"读库"换成固定块列表，测试不依赖数据库。"""
    blocks = [dict(H1), dict(CHART), dict(TEXT)]

    async def fake_load(canvas_id, user_id, db_session):
        return [dict(b) for b in blocks]

    monkeypatch.setattr(ct, "_load_canvas_blocks", fake_load)
    return blocks


def _payload(raw: str) -> dict:
    return json.loads(raw)


# ── 台账本身的语义 ──

def test_ledger_overlay_hides_removed_and_appends_added():
    ledger = ct.CanvasActionLedger()
    ledger.record_remove("text_a")
    ledger.record_add({"action": "add_text_block", "block": {"blockType": "h2", "content": "📊 每日订单量趋势"}})

    out = ledger.overlay([dict(H1), dict(CHART), dict(TEXT)])

    assert [ct._block_id(b) for b in out if ct._block_id(b)] == ["h1_a", "chart_a"]  # 已删的不出现
    assert out[-1]["content"] == "📊 每日订单量趋势"                                  # 新增的追加在末尾


def test_ledger_describe_states_actions_are_done():
    ledger = ct.CanvasActionLedger()
    ledger.record_remove("text_a")
    ledger.record_add({"action": "add_text_block", "block": {"blockType": "h1", "content": "新报告"}})

    note = ledger.describe()
    assert "不要重复执行" in note
    assert "已删除 1 个块" in note and "text_a" in note
    assert "已新增 1 个文本块" in note and "新报告" in note


# ── 工具：新增防重复 ──

@pytest.mark.asyncio
async def test_add_text_block_skips_duplicate_content(canvas):
    """画布上已有同样内容 → 不再产出 canvas_action（否则会重复写块）。"""
    tool = ct.AddTextBlockTool()
    raw = await tool.execute(block_type="h1", content="Ecommerce Orders 数据概览",
                             user_id="u1", db_session=object(), canvas_id="c1")
    payload = _payload(raw)
    assert payload.get("skipped") is True
    assert "canvas_action" not in payload
    assert "h1_a" in payload["reason"]


@pytest.mark.asyncio
async def test_add_text_block_skips_same_run_repeat(canvas):
    """本轮已加过同样内容（库内还没有）→ 也要拦下。"""
    tool = ct.AddTextBlockTool()
    ledger = ct.CanvasActionLedger()
    first = _payload(await tool.execute(block_type="h2", content="章节：渠道表现",
                                        user_id="u1", db_session=object(), canvas_id="c1",
                                        canvas_ledger=ledger))
    assert first.get("canvas_action")
    second = _payload(await tool.execute(block_type="h2", content="章节：渠道表现",
                                        user_id="u1", db_session=object(), canvas_id="c1",
                                        canvas_ledger=ledger))
    assert second.get("skipped") is True


@pytest.mark.asyncio
async def test_add_text_block_allows_new_content(canvas):
    tool = ct.AddTextBlockTool()
    payload = _payload(await tool.execute(block_type="h2", content="全新章节",
                                         user_id="u1", db_session=object(), canvas_id="c1",
                                         canvas_ledger=ct.CanvasActionLedger()))
    assert payload["canvas_action"]["action"] == "add_text_block"


# ── 工具：删除的存在性校验 ──

@pytest.mark.asyncio
async def test_remove_block_blocks_unknown_id(canvas):
    """不存在的 id → 不发 canvas_action，避免"重复删除 + 前端假回执"。"""
    tool = ct.RemoveBlockTool()
    payload = _payload(await tool.execute(block_id="text_not_exist", user_id="u1",
                                         db_session=object(), canvas_id="c1",
                                         canvas_ledger=ct.CanvasActionLedger()))
    assert payload["ok"] is False
    assert "不在当前画布上" in payload["error"]
    assert "canvas_action" not in payload


@pytest.mark.asyncio
async def test_remove_block_blocks_double_delete_in_same_run(canvas):
    tool = ct.RemoveBlockTool()
    ledger = ct.CanvasActionLedger()
    first = _payload(await tool.execute(block_id="text_a", user_id="u1", db_session=object(),
                                        canvas_id="c1", canvas_ledger=ledger))
    assert first.get("canvas_action"), first
    second = _payload(await tool.execute(block_id="text_a", user_id="u1", db_session=object(),
                                         canvas_id="c1", canvas_ledger=ledger))
    assert second["ok"] is False
    assert "已经被删除过" in second["error"]


@pytest.mark.asyncio
async def test_remove_block_blocks_id_already_gone_in_db(canvas, monkeypatch):
    """库内已无该块（上一轮删掉的）→ 同样拦下，不问用户也不重复发动作。"""
    async def fake_load(canvas_id, user_id, db_session):
        return [dict(H1)]

    monkeypatch.setattr(ct, "_load_canvas_blocks", fake_load)
    tool = ct.RemoveBlockTool()
    payload = _payload(await tool.execute(block_id="text_a", user_id="u1", db_session=object(),
                                         canvas_id="c1", canvas_ledger=ct.CanvasActionLedger()))
    assert payload["ok"] is False and "不在当前画布上" in payload["error"]


# ── get_canvas_layout：快照必须反映本轮动作 ──

@pytest.mark.asyncio
async def test_layout_reflects_this_run_actions(canvas):
    """删掉文本 + 新增标题后查布局：不应再出现被删的块，且末尾写明本轮已执行的动作。"""
    tool = ct.GetCanvasLayoutTool()
    ledger = ct.CanvasActionLedger()
    await ct.RemoveBlockTool().execute(block_id="text_a", user_id="u1", db_session=object(),
                                       canvas_id="c1", canvas_ledger=ledger)
    await ct.AddTextBlockTool().execute(block_type="h2", content="渠道表现", user_id="u1",
                                        db_session=object(), canvas_id="c1", canvas_ledger=ledger)

    payload = _payload(await tool.execute(user_id="u1", db_session=object(), canvas_id="c1",
                                         canvas_ledger=ledger))
    layout = payload["layout"]

    assert "id=text_a" not in layout, "本轮已删除的块不应再出现在快照里"
    assert "整体呈上升趋势" not in layout
    assert "渠道表现" in layout, "本轮新增的块要出现在快照里"
    assert "不要重复执行" in layout and "已删除 1 个块" in layout


@pytest.mark.asyncio
async def test_layout_without_ledger_still_works(canvas):
    """没注入台账（普通对话入口）时退化为原行为，不报错。"""
    payload = _payload(await ct.GetCanvasLayoutTool().execute(
        user_id="u1", db_session=object(), canvas_id="c1"))
    assert payload["ok"] is True
    assert "每日订单量趋势" in payload["layout"]


@pytest.mark.asyncio
async def test_layout_missing_context_returns_error():
    payload = _payload(await ct.GetCanvasLayoutTool().execute(user_id="", db_session=None))
    assert "error" in payload