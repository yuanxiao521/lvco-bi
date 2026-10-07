"""统计各提示词模板与工具 schema 的体量（字符数 + token 估算）。

token 用 tiktoken cl100k_base 近似（DeepSeek/Qwen 的真实分词略有差异，
但中文量级一致，可作相对比较）。输出按体量排序，并给出两个入口的
"单次调用实际拼装量"，用于判断哪一块才是真正的大头。
"""
from __future__ import annotations

import json
import pathlib
import sys

import tiktoken

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PROMPTS = BACKEND / "prompts"
ENC = tiktoken.get_encoding("cl100k_base")


def ntok(s: str) -> int:
    return len(ENC.encode(s))


def section(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def main() -> None:
    section("① 模板文件本体（prompts/*.yaml，含 yaml 的 name/version/template 字段）")
    rows = []
    for p in sorted(PROMPTS.glob("*.yaml"), key=lambda x: -x.stat().st_size):
        raw = p.read_text(encoding="utf-8")
        rows.append((p.name, len(raw), ntok(raw)))
    print(f"{'文件':<34}{'字符':>8}{'~token':>9}")
    for name, chars, tk in rows:
        print(f"{name:<34}{chars:>8}{tk:>9}")
    print(f"{'合计':<34}{sum(r[1] for r in rows):>8}{sum(r[2] for r in rows):>9}")

    section("② 运行时真正注入的 system 字符串（ai_prompts 常量，yaml 解析后）")
    sys.path.insert(0, str(BACKEND))
    from app.services import ai_prompts as ap

    consts = {k: v for k, v in vars(ap).items()
              if k.isupper() and isinstance(v, str) and len(v) > 200}
    total = 0
    for k in sorted(consts, key=lambda x: -len(consts[x])):
        tk = ntok(consts[k])
        total += tk
        print(f"{k:<34}{len(consts[k]):>8}{tk:>9}")
    print(f"{'合计（全部常量，非常规单次注入）':<34}{'':>8}{total:>9}")

    section("③ 工具 schema（这是常被忽略的大头）")
    from app.services.agent_tools import ToolRegistry
    from app.services.canvas_tools import CANVAS_TOOL_NAMES
    from app.services.agents.planner_agent import _get_cached_orchestrator_tools  # type: ignore

    def schema_size(names) -> tuple[int, int, list[tuple[str, int]]]:
        total = 0
        rows_ = []
        for name in sorted(names):
            tool = ToolRegistry.get(name)
            if tool is None:
                continue
            s = json.dumps(tool.schema(), ensure_ascii=False)
            tk = ntok(s)
            total += tk
            rows_.append((name, tk))
        return total, len(rows_), rows_

    canvas_names = set(CANVAS_TOOL_NAMES)
    canvas_total, canvas_n, canvas_rows = schema_size(canvas_names)
    print(f"\n【画布入口白名单】{canvas_n} 个工具，合计 ~{canvas_total} token")
    for name, tk in sorted(canvas_rows, key=lambda x: -x[1]):
        print(f"  {name:<28}{tk:>7}")

    try:
        chat_names = set(_get_cached_orchestrator_tools())
        chat_total, chat_n, chat_rows = schema_size(chat_names)
        print(f"\n【对话入口白名单】{chat_n} 个工具，合计 ~{chat_total} token")
        for name, tk in sorted(chat_rows, key=lambda x: -x[1])[:12]:
            print(f"  {name:<28}{tk:>7}")
        print(f"  ……（仅列前 12 个）")
    except Exception as e:  # noqa: BLE001
        print(f"对话白名单取不到：{e}")

    section("④ 单次调用拼装量（模板 + 工具 schema + 上下文块）")
    lead_merged = consts.get("LEAD_MERGED_SYSTEM", "")
    print(f"Lead 首轮合并调用 system = LEAD_MERGED_SYSTEM          ~{ntok(lead_merged)} token")
    print(f"Lead 后续轮 system     = LEAD_DECISION_SYSTEM          ~{ntok(consts.get('LEAD_DECISION_SYSTEM', ''))} token")
    print(f"画布执行器 system      = CANVAS_EXECUTOR_SYSTEM        ~{ntok(consts.get('CANVAS_EXECUTOR_SYSTEM', ''))} token")
    print(f"画布规划 system        = CANVAS_PLANNER_SYSTEM         ~{ntok(consts.get('CANVAS_PLANNER_SYSTEM', ''))} token")
    print(f"ReAct system           = AGENT_SYSTEM                  ~{ntok(consts.get('AGENT_SYSTEM', ''))} token")
    print(f"\n+ 工具 schema（画布入口）                              ~{canvas_total} token")
    print(f"+ 工具 schema（对话入口）                              ~{chat_total if 'chat_total' in dir() else '?'} token")

    # 上下文块：按 lead_decision 的 MAX_SUMMARY_CHARS 等常量估上界
    from app.services.agents.lead import lead_decider as ld
    print("\n上下文块（Lead 决策注入）:")
    for k in ("MAX_SUMMARY_CHARS", "CONTEXT_MAX_CHARS"):
        if hasattr(ld, k):
            v = getattr(ld, k)
            print(f"  {k} = {v} 字符 ≈ {int(v * 0.75)} token")


# ── ⑤ 按段落拆解最大的几份提示词，看清"肥肉"在哪 ──
    section("⑤ 段落级拆解（Top 9 提示词，按 markdown 标题切块）")
    import re
    import yaml as _yaml

    def load_system(p: pathlib.Path) -> str:
        try:
            data = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return str(data.get("system") or "")
        except Exception:  # noqa: BLE001
            return ""

    targets = sorted(PROMPTS.glob("*.yaml"),
                     key=lambda x: -ntok(load_system(x)))[:9]
    for p in targets:
        body = load_system(p)
        chunks = re.split(r"(?m)^(\s*#{1,4} .*)$", body)
        sections: list[tuple[str, str]] = []
        head = chunks[0]
        if head.strip():
            sections.append(("（开头/角色设定）", head))
        for i in range(1, len(chunks), 2):
            sections.append((chunks[i].strip(), chunks[i + 1] if i + 1 < len(chunks) else ""))
        total = ntok(body)
        print(f"\n── {p.name}  合计 ~{total} token ──")
        for title, content in sorted(sections, key=lambda x: -ntok(x[1]))[:8]:
            tk = ntok(content)
            bar = "█" * max(1, int(tk / 100))
            print(f"  {tk:>5}  {bar} {title[:52]}")


if __name__ == "__main__":
    main()