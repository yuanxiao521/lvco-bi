"""后台续跑注册表测试：任务/订阅解耦、断开不退订、结束自动摘除、文本快照。

任务协程（coro_factory 返回的 coroutine）通过 reg.publish / reg.update_text
把事件广播给订阅者——与真实 ai.py 的 _run_chat_task 用法完全一致。
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.chat_stream_registry import ChatStreamRegistry


def _run(coro) -> None:
    return asyncio.run(coro)


def test_start_then_subscribe_receives_events() -> None:
    """任务发布的事件能被子订阅者逐一收到，任务结束（哨兵）后订阅退出且注册表摘除。"""

    async def main() -> None:
        reg = ChatStreamRegistry()

        async def task_body() -> None:
            reg.publish("s1", {"type": "message", "delta": "a"})
            reg.publish("s1", {"type": "message", "delta": "b"})
            reg.publish("s1", {"type": "done"})

        assert reg.start("s1", task_body) is True
        assert reg.is_running("s1")
        got: list[dict] = []
        async for ev in reg.subscribe("s1"):
            got.append(ev)
        assert got == [
            {"type": "message", "delta": "a"},
            {"type": "message", "delta": "b"},
            {"type": "done"},
        ]
        # 任务结束 → 自动从注册表摘除
        assert not reg.is_running("s1")

    _run(main())


def test_duplicate_start_rejected_until_finished() -> None:
    """同会话重复 start 被拒绝；任务结束后可重新 start。"""

    async def main() -> None:
        reg = ChatStreamRegistry()

        async def task_body() -> None:
            reg.publish("s1", {"type": "done"})

        assert reg.start("s1", task_body) is True
        assert reg.start("s1", task_body) is False  # 已有任务在跑
        await asyncio.sleep(0.05)
        assert not reg.is_running("s1")
        assert reg.start("s1", task_body) is True  # 结束后可重启

        await asyncio.sleep(0.05)

    _run(main())


def test_subscribe_no_task_yields_nothing() -> None:
    """无任务时订阅为空迭代器（调用方据此走"任务已结束"分支）。"""

    async def main() -> None:
        reg = ChatStreamRegistry()
        async for _ in reg.subscribe("no-such-session"):
            pytest.fail("不应收到任何事件")

    _run(main())


def test_task_crash_broadcasts_error_and_removes() -> None:
    """任务协程抛异常：广播 error，`结束哨兵`仍发出，注册表摘除。"""

    async def main() -> None:
        reg = ChatStreamRegistry()

        async def task_body() -> None:
            raise RuntimeError("boom")

        assert reg.start("s1", task_body) is True
        got: list[dict] = []
        async for ev in reg.subscribe("s1"):
            got.append(ev)
        assert got == [{"type": "error", "message": "后台任务异常终止"}]
        assert not reg.is_running("s1")

    _run(main())


def test_disconnect_unsubscribes_but_keeps_task_alive() -> None:
    """订阅者断开只退订、不杀任务：任务继续跑到结束并自动摘除。"""

    async def main() -> None:
        reg = ChatStreamRegistry()
        gate = asyncio.Event()

        async def task_body() -> None:
            reg.publish("s1", {"type": "message", "delta": "hello"})
            await gate.wait()  # 任务停在等待处，模拟长任务
            reg.publish("s1", {"type": "done"})

        assert reg.start("s1", task_body) is True
        sub = reg.subscribe("s1")
        assert await sub.__anext__() == {"type": "message", "delta": "hello"}
        await sub.aclose()  # 模拟连接断开
        await asyncio.sleep(0.02)
        assert reg.is_running("s1")  # 任务没被杀

        gate.set()  # 放行任务
        await asyncio.sleep(0.02)
        assert not reg.is_running("s1")  # 任务正常结束并摘除

    _run(main())


def test_multiple_subscribers_all_receive_events() -> None:
    """多个订阅连接（如多标签页）在注册后都能收到同一份事件广播。"""

    async def main() -> None:
        reg = ChatStreamRegistry()
        gate = asyncio.Event()

        async def task_body() -> None:
            await gate.wait()  # 等两个订阅者都登记完成再开跑
            reg.publish("s1", {"type": "message", "delta": "x"})
            await asyncio.sleep(0)
            reg.publish("s1", {"type": "done"})

        assert reg.start("s1", task_body) is True
        got1: list[dict] = []
        got2: list[dict] = []

        async def drain(acc: list[dict]) -> None:
            async for ev in reg.subscribe("s1"):
                acc.append(ev)

        tasks = [asyncio.create_task(drain(got1)), asyncio.create_task(drain(got2))]
        await asyncio.sleep(0.02)  # 等两个 drain 都完成登记并阻塞在队列上
        gate.set()
        await asyncio.gather(*tasks)
        assert got1 == [{"type": "message", "delta": "x"}, {"type": "done"}]
        assert got2 == got1

    _run(main())


def test_update_text_snapshot_for_resume() -> None:
    """任务侧 update_text 落地的文本快照，供重连续收时补发。"""

    async def main() -> None:
        reg = ChatStreamRegistry()
        gate = asyncio.Event()

        async def task_body() -> None:
            reg.publish("s1", {"type": "message", "delta": "第一段,"})
            reg.update_text("s1", "第一段,")
            await gate.wait()
            reg.publish("s1", {"type": "done"})

        assert reg.start("s1", task_body) is True
        await asyncio.sleep(0.02)
        rt = reg.get("s1")
        assert rt is not None and rt.full_text == "第一段,"
        gate.set()
        await asyncio.sleep(0.02)

    _run(main())


def test_subscribe_receives_late_events_after_reconnect() -> None:
    """断开后重连：任务仍在跑时，新订阅者从队尾继续收，任务不因断开而终止。"""

    async def main() -> None:
        reg = ChatStreamRegistry()
        start_gate = asyncio.Event()

        async def task_body() -> None:
            await start_gate.wait()
            for i in range(50):
                reg.publish("s1", {"type": "message", "delta": f"p{i}"})
                await asyncio.sleep(0)  # 每步让出：给"断开后重连"留出订阅窗口

        assert reg.start("s1", task_body) is True
        start_gate.set()
        # 第一段订阅：读到 1 个事件后"断开"
        sub = reg.subscribe("s1")
        await sub.__anext__()
        await sub.aclose()
        assert reg.is_running("s1")  # 任务没死
        # 重连：立即订阅，续收剩余事件直到任务结束
        got: list[dict] = []
        async for ev in reg.subscribe("s1"):
            got.append(ev)
        assert len(got) >= 1  # 至少续收了一部分
        assert all(ev["type"] == "message" for ev in got)

    _run(main())