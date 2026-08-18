"""进程内准备评测环境：注册 eval 用户 + 上传 ecommerce_orders 数据源（无需后端 HTTP）。"""
import asyncio
import io

from fastapi import UploadFile

async def main():
    from app.core.database import async_session_factory
    from app.repositories import SQLAlchemyDataSourceRepository, SQLAlchemyUserRepository
    from app.services.auth_service import AuthService
    from app.services.datasource_service import DataSourceService

    async with async_session_factory() as db:
        repo = SQLAlchemyUserRepository(db)
        auth = AuthService(repo)
        user = await repo.get_by_email("eval@lvco.bi")
        if user is None:
            user = await auth.register("eval@lvco.bi", "Eval@2026bi", "Eval User")
            print("USER CREATED:", user.id)
        else:
            print("USER EXISTS:", user.id)

        ds_repo = SQLAlchemyDataSourceRepository(db)
        svc = DataSourceService(ds_repo)
        existing, _ = await ds_repo.list_datasources(user.id, page=1, page_size=100,
                                                     source_type=None, status=None, search=None)
        for ds in existing:
            if ds.name == "ecommerce_orders":
                await ds_repo.delete(ds)
                print("DELETED old ecommerce_orders")

        with open("scripts/mock_data_eval/ecommerce_orders.csv", "rb") as f:
            content = f.read()
        up = UploadFile(filename="ecommerce_orders.csv", file=io.BytesIO(content),
                        headers={"content-type": "text/csv"})
        ds = await svc.upload_file(user.id, "ecommerce_orders", up)
        fields = [(f.get("name"), f.get("data_type"), f.get("category")) for f in (ds.schema_meta or {}).get("fields", [])]
        print("DS_ID:", ds.id)
        print("ROWS:", ds.row_count, "| STATUS:", ds.status)
        print("FIELDS:", fields)
        await db.commit()
        print("COMMITTED")

asyncio.run(main())