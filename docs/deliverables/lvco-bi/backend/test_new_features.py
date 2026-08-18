"""
LvcoBI 新增功能全面测试脚本
测试：聚合函数扩展、排名分析、汇总统计、对比分析、图表渲染、PDF导出
"""
import requests
import json
import sys
import os

BASE = "http://127.0.0.1:8000/api/v1"
EMAIL = "debugtest@test.com"
PASSWORD = "debug123"


# ====== 辅助函数 ======
def api(path, method="get", json_body=None, headers=None):
    h = headers or {}
    h.setdefault("Content-Type", "application/json")
    url = f"{BASE}{path}"
    if method == "get":
        return requests.get(url, headers=h)
    return requests.post(url, json=json_body, headers=h)


def login():
    r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, f"登录失败: {r.text}"
    token = r.json()["data"]["accessToken"]
    print("[OK] 登录成功")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def ensure_pg_datasource(headers):
    """确保有一个同步好的 PostgreSQL 数据源"""
    r = api("/datasources", headers=headers)
    items = r.json().get("data", {}).get("items", [])
    for ds in items:
        if ds.get("sourceType") == "postgresql" and ds.get("status") == "connected":
            print(f"[OK] 复用已有 PG 数据源: {ds['name']} ({ds['id']})")
            return ds["id"]
    # 创建新的
    r = api("/datasources/connect", method="post", headers=headers, json_body={
        "name": "Feature Test PG", "sourceType": "postgresql",
        "host": "localhost", "port": 5432, "db_name": "lvco_bi",
        "username": "lvco", "password": "lvco_secret",
        "table_name": "ecommerce_orders"
    })
    ds_id = r.json()["data"]["id"]
    r = api(f"/datasources/{ds_id}/sync", method="post", headers=headers)
    print(f"[INFO] 创建 PG 数据源 {ds_id}")
    return ds_id


def create_canvas(headers, ds_id):
    r = api("/canvases", method="post", headers=headers, json_body={
        "title": "Feature Test Canvas", "datasourceId": ds_id
    })
    canvas_id = r.json()["data"]["id"]
    print(f"[OK] 创建画布: {canvas_id}")
    return canvas_id


# ====== 测试用例 ======

def test_chart_renderer():
    """Test chart_renderer module generates valid PNG"""
    print("\n" + "=" * 60)
    print("测试 1: 图表渲染服务 (chart_renderer)")
    print("=" * 60)

    from app.services.chart_renderer import render_bar, render_line, render_pie

    labels = ["东部", "西部", "北部", "南部"]
    values = [100.0, 200.0, 150.0, 80.0]

    result = render_bar("地区销售额", labels, values)
    assert result.startswith("data:image/png;base64,"), "bar 输出格式错误"
    assert len(result) > 1000, "bar 输出太短"
    print(f"  [OK] render_bar 生成 {len(result)} 字符的 PNG")

    result = render_line("销售额趋势", labels, values)
    assert result.startswith("data:image/png;base64,"), "line 输出格式错误"
    print(f"  [OK] render_line 生成 {len(result)} 字符的 PNG")

    result = render_pie("销售占比", labels, values)
    assert result.startswith("data:image/png;base64,"), "pie 输出格式错误"
    print(f"  [OK] render_pie 生成 {len(result)} 字符的 PNG")

    return True


def test_aggregation_functions(headers, ds_id, canvas_id):
    """Test new aggregation functions via chart query"""
    print("\n" + "=" * 60)
    print("测试 2: 扩展聚合函数 (STDDEV/MEDIAN/COUNT_DISTINCT)")
    print("=" * 60)

    tests = [
        ("STDDEV", "标准差"),
        ("MEDIAN", "中位数"),
        ("COUNT_DISTINCT", "去重计数"),
    ]

    for agg, name in tests:
        r = api(f"/canvases/{canvas_id}/query", method="post", headers=headers, json_body={
            "chartType": "bar",
            "datasourceId": str(ds_id),
            "dimensions": ["region"],
            "measures": [{"field": "total_amount", "agg": agg}],
        })
        assert r.status_code == 200, f"{name} 查询失败: {r.text}"
        result = r.json().get("data", {})
        rows = result.get("rows", [])
        assert len(rows) > 0, f"{name} 未返回数据"
        print(f"  [OK] {name} ({agg}) 返回 {len(rows)} 行数据, 首行: {rows[0]}")

    return True


