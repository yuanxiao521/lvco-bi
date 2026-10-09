"""验证 list_datasources 的真实返回与压缩截断（临时脚本）。"""
from __future__ import annotations

import asyncio
import json
import uuid

from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.datasource import DataSource
from app.models.user import User
from app.services.agent_tools import ListDatasourcesTool
from app.services.context_utils import compact_result_json


async def main() -> None:
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == "test@lvco.bi"))).scalar_one()
        ds = (await db.execute(select(DataSource).where(
            DataSource.user_id == user.id, DataSource.source_type != "postgresql",
        ).limit(1))).scalar_one()
        meta = ds.schema_meta or {}
        fields = meta.get("fields", [])
        print(f"数据源 {ds.name}: schema_meta.fields 共 {len(fields)} 个; 前3={[f.get('name') for f in fields[:3]]}")

        raw = await ListDatasourcesTool().execute(user_id=str(user.id), db_session=db)
        data = json.loads(raw)
        first = data.get("datasources", [{}])[0]
        cols = first.get("columns", [])
        print(f"list_datasources 返回: columns={len(cols)} 个 fields={len(first.get('fields', []))} 个")
        print(f"  columns 前5: {cols[:5]}")

        # 模拟 LLM 上下文里看到的版本（经过 compact_result_json）
        print(f"  原始 JSON 长度={len(raw)}")
        comp = compact_result_json(raw)
        print(f"  压缩后长度={len(comp)}  {'⚠️ 被截断' if comp.endswith('…') or '截断' in comp[-20:] else '未截断'}")
        if "…" in comp[-30:]:
            print(f"  压缩后末尾: ...{comp[-60:]}")
        comp_data = json.loads(comp)
        c2 = comp_data.get("datasources", [{}])[0]
        print(f"  压缩后 columns 实际可见: {len(c2.get('columns', []))} 个 (raw={len(cols)})")


if __name__ == "__main__":
    asyncio.run(main())