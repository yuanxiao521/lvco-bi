# -*- coding: utf-8 -*-
"""P0-② 验收：新旧 assess_subtask 在输入矩阵上**逐字节等价**。

做法：从 git HEAD 取出重构前的函数源码，exec 成参照实现，与当前实现逐一对比。
这比手写期望值可靠——期望值本身可能就是照抄新实现。
"""
import ast, itertools, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from app.services.agents.lead.lead_tools import (  # noqa: E402
    RunAnalysisResult,
    _dimensions_covered,
    assess_subtask,
    assess_subtask_detail,
)
from app.services.agents.planner_agent import _count_named_dimensions  # noqa: E402

# ── 取出 HEAD 版本的旧实现 ──
src = subprocess.run(
    ["git", "show", "HEAD:backend/app/services/agents/lead/lead_tools.py"],
    cwd=str(ROOT.parent), capture_output=True, text=True, encoding="utf-8", errors="replace",
).stdout
assert src, "git show 取不到旧版本"
tree = ast.parse(src)
old_src = next(ast.get_source_segment(src, n) for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "assess_subtask")
ns = {
    "RunAnalysisResult": RunAnalysisResult,
    "_count_named_dimensions": _count_named_dimensions,
    "_dimensions_covered": _dimensions_covered,
}
exec(compile(old_src, "<old_assess_subtask>", "exec"), ns)
old = ns["assess_subtask"]

# ── 输入矩阵 ──
CHAINS = [
    [],
    [{"name": "run_query", "ok": True}],
    [{"name": "add_chart_block", "ok": False}],
    [{"name": "add_chart_block", "ok": True, "args": '{"dimensions": ["category"]}'}],
    [{"name": "update_chart_block", "ok": True, "args": '{"dimensions": ["brand"]}'}],
]
GOALS = ["", "统计各地区销售额", "按品类和品牌分别分析", "按品类、品牌、季节三个维度分析"]
cases = []
for success, verified, blocks, entry, chain, goal in itertools.product(
    (True, False), (True, False), (0, 3), ("chat", "canvas"), CHAINS, GOALS
):
    cases.append((RunAnalysisResult(
        success=success, verified=verified, blocks_added=blocks, tool_chain=chain,
        error=None if success else "查询超时", report="r", steps=[],
    ), entry, goal))

bad = 0
for i, (res, entry, goal) in enumerate(cases):
    a = old(res, entry=entry, user_goal=goal)
    b = assess_subtask(res, entry=entry, user_goal=goal)
    d = assess_subtask_detail(res, entry=entry, user_goal=goal)
    if a != b:
        bad += 1
        if bad <= 3:
            print(f"不等价 #{i}: entry={entry} goal={goal!r}")
            print("  old:", a)
            print("  new:", b)
    assert d.text == b, f"detail.text 与 assess_subtask 不一致 #{i}"
    # hard/soft 必须恰好覆盖 reasons，且顺序与文本一致
    reasons = a.split("上轮未达标：")[1].split("。建议")[0].split("；") if "未达标" in a else []
    def _subseq(sub, seq):
        it = iter(seq)
        return all(x in it for x in sub)

    if reasons:
        # 分级必须恰好覆盖文本里的原因（多重集相等），且各自保持原有相对顺序
        assert sorted(d.hard + d.soft) == sorted(reasons), f"分级与文本不一致 #{i}"
        assert _subseq(d.hard, reasons) and _subseq(d.soft, reasons), f"分级打乱顺序 #{i}"
        assert d.hard or d.soft
        assert not d.passed
    else:
        assert d.passed and not d.hard and not d.soft

print(f"对拍 {len(cases)} 组输入：不等价 {bad} 组")
print("逐字节等价" if bad == 0 else "存在差异！")
print("HARD/SOFT 分级与文本一致：OK")

# 抽样展示分级
demo = RunAnalysisResult(success=False, verified=False, blocks_added=0, tool_chain=[
    {"name": "run_query", "ok": False}], error="超时")
d = assess_subtask_detail(demo, entry="canvas", user_goal="按品类和品牌分析")
print("\n样例 HARD:", d.hard)
print("样例 SOFT:", d.soft)
print("passed =", d.passed)
