# -*- coding: utf-8 -*-
r"""入口隔离棘轮：**不允许再新增 `entry ==` / `entry !=` 变量比较**。

为什么要这条
------------
入口差异必须只有一处映射（`capability.EntryProfile`）。一旦某人在业务代码里再写一句
`if entry == "canvas": ...`，改动就绕过了画像：这条分支不会被"新增第三个入口"覆盖，
也不会被"画布评审口径调整"同步——表现为"某个入口的某个能力悄悄不变"，
而这类静默劣化**测试通常抓不到**（没有报错、没有日志，只是行为不对）。

棘轮的工作方式（这是关键，别改成"行号快照"）
------------------------------------------
- **键 = (文件, 所在函数名, 归一化后的表达式文本)**，**不含行号**：
  行号会随无关编辑漂移，快照就变成"每次改文件都要重生成"的噪音，最后没人维护。
- **只许减不许增**：出现快照里没有的新比较 → 失败（要加？先想清楚为什么映射放不进画像）；
- **也不许无声消失**：快照里的条目不见了 → 也失败（说明有人删了判定却没更新快照，
  或是重命名/搬文件导致棘轮"空转"——那样它就不再守任何东西了）。
- 例外：`capability.py`（映射本体）与 `lead_worker.py`（派发表，entry→worker 的映射）
  是**允许**按 entry 分派的两个地方，扫描时排除。

如何更新快照
------------
改了合法范围（例如把一条分支迁进画像后删掉）时，快照会红。两种方式重生成：

    # Windows PowerShell
    $env:ENTRY_ISOLATION_UPDATE=1; .venv\Scripts\python.exe -m pytest tests/test_entry_isolation.py -q; Remove-Item Env:\ENTRY_ISOLATION_UPDATE
    # 或直接跑脚本（不经过 pytest）
    .venv\Scripts\python.exe tests/test_entry_isolation.py --update

重生成会把快照**写回本文件**（两行 SNAPSHOT 标记之间），然后正常重跑一次确认绿。

排除项（扫描时跳过）
--------------------
- Attribute 形式（`AISession.entry == ...` / `self._entry == ...` / `args.entry == ...`）：
  那是**列/字段**比较，不是"变量 entry"，与本人入口映射无关；
- `tool_name`、`session.entry` 之类的同名字段不误伤。
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND / "app"

# 允许按 entry 分派的两处（映射本体），其余一律不许
EXCLUDED_FILES: frozenset[str] = frozenset({
    "app/services/agents/lead/capability.py",
    "app/services/agents/lead/lead_worker.py",
})

_ENTRY_VAR = "entry"
_COMPARE_OPS = (ast.Eq, ast.NotEq)


# === SNAPSHOT BEGIN（由 --update 生成，勿手改）===
_SNAPSHOT: frozenset[tuple[str, str, str]] = frozenset({
    ('app/api/v1/ai.py', 'list_sessions', 'entry == "chat"'),
    ('app/services/agents/lead/lead_intent_signal.py', 'derive_intent', 'entry == "canvas"'),
})
# === SNAPSHOT END ===


# ── 扫描器（纯函数，可单测）──────────────────────────────────────────────

def _enclosing_function(node: ast.AST, parents: dict) -> str:
    """最近的函数名；模块级返回 "<module>"。"""
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
    return "<module>"


def iter_entry_comparisons(relpath: str, source: str) -> list[tuple[str, str, str]]:
    """扫出**变量 entry** 与字符串/常量的相等/不等比较。

    返回 [(relpath, 函数名, 归一化表达式)]，按 (函数名, 表达式) 排序保证确定性。
    非 entry 比较、Attribute 形式（列比较）一律跳过。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # noqa: PERF203
        raise AssertionError(
            f"{relpath} 无法解析（{exc.lineno} 行：{exc.msg}）。"
            "若这是并行编辑中的临时状态，重跑即可；否则棘轮此刻是瞎的，必须修。"
        ) from exc
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == _ENTRY_VAR):
            continue
        if not any(isinstance(op, _COMPARE_OPS) for op in node.ops):
            continue
        segment = ast.get_source_segment(source, node) or ""
        normalized = re.sub(r"\s+", " ", segment).strip()
        found.append((relpath, _enclosing_function(node, parents), normalized))
    return sorted(set(found))


def scan() -> list[tuple[str, str, str]]:
    """遍历 app/ 下所有 .py（排除映射本体），返回全部入口比较。"""
    found: list[tuple[str, str, str]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel in EXCLUDED_FILES:
            continue
        # utf-8-sig：仓库里有带 BOM 的文件（如 sql_guard.py），utf-8 读会把 BOM
        # 留成首个字符 → ast.parse 直接 SyntaxError（实测踩到）。
        found.extend(iter_entry_comparisons(rel, path.read_text(encoding="utf-8-sig")))
    return sorted(found)


# ── 快照更新 ─────────────────────────────────────────────────────────────

_BEGIN = "# === SNAPSHOT BEGIN（由 --update 生成，勿手改）==="
_END = "# === SNAPSHOT END ==="


def _render_snapshot(entries: list[tuple[str, str, str]]) -> str:
    lines = [
        _BEGIN,
        "_SNAPSHOT: frozenset[tuple[str, str, str]] = frozenset({",
    ]
    for rel, fn, text in sorted(entries):
        lines.append(f"    ({rel!r}, {fn!r}, {text!r}),")
    lines += ["})", _END]
    return "\n".join(lines)


def update_snapshot(entries: list[tuple[str, str, str]] | None = None) -> int:
    """把当前扫描结果写回本文件的 SNAPSHOT 块，返回条目数。"""
    entries = scan() if entries is None else list(entries)
    path = Path(__file__).resolve()
    source = path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(_BEGIN) + r".*?" + re.escape(_END), re.S
    )
    assert pattern.search(source), "SNAPSHOT 标记缺失，无法重生成"
    path.write_text(pattern.sub(_render_snapshot(entries), source, count=1), encoding="utf-8")
    return len(entries)


