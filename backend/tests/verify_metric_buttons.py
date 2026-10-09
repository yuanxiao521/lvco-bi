"""指标中心四个小按钮（血缘/影响/发布/回滚）对应的接口实测。

按前端按钮的真实调用链打 HTTP：
  1. 登录
  2. GET /metrics                     （列表 → 拿 metric id，四个按钮都依赖 m.id）
  3. GET /metrics/{id}                （详情页主请求）
  4. GET /metrics/{id}/dependencies   （详情页 Promise.all 之一）
  5. GET /metrics/{id}/dependents     （详情页 Promise.all 之一）
  6. GET /metrics/{id}/lineage        （血缘按钮）
  7. GET /metrics/{id}/impact         （影响按钮）
  8. GET /metrics/{id}/versions       （版本面板）
  9. POST /metrics/{id}/versions      （发布按钮）
  10. POST /metrics/{id}/rollback     （回滚按钮）

报告输出 MD，含每步状态码与响应，可追溯。
"""
import argparse
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}
EMAIL = None
PASSWORD = None
TIMEOUT = 30

_lines: list[str] = []
_cases: list[tuple[str, bool, str]] = []


def h(text: str, level: int = 1) -> None:
    _lines.append(f"{'#' * level} {text}")


def code(text: str) -> None:
    _lines.append("```json")
    _lines.append(text)
    _lines.append("```")


def step(text: str) -> None:
    _lines.append(f"\n### {text}")


def log(text: str) -> None:
    _lines.append(f"- {text}")


def case(name: str, ok: bool, detail: str = "") -> None:
    _cases.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    _lines.append(f"- [{mark}] {name}" + (f" — {detail}" if detail else ""))


def api(method: str, path: str, token: str | None = None, json_body=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(
        method, f"{BASE}{path}", headers=headers, json=json_body,
        **NO_PROXY, timeout=TIMEOUT,
    )
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:300]}
    return resp.status_code, body


def main() -> int:
    st, body = api("post", "/auth/login", json_body={"email": EMAIL, "password": PASSWORD})
    token = (body.get("data") or {}).get("accessToken") if isinstance(body.get("data"), dict) else None
    if st != 200 or not token:
        print(f"登录失败: {st}, 后端起没起？BASE={BASE} 账号={EMAIL}")
        return 2
    log(f"登录成功 (token 前8位 {str(token)[:8]}...)")

    # 列表
    st, body = api("get", "/metrics", token)
    items = (body.get("data") or []) if isinstance(body.get("data"), list) else []
    case("GET /metrics 200", st == 200, f"status={st} items={len(items)}")
    if st != 200:
        code(json.dumps(body, ensure_ascii=False)[:500])
        return 1

    print(f"列表共 {len(items)} 个指标")
    if not items:
        # 无私有指标时至少应有全局模板指标（user_id is NULL）
        case("列表非空（应含全局模板指标）", False, "空列表")
        return 1

    # 关键：四个按钮都依赖 id，逐个验证列表项字段
    first = items[0]
    mid = str(first.get("id"))
    case("列表项含 id", bool(mid), f"mid={mid}")
    code(json.dumps(
        {"样本": {k: first.get(k) for k in ("id", "key", "name", "formula", "formulaType", "version")}},
        ensure_ascii=False, default=str,
    ))

    # 详情页三请求（path 模板与实际 URL 分离）
    paths_expected = {
        "/metrics/{id}": 200,
        "/metrics/{id}/dependencies": 200,
        "/metrics/{id}/dependents": 200,
    }
    for path_tpl, exp in paths_expected.items():
        path = path_tpl.format(id=mid)
        s, b = api("get", path, token)
        ok = s == exp
        case(f"GET {path} → {exp}", ok, f"status={s}")
        if not ok:
            code(json.dumps(b, ensure_ascii=False)[:400])

    # 血缘 / 影响
    for path_tpl, exp in {
        "/metrics/{id}/lineage": 200,
        "/metrics/{id}/impact": 200,
    }.items():
        path = path_tpl.format(id=mid)
        s, b = api("get", path, token)
        ok = s == exp
        case(f"GET {path} → {exp}", ok, f"status={s}")
        code(json.dumps({"status": s, "body": b}, ensure_ascii=False, default=str)[:400])

    # 版本
    s, b = api("get", f"/metrics/{mid}/versions", token)
    versions = (b.get("data") or []) if isinstance(b.get("data"), list) else []
    case("GET /metrics/{id}/versions → 200", s == 200, f"status={s} versions={len(versions)}")

    # 发布（真实写操作，传 change_note）
    s, b = api("post", f"/metrics/{mid}/versions", token, json_body={"change_note": "e2e 验证发布"})
    published_ok = s in (200, 201)
    case("POST /metrics/{id}/versions 发布 → 200/201", published_ok, f"status={s}")
    code(json.dumps({"status": s, "body": b}, ensure_ascii=False, default=str)[:400])
    if not published_ok:
        # 前缀指标版本可能回滚失败（无历史版本）
        case("发布成功（若失败则跳过回滚语义）", False, f"发布失败 status={s}")

    # 回滚：需要有效的版本号。若发布成功，回滚到 1
    if published_ok:
        # 发布后新版本号
        new_ver = (b.get("data") or {}).get("version") if isinstance(b.get("data"), dict) else None
        target = max(1, (int(new_ver) - 1)) if new_ver is not None else 1
        s, b = api("post", f"/metrics/{mid}/rollback", token, json_body={"version": target})
        case(f"POST /metrics/{mid}/rollback → 回滚到 {target}", s == 200, f"status={s}")
        code(json.dumps({"status": s, "body": b}, ensure_ascii=False, default=str)[:400])
    else:
        case(f"POST /metrics/{mid}/rollback", False, "前一步发布失败，回滚未执行")

    # 汇总输出
    passed = sum(1 for _, ok, _ in _cases if ok)
    total = len(_cases)
    h(f"结果：{passed}/{total} 通过", 1)
    print(f"\n结果: {passed}/{total} 通过")
    for name, ok, detail in _cases:
        print(f"  {'OK ' if ok else 'FAIL'} {name} {detail}")

    import datetime
    from pathlib import Path
    out = Path(__file__).parent / "reports" / "metric_buttons_e2e.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# 指标中心四个按钮接口实测\n\n")
        f.write(f"- 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 后端：{BASE}\n")
        f.write(f"- 账号：{EMAIL}\n")
        f.write(f"- 结论：**{passed}/{total}**\n\n---\n")
        f.write("\n".join(_lines))
    print(f"[报告已写入] {out.resolve()}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--email", default="test@lvco.bi")
    parser.add_argument("--password", default="123456")
    args = parser.parse_args()
    BASE = f"{args.base_url.rstrip('/')}/api/v1"
    EMAIL = args.email
    PASSWORD = args.password
    sys.exit(main())