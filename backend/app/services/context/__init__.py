"""上下文装配包：门面（Provider）+ 取数（Source）+ 装配（Assembler）+ 压缩（Compressor）。

设计约定（四件套，见上下文重构计划）：
- **Source 只取不裁**：产出"完整内容"，绝不截断、绝不感知预算；
- **Compressor 只压不取**：按 BlockKind 查策略，不读 DB、不认识会话；
- **Assembler 只算**：算预算、定顺序、按优先级降级；
- **Provider 是唯一门面**：请求级缓存 + 失效，暴露 for_decision / for_answer / for_worker。

铁律：`context/` 之外不允许出现 `messages.append` 或 `[:N]` 形式的截断。
"""
from __future__ import annotations

from app.services.context.types import Assembled, Block, BlockKind, Ledger, Priority

__all__ = ["Assembled", "Block", "BlockKind", "Ledger", "Priority"]