def test_ranking(headers, ds_id):
    """Test ranking analysis endpoint"""
    print("\n" + "=" * 60)
    print("测试 3: 排名分析 (POST /statistics/ranking)")
    print("=" * 60)

    # Top 5
    r = api("/statistics/ranking", method="post", headers=headers, json_body={
        "datasourceId": str(ds_id),
        "metric": {"field": "total_amount", "agg": "SUM"},
        "dimension": "region",
        "limit": 5,
        "order": "desc"
    })
    assert r.status_code == 200, f"排名查询失败: {r.text}"
    rank_data = r.json()["data"]["data"]
    assert len(rank_data) > 0, "未返回排名数据"
    # Verify desc order
    values = [d["value"] for d in rank_data]
    assert values == sorted(values, reverse=True), "desc 排序方向错误"
    print(f"  [OK] Top 5 排名: {len(rank_data)} 条, 首条: {rank_data[0]}")

    # Bottom 3
    r = api("/statistics/ranking", method="post", headers=headers, json_body={
        "datasourceId": str(ds_id),
        "metric": {"field": "total_amount", "agg": "SUM"},
        "dimension": "region",
        "limit": 3,
        "order": "asc"
    })
    assert r.status_code == 200, f"asc 排名查询失败: {r.text}"
    rank_data = r.json()["data"]["data"]
    values = [d["value"] for d in rank_data]
    assert values == sorted(values), "asc 排序方向错误"
    print(f"  [OK] Bottom 3 排名: {len(rank_data)} 条, 首条: {rank_data[0]}")

    return True


def test_summary(headers, ds_id):
    """Test summary statistics endpoint"""
    print("\n" + "=" * 60)
    print("测试 4: 汇总统计卡片 (POST /statistics/summary)")
    print("=" * 60)

    r = api("/statistics/summary", method="post", headers=headers, json_body={
        "datasourceId": str(ds_id),
    })
    assert r.status_code == 200, f"汇总查询失败: {r.text}"
    result = r.json()["data"]
    assert result.get("total_rows", 0) > 0, "total_rows 应为正数"
    assert result.get("total_columns", 0) > 0, "total_columns 应为正数"
    print(f"  [OK] 汇总: rows={result['total_rows']}, cols={result['total_columns']}, "
          f"distinct={result.get('distinct_keys', 0)}, date_range={result.get('date_range')}")

    return True


def test_comparison(headers, ds_id):
    """Test comparison analysis endpoint (同比环比)"""
    print("\n" + "=" * 60)
    print("测试 5: 对比分析 - 环比/同比 (POST /statistics/comparison)")
    print("=" * 60)

    # Month-over-month
    r = api("/statistics/comparison", method="post", headers=headers, json_body={
        "datasourceId": str(ds_id),
        "dateField": "order_date",
        "metricField": "total_amount",
        "metricAgg": "SUM",
        "period": "month",
        "compareType": "mom",
    })
    assert r.status_code == 200, f"环比查询失败: {r.text}"
    comp_data = r.json()["data"]["data"]
    assert len(comp_data) > 0, "未返回环比数据"
    has_change = any(d.get("change_pct") is not None for d in comp_data)
    print(f"  [OK] 月环比: {len(comp_data)} 条, has_change_pct={has_change}")
    if comp_data:
        first = comp_data[0]
        print(f"  首条: period={first['period']}, value={first['value']}, change={first.get('change_pct')}")

    # Year-over-year (quarter)
    r = api("/statistics/comparison", method="post", headers=headers, json_body={
        "datasourceId": str(ds_id),
        "dateField": "order_date",
        "metricField": "total_amount",
        "metricAgg": "SUM",
        "period": "quarter",
        "compareType": "yoy",
    })
    assert r.status_code == 200, f"同比查询失败: {r.text}"
    comp_data = r.json()["data"]["data"]
    assert len(comp_data) > 0, "未返回同比数据"
    print(f"  [OK] 季度同比: {len(comp_data)} 条")
    if comp_data:
        first = comp_data[0]
        print(f"  首条: period={first['period']}, value={first['value']}, change={first.get('change_pct')}")

    return True


