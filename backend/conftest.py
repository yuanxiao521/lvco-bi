"""pytest 全局配置和 fixture。

为测试提供：
- asyncio 模式配置
- 测试环境变量
"""
from __future__ import annotations

import os

import pytest

# 在导入 app 之前设置测试环境变量
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-unit-tests-only")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test_db")
os.environ.setdefault("DUCKDB_DATA_DIR", "./data/test_duckdb")


def pytest_configure(config):
    """注册自定义 marker。"""
    config.addinivalue_line(
        "markers", "asyncio: mark test as async (handled by pytest-asyncio)"
    )