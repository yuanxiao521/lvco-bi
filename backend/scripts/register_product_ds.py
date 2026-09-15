"""给评测用户注册 product_performance 数据源（进程内，无需 HTTP）。

用法：cd backend && python scripts/register_product_ds.py
幂等：若已存在同名数据源则跳过。
"""
import asyncio
import io
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import UploadFile


async def main():
    from app.core.database import async_session_factory
    from app.repositories import SQLAlchemyDataSourceRepository, SQLAlchemyUserRepository
    from app.services.datasource_service import DataSourceService

    EVAL_USER_ID = "21bee02f-dcb3-4108-b721-d8448db678e4"

    async with async_session_factory() as db:
        # 1) 确认评测用户
        user_repo = SQLAlchemyUserRepository(db)
        user = await user_repo.get_by_id(__import__("uuid").UUID(EVAL_USER_ID))
        if user is None:
            print("评测用户不存在，先跑 prepare_eval_env.py")
            return 1
        print("USER:", user.email, user.id)

        ds_repo = SQLAlchemyDataSourceRepository(db)
        svc = DataSourceService(ds_repo)

        existing, _ = await ds_repo.list_datasources(
            user.id, page=1, page_size=100, source_type=None, status=None, search=None
        )
        for ds in existing:
            print("  existing ds:", ds.name, "id=", ds.id)
        if any(ds.name == "product_performance" for ds in existing):
            print("product_performance 已存在，跳过")
            return 0

        # 2) 上传 product_performance.csv
        csv_path = BACKEND_ROOT / "mock_data" / "product_performance.csv"
        with open(csv_path, "rb") as f:
            content = f.read()
        up = UploadFile(
            filename="product_performance.csv",
            file=io.BytesIO(content),
            headers={"content-type": "text/csv"},
        )
        ds = await svc.upload_file(user.id, "product_performance", up)
        fields = [
            (f.get("name"), f.get("data_type"), f.get("category"))
            for f in (ds.schema_meta or {}).get("fields", [])
        ]
        print("NEW DS_ID:", ds.id)
        print("ROWS:", ds.row_count, "| STATUS:", ds.status)
        print("FIELDS:", fields)
        await db.commit()
        print("COMMITTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))