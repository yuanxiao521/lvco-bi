# -*- coding: utf-8 -*-
"""把存量凭据升级为 v2 密文（AES-256-GCM）。

涉及两处存储：
1. db_connections.password_enc / ssl_ca_enc —— 当前模型的凭据位置；
2. datasources.connection_config.password —— 0021 迁移之前的历史位置（兜底）。

背景：DB_ENCRYPTION_KEY 未配置的那段时间里，密码是**明文**存的。加上密钥之后新写入
已是 v2 密文，但存量行还是明文/旧 CBC 密文，需要一次性迁移，否则明文会一直留在库里。

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
from app.models.db_connection import DBConnection
from app.utils.crypto import (
    CredentialKeyMissingError,
    get_encryption_key,
    is_encrypted,
    open_secret,
    seal_secret,
)


def _upgrade(raw: str) -> str | None:
    """明文/旧 CBC → v2；已经 v2 返回 None（无需改写）。"""
    if not raw or is_encrypted(raw):
        return None
    return seal_secret(open_secret(raw))


async def main(apply: bool) -> int:
    if get_encryption_key() is None:
        print("[FAIL] 未配置 DB_ENCRYPTION_KEY，先设置密钥再迁移")
        return 2

    migrated = reported = 0
    async with async_session_factory() as db:
        # ── 1) 连接表 ──────────────────────────────────────────────
        connections = (
            await db.execute(
                select(DBConnection).where(
                    DBConnection.source_type.in_(["postgresql", "mysql"])
                )
            )
        ).scalars().all()
        for conn in connections:
            changed = False
            for attr in ("password_enc", "ssl_ca_enc"):
                raw = getattr(conn, attr)
                if not raw:
                    continue
                if is_encrypted(raw):
                    continue
                reported += 1
                if not apply:
                    print(f"[DRY-RUN] 连接待迁移：{conn.name} ({conn.id}) 字段={attr}")
                    continue
                try:
                    sealed = _upgrade(raw)
                except CredentialKeyMissingError as exc:
                    print(f"[FAIL] {exc}")
                    return 2
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARN] {conn.name} ({conn.id}) {attr} 解不开：{exc}")
                    continue
                if sealed:
                    setattr(conn, attr, sealed)
                    changed = True
                    migrated += 1
                    print(f"[MIGRATED] 连接 {conn.name} ({conn.id}) 字段={attr}")
            if changed:
                await db.flush()

        # ── 2) 历史 datasource.connection_config.password ─────────
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
            if not password or is_encrypted(password):
                continue
            reported += 1
            if not apply:
                print(f"[DRY-RUN] 数据源待迁移：{ds.name} ({ds.id})")
                continue
            try:
                config["password"] = seal_secret(open_secret(password))
            except CredentialKeyMissingError as exc:
                print(f"[FAIL] {exc}")
                return 2
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] {ds.name} ({ds.id}) 无法解开旧密文：{exc}")
                continue
            config["secret_format"] = "v2"
            ds.connection_config = config
            migrated += 1
            print(f"[MIGRATED] 数据源 {ds.name} ({ds.id})")

        if apply and migrated:
            await db.commit()

    print(f"\n完成：待迁移 {reported} 处，实际迁移 {migrated} 处"
          + ("（dry-run，加 --apply 真正写入）" if not apply else ""))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入数据库（默认只报告）")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.apply)))
