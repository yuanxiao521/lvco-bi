import json
from collections.abc import AsyncIterator

import httpx

from app.config import Settings, settings


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
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._settings.openai_model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        self._check_configured()
        url = f"{self._base_url}/chat/completions"
        body = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
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
        return content if isinstance(content, str) else ""

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
