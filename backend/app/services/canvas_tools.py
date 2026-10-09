"""画布操作工具：让 Agent 能直接驱动分析画布（加图表/加文本/改块/删块/布局）。

这些工具本身**不写数据库**——它们只做"查询验证 + 产出 canvas_action"，
前端收到 canvas_action SSE 事件后在画布上实时落块并保存（前端是 blocks 的唯一写者）。
canvas_action 内嵌在工具返回 JSON 中，由 API 层提取后转发为独立 SSE 事件。
"""
import json
import logging
import uuid
from uuid import UUID

from app.services.agent_tools import BaseTool
from app.schemas.query import ChartQueryConfig, MeasureConfig
from app.services.metric_service import MetricServiceError
from app.services.query_engine import QueryEngineError, execute_chart_query

logger = logging.getLogger("lvco.canvas_tools")

# 合法聚合方式（与 query_engine.ALLOWED_AGGREGATIONS 对齐，供 LLM schema 提示）
ALLOWED_AGGS = ["SUM", "COUNT", "AVG", "MAX", "MIN"]

# 合法图表类型（与前端 VALID_CHART_TYPES / ChartType 对齐）
CHART_TYPES = [
    "bar", "line", "pie", "donut", "area", "scatter", "kpi_card",
    "grouped_bar", "stacked_bar", "horizontal_bar",
    "funnel", "heatmap", "radar", "sankey",
]

# canvas_action 中携带的最大数据行数，避免 SSE 包过大
_CANVAS_MAX_ROWS = 50

# 画布工具名：供编排器（Planner）按入口注入为可规划工具。
# 仅画布接口会注入，普通 AI 对话不注入，避免规划到无画布可落的工具。
CANVAS_TOOL_NAMES = frozenset({
    "add_chart_block", "add_text_block",
    "update_chart_block", "remove_block", "arrange_layout",
    "get_canvas_layout", "clear_canvas",
})


def _is_chart_block(b: dict) -> bool:
    """判定一个块是否为图表块（宽容探测前端字段）。"""
    return isinstance(b, dict) and (b.get("type") == "chart" or b.get("chartType") is not None)


def _is_text_block(b: dict) -> bool:
    """判定一个块是否为文本块（h1/h2/text）。"""
    return isinstance(b, dict) and (
        b.get("type") in ("text", "h1", "h2")
        or isinstance(b.get("blockType"), str)
    ) and not _is_chart_block(b)


def _block_id(b: dict) -> str:
    """宽容提取块的稳定 id（前端字段可能是 id/blockId/uuid）。

    快照里带上块 id 后，Lead 主管与执行器（如 ReAct）才能精确调用
    `update_chart_block` / `remove_block`（其 block_id 必填），
    实现"上一轮做不好 → 单独改/删某一个块"的精准修复，
    而不是靠标题/位置模糊描述或瞎猜 id。
    """
    if not isinstance(b, dict):
        return ""
    for k in ("id", "blockId", "uuid"):
        v = b.get(k)
        if v:
            return str(v)
    return ""


def _norm_text(s: str) -> str:
    """文本归一化（去空白），用于"同一内容是否重复"的比较。"""
    return "".join(str(s or "").split())


