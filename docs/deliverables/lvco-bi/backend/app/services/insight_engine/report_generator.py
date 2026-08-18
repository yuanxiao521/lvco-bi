"""Report Generator - 把 InsightRecord 转成可分享的 Report"""

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.insight_record import InsightRecord
from app.models.insight_rule import InsightRule, RunStatus
from app.models.report import Report, ReportSourceType, ReportStatus

log = structlog.get_logger("report_generator")


class ReportGeneratorError(Exception):
    """Report 生成失败"""

    pass


class ReportGenerator:
    """把 InsightRecord 转成 Report 记录，写入 reports 表

    生成的 Report:
    - source_type = ai_insight
    - source_id = record.id (反向引用 InsightRecord)
    - snapshot_blocks = 从 record.ai_narrative / charts / detected_anomalies 构造的 Markdown + 图表 blocks
    - status = published (洞察日报生成即发布)
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def generate_report(
        self,
        record: InsightRecord,
        rule: InsightRule,
    ) -> Report:
        """为一条 InsightRecord 生成对应的 Report，返回 Report 对象

        Args:
            record: 已 success 的 InsightRecord（含 ai_narrative / charts / raw_data / detected_anomalies）
            rule: 关联的 InsightRule（用于 title 和上下文）

        Raises:
            ReportGeneratorError: record.status != success 或 record.ai_narrative 为空
        """
        if record.status != RunStatus.success:
            raise ReportGeneratorError(
                f"InsightRecord {record.id} 状态非 success，无法生成 Report"
            )
        if not record.ai_narrative:
            raise ReportGeneratorError(
                f"InsightRecord {record.id} 无 ai_narrative，无法生成 Report"
            )

        title = self._build_title(rule, record)
        snapshot_blocks = self._build_snapshot_blocks(record, rule)

        report = Report(
            user_id=record.user_id,
            title=title,
            source_type=ReportSourceType.ai_insight,
            source_id=record.id,
            snapshot_blocks=snapshot_blocks,
            status=ReportStatus.published,
        )
        self.db.add(report)
        await self.db.flush()
        await self.db.refresh(report)

        # 反向关联: record.report_id = report.id
        record.report_id = report.id
        # 不再 commit，让调用方（InsightRunner）控制事务

        log.info(
            "insight_report_generated",
            record_id=str(record.id),
            report_id=str(report.id),
            rule_id=str(rule.id),
        )
        return report

    def _build_title(self, rule: InsightRule, record: InsightRecord) -> str:
        """构造 Report 标题: '{rule.name} - {YYYY-MM-DD}'"""
        date_str = record.run_at.strftime("%Y-%m-%d") if record.run_at else ""
        title = f"{rule.name} - {date_str}"
        return title[:200]  # 截断到 Report.title 字段长度

    def _build_snapshot_blocks(self, record: InsightRecord, rule: InsightRule) -> dict:
        """构造 Report.snapshot_blocks，复用现有 ReportView 的 blocks 结构

        返回格式:
        {
            "blocks": [
                {"type": "markdown", "content": "<ai_narrative>"},
                {"type": "chart", "chartType": "line", "title": ..., "config": ..., "data": ...},
                {"type": "table", "title": "异常列表", "columns": [...], "rows": [...]},
                {"type": "table", "title": "原始数据", "columns": [...], "rows": [...]},
            ]
        }
        """
        blocks: list[dict] = []

        # 1. AI 叙述（Markdown）
        blocks.append({
            "type": "markdown",
            "content": record.ai_narrative or "",
        })

        # 2. 趋势图表
        charts = record.charts or {}
        if charts:
            blocks.append({
                "type": "chart",
                "chartType": charts.get("chart_type", "line"),
                "title": charts.get("title", "趋势图"),
                "config": charts.get("config", {}),
                "data": charts.get("data", []),
            })

        # 3. 异常列表表格
        anomalies_data = record.detected_anomalies or {}
        anomaly_items = (
            anomalies_data.get("items", []) if isinstance(anomalies_data, dict) else []
        )
        if anomaly_items:
            blocks.append({
                "type": "table",
                "title": f"检测到的异常 ({len(anomaly_items)} 条)",
                "columns": ["字段", "类型", "严重性", "方向", "当前值", "预期值", "偏差", "描述"],
                "rows": [
                    [
                        a.get("field", ""),
                        a.get("type", ""),
                        a.get("severity", ""),
                        a.get("direction", ""),
                        a.get("current_value", ""),
                        a.get("expected_value", ""),
                        f"{a.get('deviation', 0):+.1%}"
                        if isinstance(a.get("deviation"), (int, float))
                        else "",
                        a.get("description", ""),
                    ]
                    for a in anomaly_items
                ],
            })

        # 4. 原始数据表格（限前 30 行）
        raw_data = record.raw_data or {}
        series = raw_data.get("series", []) if isinstance(raw_data, dict) else []
        if series:
            # 提取所有 measure 字段名作为列
            measure_fields = list(series[0].get("values", {}).keys()) if series else []
            columns = ["时间"] + measure_fields
            rows = []
            for point in series[:30]:
                ts = point.get("timestamp", "")
                values = point.get("values", {})
                rows.append([ts] + [values.get(f, "") for f in measure_fields])
            blocks.append({
                "type": "table",
                "title": f"原始数据 (前 {len(rows)} 行 / 共 {raw_data.get('total_points', len(series))} 行)",
                "columns": columns,
                "rows": rows,
            })

        return {"blocks": blocks}
