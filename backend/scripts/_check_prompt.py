# -*- coding: utf-8 -*-
"""自检：ai_prompts 的 LEAD_AGENT_SYSTEM fallback 与 yaml 是否逐字节一致。"""
import sys, textwrap
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
import yaml
from app.services.ai_prompts import LEAD_AGENT_SYSTEM

ROOT = Path(__file__).resolve().parents[1]
y = yaml.safe_load((ROOT / "prompts" / "lead_agent_system.yaml").read_text(encoding="utf-8"))
text = textwrap.dedent(y["system"]).strip("\n")
print("逐字节一致 =", LEAD_AGENT_SYSTEM == text)
print("含复杂度硬规则 =", "宁可填 complex" in LEAD_AGENT_SYSTEM)
print("含 ask_kind/confirm/clarify =", all(k in LEAD_AGENT_SYSTEM for k in ("ask_kind", "confirm", "clarify")))
