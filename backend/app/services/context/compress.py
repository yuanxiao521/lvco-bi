"""统一压缩层：所有截断只允许出现在这里。

铁律（四件套的分工）：
- Compressor **只压不取**：不读 DB、不认识会话、不决定优先级；
- 输入一个 Block + 一个 token 预算，输出压缩后的 Block + 降级原因；
- 具体的截断规则由 Policy 承担，调用方永远看不到 [:N]。

P0：策略内部**转调既有实现**（compact_result_json / compress_history），
行为与原路径逐字节等价——等价性由 tests/test_context_foundation.py 锁住。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from app.config import settings
from app.services.context.budgets import (
    TOKENS_PER_CHAR_JSON,
    TOKENS_PER_CHAR_MIXED,
    chars_for_tokens,
)
from app.services.context.types import Block, BlockKind
from app.services.context_utils import compact_result_json, compress_history, estimate_tokens

# 降级原因（会写进 Ledger.degraded，最终随 SSE 的 degradation 暴露）
DEGRADE_TOOL_COMPACTED = "tool_result_compacted"
DEGRADE_MESSAGE_LIST_FOLDED = "message_list_folded"


def _tokens_to_json_chars(tokens: int) -> int:
    """token → 字符（JSON 0.4 系数；底层 compact_result_json 仍是字符口径）。"""
    return chars_for_tokens(tokens, TOKENS_PER_CHAR_JSON)


def _tokens_to_mixed_chars(tokens: int) -> int:
    """token → 字符（混排 0.6 系数；底层 compress_history 仍是字符口径）。"""
    return chars_for_tokens(tokens, TOKENS_PER_CHAR_MIXED)


class Policy(Protocol):
    """压缩策略协议：把一块内容压进给定 token 预算。"""

    def compress(self, block: Block, max_tokens: int) -> tuple[Block, str | None]: ...


class PassThroughPolicy:
    """直通策略：不压缩。

    用于不允许压缩的块（error 结果、用户原话）以及尚未接入策略的块。
    """

    def compress(self, block: Block, max_tokens: int) -> tuple[Block, str | None]:
        return block, None


class ToolResultPolicy:
    """工具结果策略：转调 compact_result_json。

    既有语义必须完整保留：
    - error 结果**完整保留**（LLM 自纠错依赖错误与 hint）；
    - rows 只留前 N 行；元数据类结果走更大的保底上限。
    """

    def __init__(self, fallback_chars: int | None = None) -> None:
        self._fallback_chars = fallback_chars

    def compress(self, block: Block, max_tokens: int) -> tuple[Block, str | None]:
        max_chars = (
            chars_for_tokens(max_tokens, TOKENS_PER_CHAR_JSON)
            if max_tokens > 0
            else _tokens_to_json_chars(int(getattr(settings, "TOOL_RESULT_MAX_TOKENS", 800) or 800))
        )
        out = compact_result_json(block.text, max_chars=max_chars)
        if out == block.text:
            return block, None
        return replace(block, text=out), DEGRADE_TOOL_COMPACTED


class MessageListPolicy:
    """消息列表策略：转调 compress_history（保留 system 首条 + 最近 keep 条）。

    只处理结构化消息块（block.messages）；纯文本块直通——决策路径的窗口是
    渲染后的文本，由 P1 的 TextPolicy 接管。
    """

    def __init__(self, keep: int | None = None, fallback_chars: int | None = None) -> None:
        self._keep = keep
        self._fallback_chars = fallback_chars

    def compress(self, block: Block, max_tokens: int) -> tuple[Block, str | None]:
        if not block.messages:
            return block, None
        max_chars = (
            chars_for_tokens(max_tokens, TOKENS_PER_CHAR_MIXED)
            if max_tokens > 0
            else _tokens_to_mixed_chars(int(getattr(settings, "MESSAGE_LIST_MAX_TOKENS", 90000) or 90000))
        )
        keep = self._keep if self._keep is not None else int(getattr(settings, "CONTEXT_KEEP", 60) or 60)
        out = compress_history([dict(m) for m in block.messages], keep=keep, max_chars=max_chars)
        if len(out) >= len(block.messages):
            return block, None
        return replace(block, messages=tuple(out)), DEGRADE_MESSAGE_LIST_FOLDED


def default_policies() -> dict[BlockKind, Policy]:
    """默认策略表：按 BlockKind 分派。"""
    return {
        BlockKind.TOOL_RESULT: ToolResultPolicy(),
        BlockKind.WINDOW: MessageListPolicy(),
    }


class Compressor:
    """统一压缩层入口。"""

    def __init__(self, policies: dict[BlockKind, Policy] | None = None) -> None:
        self._policies: dict[BlockKind, Policy] = dict(policies or default_policies())
        self._default: Policy = PassThroughPolicy()

    def policy_for(self, kind: BlockKind) -> Policy:
        return self._policies.get(kind, self._default)

    def compress(self, block: Block, max_tokens: int) -> tuple[Block, str | None]:
        """压缩一个块；返回 (新块, 降级原因或 None)。"""
        if not block.compressible or block.is_empty:
            return block, None
        return self.policy_for(block.kind).compress(block, max_tokens)

    @staticmethod
    def measure(text_or_block: str | Block) -> int:
        """估算 token（唯一口径，禁止调用方自己做 len() 比较）。"""
        if isinstance(text_or_block, Block):
            return text_or_block.tokens()
        return estimate_tokens(text_or_block or "")