class CanvasActionLedger:
    """本轮运行内 Agent 对画布所做动作的台账（内存，随一次运行生灭）。

    为什么必须存在：这些画布工具**不写数据库**，画布由前端落块后再自动保存
    （有防抖延迟），所以 `get_canvas_layout` 读到的是"上一轮"的库内快照——
    模型看不到自己刚删掉/刚加上的东西，就会：
      ① 反复删除同一批块（每次都"成功"，前端还回执"已删除"）；
      ② 重复添加同样内容的标题/段落；
      ③ 反问用户"要删哪些块"，而它其实刚删完。
    台账把本轮动作叠加在库内快照之上，并给删除/修改提供存在性校验。
    """

    def __init__(self) -> None:
        self.added: list[dict] = []       # 本轮新增（前端落块后才有真实 id）
        self.removed: set[str] = set()    # 本轮已删除的 block_id
        self.updated: set[str] = set()    # 本轮已修改的 block_id

    def record_add(self, action: dict) -> None:
        block = action.get("block") or {}
        item = {
            "action": action.get("action"),
            "title": str(block.get("title") or ""),
            "content": str(block.get("content") or ""),
            "blockType": str(block.get("blockType") or ""),
            "chartType": str(block.get("chartType") or ""),
        }
        self.added.append(item)

    def record_remove(self, block_id: str) -> None:
        self.removed.add(str(block_id))

    def record_update(self, block_id: str) -> None:
        self.updated.add(str(block_id))

    def has_added_text(self, block_type: str, content: str) -> bool:
        """本轮是否已经加过同样内容的文本块。"""
        want = _norm_text(content)
        return any(
            a.get("content") and _norm_text(a["content"]) == want
            and (not a.get("blockType") or a.get("blockType") == block_type)
            for a in self.added
        )

    def overlay(self, blocks: list[dict]) -> list[dict]:
        """把本轮动作叠加到库内块列表：已删的不再出现，新增的追加在末尾。"""
        kept = [b for b in blocks if _block_id(b) not in self.removed]
        for a in self.added:
            if a.get("action") == "add_text_block":
                kept.append({"type": a.get("blockType") or "text",
                             "content": a.get("content") or ""})
            elif a.get("action") == "add_chart_block":
                kept.append({"type": "chart", "title": a.get("title") or "未命名图表",
                             "chartType": a.get("chartType") or "?"})
        return kept

    def describe(self) -> str:
        """本轮动作摘要（注入布局快照，让模型知道自己已经做了什么）。"""
        parts: list[str] = []
        if self.removed:
            parts.append(f"已删除 {len(self.removed)} 个块（{'、'.join(sorted(self.removed))}）")
        if self.updated:
            parts.append(f"已修改 {len(self.updated)} 个块（{'、'.join(sorted(self.updated))}）")
        add_texts = [a for a in self.added if a.get("action") == "add_text_block"]
        add_charts = [a for a in self.added if a.get("action") == "add_chart_block"]
        if add_texts:
            names = "、".join(f"「{(a.get('content') or '')[:16]}」" for a in add_texts[:5])
            parts.append(f"已新增 {len(add_texts)} 个文本块（{names}）")
        if add_charts:
            names = "、".join(f"「{a.get('title') or '未命名'}」" for a in add_charts[:5])
            parts.append(f"已新增 {len(add_charts)} 个图表块（{names}）")
        if not parts:
            return ""
        return ("本轮已执行的动作（画布由前端落块，数据库写入有秒级延迟，"
                "以下动作均已生效，**不要重复执行**）：" + "；".join(parts) + "。")

    def blocked_reason(self, block_id: str, known_ids: set[str]) -> str:
        """删除/修改前的校验：返回空串表示可以执行，否则返回拦截原因。"""
        bid = str(block_id or "").strip()
        if not bid:
            return "block_id 为空"
        if bid in self.removed:
            return f"块 {bid} 在本轮已经被删除过了，无需重复删除"
        if bid not in known_ids:
            return (f"块 {bid} 不在当前画布上（可能已被删除，或 id 有误）。"
                    f"请调用 get_canvas_layout 查看真实 id，不要凭记忆编造或反复重试")
        return ""


async def _load_canvas_blocks(canvas_id: str, user_id: str, db_session) -> list[dict] | None:
    """读取画布已落盘的块列表；画布不存在/参数缺失返回 None（不抛异常）。"""
    from uuid import UUID

    try:
        uid = UUID(str(user_id)) if user_id else None
        cid = UUID(str(canvas_id)) if canvas_id else None
    except (ValueError, TypeError):
        return None
    if uid is None or cid is None or db_session is None:
        return None
    try:
        from app.repositories.canvas_repository import SQLAlchemyCanvasRepository

        canvas = await SQLAlchemyCanvasRepository(db_session).get_by_id(cid, uid)
    except Exception as e:  # noqa: BLE001
        logger.warning("[canvas_tools] load_blocks_failed error=%s", e)
        return None
    if canvas is None:
        return None
    return list(canvas.blocks or [])


