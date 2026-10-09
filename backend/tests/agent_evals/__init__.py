"""Agent 评测模块。

包含：
- dataset.jsonl：25 道 BI 评测题
- judge.py：评分逻辑（SQL 准确率 / 工具成功率 / 输出有效率 / 图表类型）
- run_eval.py：执行入口

使用：
    cd backend && python -m tests.agent_evals.run_eval --mode mock
    cd backend && python -m tests.agent_evals.run_eval --mode real   # 需要 LLM
"""