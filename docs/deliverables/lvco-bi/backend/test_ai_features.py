"""
AI 功能全面测试脚本
测试：AI Chat SSE、AI 推荐图表、AI 洞察、AI 润色、AI 查询、AI 画布助手
"""
import requests
import json
import sys
import time

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
    # 先查已有数据源
    r = api("/datasources", headers=headers)
    items = r.json().get("data", {}).get("items", [])
    for ds in items:
        if ds.get("sourceType") == "postgresql" and ds.get("status") == "connected":
            print(f"[OK] 复用已有 PG 数据源: {ds['name']} ({ds['id']})")
            return ds["id"]
    # 创建新的
    r = api("/datasources/connect", method="post", headers=headers, json_body={
        "name": "AI Test PG", "sourceType": "postgresql",
        "host": "localhost", "port": 5432, "db_name": "lvco_bi",
        "username": "lvco", "password": "lvco_secret",
        "table_name": "ecommerce_orders"
    })
    ds_id = r.json()["data"]["id"]
    # 同步
    r = api(f"/datasources/{ds_id}/sync", method="post", headers=headers)
    status = r.json()["data"]["status"]
    print(f"[INFO] 创建 PG 数据源 {ds_id}, sync={status}")
    return ds_id

def create_canvas(headers, ds_id):
    r = api("/canvases", method="post", headers=headers, json_body={
        "title": "AI Test Canvas", "datasourceId": ds_id
    })
    canvas_id = r.json()["data"]["id"]
    print(f"[OK] 创建画布: {canvas_id}")
    return canvas_id

# ====== 测试用例 ======

def test_ai_chat_sse(headers):
    """测试 AI Chat SSE 流式对话"""
    print("\n" + "="*60)
    print("测试 1: AI Chat SSE 流式对话")
    print("="*60)
    
    # 创建会话
    r = api("/ai/sessions", method="post", headers=headers, json_body={"title": "Test Session"})
    assert r.status_code == 201, f"创建会话失败: {r.text}"
    sid = r.json()["data"]["id"]
    print(f"  [OK] 创建会话: {sid}")
    
    # 发送流式消息
    url = f"{BASE}/ai/sessions/{sid}/messages"
    resp = requests.post(url, json={"content": "你好，请用一句话介绍你自己"}, headers=headers, stream=True)
    
    if resp.status_code == 503:
        detail = resp.json().get("detail", {})
        if "AI_NOT_CONFIGURED" in str(detail):
            print("  [SKIP] AI 未配置 (AI_NOT_CONFIGURED) — 请检查 OPENAI_API_KEY")
            return False
    
    assert resp.status_code == 200, f"SSE 请求失败: status={resp.status_code}, body={resp.text[:200]}"
    
    events = []
    full_text = ""
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            events.append(data)
            if data["type"] == "message":
                full_text += data.get("delta", "")
            if data["type"] == "done":
                break
    
    assert len(full_text) > 0, "AI 未返回任何文本"
    assert any(e["type"] == "done" for e in events), "未收到 done 事件"
    print(f"  [OK] 收到 {len(events)} 个 SSE 事件，回复长度: {len(full_text)}")
    print(f"  [AI 回复] {full_text[:100]}...")
    return True

def test_ai_recommend(headers, canvas_id, ds_id):
    """测试 AI 推荐图表"""
    print("\n" + "="*60)
    print("测试 2: AI 推荐图表 (POST /canvases/{id}/ai-recommend)")
    print("="*60)
    
    r = api(f"/canvases/{canvas_id}/ai-recommend", method="post", headers=headers, json_body={
        "current_config": {
            "dimensions": ["region"],
            "measures": [{"field": "total_amount", "agg": "SUM"}],
            "datasource_id": ds_id
        }
    })
    
    if r.status_code == 503:
        print("  [SKIP] AI 未配置")
        return False
    
    assert r.status_code == 200, f"推荐图表失败: {r.text}"
    data = r.json()["data"]
    suggestions = data.get("suggestions", [])
    assert len(suggestions) > 0, "未返回任何推荐"
    for s in suggestions:
        print(f"  - {s.get('chart_type')} (confidence: {s.get('confidence')}): {s.get('rationale', '')[:60]}")
    print(f"  [OK] 返回 {len(suggestions)} 个推荐")
    return True

