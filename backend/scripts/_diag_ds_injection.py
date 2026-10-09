"""诊断：复刻 data_chat_stream 的已选数据源注入，确认 force_ds 时注入的是哪个数据源。"""
from __future__ import annotations

import asyncio
import uuid

import httpx
from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.datasource import DataSource, SourceType
from app.services.agent_tools import duckdb_client

DOUYIN_ID = "1ab854fa-beb5-4909-9470-9d8561d176b1"


async def main() -> None:
    client = httpx.AsyncClient(base_url="http://127.0.0.1:8000/api/v1", timeout=30)
    r = await client.post("/auth/login", json={"email": "test@lvco.bi", "password": "123456"})
    token = r.json()["data"]["accessToken"]
    user_uuid = uuid.UUID(r.json()["data"]["user"]["id"])
    h = {"Authorization": f"Bearer {token}"}

    # 1) API 列表里 douyin 的信息
    r = await client.get("/datasources", headers=h, params={"pageSize": 100})
    items = r.json().get("data", {}).get("items", [])
    print("API datasources 顺序:", [(d.get("name"), str(d.get("id"))[:8]) for d in items])

    # 2) 复刻 data_chat_stream 的注入查询
    async with async_session_factory() as db:
        ds = (await db.execute(select(DataSource).where(
            DataSource.id == uuid.UUID(DOUYIN_ID), DataSource.user_id == user_uuid,
        ))).scalar_one_or_none()
        if ds is None:
            print("❌ douyin 数据源不属于 test@lvco.bi -> 注入分支不生效（edfault 无注入）")
            return
        schema_meta = ds.schema_meta or {}
        fields = schema_meta.get("fields", [])
        columns = [f.get("name") for f in fields if isinstance(f, dict)]
        schema_name = duckdb_client.get_schema_name(str(user_uuid), str(ds.id), ds.name)
        table_ref = f'"{schema_name}"."data"'
        print(f"✅ 注入应为: name={ds.name} columns 数={len(columns)} 前3={columns[:3]}")
        print(f"   注入文本片段: 当前已连接数据源={ds.name} | table_ref={table_ref}")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())