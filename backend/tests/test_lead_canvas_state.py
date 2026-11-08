# -*- coding: utf-8 -*-
"""画布状态感知（CanvasStateProvider）测试。

背景：此前画布感知是"能力层定义 + 决策/执行层逐点注入"——4 处独立注入、两种形态
（canvas_layout 参数 / 手工拼进 history），于是新增分支必然漏（_answer_branch 就漏了），
且同一轮最多重复读库 3 次。本文件锁住收敛后的行为：

1. 请求级缓存：同一轮多处消费只读一次；落块后必须 force=True 才重读；
2. 不可用场景（非画布入口 / 无 canvas_id / 无 db）返回空，不报错；
3. "画布不存在"与"画布为空"语义不同：前者不产出任何布局文本；
4. narrative 惰性加载 + 重读布局后失效重取。
"""

from app.services.agents.lead.canvas_state import CanvasStateProvider

BLOCKS = [
    {"id": "c1", "type": "chart", "title": "销售额柱状图", "chartType": "bar"},
    {"id": "t1", "type": "text", "title": "东部 35.5 万居首"},
]


def _patch_blocks(monkeypatch, blocks, counter: dict):
    import app.services.agents.lead.lead_tools as tools_mod

    async def _fake(*a, **kw):  # noqa: ANN002
        counter["n"] = counter.get("n", 0) + 1
        return blocks

    monkeypatch.setattr(tools_mod, "_load_canvas_blocks", _fake)


async def test_provider_caches_within_request(monkeypatch):
    counter: dict = {}
    _patch_blocks(monkeypatch, BLOCKS, counter)

    p = CanvasStateProvider(object(), "u1", "c1", entry="canvas")
    assert p.enabled is True
    first = await p.layout()
    second = await p.layout()
    assert first == second and "销售额柱状图" in first
    assert counter["n"] == 1                       # 同轮两次消费只读一次库

    await p.layout(force=True)
    assert counter["n"] == 2                       # 落块后 force 才重读


async def test_provider_disabled_returns_empty():
    # 非画布入口 / 无 canvas_id / 无 db → 一律不可用，且不抛错
    assert await CanvasStateProvider(object(), "u1", "c1", entry="chat").layout() == ""
    assert await CanvasStateProvider(object(), "u1", None, entry="canvas").layout() == ""
    assert await CanvasStateProvider(None, "u1", "c1", entry="canvas").layout() == ""
    assert CanvasStateProvider(object(), "u1", None, entry="canvas").enabled is False


async def test_missing_canvas_vs_empty_canvas(monkeypatch):
    """画布不存在 → 不产出布局文本；画布存在但为空 → 正常产出"画布为空"提示。"""
    counter: dict = {}
    _patch_blocks(monkeypatch, None, counter)          # None = 画布不存在
    p = CanvasStateProvider(object(), "u1", "c1", entry="canvas")
    assert await p.layout() == ""
    st = await p.state()
    assert st is not None and st.layout_text == "" and st.block_count == 0

    counter2: dict = {}
    _patch_blocks(monkeypatch, [], counter2)           # [] = 画布存在但为空
    p2 = CanvasStateProvider(object(), "u1", "c1", entry="canvas")
    layout = await p2.layout()
    assert "0 个块" in layout and "画布为空" in layout


async def test_narrative_lazy_loaded_and_invalidated(monkeypatch):
    counter: dict = {}
    _patch_blocks(monkeypatch, BLOCKS, counter)

    import app.services.agents.lead.lead_tools as tools_mod

    narr_calls = {"n": 0}

    async def _narr(*a, **kw):  # noqa: ANN002
        narr_calls["n"] += 1
        return "东部 35.5 万居首"

    monkeypatch.setattr(tools_mod, "_load_canvas_narrative", _narr)

    p = CanvasStateProvider(object(), "u1", "c1", entry="canvas")
    assert await p.narrative() == "东部 35.5 万居首"
    assert await p.narrative() == "东部 35.5 万居首"
    assert narr_calls["n"] == 1                        # 惰性 + 缓存

    await p.layout(force=True)                         # 重读布局 → 要点应失效
    await p.narrative()
    assert narr_calls["n"] == 2


async def test_state_exposes_structure(monkeypatch):
    counter: dict = {}
    _patch_blocks(monkeypatch, BLOCKS, counter)
    p = CanvasStateProvider(object(), "u1", "c1", entry="canvas")
    st = await p.state()
    assert st is not None
    assert st.block_count == 2 and len(st.raw_blocks) == 2
    assert st.to_dict()["canvas_id"] == "c1"


# ── 收敛的直接收益：answer 分支也拿到画布快照 ──────────────────────────────

class _CaptureStreamLLM:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        self.messages.append(messages)
        yield "好的"