def _rect_hit(b1: dict, b2: dict) -> bool:
    """两个带坐标块的矩形是否重叠（x,y 左上角 + width×height）。"""
    try:
        x1, y1, w1, h1 = (float(b1[k]) for k in ("x", "y", "width", "height"))
        x2, y2, w2, h2 = (float(b2[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return False
    return not (x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1)


def _assign_block_labels(chart_blocks: list[dict]) -> dict[str, str]:
    """给画布块分配稳定可见编号（如 [A1]/[A2]/[B1]），供用户与 LLM 指代同一块。

    规则：按坐标排序——先按 y（行），再按 x（列）。同一行（y 容差内）归为一行，
    行字母 A→Z 递增，行内按 x 升序编号 1→n；无坐标的块按原顺序排在最后，
    序号继续累加（如 [C3]）。

    编号覆盖**所有块**（图表 + 标题 + 文本 + 图片），前端 `assignBlockLabels`
    必须采用同一规则渲染角标，保证用户看到的编号与 LLM 上下文里的编号一致。
    """
    labels: dict[str, str] = {}
    if not chart_blocks:
        return labels

    def _key(b: dict) -> tuple:
        has_pos = all(b.get(k) is not None for k in ("x", "y"))
        if has_pos:
            return (0, float(b.get("y")), float(b.get("x")))
        return (1, 0.0, 0.0)

    ordered = sorted(chart_blocks, key=_key)
    row_letters: dict[float, str] = {}
    rows_in_order: list[float] = []
    for b in ordered:
        if all(b.get(k) is not None for k in ("x", "y")):
            y = float(b.get("y"))
            # 找到容差内的已有行（10px 内视为同一行）
            row_key = next((k for k in rows_in_order if abs(k - y) <= 10), None)
            if row_key is None:
                row_key = y
                rows_in_order.append(row_key)
            if row_key not in row_letters:
                row_letters[row_key] = chr(ord("A") + len(row_letters))
    idx: dict[str, int] = {}
    for b in ordered:
        bid = _block_id(b)
        if not bid:
            continue
        has_pos = all(b.get(k) is not None for k in ("x", "y"))
        if has_pos:
            row = row_letters[next(k for k in rows_in_order if abs(k - float(b.get("y"))) <= 10)]
        else:
            # 无坐标块：行字母取当前最大行之后的新行
            row = chr(ord("A") + len(row_letters))
            row_letters[len(row_letters)] = row  # 占位避免与后续冲突
        col = idx.get(row, 0) + 1
        idx[row] = col
        labels[bid] = f"{row}{col}"
    return labels


def render_canvas_layout(blocks=None, *, canvas_id=None, max_chars: int = 900,
                         extra_note: str = "") -> str:
    """把画布块列表渲染成紧凑布局摘要文本（供 Agent / 主管感知画布现状）。

    输入是前端保存的 Canvas.blocks（本工具是只读感知，不改画布）。输出：
      块总数 / 图表块清单（标题+类型+坐标+可见编号） / 文本块数量 / 重叠检测。
    空画布或空输入返回空画布提示；超长时按 max_chars 截断（保住头部的图表清单）。

    图表块带 [编号]（如 [A1]）：该编号按坐标规则稳定生成，前端角标渲染与之一致，
    Agent 与用户可凭借编号精确指代某一张图（update_chart_block / remove_block 用）。
    """
    blocks = blocks if isinstance(blocks, list) else []
    chart_blocks = [b for b in blocks if _is_chart_block(b)]
    text_blocks = [b for b in blocks if _is_text_block(b)]
    # 其它类型（未知/未识别）不计数，仅图表/文本分类覆盖主要两类
    lines: list[str] = []
    if canvas_id:
        lines.append(f"画布 id：{canvas_id}")
    lines.append(
        f"画布当前共有 {len(blocks)} 个块：图表 {len(chart_blocks)} 个、文本 {len(text_blocks)} 个。"
    )
    if not blocks:
        lines.append("画布为空，尚未有任何内容块。")
        if extra_note:
            lines.append(extra_note)
        return "\n".join(lines)
    # 编号覆盖所有块（图表 + 文本 + 图片）：前端角标按同一规则渲染，
    # 用户说"A1"才能和 LLM 上下文里的编号对上
    labels = _assign_block_labels(blocks)
    if chart_blocks:
        lines.append("图表清单：")
        for b in chart_blocks:
            title = str(b.get("title") or b.get("name") or "未命名图表")
            ctype = b.get("chartType") or "?"
            bid = _block_id(b)
            id_part = f" id={bid}" if bid else ""
            label_part = f"[{labels.get(bid)}]" if bid and labels.get(bid) else ""
            pos = ""
            if all(b.get(k) is not None for k in ("x", "y", "width", "height")):
                pos = (f" pos=({b.get('x')},{b.get('y')}) "
                       f"size={b.get('width')}×{b.get('height')}")
            lines.append(f"- {label_part} {title}（{ctype}）{id_part}{pos}")
    if text_blocks:
        lines.append(f"文本块 {len(text_blocks)} 个：")
        for b in text_blocks[:6]:
            content = str(b.get("content") or (b.get("blocks") or [{}])[0].get("text", ""))
            snippet = content[:28].replace("\n", " ")
            bid = _block_id(b)
            id_part = f" id={bid}" if bid else ""
            label_part = f"[{labels.get(bid)}] " if bid and labels.get(bid) else ""
            lines.append(f"- {label_part}{snippet}{'…' if len(content) > 28 else ''}{id_part}")
        if len(text_blocks) > 6:
            lines.append(f"- ……共 {len(text_blocks)} 个文本块")
    # 重叠检测（仅统计带坐标的图表块，最多报 3 对）
    coord_charts = [b for b in chart_blocks if all(b.get(k) is not None for k in ("x", "y", "width", "height"))]
    overlaps: list[str] = []
    for i, a in enumerate(coord_charts):
        for b in coord_charts[i + 1:]:
            if _rect_hit(a, b):
                overlaps.append(f"{a.get('title') or '?'} 与 {b.get('title') or '?'}")
                break
        if len(overlaps) >= 3:
            break
    if overlaps:
        lines.append(f"⚠️ 检测到 {len(overlaps)} 处块重叠：{'；'.join(overlaps)}，建议调用 arrange_layout 整理。")
    if extra_note:
        lines.append(extra_note)
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0]


def _chart_type_schema():
    return {"type": "string", "enum": CHART_TYPES}


def _measure_schema():
    return {
        "type": "array",
        "items": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "metric_key": {"type": "string", "description": "引用指标中心已定义的命名指标 key，如 sales_amount"},
                        "field": {"type": "string", "description": "当指标是模板指标（formula 含 {{field}}）时，用于填充其占位字段的字段名"},
                    },
                    "required": ["metric_key"],
                },
                {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": "度量字段名，必须与数据源字段完全一致"},
                        "agg": {"type": "string", "enum": ALLOWED_AGGS},
                    },
                    "required": ["field", "agg"],
                },
            ]
        },
    }


