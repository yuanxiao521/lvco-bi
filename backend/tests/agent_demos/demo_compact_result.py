"""D4.2 记忆 · compact_result_json 三种工具结果的截断策略。

目标：理解"工具结果注入上下文"前的差异化压缩：
- error 结果：完整保留（自纠错依赖错误与 hint）
- 查询结果（含 rows）：只留前 RESULT_MAX_ROWS 行，并补 rows_total / rows_truncated
- 元数据结果（无 rows，如列名列表）：不套 1500 一刀切，改用 RESULT_MAX_META_CHARS 保底

运行（在 backend 目录下）：
    python tests/agent_demos/demo_compact_result.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.services.context_utils import compact_result_json  # noqa: E402


def main() -> None:
    print(f"RESULT_MAX_CHARS={settings.RESULT_MAX_CHARS}  "
          f"RESULT_MAX_ROWS={settings.RESULT_MAX_ROWS}  "
          f"RESULT_MAX_META_CHARS={settings.RESULT_MAX_META_CHARS}\n")

    error_payload = {
        "error": "字段 gender 不存在",
        "hint": "可用字段: " + ", ".join(f"col{i}" for i in range(200)),
    }
    error_str = json.dumps(error_payload, ensure_ascii=False)
    error_out = compact_result_json(error_str)
    print("① error 结果")
    print(f"   输入 {len(error_str)} 字符 → 输出 {len(error_out)} 字符"
          f"  （原样保留={error_out == error_str}）")
    print()

    rows_payload = {
        "columns": ["city", "channel", "sales"],
        "rows": [[f"城市{i:03d}", f"渠道{i % 5}", i * 10] for i in range(80)],
    }
    rows_str = json.dumps(rows_payload, ensure_ascii=False)
    rows_out = compact_result_json(rows_str)
    parsed = json.loads(rows_out)
    print("② 查询结果（含 80 行 rows）")
    print(f"   输入 {len(rows_str)} 字符 → 输出 {len(rows_out)} 字符")
    print(f"   rows 保留={len(parsed['rows'])} 行  rows_total={parsed['rows_total']}  "
          f"rows_truncated={parsed['rows_truncated']}")
    print()

    meta_payload = {
        "columns": [f"col{i}" for i in range(300)],
        "description": "元数据说明。" * 200,
    }
    meta_str = json.dumps(meta_payload, ensure_ascii=False)
    meta_out = compact_result_json(meta_str)
    print("③ 元数据结果（无 rows，长度略超 1500）")
    print(f"   输入 {len(meta_str)} 字符 → 输出 {len(meta_out)} 字符"
          f"  （未被 1500 腰斩={meta_out == meta_str}）")
    print()

    big_meta_str = json.dumps({"description": "元数据说明。" * 2000}, ensure_ascii=False)
    big_meta_out = compact_result_json(big_meta_str)
    print("④ 超大元数据（超过 META 上限）")
    print(f"   输入 {len(big_meta_str)} 字符 → 输出 {len(big_meta_out)} 字符"
          f"  （被截断={len(big_meta_out) < len(big_meta_str)}）")
    print()
    print("观察点：")
    print("  - error 一字不删；rows 结构保留只砍数据；元数据用更宽的 META 上限")


if __name__ == "__main__":
    main()
