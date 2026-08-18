"""InsightRunner - 集成查数据 → 检测 → 解读 → 持久化 → 通知"""

import re
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.postgres_connector import postgres_connector
from app.core.duckdb_client import duckdb_client
from app.models.datasource import DataSource, SourceType
from app.models.insight_record import InsightRecord
from app.models.insight_rule import InsightRule, RunStatus, ScheduleType
from app.models.notification import Notification, NotificationType
from app.services.insight_engine.detector import (
    Anomaly,
    TimePoint,
    detect_anomalies,
)
from app.services.insight_engine.interpreter import LLMInterpreter
from app.utils.crypto import decrypt_value, get_encryption_key

log = structlog.get_logger("insight_runner")

# 合法 SQL 标识符：字母/下划线开头，后接字母/数字/下划线，长度 1-63（Postgres 限制 63）
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _quote_ident(name: str, context: str = "identifier") -> str:
    """对 SQL 标识符做白名单校验并加双引号包裹。

    防止 query_config 中的字段名包含双引号或特殊字符导致 SQL 注入。
    合法标识符直接返回 `"name"`；非法则抛 InsightRunnerError。
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise InsightRunnerError(
            f"非法 SQL 标识符 ({context}): {name!r}，仅允许字母/下划线开头，"
            f"含字母/数字/下划线，长度 1-63"
        )
    return f'"{name}"'


class InsightRunnerError(Exception):
    """Runner 执行中的非致命错误（已持久化 failed 状态）"""

    pass


class InsightRunner:
    """洞察规则执行器

    一次 run() 调用完整执行一条规则：
    查数据 → 检测异常 → LLM 解读 → 持久化 → 推送通知
    """

    def __init__(self, interpreter: LLMInterpreter | None = None) -> None:
        self.interpreter = interpreter or LLMInterpreter()

    async def run(
        self,
        db: AsyncSession,
        rule: InsightRule,
        period_start: datetime,
        period_end: datetime,
    ) -> InsightRecord:
        """执行一条规则，返回 InsightRecord

        流程:
        1. 创建 InsightRecord 占位 (status=running)
        2. 查数据 → list[TimePoint]
        3. detect_anomalies
        4. LLMInterpreter.interpret
        5. 更新 InsightRecord (status=success + 全字段)
        6. 更新 InsightRule (last_run_at/status/next_run_at)
        7. 推送 Notification (insight_ready)

        失败时:
        - 更新 InsightRecord.status=failed, error_message
        - 更新 InsightRule.last_run_status=failed
        - 推送 Notification (insight_failed)
        - 不抛异常（返回 failed record），让调度器继续运行下一条规则
        """
        now = datetime.utcnow()
        record = InsightRecord(
            rule_id=rule.id,
            user_id=rule.user_id,
            datasource_id=rule.datasource_id,
            run_at=now,
            period_start=period_start,
            period_end=period_end,
            status=RunStatus.running,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

        try:
            # 1. 连接数据源并查询
            datasource = (
                await db.execute(
                    select(DataSource).where(DataSource.id == rule.datasource_id)
                )
            ).scalar_one_or_none()
            if datasource is None:
                raise InsightRunnerError(f"数据源 {rule.datasource_id} 不存在")

            if datasource.source_type != SourceType.postgresql:
                raise InsightRunnerError("目前仅支持 PostgreSQL 数据源")

            # ATTACH (复用 auto_discovery 逻辑)
            schema_name = duckdb_client.get_schema_name(rule.user_id, rule.datasource_id)
            conn_info = dict(datasource.connection_config or {})
            key = get_encryption_key()
            password = conn_info.get("password", "")
            if key and password:
                conn_info["password"] = decrypt_value(password, key)
            conn_info["host"] = conn_info.get("host", "localhost")
            conn_info["port"] = conn_info.get("port", 5432)
            conn_info["user"] = conn_info.get("username", "postgres")
            conn_info["database"] = conn_info.get("db_name", "")
            try:
                duckdb_client.execute(f'DETACH "{schema_name}"')
            except Exception:
                pass
            attach_sql = postgres_connector.get_attach_sql(conn_info, schema_name)
            duckdb_client.execute(attach_sql)

            # 2. 构造 SQL 并查询
            sql, params = self._build_query_sql(
                rule.query_config, schema_name, period_start, period_end
            )
            rows = duckdb_client.fetchall(sql, params)

            # 3. 转 TimePoint
            time_field = rule.query_config.get("time_field", "")
            measures = rule.query_config.get("measures", [])
            series = self._rows_to_series(rows, time_field, measures)

            if not series:
                raise InsightRunnerError("查询结果为空，无法分析")

            # 4. 异常检测
            measure_fields = [m.get("field") for m in measures if m.get("field")]
            threshold = rule.threshold
            anomalies = detect_anomalies(series, measure_fields, threshold)

            # 5. LLM 解读
            current, historical = self._split_current_historical(series, current_days=7)
            interp_result = await self.interpreter.interpret(
                anomalies, current, historical, rule.query_config
            )

            # 6. 更新 record
            record.status = RunStatus.success
            record.ai_narrative = interp_result.narrative
            record.charts = self._build_chart_payload(series, rule.query_config)
            record.raw_data = {
                "series": [
                    {"timestamp": p.timestamp.isoformat(), "values": p.values}
                    for p in series[:50]  # 限制 50 点避免过大
                ],
                "total_points": len(series),
            }
            record.detected_anomalies = {
                "items": self._serialize_anomalies(anomalies),
                "count": len(anomalies),
            }
            record.llm_model = interp_result.llm_model
            record.llm_tokens_input = interp_result.llm_tokens_input
            record.llm_tokens_output = interp_result.llm_tokens_output
            record.error_message = None

            # 7. 更新 rule
            rule.last_run_at = now
            rule.last_run_status = RunStatus.success
            rule.next_run_at = self._compute_next_run(rule, now)

            # 7.5 生成 Report（失败不影响 success 状态）
            # ReportGenerator 内部用 flush 不用 commit，让外层 db.commit() 统一提交
            # record + rule + report 三个对象。失败仅 log warning，不阻断流程。
            from app.services.insight_engine.report_generator import (
                ReportGenerator,
                ReportGeneratorError,
            )

            try:
                generator = ReportGenerator(db)
                report = await generator.generate_report(record, rule)
                # record.report_id 已在 generate_report 内设置
                log.info(
                    "insight_report_linked",
                    record_id=str(record.id),
                    report_id=str(report.id),
                )
            except Exception as report_err:
                log.warning(
                    "insight_report_generation_failed",
                    record_id=str(record.id),
                    error=str(report_err),
                )

            await db.commit()
            await db.refresh(record)

            log.info(
                "insight_run_success",
                rule_id=str(rule.id),
                record_id=str(record.id),
                anomalies=len(anomalies),
            )

            # 8. 推送通知（失败不影响已 commit 的 success 状态）
            try:
                await self._push_notification(
                    db,
                    rule.user_id,
                    NotificationType.insight_ready,
                    title=f"洞察日报已生成: {rule.name}",
                    body=interp_result.summary or f"规则 {rule.name} 执行完成",
                    resource_type="insight_record",
                    resource_id=record.id,
                    metadata={"rule_id": str(rule.id), "anomaly_count": len(anomalies)},
                )
            except Exception as notif_err:
                # 通知失败不回滚 success 状态，仅记录日志
                log.warning(
                    "insight_notification_failed",
                    rule_id=str(rule.id),
                    record_id=str(record.id),
                    error=str(notif_err),
                )
            return record

        except Exception as e:
            # 失败: 更新 record + rule + 推送通知
            log.exception("insight_run_failed", rule_id=str(rule.id), error=str(e))
            record.status = RunStatus.failed
            record.error_message = f"{type(e).__name__}: {str(e)}"[:500]
            rule.last_run_at = now
            rule.last_run_status = RunStatus.failed
            rule.next_run_at = self._compute_next_run(rule, now)
            await db.commit()
            await db.refresh(record)

            try:
                await self._push_notification(
                    db,
                    rule.user_id,
                    NotificationType.insight_failed,
                    title=f"洞察执行失败: {rule.name}",
                    body=f"错误: {str(e)[:200]}",
                    resource_type="insight_record",
                    resource_id=record.id,
                    metadata={"rule_id": str(rule.id), "error": str(e)[:500]},
                )
            except Exception as notif_err:
                log.warning(
                    "insight_failure_notification_failed",
                    rule_id=str(rule.id),
                    error=str(notif_err),
                )
            return record  # 不抛异常，返回 failed record

    def _build_query_sql(
        self,
        query_config: dict,
        schema_name: str,
        period_start: datetime,
        period_end: datetime,
    ) -> tuple[str, list[Any]]:
        """构造时间序列查询 SQL，返回 (sql, params)

        SQL 格式:
        SELECT "time_field", agg1("m1") AS m1, agg2("m2") AS m2
        FROM "schema".public."table"
        WHERE "time_field" BETWEEN ? AND ?
        GROUP BY "time_field"
        ORDER BY "time_field" ASC

        返回 params = [period_start, period_end]
        """
        table = query_config.get("table", "")
        time_field = query_config.get("time_field", "")
        measures = query_config.get("measures", [])

        if not table or not time_field:
            raise InsightRunnerError("query_config 缺少 table 或 time_field")

        # 标识符白名单校验，防止 SQL 注入（schema_name 已由 UUID 派生，安全）
        q_table = _quote_ident(table, "table")
        q_time_field = _quote_ident(time_field, "time_field")

        # 构造 SELECT 子句
        select_parts = [q_time_field]
        for i, m in enumerate(measures):
            field = m.get("field", "")
            agg = (m.get("agg") or "SUM").upper()
            if agg not in {"SUM", "AVG", "MAX", "MIN", "COUNT", "COUNT_DISTINCT"}:
                agg = "SUM"
            q_field = _quote_ident(field, f"measures[{i}].field")
            if agg == "COUNT_DISTINCT":
                select_parts.append(f'COUNT(DISTINCT {q_field}) AS "m{i}"')
            else:
                select_parts.append(f'{agg}({q_field}) AS "m{i}"')

        sql = (
            f'SELECT {", ".join(select_parts)} '
            f'FROM "{schema_name}".public.{q_table} '
            f'WHERE {q_time_field} BETWEEN ? AND ? '
            f'GROUP BY {q_time_field} '
            f'ORDER BY {q_time_field} ASC'
        )
        return sql, [period_start, period_end]

    def _rows_to_series(
        self,
        rows: list[tuple],
        time_field: str,
        measures: list[dict],
    ) -> list[TimePoint]:
        """把查询结果转成 TimePoint 列表

        rows[0] 是 time_field 值，rows[1:] 是各 measure 聚合值
        """
        series = []
        for row in rows:
            if not row or len(row) < 1:
                continue
            ts = row[0]
            # ts 可能是 datetime / date / string
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except ValueError:
                    continue
            values = {}
            for i, m in enumerate(measures):
                field = m.get("field", "")
                val = row[i + 1] if i + 1 < len(row) else 0
                try:
                    values[field] = float(val) if val is not None else 0.0
                except (TypeError, ValueError):
                    values[field] = 0.0
            series.append(TimePoint(timestamp=ts, values=values))
        return series

    def _split_current_historical(
        self,
        series: list[TimePoint],
        current_days: int = 7,
    ) -> tuple[list[TimePoint], list[TimePoint]]:
        """切分当前周期（最近 N 天）和历史数据

        返回 (current, historical)。historical 包含全部数据（含 current），
        因为 detector 需要完整序列才能算 z-score/移动平均等。
        """
        if not series:
            return [], []
        # current: 最后 current_days 个点
        current = (
            series[-current_days:] if len(series) > current_days else list(series)
        )
        # historical: 全部数据（detector 需要完整序列）
        historical = list(series)
        return current, historical

    def _serialize_anomalies(self, anomalies: list[Anomaly]) -> list[dict]:
        """Anomaly 列表转可持久化的 dict 列表"""
        return [
            {
                "type": a.type.value,
                "field": a.field,
                "severity": a.severity.value,
                "current_value": a.current_value,
                "expected_value": a.expected_value,
                "deviation": a.deviation,
                "direction": a.direction,
                "description": a.description,
            }
            for a in anomalies
        ]

    def _compute_next_run(self, rule: InsightRule, now: datetime) -> datetime:
        """计算下次运行时间（基于 schedule 和 schedule_time）"""
        t = rule.schedule_time
        candidate = now.replace(
            hour=t.hour, minute=t.minute, second=t.second, microsecond=0
        )
        if rule.schedule == ScheduleType.weekly:
            candidate += timedelta(days=7)
        else:
            if candidate <= now:
                candidate += timedelta(days=1)
        return candidate

    def _build_chart_payload(
        self,
        series: list[TimePoint],
        query_config: dict,
    ) -> dict:
        """构造前端可渲染的图表数据（用于 InsightRecord.charts）

        返回 {"chart_type": "line", "title": "...", "config": {...}, "data": [...]}
        """
        time_field = query_config.get("time_field", "time")
        measures = query_config.get("measures", [])

        # 构造 ECharts line 图配置
        data = [
            {
                "timestamp": p.timestamp.isoformat(),
                **{k: v for k, v in p.values.items()},
            }
            for p in series
        ]

        return {
            "chart_type": "line",
            "title": f"趋势图 ({time_field})",
            "config": {
                "x_field": time_field,
                "y_fields": [m.get("field") for m in measures if m.get("field")],
                "aggs": [m.get("agg", "SUM") for m in measures],
            },
            "data": data[:200],  # 限制 200 点
        }

    async def _push_notification(
        self,
        db: AsyncSession,
        user_id,
        type_: NotificationType,
        *,
        title: str,
        body: str,
        resource_type: str | None = None,
        resource_id=None,
        metadata: dict | None = None,
    ) -> None:
        """写入 Notification 表 + SSE 实时推送

        复用 NotificationService（DB + SSE 双通道），SSE 失败不影响 DB。
        """
        from app.services.notification_service import NotificationService

        service = NotificationService(db)
        await service.push(
            user_id,
            type_,
            title=title,
            body=body,
            link_url=f"/insights/records/{resource_id}" if resource_id else None,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )
