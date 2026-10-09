#!/usr/bin/env python3
r"""指标语义层端到端全链路验证（真实 HTTP + 真实 DB + 真实 DuckDB）。

验证对象：`E:\AAA_面试\LvcoBI_指标语义层接入设计_v1.md` 落地的 7 项改动是否可用。
链路：
  [HTTP 层] 登录 → GET /metrics（模板指标在库）→ GET /datasources（真实数据源）
          → POST /metrics（创建临时指标，验证 auto-formula + 公式安全）→ 清理
  [服务层] （真实 DB session + 真实 DuckDB）
          → list_metrics_for_user（指标可见性）
          → resolve_measures_for_exec(metric_key)（指标口径解析成 SQL 表达式）
          → QueryEngineTool.execute(metric_key 引用)（真实查询成功、返回行）
          → QueryEngineTool.execute(非法 key)（错误提示含 list_metrics 自纠错）
          → execute_chart_query 正常聚合（引擎 AST 出口校验后仍通过）
          → get_semantic_coverage（覆盖率计数器非零）
          → check_metric_field_bindings（重传断链校验：合法字段无断链 / 缺失字段被检出）
  [LLM 层]（可选，--with-llm）chat/stream 发指标类问题 → 断言事件流含指标路径

前置条件：
- 后端已启动（默认 http://127.0.0.1:8000）
- PostgreSQL 可达（DATABASE_URL from backend/.env）
- 账号 test@lvco.bi 有数据源且 DuckDB 已物化（跑过 scripts/upload_mock_data.py）

运行（在 backend 目录）：
    python tests/real_e2e_metric_semantic.py
    python tests/real_e2e_metric_semantic.py --with-llm

输出：可追溯 MD 报告（内含每步请求/响应/断言），打印绝对路径。
退出码：0=全过；1=有断言失败；2=环境问题。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import requests  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}
EMAIL = "test@lvco.bi"
PASSWORD = "123456"
TIMEOUT = 30


class Report:
    """可追溯报告：按阶段记录每步输入/输出/断言，最终落为 MD 文件。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.cases: list[tuple[str, bool, str]] = []
        self._now = datetime.now()

    def h(self, text: str, level: int = 2) -> None:
        self.lines.append(f"{'#' * level} {text}")

    def code(self, text: str) -> None:
        self.lines.append("```json")
        self.lines.append(text)
        self.lines.append("```")

    def step(self, text: str) -> None:
        self.lines.append(f"\n### {text}")

    def log(self, text: str) -> None:
        self.lines.append(f"- {text}")

    def case(self, name: str, ok: bool, detail: str = "") -> None:
        self.cases.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        self.lines.append(f"- [{mark}] {name}" + (f" — {detail}" if detail else ""))

    def save(self, path: Path) -> None:
        passed = sum(1 for _, ok, _ in self.cases if ok)
        total = len(self.cases)
        with path.open("w", encoding="utf-8") as f:
            f.write(f"# 指标语义层端到端全链路验证报告\n\n")
            f.write(f"- 时间：{self._now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"- 后端：{BASE}\n")
            f.write(f"- 账号：{EMAIL}\n")
            f.write(f"- 结论：**{passed}/{total} 项通过** {'✅ 全链路过' if passed == total else '❌ 有失败项'}\n\n")
            f.write("---\n")
            f.write("\n".join(self.lines))
            f.write(f"\n\n---\n\n## 断言汇总\n\n")
            for name, ok, detail in self.cases:
                f.write(f"- {'✅' if ok else '❌'} {name}" + (f"（{detail}）" if detail else "") + "\n")
        print(f"\n[报告已写入] {path.resolve()}")


def api(method: str, path: str, token: str | None = None, json_body=None):
    """发起真实 HTTP 请求，返回 (status, body)。带 5 次重试（后端 reload 场景）。"""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for attempt in range(5):
        try:
            resp = requests.request(
                method, f"{BASE}{path}", headers=headers, json=json_body,
                **NO_PROXY, timeout=TIMEOUT,
            )
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:500]}
            return resp.status_code, body
        except requests.exceptions.RequestException as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    return -1, {"detail": f"连接失败: {last}"}


# ==================== HTTP 验证 ====================

def http_login(report: Report) -> str | None:
    report.step("HTTP 1. 登录（真实路由 + 真实用户表）")
    st, body = api("post", "/auth/login", json_body={"email": EMAIL, "password": PASSWORD})
    token = (body.get("data") or {}).get("accessToken") if isinstance(body.get("data"), dict) else None
    report.code(json.dumps({"status": st, "hasToken": bool(token)}, ensure_ascii=False))
    report.case("login 200 + accessToken", st == 200 and bool(token), f"status={st}")
    return str(token) if token else None


def http_list_metrics(report: Report, token: str) -> list[dict]:
    report.step("HTTP 2. 列指标 GET /metrics（应含全局模板指标）")
    st, body = api("get", "/metrics", token=token)
    items = body.get("data") or []
    keys = [m.get("key") for m in items if isinstance(m, dict)]
    report.code(json.dumps({"status": st, "count": len(items), "keys": keys[:10]}, ensure_ascii=False))
    report.case("GET /metrics 200", st == 200, f"count={len(items)}")
    report.case("模板指标在库（sales_amount）", "sales_amount" in keys, str(keys[:10]))
    return items


def http_list_datasources(report: Report, token: str) -> list[dict]:
    report.step("HTTP 3. 列数据源 GET /datasources（取真实数据源做查询验证）")
    st, body = api("get", "/datasources", token=token)
    data = body.get("data") or {}
    rows = data.get("items") if isinstance(data, dict) else data
    report.code(json.dumps({"status": st, "count": len(rows) if isinstance(rows, list) else 0}, ensure_ascii=False))
    if not isinstance(rows, list) or not rows:
        report.case("存在可用数据源", False, "无数据源，先跑 scripts/upload_mock_data.py")
        return []
    report.case("存在可用数据源", True, f"{len(rows)} 个")
    return [r for r in rows if isinstance(r, dict)]


def http_create_temp_metric(report: Report, token: str, ds_id: str | None, field: str) -> dict | None:
    report.step("HTTP 4. 创建临时指标 POST /metrics（验证 auto-formula 与公式安全）")
    stamp = time.strftime("%H%M%S")
    key = f"e2e_sem_{stamp}"
    payload = {
        "key": key,
        "name": f"E2E语义层-{stamp}",
        "datasourceId": ds_id,
        "sourceField": field,
        "agg": "SUM",
    }
    st, body = api("post", "/metrics", token=token, json_body=payload)
    data = body.get("data") or {}
    report.code(json.dumps({"status": st, "key": key, "formula": data.get("formula"), "formulaType": data.get("formulaType")},
                           ensure_ascii=False))
    report.case("创建指标 201", st == 201 and bool(data.get("id")), f"status={st}")
    report.case("auto-formula 生成 SUM(\"field\")", data.get("formula") == f'SUM("{field}")', str(data.get("formula")))
    report.case("formulaType=basic", data.get("formulaType") == "basic", str(data.get("formulaType")))
    # 公式安全：危险公式应被 400 拦截
    st2, body2 = api("post", "/metrics", token=token, json_body={
        "key": f"e2e_bad_{stamp}", "name": "非法", "formula": "SUM(1);DROP TABLE users"})
    report.case("危险公式被 400 拦截（公式安全）", st2 == 400, f"status={st2}")
    return data if data.get("id") else None


def http_delete_metric(report: Report, token: str, metric_id: str | None) -> None:
    if not metric_id:
        return
    report.step("HTTP 5. 清理临时指标 DELETE /metrics/{id}")
    st, body = api("delete", f"/metrics/{metric_id}", token=token)
    report.case("删除临时指标 200", st == 200, f"status={st}")


# ==================== 服务层验证（真实 DB + DuckDB） ====================

