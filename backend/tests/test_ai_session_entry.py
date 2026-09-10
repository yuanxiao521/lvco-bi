"""AI 会话入口隔离（entry / canvas_id）单元测试：

- AISession 模型默认 entry='chat'（python 侧 default）
- CanvasChatRequest 支持 new_session 强制新建标记
- AISessionResponse 序列化携带 entry / canvas_id（供前端会话列表分组）
"""
import uuid
from datetime import datetime, timezone

from app.models.ai_session import AISession
from app.schemas import AISessionResponse, CanvasChatRequest


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