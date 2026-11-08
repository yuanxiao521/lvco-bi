# -*- coding: utf-8 -*-
"""把某个 prompts/<name>.yaml 的 system 正文同步成 ai_prompts 里的 fallback 常量。

用法：python scripts/_sync_prompt_fallback.py <name> <CONST_NAME>
约定：ai_prompts 里的常量形如 _<CONST>_FALLBACK = 三引号字符串，yaml 名与 <name> 一致。
新建常量时用 --create 追加 fallback 常量与 <CONST> = _load(...)。
"""
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import yaml  # noqa: E402

name = sys.argv[1]
const = sys.argv[2]
create = "--create" in sys.argv

if name == "lead_agent_system":
    # 派生别名：从两段组合**重生成** yaml（方向与其余段相反）。
    from app.services import ai_prompts as ap  # noqa: E402  sys.path 已在上面注入

    composed = ap.compose_lead_system(ap.LEAD_CORE, ap.CHAT_PROFILE)
    body = "\n".join(("  " + ln) if ln else "" for ln in composed.split("\n"))
    content = (
        "# P1-D 兼容别名：= lead_core + chat_profile（由 ai_prompts.compose_lead_system 组合）。\n"
        "# 真正的内容源是 lead_core / chat_profile 两个 yaml；本文件由脚本生成，勿手改。\n"
        f"name: {name}\nversion: v1\nsystem: |-\n{body}\n"
    )
    (ROOT / "prompts" / f"{name}.yaml").write_text(content, encoding="utf-8", newline="")
    print(f"重生成派生别名 {name}.yaml：{len(composed)} 字符（来自 lead_core + chat_profile）")
    sys.exit(0)
text = textwrap.dedent(
    yaml.safe_load((ROOT / "prompts" / f"{name}.yaml").read_text(encoding="utf-8"))["system"]
).strip("\n")
assert '"""' not in text, "正文含三引号，需换写法"

ap = ROOT / "app" / "services" / "ai_prompts.py"
s = ap.read_text(encoding="utf-8")
pat = re.compile(r'_' + const + r'_FALLBACK = """.*?"""', re.S)
if pat.search(s):
    s = pat.sub('_' + const + '_FALLBACK = """' + text + '"""', s, count=1)
    action = "更新"
else:
    assert create, f"{const} 的 fallback 不存在，需加 --create"
    anchor = 'LEAD_AGENT_SYSTEM = _load("lead_agent_system", _LEAD_AGENT_SYSTEM_FALLBACK)'
    assert s.count(anchor) == 1
    s = s.replace(anchor, anchor + "\n\n"
                  + '_' + const + '_FALLBACK = """' + text + '"""\n'
                  + const + ' = _load("' + name + '", _' + const + '_FALLBACK)')
    action = "新建"
ap.write_text(s, encoding="utf-8", newline="")

import importlib  # noqa: E402
import app.services.ai_prompts as m  # noqa: E402
importlib.reload(m)
got = getattr(m, const)
print(f"{action} {const}：逐字节一致 =", got == text, f"({len(text)} 字符)")
