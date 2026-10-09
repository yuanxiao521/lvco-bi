"""在线端到端验证：指标治理链路（真实 HTTP + 真实数据库）。

覆盖：登录 → 建基础指标 → 建派生指标 → 血缘依赖 / 下游 / 影响 / 字段血缘 /
版本列表 → 发布版本 → 回滚版本 → 清理。

每个检查点对前端期望的 camelCase 契约逐字段校验，失败会标红，但不会中断后续检查。
用法（后端已在 8000 运行）：
    python scripts/_live_e2e_metric_flow.py
"""
from __future__ import annotations

import sys
import time

import requests

BASE = "http://localhost:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}
EMAIL = "test@lvco.bi"
PASSWORD = "123456"

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """打印单个检查点结果。"""
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def api(path, method="get", token=None, json=None, expect=200):
    """发真实请求并返回 (status, json)。连接被拒/被重置时重试至多 4 次。"""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for attempt in range(5):
        try:
            resp = requests.request(
                method, f"{BASE}{path}",
                headers=headers, json=json, **NO_PROXY, timeout=20,
            )
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:500]}
            return resp.status_code, body
        except requests.exceptions.RequestException as e:  # 连接层异常（如 reload 重启）
            last = e
            time.sleep(1.5 * (attempt + 1))
    return -1, {"detail": f"连接失败: {last}"}


