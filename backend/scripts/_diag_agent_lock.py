"""诊断2：绕开 HTTP，手动构造注入文本直接驱动 agent_stream，二分定位「锁源」失效层。

若此处 LLM 锁 douyin → 问题在 HTTP/CamelModel 层；
若仍挑 Ecommerce → 问题在 agent 层（prompt/工具）。
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.datasource import DataSource, SourceType
from app.services.agent_tools import duckdb_client
from app.services.ai_service import AIService
from app.services.llm_client import LLMClient

DOUYIN_ID = "1ab854fa-beb5-4909-9470-9d8561d176b1"


async def main() -> None:
    async with async_session_factory() as db:
        from app.models.user import User
        user = (await db.execute(select(User).where(User.email == "test@lvco.bi"))).scalar_one()
        ds = (await db.execute(select(DataSource).where(
            DataSource.id == uuid.UUID(DOUYIN_ID), DataSource.user_id == user.id,
        ))).scalar_one()
        schema_meta = ds.schema_meta or {}
        columns = [f.get("name") for f in (schema_meta.get("fields") or []) if isinstance(f, dict)][:12]
        schema_name = duckdb_client.get_schema_name(str(user.id), str(ds.id), ds.name)
        table_ref = f'"{schema_name}"."data"'

        injection = (
            f"【系统注入：当前已连接数据源】\n"
            f"数据源名称: {ds.name}\n"
            f"数据源 ID: {ds.id}\n"
            f"table_ref: {table_ref}\n"
            f"列名(columns): {', '.join(columns)}\n\n"
            f"用户问题: 统计男女博主的平均粉丝数"
        )
        print(f"注入片段: {injection[:200]}")

        svc = AIService(LLMClient())
        events: list[dict] = []
        async for ev in svc.agent_stream(
            user_id=str(user.id),
            user_msg=injection,
            history=[],
            db_session=db,
            initial_phase="analyzing",
            selected_datasource_id=str(ds.id),
        ):
            events.append(ev)
            if ev.get("type") == "tool_call":
                print(f"[tool_call] {ev.get('name')} args={str(ev.get('args'))[:120]}")
            elif ev.get("type") in ("error", "done", "text"):
                print(f"[{ev.get('type')}] {str(ev.get('content', ev.get('message', '')))[:80]}")
            if ev.get("type") == "tool_result":
                r = str(ev.get("result", ""))
                print(f"[tool_result] {ev.get('name')}: {'error' if 'error' in r else 'ok'} rows样本={r[:120]}")

        tools_called = [e.get("name") for e in events if e.get("type") == "tool_call"]
        print(f"\n全部工具调用: {tools_called}")


if __name__ == "__main__":
    asyncio.run(main())