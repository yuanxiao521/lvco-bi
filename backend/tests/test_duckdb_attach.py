"""duckdb_client.ensure_attached 单元测试（首次 ATTACH 后复用连接）。"""
from __future__ import annotations

from unittest.mock import patch

from app.core.duckdb_client import duckdb_client


def setup_function() -> None:
    """每个用例前清空 attach 缓存，模拟独立会话。"""
    duckdb_client._attached_cache.clear()


def test_first_attach_executes_then_reuses() -> None:
    """首次 ATTACH 返回 True；同 schema 再次调用直接复用（不再执行 SQL）。"""
    with patch.object(duckdb_client, "_get_connection"), patch.object(
        duckdb_client, "execute"
    ) as mock_exec:
        assert duckdb_client.ensure_attached("sch_a", "ATTACH 'a'") is True
        assert duckdb_client.ensure_attached("sch_a", "ATTACH 'a'") is False
        # 首次：DETACH(可能失败) + ATTACH = 至少 1 次执行；复用时应无新增执行
        assert mock_exec.call_count >= 1
        calls_a = mock_exec.call_count
        duckdb_client.ensure_attached("sch_a", "ATTACH 'a'")
        assert mock_exec.call_count == calls_a  # 复用：不再有任何执行


def test_different_schemas_attach_independently() -> None:
    """不同 schema 各自 attach，互不干扰。"""
    with patch.object(duckdb_client, "execute") as mock_exec:
        assert duckdb_client.ensure_attached("sch_a", "ATTACH 'a'") is True
        assert duckdb_client.ensure_attached("sch_b", "ATTACH 'b'") is True
        assert any("ATTACH 'b'" in str(c) for c in mock_exec.call_args_list)


def test_force_rebuilds_even_if_cached() -> None:
    """force=True 跳过缓存强制重建：即使已 attach 也再执行 DETACH+ATTACH。"""
    with patch.object(duckdb_client, "execute") as mock_exec:
        duckdb_client.ensure_attached("sch_a", "ATTACH 'a'")
        before = mock_exec.call_count
        assert duckdb_client.ensure_attached("sch_a", "ATTACH 'a-new'", force=True) is True
        assert mock_exec.call_count > before
        assert any("ATTACH 'a-new'" in str(c) for c in mock_exec.call_args_list)


def test_cache_cleared_reattaches_on_first_call() -> None:
    """进程重启（缓存清空）后首个请求重新 ATTACH，覆盖旧连接失效。"""
    with patch.object(duckdb_client, "execute") as mock_exec:
        duckdb_client.ensure_attached("sch_a", "ATTACH 'a'")
        head = mock_exec.call_count
        # 模拟进程重启：缓存被清空
        duckdb_client._attached_cache.clear()
        assert duckdb_client.ensure_attached("sch_a", "ATTACH 'a'") is True
        assert mock_exec.call_count > head  # 重建连接