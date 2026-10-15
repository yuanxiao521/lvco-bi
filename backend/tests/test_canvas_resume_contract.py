# -*- coding: utf-8 -*-
"""画布续收契约（源级守卫）。

【背景】画布入口原先的续收只看**进程内注册表**：
    rt = chat_registry.get(task_sid)
    if rt is None: 直接报"已结束"
换进程（队列模式/多副本）或稍晚重连，注册表里根本没有这个任务 → 一律判"已结束"，
续收失效。事件类型（决策/步骤/落块）也在刷新瞬间永久丢失。

【修法】与对话入口统一：以**事件日志**（Redis Stream）为准——
  回放 + 续读，日志里有 done 才算跑完。

【为什么要源级守卫】这些行为运行时很难测：
  · 续收分支的行为需要"任务在跑 + 连接断开 + 重连"三者同时成立；
  · 画布任务体是嵌套函数，无法直接 import 调用；
  · 回归不会让现有测试变红（都是"看起来正常"的静默退化）。
  所以把契约钉在源码层：宁可测试看起来"土"，也不要它悄悄失效。
"""
from __future__ import annotations

import ast
import pathlib
import textwrap

BACKEND = pathlib.Path(__file__).resolve().parents[1]
AI_PY = BACKEND / "app" / "api" / "v1" / "ai.py"


def _src() -> str:
    return AI_PY.read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    """取出函数源码（支持嵌套函数）。"""
    src = _src()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg, f"取不到 {name} 的源码段"
            return textwrap.dedent(seg)
    raise AssertionError(f"未找到函数 {name}（改名了？请同步本守卫）")


def _resume_blocks() -> list:
    """所有"判定 resume"的 if 块（对话入口 + 画布入口各一个）。"""
    out = []
    for node in ast.walk(ast.parse(_src())):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "resume" in test:
            out.append(ast.unparse(node))
    return out


def test_both_entries_have_a_resume_branch():
    """对话与画布都必须有 resume 分支（少一个说明画布又不支持续收了）。

    注意两个入口的判定写法本来就不同（对话是 `if body.resume`，画布是
    `if getattr(body, "resume", False) and body.session_id`），所以用**各自
    分支里的独有标记**来认定，而不是靠判定表达式的形状。
    """
    blocks = _resume_blocks()
    assert len(blocks) >= 2, f"应至少 2 个 resume 分支（对话/画布），实际 {len(blocks)}"
    joined = "\n".join(blocks)
    assert "画布内容以画布为准" in joined, "画布入口缺 resume 分支"
    assert "worker_alive" in joined, "对话入口缺 resume 分支"


def test_resume_paths_are_event_log_based_not_registry_only():
    """**核心回归**：任何 resume 分支都不得只认进程内注册表。

    只认注册表的后果：换进程/多副本时 running 恒 False → "刷新即报已结束"，
    用户看到的是"进度丢了"，而不是报错——最难发现的那类退化。
    """
    for i, block in enumerate(_resume_blocks()):
        assert "_stream_from_event_log" in block, (
            f"第 {i + 1} 个 resume 分支没走事件日志回放+续读"
        )
        assert "event_stream.has_events" in block, (
            f"第 {i + 1} 个 resume 分支没用事件日志判定【这一轮是否还在跑】"
        )
        assert "chat_registry.subscribe" not in block, (
            f"第 {i + 1} 个 resume 分支退回了【只认进程内注册表】——换进程即失效"
        )


def test_canvas_task_body_writes_event_log_before_broadcast():
    """画布任务体必须把事件落进日志，且**先落日志、后广播注册表**。

    顺序反了的后果：连接断开的瞬间，恰好有事件"已广播、未落日志"，
    重连的读者从旧游标续读时那条事件永久丢失（且无法察觉）。
    """
    src = _func_src("_consume_canvas_task")
    assert "event_stream.publish" in src, "画布任务体没把事件写进日志——续收无内容可回放"
    assert "chat_registry.publish" in src, "画布任务体应保留注册表实时广播"
    assert src.index("event_stream.publish") < src.index("chat_registry.publish"), (
        "应先落事件日志、后广播注册表（否则断线缝隙里的事件会丢）"
    )


def test_canvas_session_list_reports_running():
    """画布会话列表必须给出 running——这是前端"刷新自动续收"的触发依据。

    只给 interrupted 不给 running 的后果：前端永远不知道该续收，
    续收能力再强也不会被调用（用户看到的仍是"刷新后进度没了"）。
    """
    src = _func_src("list_canvas_sessions")
    assert 'd["running"]' in src, "画布会话列表缺 running 字段"
    assert 'd["interrupted"]' in src, "画布会话列表应保留 interrupted 字段"


def test_canvas_error_branches_emit_done():
    """画布任务级错误分支必须补 done：前端只在 done 时解锁输入框。

    只发 error 的后果：用户一直停在"思考中"，直到连接关闭/空闲超时才恢复。
    """
    tree = ast.parse(_func_src("_canvas_chat_task"))
    seen = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = ast.unparse(node)
        if "'error'" not in body:
            continue  # 只治理"对外报错"的分支（内部 rollback 之类不管）
        seen += 1
        assert "'done'" in body, (
            f"第 {seen} 个对外错误分支没带 done——前端会一直卡在思考中"
        )
    assert seen >= 3, f"应有至少 3 个对外错误分支（未配置/上游错误/未知异常），实际 {seen}"
