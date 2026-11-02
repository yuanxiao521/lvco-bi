# -*- coding: utf-8 -*-
"""把存量数据源的密码升级为 v2 密文（AES-256-GCM）。

背景：DB_ENCRYPTION_KEY 未配置的那段时间里，数据库型数据源的密码是**明文**存的
（datasources.connection_config.password）。加上密钥之后，新写入的已是 v2 密文，
但存量行还是明文/旧 CBC 密文，需要一次性迁移，否则明文会一直留在库里。

用法（在 backend 目录）：
    .venv/Scripts/python.exe scripts/migrate_datasource_secrets.py            # 只报告
    .venv/Scripts/python.exe scripts/migrate_datasource_secrets.py --apply    # 真正改写
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.datasource import DataSource, SourceType
from app.utils.crypto import (
    CredentialKeyMissingError,
    get_encryption_key,
    is_encrypted,
    open_secret,
    seal_secret,
)


async def main(apply: bool) -> int:
    if get_encryption_key() is None:
        print("[FAIL] 未配置 DB_ENCRYPTION_KEY，先设置密钥再迁移")
        return 2

    migrated = reported = 0
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(DataSource).where(
                    DataSource.source_type.in_([SourceType.postgresql, SourceType.mysql])
                )
            )
        ).scalars().all()
        for ds in rows:
            config = dict(ds.connection_config or {})
            password = config.get("password")
            if not password:
                continue
            if is_encrypted(password):
                continue
            reported += 1
            try:
                plaintext = open_secret(password)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] {ds.name} ({ds.id}) 无法解开旧密文：{exc}")
                continue
            if apply:
                try:
                    config["password"] = seal_secret(plaintext)
                except CredentialKeyMissingError as exc:
                    print(f"[FAIL] {exc}")
                    return 2
                config["secret_format"] = "v2"
                ds.connection_config = config
                migrated += 1
                print(f"[MIGRATED] {ds.name} ({ds.id})")
            else:
                print(f"[DRY-RUN] 待迁移：{ds.name} ({ds.id})")
        if apply and migrated:
            await db.commit()

    print(f"\n完成：待迁移 {reported} 条，实际迁移 {migrated} 条"
          + ("（dry-run，加 --apply 真正写入）" if not apply else ""))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入数据库（默认只报告）")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.apply)))