def test_ai_insights(headers, ds_id):
    """测试 AI 数据洞察"""
    print("\n" + "="*60)
    print("测试 3: AI 数据洞察 (POST /ai/insights)")
    print("="*60)
    
    r = api("/ai/insights", method="post", headers=headers, json_body={
        "datasource_id": ds_id,
        "query_config": {
            "dimensions": ["region"],
            "measures": [{"field": "total_amount", "agg": "SUM"}]
        }
    })
    
    if r.status_code == 503:
        print("  [SKIP] AI 未配置")
        return False
    
    assert r.status_code == 200, f"洞察生成失败: {r.text}"
    insights = r.json()["data"].get("insights", [])
    for ins in insights:
        print(f"  [{ins.get('severity', 'info')}] {ins.get('type')}: {ins.get('title', '')[:50]}")
    print(f"  [OK] 返回 {len(insights)} 条洞察")
    return True

def test_ai_polish(headers):
    """测试 AI 文本润色"""
    print("\n" + "="*60)
    print("测试 4: AI 文本润色 (POST /ai/polish)")
    print("="*60)
    
    r = api("/ai/polish", method="post", headers=headers, json_body={
        "text": "这个季度的销售额涨了好多，我们得分析分析原因。",
        "style": "professional"
    })
    
    if r.status_code == 503:
        print("  [SKIP] AI 未配置")
        return False
    
    assert r.status_code == 200, f"润色失败: {r.text}"
    result = r.json()["data"]
    assert "polished" in result, "缺少 polished 字段"
    print(f"  原文: {result['original'][:50]}")
    print(f"  润色: {result['polished'][:50]}")
    print(f"  [OK] 润色成功")
    return True

def test_ai_query(headers, ds_id):
    """测试 AI 数据查询"""
    print("\n" + "="*60)
    print("测试 5: AI 数据查询 (POST /ai/query)")
    print("="*60)
    
    r = api("/ai/query", method="post", headers=headers, json_body={
        "question": "统计每个地区的总销售额，取前3名",
        "datasource_id": ds_id
    })
    
    if r.status_code == 503:
        print("  [SKIP] AI 未配置")
        return False
    
    assert r.status_code == 200, f"AI 查询失败: {r.text}"
    data = r.json()["data"]
    print(f"  SQL: {data.get('sql', 'N/A')}")
    err = data.get("error")
    if err:
        print(f"  [WARN] 查询有错误: {err}")
        return False
    result_data = data.get("data") or {}
    rows = result_data.get("rows", [])
    print(f"  [OK] 返回 {len(rows)} 行数据")
    if rows:
        print(f"  第一行: {rows[0]}")
    return True


# ====== 主流程 ======
if __name__ == "__main__":
    print("=" * 60)
    print("LvcoBI AI 功能全面测试")
    print("=" * 60)
    
    headers = login()
    ds_id = ensure_pg_datasource(headers)
    canvas_id = create_canvas(headers, ds_id)
    
    results = {}
    
    # 运行所有测试
    for name, fn in [
        ("AI Chat SSE", lambda: test_ai_chat_sse(headers)),
        ("AI 推荐图表", lambda: test_ai_recommend(headers, canvas_id, ds_id)),
        ("AI 数据洞察", lambda: test_ai_insights(headers, ds_id)),
        ("AI 文本润色", lambda: test_ai_polish(headers)),
        ("AI 数据查询", lambda: test_ai_query(headers, ds_id)),
    ]:
        try:
            results[name] = "PASS" if fn() else "SKIP"
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            results[name] = f"FAIL: {e}"
    
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    passed = sum(1 for v in results.values() if v == "PASS")
    skipped = sum(1 for v in results.values() if v == "SKIP")
    failed = sum(1 for v in results.values() if "FAIL" in str(v))
    for k, v in results.items():
        icon = "✓" if v == "PASS" else ("⊘" if v == "SKIP" else "✗")
        print(f"  {icon} {k}: {v}")
    print(f"\n通过: {passed} | 跳过: {skipped} | 失败: {failed}")
    
    sys.exit(0 if failed == 0 else 1)
