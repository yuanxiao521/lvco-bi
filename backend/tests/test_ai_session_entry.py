"""AI 会话入口隔离（entry / canvas_id）单元测试：

- AISession 模型默认 entry='chat'（python 侧 default）
- CanvasChatRequest 支持 new_session 强制新建标记
- AISessionResponse 序列化携带 entry / canvas_id（供前端会话列表分组）
- SSE 事件流空闲看门狗（Agent 停在 await 时强制收尾，避免前端永远"思考中"）
"""
import asyncio
import inspect
import uuid
from datetime import datetime, timezone

import app.api.v1.ai as ai_module
from app.api.v1.ai import STREAM_IDLE_TIMEOUT, _idle_guard
from app.models.ai_session import AISession
from app.schemas import AISessionResponse, CanvasChatRequest
from app.services.agents.lead.lead_agent import LeadAgent


def test_ai_session_defaults_to_chat_entry() -> None:
    """entry 列 schema 默认 chat（未显式赋值时 DB/flush 注入 'chat'）。"""
    sess = AISession(user_id=uuid.uuid4())
    # SQLAlchemy default 在 flush 时注入；此处验证列级默认值定义
    col = AISession.__table__.c.entry
    assert col.default.arg == "chat"
    assert sess.canvas_id is None


def test_ai_session_can_be_canvas_entry() -> None:
    """画布会话显式 entry='canvas' + canvas_id。"""
    cid = uuid.uuid4()
    sess = AISession(user_id=uuid.uuid4(), entry="canvas", canvas_id=cid)
    assert sess.entry == "canvas"
    assert sess.canvas_id == cid


def test_canvas_chat_request_new_session_flag() -> None:
    """CanvasChatRequest 携带 new_session 字段（默认 None / False 等价）。"""
    req = CanvasChatRequest(message="按 region 汇总销售额")
    assert req.new_session is None

    req2 = CanvasChatRequest(message="新对话", new_session=True)
    assert req2.new_session is True


def test_ai_session_response_exposes_entry_and_canvas_id() -> None:
    """AISessionResponse 输出 entry / canvasId，前端据此分组会话。"""
    cid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    sess = AISession(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model="gpt-4o",
        entry="canvas",
        canvas_id=cid,
        title="画布对话",
        created_at=now,
    )
    data = AISessionResponse.model_validate(sess).model_dump(by_alias=True)
    assert data["entry"] == "canvas"
    assert data["canvasId"] == cid
    assert data["title"] == "画布对话"


# ── SSE 事件流空闲看门狗 ──────────────────────────────────────────────

async def test_idle_guard_passes_through_and_ends_normally():
    """正常事件流：原样透传，源结束即结束（不产生额外事件）。"""
    async def src():
        yield {"type": "text", "content": "a"}
        yield {"type": "done"}

    got = [ev async for ev in _idle_guard(src(), timeout=1.0)]
    assert got == [{"type": "text", "content": "a"}, {"type": "done"}]


async def test_idle_guard_force_closes_when_agent_parks():
    """Agent 停在 await（源长期不出事件）→ 补 error + done，客户端不会永远转圈。"""
    async def src():
        yield {"type": "progress", "index": 1}
        await asyncio.sleep(10)  # 模拟"卡住"：远超过期时间
        yield {"type": "done"}

    got = [ev async for ev in _idle_guard(src(), timeout=0.05, label="t")]

    assert got[0] == {"type": "progress", "index": 1}
    assert got[1]["type"] == "error" and "超时" in got[1]["message"]
    assert got[2]["type"] == "done" and got[2]["degraded"] is True
    assert len(got) == 3, "超时后必须立刻收尾，不再等待源"


def test_idle_timeout_is_above_single_step_budget():
    """阈值必须大于单步超时（45s），否则会把正常的长步骤误判为卡住。"""
    assert STREAM_IDLE_TIMEOUT >= 60


def test_sub_executor_done_is_not_forwarded():
    """子执行器的内部 done 不得透传给客户端（否则一次请求出现两个 done，前端提前解锁）。

    复现依据：CanvasOrchestrator/AgentOrchestrator 结束时都会 emit {"type":"done"}，
    经 run_analysis → Lead._forward 原样转发后，前端会在子任务落块完就把流标记完成，
    而外层 Lead 还在继续（决策 stop / 记忆回流）。
    """
    src = inspect.getsource(LeadAgent._run_analysis_branch)
    assert 'ev.get("type") == "done"' in src, "Lead._forward 必须过滤子执行器的 done"
    # 过滤后紧跟着才 put：确认 done 被拦在 out_q 之外
    assert "if isinstance(ev, dict) and ev.get(\"type\") == \"done\":\n                return" in src


def test_canvas_endpoint_sends_exactly_one_done_at_stream_end():
    """画布端点：事件流里的 done 只记录降级标记，流尾统一下发唯一 done。"""
    src = inspect.getsource(ai_module)
    # 事件分发处不再直接 yield done
    assert 'yield _sse({"type": "done", "degraded": event.get("degraded", False)})' not in src
    assert "stream_degraded = bool(event.get(\"degraded\", stream_degraded))" in src
    assert 'yield _sse({"type": "done", "degraded": stream_degraded})' in src


# ── 会话级并发锁（抢锁失败提前 return 不能踩到 finally 的未绑定变量） ──────────


def _event_generator_src(endpoint: object) -> str:
    src = inspect.getsource(endpoint)
    start = src.index("async def event_generator")
    return src[start:]


def test_stream_locals_bound_before_try() -> None:
    """画布端点 finally 会引用这些局部变量，必须都在 try 之前初始化。

    对话端点已后台任务化（事件循环/落库整体搬到 _run_chat_task），连接层不再有
    finally，改为独立断言（见 test_chat_endpoint_delegates_to_background_task）。
    """
    endpoint = ai_module.canvas_ai_chat
    gen_src = _event_generator_src(endpoint)
    try_at = gen_src.index("\n        try:\n")
    head = gen_src[:try_at]
    for name in ("assistant_msg: AIMessage | None = None", "full_content = \"\""):
        assert name in head, f"{endpoint.__name__}: {name} 必须在 try 之前初始化"
    assert head.count("assistant_msg: AIMessage | None = None") == 1, (
        f"{endpoint.__name__}: assistant_msg 不应重复初始化（真源在 try 之前）"
    )


def test_chat_endpoint_delegates_to_background_task() -> None:
    """对话端点已后台任务化：占位行的最终更新/锁的最终释放都交由 _run_chat_task。

    复现依据：老实现里 finally 落库 + 释放锁，连接断开会杀任务/误释放锁；
    新实现把执行委托给后台任务，连接层只剩一个「任务未启动时的锁泄漏清理」finally。
    """
    gen_src = _event_generator_src(ai_module.data_chat_stream)
    gen_src_full = inspect.getsource(ai_module)
    # 占位行的最终更新必须由后台任务完成（连接层不得再出现落库 assistant 的代码）
    assert "assistant_msg.content = full_content" not in gen_src
    # 任务主体存在（含事件消费/落库/释放锁）
    assert "_run_chat_task(" in gen_src_full
    # 重连续收走 resume 分支（纯订阅，不再落新消息）
    assert "body.resume" in gen_src
    # 连接层唯一允许的 finally：任务启动被取消/竞态时的锁与任务态清理（防 TTL 泄漏）
    assert "task_launched" in gen_src
    assert "if not task_launched:" in gen_src