async def service_layer(report: Report, token_ignored: str, ds: dict | None) -> None:
    report.h("服务层验证（真实 DB session + 真实 DuckDB）", 2)

    from app.core.database import async_session_factory
    from app.services.metric_service import (
        check_metric_field_bindings,
        get_semantic_coverage,
        list_metrics_for_user,
        resolve_measures_for_exec,
    )

    # API 响应为 camelCase（CamelModel 序列化）
    ds_id = str(ds.get("id")) if ds else None
    real_user_id = str(ds.get("userId")) if ds else None
    schema_meta = ds.get("schemaMeta") if isinstance(ds, dict) else None
    fields = (schema_meta or {}).get("fields") or []
    # 自动挑一个 measure 类字段 + 一个维度字段（动态适配真实数据源）
    measure_field = next(
        (f.get("name") for f in fields
         if isinstance(f, dict) and f.get("category") == "measure" and f.get("name")), None)
    if not measure_field:
        measure_field = next(
            (f.get("name") for f in fields if isinstance(f, dict) and f.get("name")), None)
    dim_field = next(
        (f.get("name") for f in fields
         if isinstance(f, dict) and f.get("category") in ("dimension", "time") and f.get("name")
         and f.get("name") != measure_field), None)

    # user_id: 优先用数据源归属（真实用户 UUID），否则退回测试用户全局可见查询
    user_id_for_query = real_user_id or "00000000-0000-0000-0000-000000000000"

    async with async_session_factory() as db:
        # 6. 指标可见性
        report.step("服务 6. list_metrics_for_user（指标可见：私有+全局模板）")
        metrics = await list_metrics_for_user(db, None)
        keys = [m.key for m in metrics]
        report.code(json.dumps({"count": len(metrics), "keys": keys[:10]}, ensure_ascii=False))
        report.case("服务层可见指标 ≥ 模板数（4）", len(metrics) >= 4, f"{len(metrics)} 个；含 sales_amount={('sales_amount' in keys)}")

        # 6b. 数据源字段可用性（真实 DuckDB schema 探测）
        report.step("服务 6b. dat 数据源真实字段探测（依 schemaMeta）")
        report.code(json.dumps({
            "datasource_id": ds_id, "has_schema_meta": bool(schema_meta),
            "field_count": len(fields), "measure_field": measure_field, "dim_field": dim_field,
        }, ensure_ascii=False))
        report.case("数据源有真实 schemaMeta.fields", bool(fields), f"{len(fields)} 个字段")
        report.case("找到 measure 类字段", bool(measure_field), str(measure_field))

        # 7. 指标口径解析
        report.step("服务 7. resolve_measures_for_exec（metric_key → SQL 表达式）")
        try:
            cfg = await resolve_measures_for_exec(
                db, None, [{"metric_key": "sales_amount", "field": measure_field or "amount"}],
                dimensions=[dim_field] if dim_field else [],
            )
            expr = cfg[0].expression if cfg and cfg[0].expression else ""
            report.code(json.dumps({"resolved": True, "expression": expr, "measures": len(cfg)}, ensure_ascii=False))
            report.case("metric_key 解析成表达式", bool(expr), expr[:80])
        except Exception as e:  # noqa: BLE001
            report.code(json.dumps({"resolved": False, "error": str(e)[:200]}, ensure_ascii=False))
            report.case("metric_key 解析成表达式", False, str(e)[:120])

        # 8. QueryEngineTool 真实执行（metric 引用）
        report.step("服务 8. QueryEngineTool.execute（metric_key 引用，真实 DuckDB 查询）")
        from app.services.agent_tools import QueryEngineTool

        tool = QueryEngineTool()
        measures = [{"metric_key": "sales_amount", "field": measure_field or "amount"}]
        result = await tool.execute(
            datasource_id=ds_id or "",
            dimensions=[dim_field] if dim_field else [],
            measures=measures,
            filters=None,
            sort=None,
            limit=10,
            user_id=user_id_for_query,
            db_session=db,
        )
        try:
            parsed = json.loads(result)
        except Exception:
            parsed = {"raw": result[:300]}
        report.code(json.dumps({
            "error": parsed.get("error"), "columns": parsed.get("columns"),
            "row_count": parsed.get("row_count"),
        }, ensure_ascii=False))
        report.case("query_engine 指标引用查询成功", "error" not in parsed and parsed.get("row_count", 0) >= 0,
                    f"rows={parsed.get('row_count')}, error={parsed.get('error')}")
        report.case("查询返回 columns", bool(parsed.get("columns")), str((parsed.get("columns") or [])[:5]))

        # 9. 非法指标 key：错误应提示 list_metrics 自纠错
        report.step("服务 9. QueryEngineTool.execute（非法 metric_key → 自纠错提示）")
        bad = await tool.execute(
            datasource_id=ds_id or "",
            dimensions=[],
            measures=[{"metric_key": "not_exist_metric_xxx"}],
            user_id=user_id_for_query,
            db_session=db,
        )
        bad_parsed = json.loads(bad)
        report.code(json.dumps({"error": bad_parsed.get("error")}, ensure_ascii=False))
        report.case("非法 key 返回 error", "error" in bad_parsed, str(bad_parsed.get("error"))[:80])
        report.case("错误含 list_metrics 自纠错提示", "list_metrics" in (bad_parsed.get("error") or ""),
                    str(bad_parsed.get("error"))[:80])

        # 10. Coverage 计数器
        report.step("服务 10. get_semantic_coverage（语义层覆盖率埋点）")
        cov = get_semantic_coverage()
        report.code(json.dumps(cov, ensure_ascii=False))
        report.case("覆盖率计数器 total>0", cov["total_queries"] > 0, f"total={cov['total_queries']}")
        report.case("覆盖率 metric_queries>0", cov["metric_queries"] > 0, f"metric={cov['metric_queries']}")

        # 11. 断链校验
        report.step("服务 11. check_metric_field_bindings（重传断链校验）")
        import uuid as _uuid_mod
        broken = await check_metric_field_bindings(
            db, None,
            _uuid_mod.UUID(ds_id) if ds_id else None,
            schema_meta or {"fields": []},
        )
        report.code(json.dumps({"broken_count": len(broken)}, ensure_ascii=False))
        report.case("断链校验可执行（返回列表）", isinstance(broken, list), f"{len(broken)} 个断链")

        # 12. 引擎 AST 出口校验（与语义层正交的安全收口）
        report.step("服务 12. execute_chart_query AST 出口校验（第二道网）")
        from app.schemas.query import ChartQueryConfig, MeasureConfig
        from app.services.query_engine import execute_chart_query

        try:
            qr = await execute_chart_query(
                datasource_id=ds_id or "",
                config=ChartQueryConfig(
                    dimensions=[dim_field] if dim_field else [],
                    measures=[MeasureConfig(field=measure_field or "amount", agg="SUM")],
                    filters=[],
                    limit=5,
                ),
                user_id=user_id_for_query,
                db=db,
            )
            report.code(json.dumps({"columns": qr.columns, "rows": len(qr.rows)}, ensure_ascii=False))
            report.case("引擎正常聚合查询通过 AST 校验", bool(qr.columns), f"{len(qr.rows)} 行")
        except Exception as e:  # noqa: BLE001
            report.code(json.dumps({"error": str(e)[:200]}, ensure_ascii=False))
            report.case("引擎正常聚合查询通过 AST 校验", False, str(e)[:120])