def _dimension_schema():
    return {"type": "array", "items": {"type": "string"}}


class AddChartBlockTool(BaseTool):
    """在画布上新增一个图表块，会先用真实数据验证查询可行性并取数。"""

    name = "add_chart_block"
    description = (
        "在分析画布上新增一个图表块。会在后端先用真实数据验证查询可行性并取回数据，"
        "成功后才能被前端渲染。用于批量搭建分析报告。"
        "注意：展示单个聚合数值（如总销售额、订单总数）用 kpi_card，其 dimensions 可为空数组；"
        "其他图表类型必须提供至少一个维度。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "图表标题，如：渠道获客对比"},
                        "chart_type": _chart_type_schema(),
                        "datasource_id": {"type": "string", "description": "数据源 UUID"},
                        "dimensions": _dimension_schema(),
                        "measures": _measure_schema(),
                    },
                    "required": ["title", "chart_type", "datasource_id", "dimensions", "measures"],
                },
            },
        }

    async def execute(self, title: str = "", chart_type: str = "", datasource_id: str = "",
                      dimensions: list[str] | None = None, measures: list[dict] | None = None,
                      user_id: str = "", db_session=None, **kwargs) -> str:
        """验证查询可行性并取数，返回 canvas_action。

        measures 支持两种形态：{field, agg}（普通度量）或 {metric_id/metric_key, [field], [agg]}
        （引用指标中心的命名指标）。指标度量会被解析为当前口径的表达式，前端据此随口径刷新。

        维度约束：普通图表需要至少一个维度；kpi_card 允许零维度（全表聚合单值），
        查询引擎对空 dimensions 天然支持（无 GROUP BY）。

        缺参守卫：参数允许缺省，若必填缺失直接返回友好错误（而非 Python TypeError 崩溃），
        让 LLM 能据报错补齐自纠错，避免"空参调用直接崩溃"击穿整步。
        """
        measures = measures or []
        dimensions = dimensions or []
        missing = []
        if not title:
            missing.append("title")
        if not chart_type:
            missing.append("chart_type")
        if not datasource_id:
            missing.append("datasource_id")
        if not measures:
            missing.append("measures")
        if not dimensions and chart_type not in (None, "", "kpi_card"):
            missing.append("dimensions")
        if missing:
            return json.dumps({"error": f"add_chart_block 缺少必填参数: {', '.join(missing)}，请补齐后重新调用"}, ensure_ascii=False)
        if not measures:
            return json.dumps({"error": "add_chart_block 需要至少一个度量"}, ensure_ascii=False)
        if not dimensions and chart_type != "kpi_card":
            return json.dumps({
                "error": f"add_chart_block 维度为空：{chart_type} 需要至少一个维度字段；"
                         "如需展示单个聚合数值（如总销售额）请改用 kpi_card（KPI 卡片，无需维度）"
            }, ensure_ascii=False)

        try:
            from app.services.metric_service import resolve_measures
            m_configs, display_measures = await resolve_measures(
                db_session, UUID(user_id) if user_id else None, measures, dimensions=[str(d) for d in dimensions],
            )
            if not m_configs:
                return json.dumps({"error": "度量字段无效，每个度量需包含 field+agg 或 metric_id"}, ensure_ascii=False)
            config = ChartQueryConfig(
                dimensions=[str(d) for d in dimensions],
                measures=m_configs,
                filters=[],
                chart_type=chart_type,
                datasource_id=datasource_id,
                limit=_CANVAS_MAX_ROWS,
            )
            result = await execute_chart_query(
                datasource_id=UUID(datasource_id),
                config=config,
                user_id=UUID(user_id),
                db=db_session,
            )
        except (QueryEngineError, ValueError) as e:
            logger.warning("[add_chart_block] query_failed error=%s", str(e))
            return json.dumps({"error": f"查询验证失败: {str(e)}"}, ensure_ascii=False)
        except MetricServiceError as e:
            logger.warning("[add_chart_block] metric_error error=%s", str(e))
            return json.dumps({"error": f"指标解析失败: {str(e)}"}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("[add_chart_block] unexpected error=%s", str(e))
            return json.dumps({"error": f"生成图表失败: {str(e)}"}, ensure_ascii=False)

        rows = list(result.rows)[:_CANVAS_MAX_ROWS]
        action = {
            "action": "add_chart_block",
            "block": {
                "title": title,
                "chartType": chart_type,
                "datasourceId": datasource_id,
                "queryConfig": {
                    "dimensions": [str(d) for d in dimensions],
                    "measures": display_measures,
                    "filters": [],
                    "limit": _CANVAS_MAX_ROWS,
                },
                "columns": list(result.columns),
                "rows": rows,
            },
        }
        logger.info("[add_chart_block] ok title=%s chart_type=%s rows=%d", title, chart_type, len(rows))
        ledger: CanvasActionLedger | None = kwargs.get("canvas_ledger")
        if ledger is not None:
            ledger.record_add(action)
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False, default=str)


