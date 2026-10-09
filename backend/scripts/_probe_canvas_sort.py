"""P1-4a 真实回归：直查 PG 取真实用户/数据源，以画布同款 execute_chart_query 验证排序兼容（临时脚本）。

- A 别名排序（画布正常场景）→ 应成功有行
- B 源字段排序（修复前 Binder error）→ 修复后应成功
- C 乱字段排序 → 应降级默认排序成功
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.datasource import DataSource
from app.models.user import User
from app.schemas.query import ChartQueryConfig, MeasureConfig, SortConfig
from app.services.query_engine import execute_chart_query


async def main() -> None:
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == "test@lvco.bi"))).scalar_one_or_none()
        if user is None:
            print("用户不存在"); return
        ds = (await db.execute(select(DataSource).where(
            DataSource.user_id == user.id, DataSource.source_type != "postgresql",
        ).limit(1))).scalar_one_or_none()
        if ds is None:
            print("无本地数据源"); return
        uid, did = uuid.UUID(str(user.id)), uuid.UUID(str(ds.id))
        print(f"用户={user.email} 数据源={ds.name} id={ds.id}")

        for label, sort in [
            ("A 别名排序 sum_likes", SortConfig(field="sum_likes", order="desc")),
            ("B 源字段排序 likes（修复前 Binder error）", SortConfig(field="likes", order="desc")),
            ("C 乱字段排序 not_exist（应降级默认）", SortConfig(field="not_exist", order="desc")),
        ]:
            cfg = ChartQueryConfig(
                dimensions=["category"],
                measures=[MeasureConfig(field="likes", agg="SUM")],
                sort=sort,
                limit=50,
            )
            try:
                res = await execute_chart_query(datasource_id=did, config=cfg, user_id=uid, db=db)
                print(f"  [{label}] OK rows={len(res.rows)} 首行={res.rows[0] if res.rows else '-'}")
            except Exception as e:
                print(f"  [{label}] FAIL: {str(e)[:150]}")


if __name__ == "__main__":
    asyncio.run(main())