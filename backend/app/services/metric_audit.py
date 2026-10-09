"""指标口径审计留痕（可追溯）。
记录"哪次查询/问答命中了哪个指标、用了什么口径(formula)"，
复用 operation_logs 审计链路，形成 问题 → 指标 → 口径 → SQL → 结果 的可复核记录。
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operation_log import OperationLog

logger = logging.getLogger("lvco.metric_audit")


async def record_metric_query(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    metric_key: str,
    formula: str,
    metric_id: uuid.UUID | None = None,
    sql: str | None = None,
    datasource_id: uuid.UUID | None = None,
    scenario: str = "canvas_query",
    extra: dict[str, Any] | None = None,
) -> None:
    """写入一条指标查询审计记录。

    Args:
        db: 异步会话（可为 None，失败时不阻断主流程）。
        user_id: 操作用户。
        metric_key: 命中的指标 key（口径一致性的锚点）。
        formula: 本次解析所用的口径快照（formula）。
        metric_id: 指标定义 id。
        sql: 生成的 SQL（可选）。
        datasource_id: 命中指标所归属的数据源。
        scenario: canvas_query / ai_query。
        extra: 额外上下文（维度、过滤等）。
    """
    if db is None:
        return
    try:
        payload: dict[str, Any] = {
            "metric_key": metric_key,
            "formula": formula,
            "scenario": scenario,
        }
        if metric_id is not None:
            payload["metric_id"] = str(metric_id)
        if datasource_id is not None:
            payload["datasource_id"] = str(datasource_id)
        if sql:
            payload["sql"] = sql
        if extra:
            payload.update(extra)
        db.add(OperationLog(
            user_id=user_id,
            action="metric.query",
            resource_type="metric",
            resource_id=metric_id,
            method="QUERY",
            path="/metrics/query",
            status_code=200,
            duration_ms=0,
            extra={"metric": payload, "occurred_at": datetime.now(timezone.utc).isoformat()},
        ))
        await db.flush()
    except Exception:  # noqa: BLE001
        logger.warning("metric_audit_failed", exc_info=True)