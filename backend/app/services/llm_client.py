import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.config import Settings, settings

logger = logging.getLogger("lvco.llm")


class AINotConfiguredError(Exception):
    """Raised when OPENAI_API_KEY is not set."""


class AIUpstreamError(Exception):
    """Raised when LLM upstream returns 4xx/5xx."""


class LLMClient:
    def __init__(self, settings_override: Settings | None = None) -> None:
        self._settings: Settings = settings_override or settings

    @property
    def _api_key(self) -> str:
        return self._settings.openai_api_key or ""

    @property
    def _base_url(self) -> str:
        return self._settings.openai_base_url.rstrip("/")

    @property
    def _timeout(self) -> float:
        return float(self._settings.openai_timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _check_configured(self) -> None:
        if not self._settings.is_ai_configured or not self._api_key:
            raise AINotConfiguredError("请在 .env 配置 OPENAI_API_KEY")

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int | None,
        stream: bool,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        enable_thinking: bool | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": model or self._settings.openai_model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if response_format is not None:
            body["response_format"] = response_format
        # DeepSeek 思考模式开关（百炼 compatible-mode 支持）：轻量判断任务关闭思考可显著提速省 token
        if enable_thinking is not None:
            body["enable_thinking"] = enable_thinking
        return body

    def _log_usage(self, usage: dict) -> None:
        """统一记录一次调用的 token 与缓存命中（成本分析的基础数据）。"""
        if not usage:
            return
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
        logger.info(
            "[llm] call model=%s in=%d out=%d cached=%d",
            self._settings.openai_model,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            cached,
        )

    def _log_stream_usage(self, usage: dict) -> None:
        """流式调用在 [DONE] 前补发的 usage chunk（include_usage 开启后才有）。"""
        if usage:
            self._log_usage(usage)
            return
        logger.info("[llm] call stream=1 usage=missing（服务端未返回 usage，可能是流被中途断开）")

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        enable_thinking: bool | None = None,
        model: str | None = None,
        return_usage: bool = False,
    ) -> str | tuple[str, dict]:
        """非流式补全。

        `return_usage=True` 时返回 `(content, meta)`，meta 含 `model / input /
        output`（token 数），供观测层上报 token 与成本；默认仍只返回 content
        字符串，保持向后兼容。
        """
        self._check_configured()
        url = f"{self._base_url}/chat/completions"
        body = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            response_format=response_format,
            enable_thinking=enable_thinking,
            model=model,
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, headers=self._headers(), json=body)
        if response.status_code < 200 or response.status_code >= 300:
            text = response.text or ""
            raise AIUpstreamError(f"LLM {response.status_code}: {text[:200]}")
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise AIUpstreamError("LLM empty choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        usage = data.get("usage") or {}
        # 无条件记录：usage 是服务端每次都会返回的，与调用方是否要 meta 无关。
        # 否则"没显式要 usage 的调用点"完全不产生 token 数据，成本账单永远算不全。
        self._log_usage(usage)
        if not return_usage:
            return content
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
        meta = {
            "model": data.get("model") or body.get("model") or self._settings.openai_model,
            "input": int(usage.get("prompt_tokens") or 0),
            "output": int(usage.get("completion_tokens") or 0),
        }
        if cached:
            meta["cached"] = cached
        return content, meta

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self._check_configured()
        url = f"{self._base_url}/chat/completions"
        body = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        # 让服务端在流末尾补一个 usage chunk：否则流式调用完全不产生 token 数据，
        # 成本分析会漏掉最重的那些调用（执行器/汇总都是流式出字的）。
        body["stream_options"] = {"include_usage": True}
        stream_usage: dict = {}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST", url, headers=self._headers(), json=body
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    err_body = await response.aread()
                    text = err_body.decode("utf-8", errors="replace") if err_body else ""
                    raise AIUpstreamError(
                        f"LLM {response.status_code}: {text[:200]}"
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    stripped = line.lstrip()
                    if not stripped.startswith("data:"):
                        continue
                    payload = stripped[5:].strip()
                    if not payload or payload == "[DONE]":
                        if payload == "[DONE]":
                            self._log_stream_usage(stream_usage)
                            return
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        stream_usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield content

    async def stream_chat_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        *,
        temperature: float = 0.5,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict]:
        """流式调用 LLM，支持 tool_calls 解析。

        Yield 事件:
            {"type": "text", "content": "..."}
            {"type": "tool_call", "id": "...", "name": "...", "arguments": "..."}
            {"type": "done"}
        """
        self._check_configured()
        url = f"{self._base_url}/chat/completions"
        body = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            tools=tools,
        )

        tool_calls_acc: dict[int, dict] = {}
        reasoning_content: str = ""  # DeepSeek reasoning 模式需要保存该字段

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST", url, headers=self._headers(), json=body
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    err_body = await response.aread()
                    text = err_body.decode("utf-8", errors="replace") if err_body else ""
                    raise AIUpstreamError(f"LLM {response.status_code}: {text[:200]}")

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    stripped = line.lstrip()
                    if not stripped.startswith("data:"):
                        continue
                    payload = stripped[5:].strip()
                    if not payload or payload == "[DONE]":
                        if payload == "[DONE]":
                            for idx in sorted(tool_calls_acc.keys()):
                                tc = tool_calls_acc[idx]
                                yield {
                                    "type": "tool_call",
                                    "id": tc["id"] or "",
                                    "name": tc["name"] or "",
                                    "arguments": tc.get("arguments", ""),
                                    "reasoning_content": reasoning_content,
                                }
                            tool_calls_acc.clear()
                            yield {"type": "done"}
                            return
                        continue

                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue

                    delta = choices[0].get("delta") or {}

                    # 捕获 reasoning_content（DeepSeek reasoning 模式）
                    rc = choices[0].get("delta", {}).get("reasoning_content")
                    if isinstance(rc, str) and rc:
                        reasoning_content = rc

                    tc_deltas = delta.get("tool_calls")
                    if tc_deltas:
                        for tc_delta in tc_deltas:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                            acc = tool_calls_acc[idx]
                            if tc_delta.get("id"):
                                acc["id"] = tc_delta["id"]
                            func = tc_delta.get("function") or {}
                            if func.get("name"):
                                acc["name"] += func["name"]
                            if func.get("arguments"):
                                acc["arguments"] += func["arguments"]

                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield {"type": "text", "content": content}

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> dict:
        """非流式补全 + tool_calls 解析（与 stream_chat_with_tools 语义等价，但不分片）。

        返回 {"content": str, "tool_calls": [{"type","id","name","arguments"}]}。
        用途：执行器的文本不需要逐字展示时，**省掉流式分片拼接**（id 只来一次、
        name 可能被拆、arguments 逐块拼——这套累积逻辑可以直接不要）。

        注意：非流式下整个生成期间没有任何数据回传，**读超时必须覆盖完整生成时间**，
        因此这里默认给比流式更宽的 timeout（流式的超时只兜"块与块之间的间隔"）。
        """
        self._check_configured()
        url = f"{self._base_url}/chat/completions"
        body = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            tools=tools,
        )
        read_timeout = timeout if timeout is not None else max(self._timeout, 300.0)
        async with httpx.AsyncClient(timeout=read_timeout) as client:
            response = await client.post(url, headers=self._headers(), json=body)
        if response.status_code < 200 or response.status_code >= 300:
            text = response.text or ""
            raise AIUpstreamError(f"LLM {response.status_code}: {text[:200]}")
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise AIUpstreamError("LLM empty choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        reasoning = message.get("reasoning_content") or ""
        tool_calls: list[dict] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append({
                "type": "tool_call",
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
                "reasoning_content": reasoning,
            })
        self._log_usage(data.get("usage") or {})
        return {"content": content, "tool_calls": tool_calls}

    @property
    def tool_streaming_enabled(self) -> bool:
        """工具调用是否走流式（配置开关，默认 True 保持历史行为）。"""
        return bool(getattr(self._settings, "LLM_TOOL_STREAMING", True))

    async def tool_call_round(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        streaming: bool | None = None,
    ) -> AsyncIterator[dict]:
        """工具调用轮次的**统一入口**：按配置在「流式」与「非流式」之间切换。

        对调用方完全透明——两种模式 yield 的事件形状一致：
            {"type": "text", "content": "..."}   （非流式时一次性给出全部文本）
            {"type": "tool_call", "id", "name", "arguments", ...}
            {"type": "done"}

        为什么要有这个入口：执行器不该关心"传输方式"。关掉流式后，tool_calls 的
        分片拼接逻辑（id 只来一次 / name 可能被拆开 / arguments 逐块拼）就完全用不到了。

        参数 streaming：
            None  → 沿用全局配置 LLM_TOOL_STREAMING
            True  → 强制流式（**输出要给用户实时看**的路径，如 ReAct 的报告正文）
            False → 强制非流式（**输出只给程序消费**的路径，如编排器的工具循环中间输出）
        判断标准：这条输出有人盯着屏幕等它一个字一个字出来吗？
        """
        use_stream = self.tool_streaming_enabled if streaming is None else streaming
        if use_stream:
            async for event in self.stream_chat_with_tools(
                messages, tools, temperature=temperature, max_tokens=max_tokens,
            ):
                yield event
            return
        res = await self.complete_with_tools(
            messages, tools, temperature=temperature, max_tokens=max_tokens,
        )
        if res.get("content"):
            yield {"type": "text", "content": res["content"]}
        for tc in res.get("tool_calls") or []:
            yield tc
        yield {"type": "done"}
