# -*- coding: utf-8 -*-
"""确认路由分布：unified 是否更多走了 react 轻量内核。"""
import re, sys
from collections import Counter
from pathlib import Path
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
LOGS = Path(r"E:\BI\LvcoBI\lvco-bi\backend\eval_logs")

for tag, name in (("u", "unified"), ("l", "legacy")):
    text = "".join(f.read_text(encoding="utf-8", errors="replace")
                   for f in sorted(LOGS.glob(f"{tag}[0-9]*.log")))
    modes = Counter(re.findall(r"run_analysis done mode=(\w+)", text))
    print(f"{name}: 派发内核分布 = {dict(modes)}  合计 {sum(modes.values())}")