class AddTextBlockTool(BaseTool):
    """在画布上新增文本/标题块。"""

    name = "add_text_block"
    description = (
        "在分析画布上新增一个文本块，用于写报告标题(h1)、章节标题(h2)或叙事段落(text)。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_type": {"type": "string", "enum": ["h1", "h2", "text"], "description": "h1 报告大标题 / h2 章节标题 / text 叙事段落"},
                        "content": {"type": "string", "description": "文本内容"},
                    },
                    "required": ["block_type", "content"],
                },
            },
        }

    async def execute(self, block_type: str, content: str, user_id: str = "",
                      db_session=None, **kwargs) -> str:
        """返回 canvas_action，前端据此插入文本块。

        防重复：同一内容（去空白比较）已在画布上、或本轮已加过 → 直接返回 skipped，
        不再产出 canvas_action。否则模型"看不到自己的新增"会反复加同样的标题/段落。
        """
        if block_type not in ("h1", "h2", "text"):
            return json.dumps({"error": "block_type 必须是 h1/h2/text"}, ensure_ascii=False)
        if not content or not content.strip():
            return json.dumps({"error": "文本内容不能为空"}, ensure_ascii=False)
        content = content.strip()
        ledger: CanvasActionLedger | None = kwargs.get("canvas_ledger")
        canvas_id = str(kwargs.get("canvas_id") or "")
        if ledger is not None and ledger.has_added_text(block_type, content):
            return json.dumps(
                {"ok": True, "skipped": True, "reason": "本轮已添加过同样内容的块，未重复添加"},
                ensure_ascii=False,
            )
        blocks = await _load_canvas_blocks(canvas_id, user_id, db_session)
        if blocks is not None:
            want = _norm_text(content)
            dup = next(
                (b for b in blocks
                 if _is_text_block(b) and _norm_text(str(b.get("content") or "")) == want),
                None,
            )
            if dup is not None:
                return json.dumps(
                    {"ok": True, "skipped": True,
                     "reason": f"画布上已存在相同内容的块（id={_block_id(dup)}），未重复添加"},
                    ensure_ascii=False,
                )
        action = {"action": "add_text_block", "block": {"blockType": block_type, "content": content}}
        if ledger is not None:
            ledger.record_add(action)
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False)