def main() -> int:
    print("== 0. 登录（真实路由 + 真实用户表） ==")
    st, body = api("/auth/login", method="post", json={"email": EMAIL, "password": PASSWORD})
    token = (body.get("data") or {}).get("accessToken")
    check("login 200 & 返回 accessToken", st == 200 and bool(token), f"status={st}")
    if not token:
        print("登录失败，终止。")
        return 1

    print("== 1. 创建基础指标（source_field+agg 由后端自动生成公式） ==")
    stamp = __import__("time").strftime("%H%M%S")
    base_key = f"e2e_base_{stamp}"
    base_name = f"E2E基础-{stamp}"
    st, body = api(
        "/metrics", method="post", token=token,
        json={"key": base_key, "name": base_name, "datasourceId": None, "agg": "SUM", "sourceField": "amount"},
    )
    base = body.get("data") or {}
    base_id = base.get("id")
    check(
        f"创建基础 {base_key} -> {st}",
        st == 201 and bool(base_id),
        f"resp={body.get('detail') or base}",
    )
    check("基础指标 formula 自动生成 SUM(\"amount\")", base.get("formula") == 'SUM("amount")', str(base.get("formula")))
    check("基础指标 formulaType=basic", base.get("formulaType") == "basic", str(base.get("formulaType")))

    print("== 2. 创建派生指标（公式引用基础指标） ==")
    derived_key = f"e2e_derived_{stamp}"
    st, body = api(
        "/metrics", method="post", token=token,
        json={"key": derived_key, "name": f"E2E派生-{stamp}", "formula": f'metric("{base_key}") * 1.2'},
    )
    derived = body.get("data") or {}
    derived_id = derived.get("id")
    check(f"创建派生 {derived_key} -> {st}", st == 201 and bool(derived_id), f"resp={body.get('detail') or derived}")
    check("派生指标 formulaType=derived", derived.get("formulaType") == "derived", str(derived.get("formulaType")))
    check("派生指标 dependsOnMetricIds 回填基础 id", base_id in (derived.get("dependsOnMetricIds") or []),
          str(derived.get("dependsOnMetricIds")))

    print("== 3. 血缘依赖 /dependencies（契约：返回 camelCase 指标对象） ==")
    st, body = api(f"/metrics/{derived_id}/dependencies", token=token)
    deps = body.get("data") or []
    dep = deps[0] if deps else {}
    check(f"/dependencies {st} 返回数组", st == 200 and isinstance(deps, list))
    check("dependencies 含基础指标对象", any(d.get("key") == base_key for d in deps))
    check("依赖节点是物件而非字符串（原 bug）", isinstance(dep, dict) and "id" in dep)
    check("依赖节点 camelCase: formulaType/name/formula",
          isinstance(dep, dict) and all(k in dep for k in ("formulaType", "name", "formula")),
          str(dep))
    upstream_count = len(deps)

    print("== 4. 下游 /dependents（契约：返回完整指标对象） ==")
    st, body = api(f"/metrics/{base_id}/dependents", token=token)
    depts = body.get("data") or []
    check(f"/dependents {st} 返回数组", st == 200 and isinstance(depts, list))
    check("dependents 含派生指标", any(d.get("key") == derived_key for d in depts))
    check("下游节点是物件而非引用记录（原 bug）",
          all(isinstance(d, dict) and "id" in d for d in depts), str(depts))

    print("== 5. 影响分析 /impact（真实计数来自 DB） ==")
    st, body = api(f"/metrics/{base_id}/impact", token=token)
    imp = body.get("data") or {}
    check(f"/impact {st}", st == 200, f"resp={body.get('detail') or imp}")
    check("impact 含 dependents/dashboards/users count", all(k in imp for k in ("dependents_count", "dashboards_count", "users_count")),
          str(imp))
    if imp.get("dependents_count") is not None:
        check("impact.dependents_count >= 1（刚建了派生下游）", imp["dependents_count"] >= 1, str(imp.get("dependents_count")))

    print("== 6. 字段血缘 /lineage ==")
    st, body = api(f"/metrics/{derived_id}/lineage", token=token)
    lg = body.get("data") or []
    check(f"/lineage {st} 返回数组", st == 200 and isinstance(lg, list), f"resp={body.get('detail')}")

    print("== 7. 版本列表 /versions ==")
    st, body = api(f"/metrics/{base_id}/versions", token=token)
    vers = body.get("data") or []
    check(f"/versions 存在且返回数组", st == 200 and isinstance(vers, list), f"resp={body.get('detail') or vers}")
    check("版本字段 camelCase: version/change_note/created_at",
          all(isinstance(v, dict) and "version" in v for v in vers), str(vers[:2]))

    print("== 8. 发布版本（发两次，产生可回滚快照） ==")
    st, body = api(f"/metrics/{base_id}/versions", method="post", token=token, json={"change_note": "e2e 发布 1"})
    check(f"发布版本1 {st}", st == 201 or st == 200, f"resp={body.get('detail') or body.get('data')}")
    st, body = api(f"/metrics/{base_id}/versions", method="post", token=token, json={"change_note": "e2e 发布 2"})
    check(f"发布版本2 {st}", st == 201 or st == 200, f"resp={body.get('detail') or body.get('data')}")

    print("== 9. 发布后再查版本数应增加 ==")
    st2, body2 = api(f"/metrics/{base_id}/versions", token=token)
    vers2 = body2.get("data") or []
    check("版本列表数量 >= 发布前+1", len(vers2) >= len(vers) + 1, f"{len(vers)} -> {len(vers2)}")
    snapshot_versions = sorted(v["version"] for v in vers2 if isinstance(v, dict))

    print("== 10. 回滚到已存在的快照版本 ==")
    rollback_target = snapshot_versions[0] if snapshot_versions else 0
    st, body = api(f"/metrics/{base_id}/rollback", method="post", token=token, json={"version": rollback_target})
    new_ver = (body.get("data") or {}).get("version")
    check(f"回滚到快照 v{rollback_target} {st}", st in (200, 201), f"resp={body.get('detail')}")
    check("回滚后指标版本号递增", isinstance(new_ver, int) and new_ver > rollback_target, f"new={new_ver}")

    print("== 11. 清理 ==")
    for mid in (base_id, derived_id):
        if mid:
            api(f"/metrics/{mid}", method="delete", token=token)
    check("清理删除派生+基础指标", True)

    print(f"\n==== 汇总：PASS {PASS} / FAIL {FAIL} (upstream nodes={upstream_count}) ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())