def _should_update() -> bool:
    return os.environ.get("ENTRY_ISOLATION_UPDATE") == "1" or "--update" in sys.argv


# ── 用例 ─────────────────────────────────────────────────────────────────

def test_scanner_catches_variable_comparisons():
    """扫描器自证：**认出**变量 entry 比较（否则整个棘轮空转）。"""
    src = (
        "def f(entry):\n"
        "    if entry == \"canvas\":\n"
        "        return 1\n"
        "    return 0 if entry != 'chat' else 2\n"
    )
    got = iter_entry_comparisons("app/demo.py", src)
    assert [g[1] for g in got] == ["f", "f"]
    assert [g[2] for g in got] == ['entry != \'chat\'', 'entry == "canvas"'] or {
        g[2] for g in got
    } == {'entry != \'chat\'', 'entry == "canvas"'}


def test_scanner_ignores_column_and_other_name_comparisons():
    """扫描器自证：**不误伤**列比较（AISession.entry）与其他名字（tool_name）。"""
    src = (
        "def f(session, entry_ctx, tool_name):\n"
        "    if session.entry == \"canvas\":\n"
        "        pass\n"
        "    if AISession.entry == \"canvas\":\n"
        "        pass\n"
        "    if tool_name == \"query_sql\":\n"
        "        pass\n"
        "    return entry_ctx\n"
    )
    assert iter_entry_comparisons("app/demo.py", src) == []


def test_multi_line_comparison_is_normalized_without_line_numbers():
    """跨行的比较归一成一行（键里没有行号，插一行注释不该让快照变红）。"""
    a = (
        "def f(entry):\n"
        "    if (entry\n"
        "            == \"canvas\"):\n"
        "        pass\n"
    )
    b = "def f(entry):\n\n\n    if entry == \"canvas\":\n        pass\n"
    assert iter_entry_comparisons("app/demo.py", a) == iter_entry_comparisons(
        "app/demo.py", b
    ) == [("app/demo.py", "f", 'entry == "canvas"')]


def test_mapping_modules_are_excluded():
    """映射本体（capability / lead_worker）允许按 entry 分派，扫描时排除。"""
    assert "app/services/agents/lead/capability.py" in EXCLUDED_FILES
    assert "app/services/agents/lead/lead_worker.py" in EXCLUDED_FILES
    scanned_files = {rel for rel, _fn, _txt in scan()}
    assert not (scanned_files & EXCLUDED_FILES), "排除失效：映射本体仍在扫描范围内"


def test_no_new_entry_comparisons():
    """**棘轮主断言**：app/ 里不得出现快照之外的 entry 比较。

    新增一条 → 说明入口差异又被写进了业务代码。正确做法：把差异表达成
    EntryProfile 的字段（工具子集 / 配额 / review_policy / artifacts / worker_row），
    让 capability 继续做唯一映射点。
    """
    if _should_update():
        update_snapshot()
        pytest.skip("快照已重生成，请去掉 update 标记后重跑")
    found = set(scan())
    new = sorted(found - _SNAPSHOT)
    assert not new, (
        "发现**新增**的入口硬编码比较（入口差异必须进 capability.EntryProfile）：\n"
        + "\n".join(f"  {rel}::{fn}  {txt}" for rel, fn, txt in new)
        + "\n若确属合法例外，请说明理由并按文件头的方式重生成快照。"
    )


def test_no_entry_comparison_silently_removed():
    """快照条目消失也要红：否则棘轮会"空转"（搬文件/重命名后它什么都不守了）。"""
    if _should_update():
        pytest.skip("快照已重生成，请去掉 update 标记后重跑")
    found = set(scan())
    missing = sorted(_SNAPSHOT - found)
    assert not missing, (
        "快照里的入口比较不见了（被删/被搬走/函数改名？）：\n"
        + "\n".join(f"  {rel}::{fn}  {txt}" for rel, fn, txt in missing)
        + "\n这是**合法的收窄**吗？是则按文件头方式重生成快照；"
        "不是则说明判定丢了一处（行为会静默变化）。"
    )


def test_snapshot_sorted_for_stable_diffs():
    """快照的**渲染顺序**必须有序，否则每次重生成都产生无意义 diff。"""
    rendered = [ln for ln in _render_snapshot(list(_SNAPSHOT)).splitlines()
                if ln.strip().startswith("(")]
    assert rendered == sorted(rendered)


if __name__ == "__main__":  # python tests/test_entry_isolation.py --update
    count = update_snapshot()
    print(f"snapshot updated: {count} entries")