class UpdateChartBlockTool(BaseTool):
    """修改画布上已存在的图表块（改标题/类型/维度/度量）。"""

    name = "update_chart_block"
    description = (
        "修改画布上已存在图表块的标题、图表类型、维度或度量。block_id 必须取自已注入的"
        "「画布当前布局」图表清单中的真实 id——清单每行带可见编号（如 [A1]），"
        "用户用编号（把 A1 改成…）或标题/类型描述（把按渠道那张柱状图改成折线）指代时，"
        "先按编号/描述匹配出该行再取其 id，不要编造。适用于主管评审发现某张图做错后的精准修复。"
    )

    _PATCH_FIELDS = ("title", "chart_type", "dimensions", "measures")

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string", "description": "要修改的图表块 ID"},
                        "title": {"type": "string", "description": "新标题（可选）"},
                        "chart_type": _chart_type_schema(),
                        "dimensions": _dimension_schema(),
                        "measures": _measure_schema(),
                    },
                    "required": ["block_id"],
                },
            },
        }

    async def execute(self, block_id: str, user_id: str = "", db_session=None, **kwargs) -> str:
        """返回 canvas_action，前端据此合并且重查图表。"""
        patch: dict = {}
        if kwargs.get("title") is not None:
            patch["title"] = kwargs["title"]
        if kwargs.get("chart_type") is not None:
            patch["chartType"] = kwargs["chart_type"]
        if kwargs.get("dimensions") is not None:
            patch["dimensions"] = [str(d) for d in kwargs["dimensions"]]
        if kwargs.get("measures") is not None:
            patch["measures"] = []
            for m in kwargs["measures"]:
                if not isinstance(m, dict):
                    continue
                if m.get("metric_id") or m.get("metric_key") or m.get("metricKey"):
                    patch["measures"].append({
                        "metric_id": m.get("metric_id") or m.get("metricId"),
                        "metric_key": m.get("metric_key") or m.get("metric_key"),
                    })
                elif m.get("field"):
                    patch["measures"].append({"field": m["field"], "agg": m.get("agg", "SUM")})
        if not patch:
            return json.dumps({"error": "没有提供任何要修改的字段"}, ensure_ascii=False)
        ledger: CanvasActionLedger | None = kwargs.get("canvas_ledger")
        canvas_id = str(kwargs.get("canvas_id") or "")
        blocks = await _load_canvas_blocks(canvas_id, user_id, db_session)
        if blocks is not None:
            known = {_block_id(b) for b in blocks if _block_id(b)}
            reason = ledger.blocked_reason(block_id, known) if ledger else (
                "" if str(block_id or "").strip() in known else
                f"块 {block_id} 不在当前画布上（可能已被删除，或 id 有误），请先调用 get_canvas_layout 确认真实 id"
            )
            # 允许修改本轮刚新增的图表：它们在前端已有真实 id，但库内还没有
            if reason and ledger is not None and any(
                a.get("action") == "add_chart_block" for a in ledger.added
            ):
                reason = ""
            if reason:
                logger.info("[update_chart_block] blocked block_id=%s reason=%s", block_id, reason)
                return json.dumps({"ok": False, "error": reason}, ensure_ascii=False)
        action = {"action": "update_chart_block", "blockId": block_id, "patch": patch}
        if ledger is not None:
            ledger.record_update(block_id)
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False)


