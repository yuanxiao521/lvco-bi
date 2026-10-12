# -*- coding: utf-8 -*-
"""画布后端唯一落库（P1）测试：CanvasBlockWriter 动作 → DB blocks。

真实集成测试（连本地 PG 容器）：建临时画布 → 依次应用各 canvas_action →
断言 DB blocks 与前端旧链路会产出的结构一致。
"""
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from dotenv import dotenv_values

from app.models.canvas import Canvas
from app.models.user import User
from app.services import canvas_layout_service as laysvc
from app.services.canvas_block_writer import CanvasBlockWriter, maybe_persist

# conftest.py 把 DATABASE_URL setdefault 成 test_db（单元测试隔离约定）；
# 本文件是真实集成测试（需要真实 User/Canvas 数据），自建 engine 直连 .env 的真实库。
_REAL_URL = dotenv_values(".env")["DATABASE_URL"]




ADD_CHART_ACTION = {
    "action": "add_chart_block",
    "block": {
        "title": "销售额趋势",
        "chartType": "line",
        "datasourceId": "11111111-1111-1111-1111-111111111111",
        "queryConfig": {
            "dimensions": ["order_date"],
            "measures": [{"field": "amount", "agg": "SUM"}],
            "filters": [],
            "limit": 20,
        },
        "columns": ["order_date", "amount"],
        "rows": [["2026-01", 100], ["2026-02", 200]],
    },
}


async def _make_canvas(db, user_id) -> Canvas:
    canvas = Canvas(user_id=user_id, title="writer-test", blocks=[])
    db.add(canvas)
    await db.flush()
    return canvas


