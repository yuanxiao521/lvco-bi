"""临时探针：观察思考模式下 reasoning_content 的流式形态，以及 tool_calls 回写是否要求携带 reasoning_content。

用途：为复习笔记里的讲解取「实机证据」。沿用本项目 scripts/_probe_* 临时脚本约定，可随时重跑或删除。
运行：cd backend && python scripts/_probe_reasoning_content.py
安全：只打印 base_url / model / key 是否存在，绝不打印密钥本身。
"""

import asyncio
import json
import pathlib
import sys

import httpx

# 确保 backend/ 在 sys.path 最前（scripts/ 下探针脚本的通用约定）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_now_time",
            "description": "获取当前时间（探针演示用）",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

USER_MSG = {"role": "user", "content": "现在几点了？请调用 get_now_time 工具查看，不要凭记忆回答。"}


async def probe_stream() -> dict:
    url = settings.openai_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}
    body = {
        "model": settings.openai_model,
        "messages": [USER_MSG],
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.3,
        "max_tokens": 500,
        "stream": True,
    }
    reasoning_frags: list[str] = []
    tool_frags: dict[int, dict] = {}
    first_delta_printed = False
    finish_reason = None

    async with httpx.AsyncClient(timeout=90) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            print("== 1) 流式 tool_calls 探测 ==")
            print("HTTP status:", resp.status_code)
            if resp.status_code != 200:
                print("ERROR BODY:", (await resp.aread()).decode("utf-8", errors="replace")[:500])
                return {}
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if not first_delta_printed and delta:
                    print("first delta keys:", sorted(delta.keys()))
                    first_delta_printed = True
                rc = delta.get("reasoning_content")
                if isinstance(rc, str) and rc:
                    reasoning_frags.append(rc)
                    if len(reasoning_frags) <= 6:
                        print(f"  rc #{len(reasoning_frags)} len={len(rc)} head={rc[:40]!r}")
                for tcd in delta.get("tool_calls") or []:
                    idx = tcd.get("index", 0)
                    slot = tool_frags.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tcd.get("id"):
                        slot["id"] = tcd["id"]
                    fn = tcd.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]

    total = sum(len(x) for x in reasoning_frags)
    print("finish_reason:", finish_reason)
    print("reasoning 分片数:", len(reasoning_frags), "| 拼接总长:", total)
    if reasoning_frags:
        print("最后一片长度:", len(reasoning_frags[-1]), "（≈总长 → 累积型；远小于总长 → 增量型）")
        print("拼接开头:", "".join(reasoning_frags)[:60])
    print("捕获到的 tool_calls:", json.dumps(tool_frags, ensure_ascii=False)[:400])
    return {"reasoning": "".join(reasoning_frags), "tool": tool_frags}


async def probe_roundtrip(state: dict) -> None:
    print("\n== 2) tool_calls 回写探测（A 不带 / B 带 reasoning_content） ==")
    url = settings.openai_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}

    tool = state.get("tool") or {}
    if tool:
        call = tool[sorted(tool.keys())[0]]
        call_id = call["id"] or "call_probe_1"
        call_name = call["name"] or "get_now_time"
        call_args = call["args"] or "{}"
    else:
        call_id, call_name, call_args = "call_probe_1", "get_now_time", "{}"
        print("（未捕获到真实 tool_call，改用伪造调用做回写探测）")

    base_messages = [
        USER_MSG,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": call_name, "arguments": call_args}}
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": '{"time": "2026-10-04 15:30:00"}'},
    ]

    async with httpx.AsyncClient(timeout=90) as client:
        body_a = {"model": settings.openai_model, "messages": base_messages, "max_tokens": 200, "temperature": 0.3}
        ra = await client.post(url, headers=headers, json=body_a)
        print("A) 不带 reasoning_content ->", ra.status_code)
        if ra.status_code != 200:
            print("   body:", ra.text[:300])
        else:
            msg = (ra.json().get("choices") or [{}])[0].get("message") or {}
            print("   回答:", str(msg.get("content"))[:80])

        msgs_b = json.loads(json.dumps(base_messages))
        msgs_b[1]["reasoning_content"] = state.get("reasoning") or "（伪造的推理内容）"
        body_b = {"model": settings.openai_model, "messages": msgs_b, "max_tokens": 200, "temperature": 0.3}
        rb = await client.post(url, headers=headers, json=body_b)
        print("B) 带 reasoning_content ->", rb.status_code)
        if rb.status_code != 200:
            print("   body:", rb.text[:300])
        else:
            msg = (rb.json().get("choices") or [{}])[0].get("message") or {}
            print("   回答:", str(msg.get("content"))[:80])


async def main() -> None:
    print("base_url:", settings.openai_base_url)
    print("model   :", settings.openai_model)
    print("api key configured:", bool(settings.openai_api_key), "\n")
    state = await probe_stream()
    await probe_roundtrip(state)


if __name__ == "__main__":
    asyncio.run(main())