class RemoveBlockTool(BaseTool):
    """删除画布上的一个文本或图表块。"""

    name = "remove_block"
    description = (
        "删除画布上的一个块。block_id 必须取自已注入的「当前画布状态」或 get_canvas_layout "
        "返回清单中的真实 id（每行形如 “id=xxx”），不要编造。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string", "description": "要删除的块 ID"},
                    },
                    "required": ["block_id"],
                },
            },
        }

    async def execute(self, block_id: str, user_id: str = "", db_session=None, **kwargs) -> str:
        """返回 canvas_action，前端据此删除块。

        先校验目标块确实存在：否则模型会对同一批（其实已删除的）块反复发出删除动作，
        每次都"成功"，前端还回执"已删除块"，用户看到的是一串重复回执 + 模型回头反问。
        """
        ledger: CanvasActionLedger | None = kwargs.get("canvas_ledger")
        canvas_id = str(kwargs.get("canvas_id") or "")
        blocks = await _load_canvas_blocks(canvas_id, user_id, db_session)
        if blocks is not None:
            known = {_block_id(b) for b in blocks if _block_id(b)}
            known |= {_block_id(b) for b in (ledger.overlay(blocks) if ledger else blocks) if _block_id(b)}
            reason = ledger.blocked_reason(block_id, known) if ledger else (
                "" if str(block_id or "").strip() in known else
                f"块 {block_id} 不在当前画布上（可能已被删除，或 id 有误），请先调用 get_canvas_layout 确认真实 id"
            )
            if reason:
                logger.info("[remove_block] blocked block_id=%s reason=%s", block_id, reason)
                return json.dumps({"ok": False, "error": reason}, ensure_ascii=False)
        action = {"action": "remove_block", "blockId": block_id}
        if ledger is not None:
            ledger.record_remove(block_id)
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False)


class ClearCanvasTool(BaseTool):
    """一次性清空画布上的所有块（原子操作）。"""

    name = "clear_canvas"
    description = (
        "一次性清空画布上的全部块（所有文本块与图表块），一次调用即可完成。"
        "当用户要求「清空画布 / 清空所有块 / 全部删除 / 都删掉 / 重新开始 / 把画布弄干净」时，"
        "必须调用本工具一次完成，不要逐个调用 remove_block："
        "逐块删除受单轮工具并发上限限制会被截断，导致删不干净、模型凭记忆重提已删除的 id、"
        "并反复回头向用户确认。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }

    async def execute(self, user_id: str = "", db_session=None, **kwargs) -> str:
        """返回 canvas_action，前端据此一次性清空全部块。

        与 remove_block 的区别：remove_block 一次只删一个块，清空 N 个块需要 N 次调用，
        而执行层单轮并发上限（MAX_PARALLEL_TOOL_CALLS）会截断多余调用且此前不回喂模型，
        导致"删不干净 + 凭记忆重提已删 id + 回头反问用户"。清空场景必须走本原子动作。
        """
        ledger: CanvasActionLedger | None = kwargs.get("canvas_ledger")
        canvas_id = str(kwargs.get("canvas_id") or "")
        blocks = await _load_canvas_blocks(canvas_id, user_id, db_session)
        cleared = 0
        if blocks:
            ids = [_block_id(b) for b in blocks if _block_id(b)]
            cleared = len(ids)
            # 台账同步记录为已移除，避免清空后模型仍对旧 id 发起删除
            if ledger is not None:
                for bid in ids:
                    ledger.record_remove(bid)
        logger.info("[clear_canvas] canvas_id=%s cleared=%d", canvas_id, cleared)
        action = {"action": "clear_canvas", "cleared": cleared}
        return json.dumps(
            {"ok": True, "canvas_action": action, "cleared": cleared}, ensure_ascii=False
        )


class ArrangeLayoutTool(BaseTool):
    """自动重排画布布局（前端报告式布局已实现）。"""

    name = "arrange_layout"
    description = (
        "自动重排画布上所有块为报告式布局：标题/文本通栏、图表双列网格。"
        "当画布块位置混乱、重叠或用户要求整理排版时调用。"
        "注意：每次批量落块完成后系统会自动重排，通常无需手动调用；"
        "仅当用户明确要求'整理布局/排版/对齐'或多次落块后仍发现块重叠时再调用。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layout": {"type": "string", "enum": ["auto"], "description": "自动布局模式"},
                    },
                    "required": ["layout"],
                },
            },
        }

    async def execute(self, layout: str = "auto", user_id: str = "", db_session=None, **kwargs) -> str:
        """返回 canvas_action；前端按报告式布局全量重排。"""
        action = {"action": "arrange_layout", "layout": layout}
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False)