@pytest.fixture()
async def ctx():
    """真实库 session + test@lvco.bi 用户；每测试独立 engine（asyncpg 连接绑定事件循环）。"""
    import sqlalchemy
    engine = create_async_engine(_REAL_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            user = (await db.execute(
                sqlalchemy.select(User).where(User.email == "test@lvco.bi")
            )).scalar_one()
            canvas = await _make_canvas(db, user.id)
            yield db, user.id, canvas.id
            await db.delete(canvas)
            await db.commit()
    finally:
        await engine.dispose()


async def test_layout_service_parity():
    """布局算法直译一致性：fallbackW 下双列几何与关键槽位符合前端同输入行为。"""
    geo = laysvc.resolve_geometry()  # fallbackW=980
    assert geo["fullW"] == 980
    assert geo["cols"] == 2
    assert geo["chartW"] == 480  # (980-20)/2
    assert geo["leftX"] == 20 and geo["rightX"] == 20 + 480 + 20

    slot = laysvc.next_chart_slot([])
    assert slot == {"x": 20, "y": 32}  # 空画布从 startY 起
    slot2 = laysvc.next_full_width_slot([{"type": "chart", "x": 20, "y": 32,
                                          "width": 480, "height": 320}])
    assert slot2 == {"x": 20, "y": 32 + 320 + 24}  # 通栏 = 两列最深底 + gapY

    labels = laysvc.assign_block_labels([
        {"x": 20, "y": 32}, {"x": 520, "y": 32}, {"x": 20, "y": 376},
    ])
    assert labels == ["A1", "A2", "B1"]


async def test_add_chart_persists_full_block(ctx):
    db, user_id, canvas_id = ctx
    writer = CanvasBlockWriter(db, user_id, str(canvas_id))
    action = await writer.apply(dict(ADD_CHART_ACTION))
    assert action is not None and action["persisted"] is True

    refreshed = await db.get(Canvas, canvas_id)
    assert len(refreshed.blocks) == 1
    b = refreshed.blocks[0]
    assert b["type"] == "chart"
    assert b["blockId"] == action["blockId"]
    assert b["title"] == "销售额趋势"
    assert b["x"] == 20 and b["y"] == 32            # 首块槽位
    assert b["width"] == 480 and b["height"] == 320  # fallbackW 双列
    assert b["_chartConfig"]["chartType"] == "line"
    assert b["_chartResult"]["rows"] == [["2026-01", 100], ["2026-02", 200]]


async def test_add_text_then_chart_slot_sequence(ctx):
    """先文本通栏、再图表 → 图表位置在文本下方（与前端 nextChartSlot 同行为）。"""
    db, user_id, canvas_id = ctx
    writer = CanvasBlockWriter(db, user_id, str(canvas_id))
    await writer.apply({"action": "add_text_block", "block": {"blockType": "text", "content": "标题" * 30}})
    action = await writer.apply(dict(ADD_CHART_ACTION))

    refreshed = await db.get(Canvas, canvas_id)
    assert len(refreshed.blocks) == 2
    text_b, chart_b = refreshed.blocks
    assert text_b["type"] == "text" and text_b["width"] == 980
    # 图表槽位在文本块底部之下（通栏块压双列游标）
    assert chart_b["y"] >= text_b["y"]
    assert action["blockId"] == chart_b["blockId"]


async def test_update_chart_and_arrange(ctx):
    db, user_id, canvas_id = ctx
    writer = CanvasBlockWriter(db, user_id, str(canvas_id))
    action = await writer.apply(dict(ADD_CHART_ACTION))
    block_id = action["blockId"]

    await writer.apply({"action": "update_chart_block", "blockId": block_id,
                        "patch": {"title": "改成折线", "chartType": "line"}})
    refreshed = await db.get(Canvas, canvas_id)
    assert refreshed.blocks[0]["title"] == "改成折线"
    assert refreshed.blocks[0]["_chartConfig"]["chartType"] == "line"

    arranged = await writer.apply({"action": "arrange_layout", "layout": "auto"})
    assert arranged is not None
    refreshed = await db.get(Canvas, canvas_id)
    assert all(isinstance(b.get("x"), (int, float)) for b in refreshed.blocks)
    assert refreshed.blocks[0]["width"] == 980 or refreshed.blocks[0]["width"] == 480


async def test_remove_and_clear(ctx):
    db, user_id, canvas_id = ctx
    writer = CanvasBlockWriter(db, user_id, str(canvas_id))
    action = await writer.apply(dict(ADD_CHART_ACTION))
    block_id = action["blockId"]

    await writer.apply({"action": "remove_block", "blockId": block_id})
    assert len((await db.get(Canvas, canvas_id)).blocks) == 0

    await writer.apply(dict(ADD_CHART_ACTION))
    await writer.apply({"action": "clear_canvas"})
    assert (await db.get(Canvas, canvas_id)).blocks == []


async def test_invalid_chart_rejected_like_frontend(ctx):
    """与前端 add 分支同校验：measures 为空 → 不落块（返回 None）。"""
    db, user_id, canvas_id = ctx
    writer = CanvasBlockWriter(db, user_id, str(canvas_id))
    bad = {"action": "add_chart_block", "block": {"title": "x", "chartType": "bar",
                                                  "queryConfig": {"dimensions": ["d"], "measures": []}}}
    assert await writer.apply(bad) is None
    assert (await db.get(Canvas, canvas_id)).blocks == []


async def test_switch_off_no_persist(ctx, monkeypatch):
    """灰度开关关闭：maybe_persist 返回 None，不写库。"""
    from app.config import settings
    monkeypatch.setattr(settings, "CANVAS_BACKEND_PERSIST", False)
    db, user_id, canvas_id = ctx
    got = await maybe_persist(dict(ADD_CHART_ACTION), user_id=str(user_id),
                              canvas_id=str(canvas_id), db=db)
    assert got is None
    assert (await db.get(Canvas, canvas_id)).blocks == []


async def test_switch_on_via_maybe_persist(ctx, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "CANVAS_BACKEND_PERSIST", True)
    db, user_id, canvas_id = ctx
    got = await maybe_persist(dict(ADD_CHART_ACTION), user_id=str(user_id),
                              canvas_id=str(canvas_id), db=db)
    assert got is not None and got["persisted"] is True
    assert len((await db.get(Canvas, canvas_id)).blocks) == 1
