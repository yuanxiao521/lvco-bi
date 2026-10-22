# -*- coding: utf-8 -*-
"""真机验证 query_sample：用真实数据源取 5 行样本（只读）。

用法（backend 目录）：.venv\Scripts\python.exe scripts/_verify_query_sample.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import async_session_factory  # noqa: E402
from app.services.agents.lead.lead_observe import query_sample  # noqa: E402

USER = "2aace5b5-57d4-42d6-b90d-8d3e9b97ecb1"
DS = "970d798d-f5da-4442-8d25-3563020d5a29"


async def main() -> int:
    async with async_session_factory() as session:
        deg: list[str] = []
        out = await query_sample(session, USER, DS, degradation=deg)
        print("degradation =", deg)
        print("returned     =", out is not None)
        if out is None:
            return 1
        payload = json.loads(out)
        print("ok           =", payload.get("ok"))
        print("table_ref    =", payload.get("table_ref"))
        print("columns      =", payload.get("columns"))
        print("row_count    =", payload.get("row_count"))
        print("first_row    =", json.dumps(payload["rows"][0], ensure_ascii=False)[:400])
        ok = payload.get("ok") is True and 0 < payload.get("row_count", 0) <= 5
        print("\n[verify] sample_ok =", ok)

        # 反证 1：不存在的数据源 → 必须降级而不是退回别的源
        deg2: list[str] = []
        bogus = await query_sample(
            session, USER, "00000000-0000-0000-0000-000000000000", degradation=deg2
        )
        print("[verify] bogus_ds_returns_none =", bogus is None, "| degradation =", deg2)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))