class GetCanvasLayoutTool(BaseTool):
    """读取当前画布的整体布局快照（**只读感知工具**，不产生任何画布变更）。

    数据源是数据库里已落盘的 Canvas.blocks（前端保存），比请求时刻上下文里的
    前端快照更新：同一轮分析落完块后，调一次即可看到最新布局，避免重复添加
    相同标题的图表、或落块后检查是否整齐/重叠而决定是否 arrange_layout。
    """

    name = "get_canvas_layout"
    description = (
        "读取当前画布的整体布局快照（只读，不会修改画布）：块总数、图表块清单"
        "（标题/图表类型/坐标）、文本块数量、块重叠检测结果。"
        "已包含**本轮已执行的动作**（已删除的块不会出现在清单里，已新增的追加在末尾）——"
        "若末尾写明「已删除/已新增」，说明那些动作已经生效，绝对不要重复执行、也不要反问用户。"
        "用法：落块前先查一次，避免重复添加相同内容的图表；落完一批块后再查一次，"
        "确认布局是否整齐/有重叠，必要时再调 arrange_layout。"
        "canvas_id 由系统自动注入，直接空参调用即可，不要自行编造；"
        "仅当确实要读取另一张画布时才显式传入。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "canvas_id": {"type": "string", "description": "画布 UUID（可选，见『画布 id』）"},
                    },
                    "required": [],
                },
            },
        }

    async def execute(self, user_id: str = "", db_session=None, **kwargs) -> str:
        """读 Canvas.blocks 并以结构化文本返回布局快照。异常不抛出，返回错误 JSON。

        快照 = 库内块 **叠加本轮动作台账**：本轮已删除的不再出现、已新增的追加在末尾，
        末尾附一句"本轮已执行的动作"。否则库内状态落后于前端自动保存，模型会以为
        自己的删除/新增没生效而重复执行、甚至反问用户。
        """
        canvas_id = str(kwargs.get("canvas_id") or kwargs.get("canvasId") or "").strip()
        ledger: CanvasActionLedger | None = kwargs.get("canvas_ledger")
        blocks = await _load_canvas_blocks(canvas_id, user_id, db_session)
        if blocks is None:
            return json.dumps(
                {"error": "缺少画布上下文（canvas_id 或会话归属）或画布不存在，无法读取画布"},
                ensure_ascii=False,
            )
        overlay = ledger.overlay(blocks) if ledger else blocks
        summary = render_canvas_layout(
            overlay, canvas_id=canvas_id, extra_note=ledger.describe() if ledger else ""
        )
        logger.info("[get_canvas_layout] ok canvas_id=%s blocks=%d", canvas_id, len(overlay))
        return json.dumps({"ok": True, "layout": summary}, ensure_ascii=False)


# ==================== 在 canvas_tools 自身底部注册 ====================
# 不反向 import 到 agent_tools，避免 partially-initialized 循环依赖。
# 本模块顶部只 import BaseTool；这里在类全部定义完后注册。
from app.services.agent_tools import ToolRegistry  # noqa: E402

ToolRegistry.register(AddChartBlockTool())
ToolRegistry.register(AddTextBlockTool())
ToolRegistry.register(UpdateChartBlockTool())
ToolRegistry.register(RemoveBlockTool())
ToolRegistry.register(ClearCanvasTool())
ToolRegistry.register(ArrangeLayoutTool())
ToolRegistry.register(GetCanvasLayoutTool())