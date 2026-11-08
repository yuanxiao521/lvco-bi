# -*- coding: utf-8 -*-
"""能力画像（EntryProfile）：**入口差异的唯一映射点**。

为什么要有这一层
----------------
"对话页 / 分析画布"是两个产品，而不是同一个 Agent 的两块皮肤。差异真实存在于五处：

  1. **提示词**：画布要讲落块交付、布局、破坏性操作确认；对话页要讲"本页没有画布"；
  2. **工具子集**：观察类里 `get_canvas_layout` 在对话页**恒返回空串**（canvas_state 的
     enabled 要求 entry=canvas）——它在对话入口就是死工具，不该出现在工具表里；
  3. **配额**：每类工具的次数上限（观察/派发/补发的预算不同）；
  4. **验收口径**：画布"零落块"是 HARD 事实性失败，对话页则看报告是否有数字支撑；
  5. **派发内核**：同一复杂度在画布走画布编排、在对话页走通用编排。

此前这五处靠 `entry == "canvas"` 散落在 6 个模块（评审、补图、prompt 选择、
数据源注入、意图信号…）。每加一个入口就要翻遍全仓找比较点，且**漏一处就静默劣化**
（表现为"某个入口的某个能力悄悄不变"）。本模块把这五处收成一张表：
**映射只在这里，其他模块一律向画像要能力，不允许再出现 `entry ==` 的判定**
（由 `tests/test_entry_isolation.py` 的棘轮守住）。

新增第三个入口（如"报告页""运维台"）的姿势
------------------------------------------
```python
REPORT = EntryProfile(
    entry="report",
    core_prompt_key="lead_core",
    profile_prompt_key="report_profile",   # prompts/report_profile.yaml
    observe_tools=OBSERVE_TOOL_NAMES - {"get_canvas_layout"},
    ...
)
register_profile(REPORT)                    # 或直接在 PROFILES 里加一行
```
不需要改任何 `if/elif`——这正是本模块存在的意义。

设计约束
--------
- **不可变**：frozen dataclass + frozenset，避免运行期被某处悄悄改写；
- **不做 IO**：提示词在这里只记"键"（`profile_prompt_key`），真正的组合在
  `ai_prompts.compose_lead_system`，避免本模块 import 时就加载 yaml；
- **与既有表单向引用**：配额/派发表仍以 `lead_agent_tools.QUOTA` 与
  `lead_worker.ENTRY_WORKERS` 为**唯一**定义，这里只引用不复制（有测试守 identity）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from app.services.agents.lead.lead_agent_tools import (
    DISPATCH_TOOL_NAMES,
    OBSERVE_TOOL_NAMES,
    QUOTA,
    TERMINAL_TOOL_NAMES,
)
from app.services.agents.lead.lead_worker import (
    ENTRY_WORKERS,
    FALLBACK_WORKER,
    WORKERS,
    WorkerSpec,
)

# 提示词段键（与 prompts/<key>.yaml 一一对应）
CORE_PROMPT_KEY = "lead_core"
CHAT_PROMPT_KEY = "chat_profile"
CANVAS_PROMPT_KEY = "canvas_profile"

# 产物白名单取值：本入口承诺交付什么
ARTIFACT_REPORT = "report"        # 一份文本结论/报告
ARTIFACT_CHART = "chart"          # 对话框内嵌图（render_chart 的 option）
ARTIFACT_BLOCK = "block"          # 画布内容块（前端落盘）
ARTIFACT_CANVAS_ACTION = "canvas_action"
ARTIFACT_LAYOUT = "layout"        # 画布布局被整理过


@dataclass(frozen=True)
class ReviewPolicy:
    """验收口径：哪些是 HARD（不得翻案）、哪些是 SOFT（可裁量）、哪些豁免。

    HARD 与 SOFT 的判定实现在 `lead_tools.assess_subtask_detail`（文本逐字节兼容
    是硬要求，所以判定顺序留在那里），本类只表达**入口之间的差异化开关**。
    """

    # 报告数字未过后验时：always=一律记 SOFT；without_blocks=仅当零产出时才记
    # （画布已落块时"数字无法严格后验"只属软存疑，不该把整体敲成失败触发重派级联）
    numbers_mode: str = "always"
    # 画布入口：成功却零块 = HARD 事实性失败（"什么都没干"）
    hard_zero_artifact: bool = False
    # 上述 HARD 的豁免：清空/删块/重排等变更类操作本身就产出，blocks_added 天然为 0
    zero_block_exempt_on_mutation: bool = True
    # 用户点名 N 个维度但图表覆盖不全 → SOFT（画布专属的质量判断）
    dimension_coverage: bool = False
    # Lead 自己声明的 acceptance 未满足 → SOFT（两入口都适用）
    acceptance: bool = True
    # SOFT 非空时是否触发一次裁量（True=按需裁量；False=直接按代码结论）
    adjudicate_on_soft: bool = True

    def flags_unverified_numbers(self, blocks_added: int) -> bool:
        """报告数字未过后验时，是否记为 SOFT。"""
        if self.numbers_mode == "always":
            return True
        if self.numbers_mode == "without_blocks":
            return blocks_added == 0
        return False

    def zero_artifact_is_hard(self, *, success: bool, blocks_added: int, has_mutation: bool) -> bool:
        """零产物是否构成 HARD 失败（画布入口专属，对话页恒 False）。"""
        if not self.hard_zero_artifact or not success or blocks_added != 0:
            return False
        if self.zero_block_exempt_on_mutation and has_mutation:
            return False
        return True


@dataclass(frozen=True)
class EntryProfile:
    """一个入口的能力画像（不可变）。"""

    entry: str
    # ── 提示词：三段式（core + profile），键名对应 prompts/<key>.yaml ──
    core_prompt_key: str
    profile_prompt_key: str
    # ── 工具子集（catalog 的子集；写工具永不在其中）──
    observe_tools: frozenset[str]
    dispatch_tools: frozenset[str] = DISPATCH_TOOL_NAMES
    terminal_tools: frozenset[str] = TERMINAL_TOOL_NAMES
    # ── 循环闸配额（引用 lead_agent_tools.QUOTA，不复制）──
    quota: Mapping[str, int] = field(default_factory=lambda: QUOTA)
    # ── 验收口径 ──
    review_policy: ReviewPolicy = field(default_factory=ReviewPolicy)
    # ── 派发内核：引用 lead_worker 的那一行派发表（不复制）──
    worker_row: Mapping[str, str] = field(default_factory=dict)
    fallback_worker: str = FALLBACK_WORKER
    # ── 产物白名单：本入口承诺交付什么（写进提示词，也用于验收解释）──
    artifacts: frozenset[str] = frozenset()

    # ── 工具 ────────────────────────────────────────────────────────────
    @property
    def tool_names(self) -> frozenset[str]:
        """本入口 LLM 可见的工具全名集合。"""
        return self.observe_tools | self.dispatch_tools | self.terminal_tools

    def allows_tool(self, name: str) -> bool:
        return name in self.tool_names

    # ── 提示词 ──────────────────────────────────────────────────────────
    def prompt_keys(self) -> tuple[str, str]:
        """(core, profile) 两个键——组合顺序由 compose_lead_system 决定。"""
        return (self.core_prompt_key, self.profile_prompt_key)

    def system_prompt(self) -> str:
        """组装本入口的 system prompt（core + profile）。

        延迟 import：`ai_prompts` 在 import 时就会读 yaml。本模块被 `lead_tools`
        这类"轻量单元测试也会 import"的模块引用，不在 import 期触发提示词加载。
        """
        from app.services.ai_prompts import compose_lead_system, load_prompt_segment

        return compose_lead_system(
            load_prompt_segment(self.core_prompt_key),
            load_prompt_segment(self.profile_prompt_key),
        )

    # ── 派发 ────────────────────────────────────────────────────────────
    def resolve_worker(self, complexity: str, explicit: str | None = None) -> WorkerSpec:
        """解析本入口该派给哪个执行内核（确定性，零 LLM）。

        优先级与 `lead_worker.resolve_worker` 完全一致：
        explicit > 本入口派发表的该复杂度格 > 兜底。
        """
        name = (
            explicit
            or self.worker_row.get(complexity or "complex")
            or self.fallback_worker
        )
        return WORKERS.get(name) or WORKERS[self.fallback_worker]

    # ── 观测 ────────────────────────────────────────────────────────────
    def describe(self) -> dict:
        """画像摘要（日志/测试用；不含提示词正文，避免日志过大）。"""
        return {
            "entry": self.entry,
            "prompt_keys": list(self.prompt_keys()),
            "tools": sorted(self.tool_names),
            "quota": dict(self.quota),
            "artifacts": sorted(self.artifacts),
            "review": {
                "numbers_mode": self.review_policy.numbers_mode,
                "hard_zero_artifact": self.review_policy.hard_zero_artifact,
                "dimension_coverage": self.review_policy.dimension_coverage,
                "acceptance": self.review_policy.acceptance,
            },
            "worker_row": dict(self.worker_row),
        }


# ── 两个真实入口 ──────────────────────────────────────────────────────────

CHAT = EntryProfile(
    entry="chat",
    core_prompt_key=CORE_PROMPT_KEY,
    profile_prompt_key=CHAT_PROMPT_KEY,
    # 对话页没有画布：get_canvas_layout 在这里恒返回空串（canvas_state.enabled 要求
    # entry=canvas），是**死工具**——不进工具表，模型也就不会白调一次。
    observe_tools=OBSERVE_TOOL_NAMES - {"get_canvas_layout"},
    quota=QUOTA,
    review_policy=ReviewPolicy(
        numbers_mode="always",
        hard_zero_artifact=False,
        dimension_coverage=False,
        acceptance=True,
    ),
    worker_row=ENTRY_WORKERS["chat"],
    fallback_worker=FALLBACK_WORKER,
    artifacts=frozenset({ARTIFACT_REPORT, ARTIFACT_CHART}),
)

CANVAS = EntryProfile(
    entry="canvas",
    core_prompt_key=CORE_PROMPT_KEY,
    profile_prompt_key=CANVAS_PROMPT_KEY,
    observe_tools=OBSERVE_TOOL_NAMES,          # 画布入口需要 get_canvas_layout
    quota=QUOTA,
    review_policy=ReviewPolicy(
        numbers_mode="without_blocks",         # 已落块时数字存疑只算 SOFT
        hard_zero_artifact=True,               # 成功却零块 = 什么都没干
        zero_block_exempt_on_mutation=True,     # 清空/删块/重排不算零产出
        dimension_coverage=True,                # 用户点名维度覆盖不全 → SOFT
        acceptance=True,
    ),
    worker_row=ENTRY_WORKERS["canvas"],
    fallback_worker=FALLBACK_WORKER,
    artifacts=frozenset(
        {ARTIFACT_BLOCK, ARTIFACT_CANVAS_ACTION, ARTIFACT_LAYOUT, ARTIFACT_REPORT}
    ),
)

# 映射表：**唯一**的 entry → profile 映射点
PROFILES: dict[str, EntryProfile] = {
    CHAT.entry: CHAT,
    CANVAS.entry: CANVAS,
}

# 未登记入口的兜底：宁可退化成对话画像，也不要在主链路上抛错
# （与 lead_worker 的 FALLBACK_WORKER 同一原则：派发/装配不该让整轮失败）
DEFAULT_PROFILE = CHAT


def get_profile(entry: object) -> EntryProfile:
    """按入口取能力画像；未登记 → 对话画像（不抛错）。"""
    key = str(entry or "").strip().lower()
    return PROFILES.get(key) or DEFAULT_PROFILE


def register_profile(profile: EntryProfile) -> None:
    """登记一个新入口画像（新增入口用；测试也用它对拍"第三入口"）。"""
    PROFILES[profile.entry] = profile


def all_profiles() -> tuple[EntryProfile, ...]:
    return tuple(PROFILES.values())
