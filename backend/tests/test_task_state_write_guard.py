# -*- coding: utf-8 -*-
"""任务态共享键的写入路径守卫（源级契约测试）。

【背景】ai:chat:task:{sid} / ai:canvas:task:{sid} 是**跨进程共享键**：
  一个进程写 running，另一个进程删掉或改写。
  若误用通用 set/delete（双写 Redis + 本进程内存副本），跨进程删除后写入方
  再 get 会因 Redis miss **回退命中自己的内存旧值** → 幽灵 running。

【为什么必须用源级守卫】这类回归不会让任何现有测试变红：
  单进程测试里内存副本与 Redis 内容一致，"看起来是对的"。
  只有源码层面钉死"任务态键只能走 *_shared"，才能防止它被改回去。

【规则】
  1. 第一个实参是任务态键的缓存调用，写操作只允许 set_shared / delete_shared。
  2. 读操作允许 get：只要没人写内存副本，get 的 Redis-miss 回退就无旧值可命中。
  3. 覆盖率断言：已知的 6 处写入点必须全部被识别到，
     防止文件改名/重构后守卫静默失效（发现 0 处也算"通过"）。
"""
from __future__ import annotations

import ast
import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[1]

# 需要守卫的模块（任务态的写入方 / 读取方）
GUARDED_FILES = ("app/api/v1/ai.py", "app/services/chat_task.py")

# 任务态键前缀
TASK_KEY_PREFIX = re.compile(r"^ai:(chat|canvas):task:")

# 承载任务态键的参数名（chat_task.run_chat_task(task_key=...) 由调用方传入）
TASK_KEY_PARAMS = {"task_key"}

# 读方法放行；写方法只允许 *_shared
ALLOWED_READ = {"get"}
ALLOWED_WRITE = {"set_shared", "delete_shared"}
FORBIDDEN_WRITE = {"set", "delete"}

# 已知写入点（文件, 方法, 键表达式）——少一个就说明守卫失效
REQUIRED_WRITE_SITES = {
    ("app/api/v1/ai.py", "set_shared", "task_key"),             # 对话入口：标记 running
    ("app/api/v1/ai.py", "set_shared", "ai:canvas:task:"),      # 画布入口：标记 running
    ("app/api/v1/ai.py", "delete_shared", "ai:canvas:task:"),   # 画布正常完成：清除
    ("app/api/v1/ai.py", "delete_shared", "task_key"),          # 对话入口：任务未启动时清理
    ("app/services/chat_task.py", "set_shared", "task_key"),    # 任务体异常：标记 interrupted
    ("app/services/chat_task.py", "delete_shared", "task_key"), # 任务体正常完成：清除
}


def _joined_literal(node: ast.JoinedStr) -> str:
    """取 f-string 中的字面量部分（FormattedValue 略过）。"""
    return "".join(
        v.value for v in node.values
        if isinstance(v, ast.Constant) and isinstance(v.value, str)
    )


def _task_key_names(tree: ast.AST) -> set:
    """本模块内被赋值为任务态键的变量名（含已知参数名）。"""
    names = set(TASK_KEY_PARAMS)
    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if not isinstance(value, ast.JoinedStr):
            continue
        if not TASK_KEY_PREFIX.match(_joined_literal(value)):
            continue
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            targets = [getattr(node, "target", None)]
        for t in targets:
            if isinstance(t, ast.Name):
                names.add(t.id)
    return names


def _iter_task_key_calls():
    """产出 (文件, 行号, 方法名, 键表达式)。"""
    for rel in GUARDED_FILES:
        src = (BACKEND / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        key_names = _task_key_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            if fn.attr not in (ALLOWED_READ | ALLOWED_WRITE | FORBIDDEN_WRITE):
                continue
            a0 = node.args[0]
            if isinstance(a0, ast.JoinedStr):
                lit = _joined_literal(a0)
                if TASK_KEY_PREFIX.match(lit):
                    yield rel, node.lineno, fn.attr, lit
            elif isinstance(a0, ast.Name) and a0.id in key_names:
                yield rel, node.lineno, fn.attr, a0.id


def test_no_generic_write_on_task_state_key():
    """任务态键不得走通用 set/delete（双写内存副本 → 跨进程幽灵态）。"""
    bad = [
        f"{f}:{ln} 用了 {m}(...)（任务态键应改为 {m}_shared）"
        for f, ln, m, _ in _iter_task_key_calls()
        if m in FORBIDDEN_WRITE
    ]
    assert not bad, "任务态键出现通用写方法：\n" + "\n".join(bad)


def test_required_task_state_write_sites_all_discovered():
    """已知 6 处写入点必须全部被守卫识别（防止守卫静默失效）。"""
    found = {(f, m, k) for f, _, m, k in _iter_task_key_calls() if m in ALLOWED_WRITE}
    missing = REQUIRED_WRITE_SITES - found
    assert not missing, f"守卫未识别到这些写入点（文件改名或写法变更？）：{sorted(missing)}"


def test_both_entries_have_a_running_write():
    """两个入口都必须有 running 写入（漏一个就退回"刷新后无法判断任务态"）。"""
    found = {(f, m, k) for f, _, m, k in _iter_task_key_calls() if m in ALLOWED_WRITE}
    assert ("app/api/v1/ai.py", "set_shared", "task_key") in found, "对话入口缺 running 写入"
    assert ("app/api/v1/ai.py", "set_shared", "ai:canvas:task:") in found, "画布入口缺 running 写入"


def test_detector_recognises_both_key_forms():
    """识别能力自检：字面量 f-string 与变量两种写法都要能被认出来。

    若只认变量写法，未来有人写回 set(f"ai:canvas:task:{sid}", ...) 就会漏网——
    守卫本身失效比没有守卫更危险。
    """
    sites = list(_iter_task_key_calls())
    literal_forms = {k for _, _, _, k in sites if k.startswith("ai:")}
    var_forms = {k for _, _, _, k in sites if not k.startswith("ai:")}
    assert literal_forms, "守卫认不出字面量形式的任务态键——直接写键名的调用会漏网"
    assert var_forms, "守卫认不出变量形式的任务态键——经变量间接写入的调用会漏网"
    assert {"ai:chat:task:", "ai:canvas:task:"} <= literal_forms, "两个入口的任务态键都要能被识别"