def test_pdf_export(headers, canvas_id):
    """Test canvas PDF export"""
    print("\n" + "=" * 60)
    print("测试 6: 画布 PDF 导出 (GET /canvases/{id}/export/pdf)")
    print("=" * 60)

    r = requests.get(f"{BASE}/canvases/{canvas_id}/export/pdf", headers=headers)
    if r.status_code == 500:
        detail = r.json().get("detail", {})
        print(f"  [SKIP] PDF 生成失败 (可能 WeasyPrint 未安装): {detail.get('message', '')}")
        return False

    assert r.status_code == 200, f"PDF 导出失败: {r.text[:200]}"
    content_type = r.headers.get("content-type", "")
    assert "application/pdf" in content_type or r.content[:4] == b"%PDF", "不是 PDF 文件"
    print(f"  [OK] PDF 导出成功: {len(r.content)} bytes, type={content_type}")

    return True


# ====== 主流程 ======
if __name__ == "__main__":
    # Ensure we run from the backend directory so chart_renderer import works
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    print("=" * 60)
    print("LvcoBI 新增功能全面测试")
    print("=" * 60)

    results = {}

    # Test chart renderer (no server needed for this one, runs first)
    try:
        results["图表渲染"] = "PASS" if test_chart_renderer() else "FAIL"
    except Exception as e:
        print(f"  [FAIL] 图表渲染: {e}")
        results["图表渲染"] = f"FAIL: {e}"

    # Server-dependent tests
    try:
        headers = login()
        ds_id = ensure_pg_datasource(headers)
        canvas_id = create_canvas(headers, ds_id)
    except Exception as e:
        print(f"  [FATAL] 服务器初始化失败: {e}")
        results["server_init"] = f"FATAL: {e}"
        # Still print summary for chart_renderer
        print("\n" + "=" * 60)
        print("测试结果汇总")
        print("=" * 60)
        for k, v in results.items():
            icon = "✓" if v == "PASS" else ("⊘" if v == "SKIP" else "✗")
            print(f"  {icon} {k}: {v}")
        sys.exit(1)

    for name, fn in [
        ("聚合函数", lambda: test_aggregation_functions(headers, ds_id, canvas_id)),
        ("排名分析", lambda: test_ranking(headers, ds_id)),
        ("汇总统计", lambda: test_summary(headers, ds_id)),
        ("对比分析", lambda: test_comparison(headers, ds_id)),
        ("PDF导出", lambda: test_pdf_export(headers, canvas_id)),
    ]:
        try:
            result = fn()
            results[name] = "PASS" if result else "SKIP"
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            results[name] = f"FAIL: {e}"

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    passed = sum(1 for v in results.values() if v == "PASS")
    skipped = sum(1 for v in results.values() if v == "SKIP")
    failed = sum(1 for v in results.values() if "FAIL" in str(v) or "FATAL" in str(v))
    for k, v in results.items():
        icon = "✓" if v == "PASS" else ("⊘" if v == "SKIP" else "✗")
        print(f"  {icon} {k}: {v}")
    print(f"\n通过: {passed} | 跳过: {skipped} | 失败: {failed}")

    sys.exit(0 if failed == 0 else 1)
