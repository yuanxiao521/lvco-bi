"""D5.3 监控 · 超时降级 → 纯模板报告（无 LLM 依赖）。

真实路径（agent_orchestrator.py execute_task）：
    await asyncio.wait_for(self.graph.invoke(...), timeout=AGENT_ORCHESTRATOR_TIMEOUT)
    except asyncio.TimeoutError:
        report = self._generate_template_report(task_summary, steps_info)
        await emit({"type": "text", "content": report, "report_source": "template"})
        await emit({"type": "done", "timeout": True})

本 demo 不真跑图（那需要 DB/LLM），而是精确复刻超时分支的产物生成：
1) 构造执行到一半的 plan + 部分 results（2 步完成、1 步失败、1 步步骤级超时、1 步未执行）
2) 走 _build_steps_info → _generate_template_report
3) 复现 emit 序列，核对 report_source=template 与 done.timeout=True

运行（在 backend 目录下）：
    python tests/agent_demos/demo_timeout_degrade.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.agent_orchestrator import AgentOrchestrator  # noqa: E402

# 无重依赖实例化（本 demo 只用纯方法；llm/db 均不使用）
orch = AgentOrchestrator(llm=None, db_session=None)

# 全局超时发生在第 3 步前后：steps 4 个，results 只回填了前 3 个
PLAN = {
    "task_summary": "各地区销量分析",
    "steps": [
        {"step_id": "1", "goal": "汇总订单销售额", "tool": "query_sql"},
        {"step_id": "2", "goal": "按地区分组统计", "tool": "query_sql", "depends_on": ["1"]},
        {"step_id": "3", "goal": "生成柱状图", "tool": "render_chart", "depends_on": ["2"]},
        {"step_id": "4", "goal": "撰写结论", "tool": "text", "depends_on": ["2"]},
    ],
}

# 已完成结果的四种形态：成功 JSON / 失败 JSON / 步骤级超时 / 空（未执行）
RESULTS = {
    "1": '{"summary": "销售总额 1200 万"}',
    "2": '{"error": "字段 region 不存在", "hint": "请用 area 字段"}',
    "3": '{"skipped": true, "timeout": true}',
    # "4" 没有结果 → 未执行
}

def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    # ── 复刻超时分支 ──
    task_summary = PLAN.get("task_summary") or ""
    steps_info = orch._build_steps_info(PLAN, RESULTS)
    report = orch._generate_template_report(task_summary, steps_info)

    print("── 事件序列（与真实超时分支一致）──")
    print("  1. text 事件：")
    print(f"     report_source = template   content 长度 = {len(report)} 字符")
    print("  2. done 事件：")
    print("     timeout = True")

    print("\n── 模板报告全文 ──")
    for line in report.splitlines():
        print("  | " + line)

    print("\n── 每步状态判定（_infer_step_status）──")
    for info in steps_info:
        print(f"  步骤 {info['step_id']} {info['goal']:<12s} → {orch._infer_step_status(info['result'])}")

    print("\n观察点：")
    print("  - 状态判定纯字符串驱动：JSON 含 error→失败 / skipped+timeout→超时")
    print("  - detail 只输出前 5 行数据/一句图表说明，避免超时时再爆上下文")
    print("  - 注意 run_analysis（lead_tools.py）也会透传 report_source，") 
    print("    所以 report_source=template 是『降级』的明确信号")


if __name__ == "__main__":
    main()