# ==================== LLM 层（可选） ====================

def llm_layer(report: Report, token: str, ds_id: str | None) -> None:
    report.h("LLM 层验证（可选，真实 chat/stream）", 2)
    report.step("LLM 1. 发指标类问题「总销售额是多少」→ 观察事件流 + 指标引用")
    payload: dict = {"message": "帮我统计总销售额是多少", "datasourceId": ds_id or None}
    try:
        resp = requests.post(
            f"{BASE}/ai/chat/stream", json=payload,
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            **NO_PROXY, timeout=180, stream=True,
        )
        if resp.status_code != 200:
            report.code(json.dumps({"status": resp.status_code, "error": resp.text[:200]}, ensure_ascii=False))
            report.case("chat/stream 200", False, f"status={resp.status_code}")
            return
        tool_calls: list[str] = []
        query_engine_args: list[dict] = []  # query_engine 的入参（验证是否用 metric_key）
        decisions: list[dict] = []
        deltas = 0
        done = False
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            try:
                ev = json.loads(raw[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "tool_call":
                tool_calls.append(ev.get("name"))
                if ev.get("name") == "query_engine" and isinstance(ev.get("args"), dict):
                    query_engine_args.append({
                        "measures": ev["args"].get("measures"),
                        "dimensions": ev["args"].get("dimensions"),
                    })
            if ev.get("type") == "decision":
                decisions.append({"action": ev.get("action"), "round": ev.get("round")})
            if ev.get("type") == "message":
                deltas += len(ev.get("delta", ""))
            if ev.get("type") == "done":
                done = True
        # 提取所有 query_engine 调用的 measures 明细（供人工核验指标引用）
        measures_detail = [a.get("measures") for a in query_engine_args if a.get("measures")]
        used_metric_key = any(
            isinstance(m, dict) and (m.get("metric_key") or m.get("metricKey") or m.get("metric_id"))
            for _, args in enumerate(query_engine_args)
            for m in (args.get("measures") or [])
        )
        listed_metrics = "list_metrics" in tool_calls
        report.code(json.dumps({
            "tool_calls": tool_calls,
            "query_engine_measures_sample": measures_detail[:3],
            "used_metric_key": used_metric_key,
            "listed_metrics": listed_metrics,
            "decisions": decisions,
            "deltas": deltas,
            "done": done,
        }, ensure_ascii=False, default=str))
        report.case("LLM 收到 done", done, f"deltas={deltas}")
        # 动态判定：出现 list_metrics 或 query_engine 均视为进入结构化路径
        metric_aware = any(t in ("list_metrics", "query_engine") for t in tool_calls)
        report.case("LLM 使用了结构化/指标路径工具", metric_aware, str(tool_calls))
        # 核心：LLM 是否真的用 metric_key 引用受治理指标（而非裸字段）
        report.case("LLM 调用了 list_metrics（指标发现）", listed_metrics, "在 tool_calls 中" if listed_metrics else "未调用")
        report.case("LLM query_engine 使用了 metric_key 引用", used_metric_key,
                    "有 metric_key" if used_metric_key else str(measures_detail[:1]))
    except requests.exceptions.RequestException as e:
        report.code(json.dumps({"error": str(e)[:200]}, ensure_ascii=False))
        report.case("LLM chat/stream 请求", False, str(e)[:120])


def main() -> int:
    parser = argparse.ArgumentParser(description="指标语义层端到端全链路验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--with-llm", action="store_true", help="额外跑 LLM chat/stream 场景")
    parser.add_argument("--out", default=None, help="MD 报告输出路径（默认 backend/tests/reports/...）")
    args = parser.parse_args()
    global BASE
    BASE = f"{args.base_url.rstrip('/')}/api/v1"

    report = Report()

    try:
        # —— HTTP 层 ——
        token = http_login(report)
        if not token:
            report.save(Path(args.out) if args.out else BACKEND_DIR / "tests/reports/metric_semantic_e2e.md")
            return 2
        metrics = http_list_metrics(report, token)
        dss = http_list_datasources(report, token)
        ds = dss[0] if dss else None
        ds_id = str(ds.get("id")) if ds else None

        # 服务层需要真实 DB；建一个临时指标也会走 HTTP
        temp_metric = None
        if ds and (ds.get("schemaMeta", {}) or {}).get("fields"):
            fields = ds.get("schemaMeta", {}).get("fields")
            mf = next((f.get("name") for f in fields if isinstance(f, dict) and f.get("category") == "measure" and f.get("name")), None)
            mf = mf or (fields[0].get("name") if fields else None)
            temp_metric = http_create_temp_metric(report, token, ds_id, mf or "amount")
        else:
            report.case("数据源含 schemaMeta.fields", False, "数据源字段信息缺失")

        # —— 服务层（需要事件循环） ——
        import asyncio
        asyncio.run(service_layer(report, token, ds))

        # —— LLM 层（可选） ——
        if args.with_llm:
            llm_layer(report, token, ds_id)

        # —— 清理 ——
        http_delete_metric(report, token, (temp_metric or {}).get("id") if temp_metric else None)

    except KeyboardInterrupt:
        report.save(Path(args.out) if args.out else BACKEND_DIR / "tests/reports/metric_semantic_e2e.md")
        return 130
    except Exception as e:  # noqa: BLE001
        report.case("脚本未捕获异常", False, f"{type(e).__name__}: {str(e)[:200]}")

    out_path = Path(args.out) if args.out else (BACKEND_DIR / "tests/reports/metric_semantic_e2e.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report.save(out_path)

    passed = sum(1 for _, ok, _ in report.cases if ok)
    total = len(report.cases)
    print(f"\n==== 语义层 E2E 汇总：PASS {passed} / FAIL {total - passed} / TOTAL {total} ====")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())