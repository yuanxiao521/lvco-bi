from pathlib import Path

from pydantic_settings import BaseSettings

# .env 优先于系统环境变量：本机环境变量可能残留旧 LLM key/base_url
# （如用户级 OPENAI_API_KEY 指向旧提供方），而 pydantic-settings
# 默认"环境变量 > .env 文件"，会导致换 key 后进程仍用旧配置。
# 这里对 LLM 三项强制以 .env 为准（.env 是本项目的配置源）。
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _dotenv_first(*names: str) -> None:
    """把 .env 中指定的项注入进程环境变量（覆盖已存在的同名项）。"""
    import os

    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    values = dotenv_values(_ENV_FILE)
    for name in names:
        val = values.get(name)
        if val:
            os.environ[name] = val


_dotenv_first("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    DUCKDB_DATA_DIR: str = "./data/duckdb"
    DUCKDB_MEMORY_LIMIT: str = "2GB"
    # SQL 查询执行超时（秒）：guard 通过后抛给执行层的最长耗时。
    # 查询超时保护：DuckDB 查询在 to_thread 中异步执行，超过该值直接中断并返回错误。
    QUERY_EXEC_TIMEOUT: int = 30
    openai_api_key: str | None = None
    openai_model: str = "deepseek-v4-flash"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout: int = 30
    MAX_UPLOAD_SIZE_MB: int = 100
    UPLOAD_DIR: str = "./data/uploads"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost,http://localhost:5173,http://127.0.0.1:5173"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_ttl: int = 300
    # 会话级并发锁 TTL（秒）：同一会话同时只允许一轮 Agent 在跑。
    # 正常路径靠 finally 释放，TTL 只是"进程崩溃/断线"的自愈兜底，
    # 因此必须明显大于单轮最长耗时（否则锁提前过期，互斥失效）。
    ai_session_lock_ttl: int = 600

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "lvco-uploads"

    # DB Encryption
    db_encryption_key: str | None = None

    # Langfuse 可观测性
    LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # 模型路由（任务分级）
    LLM_MODEL_SIMPLE: str = ""
    LLM_MODEL_COMPLEX: str = ""

    # 多工具编排器（Feature Flag）
    # True: 复杂任务走规划-执行编排器（AgentOrchestrator：Planner 动态规划 → Executor 执行工具）
    # False: 所有任务走单 Agent ReAct 状态机（快速路径）
    # 简单任务（短消息/列数据源）始终走状态机；仅复杂任务进入编排
    AGENT_ORCHESTRATOR_ENABLED: bool = True

    # Task 6 (P1-8)：编排器超时控制
    AGENT_STEP_TIMEOUT: int = 30  # 单步骤超时（秒）
    # 多步骤编排整体超时：思考模式 LLM 单轮流式 tool-calling 可能 10-20s，
    # 4-6 步查询+图表任务轻松超过 60s（实测固定 60s 常触发模板报告降级），放宽到 180s
    AGENT_ORCHESTRATOR_TIMEOUT: int = 180

    # Executor 上下文是否注入「全局计划」（步骤全景图：共几步/第几步/已完成/待执行）
    # 开关用于 AB 实验：True 给执行 LLM 补全局视野，False 保持纯单步工单
    AGENT_PLAN_INJECTION_ENABLED: bool = True

    # 主导 Agent（LeadAgent）Feature Flag —— 【已退役】
    # 主导 Agent 现在是唯一执行路径（旧双路径已整体移除），此字段不再被任何代码读取。
    # 之所以保留而非删除：Settings 的 extra 策略是 forbid，直接删字段会让 .env / 部署环境里
    # 残留的 LEAD_AGENT_ENABLED 变成"多余输入"，导致服务启动直接失败。彻底移除需先同步
    # 清理 .env 与部署配置。
    LEAD_AGENT_ENABLED: bool = True

    # 意图识别超时（秒）：超时降级为规则意图，不阻塞主链路。
    # 8s 实测偏紧（真机探针一轮合并调用 2.2s 正常，但深思考模型尖峰可到 9-10s），
    # 超时降级的代价是关键词兜底误判意图（"清空画布"被判 chat → 答非所问），故放宽。
    LEAD_INTENT_TIMEOUT: float = 12.0
    # 决策超时（秒）：超时降级为确定性决策（按意图直接映射动作）
    LEAD_DECISION_TIMEOUT: float = 15.0
    # 注入 LeadContext 的最近轮次上限（短期记忆窗口）
    LEAD_MAX_TURNS_IN_CTX: int = 30  # AB-B: 20->30
    # 记忆合并节流：未并入长期记忆的用户轮数达到该阈值才触发一次累积合并
    LEAD_MEMORY_MERGE_ROUNDS: int = 6  # AB-B: 4->6
    # 清空类目标代码直执行开关（AB 实验）：False 时回落 LLM 推理选工具（观察用）
    LEAD_CLEAR_DIRECT_EXEC: bool = True
    # 清空类目标是否代码直执行 clear_canvas（绕过 LLM）。AB 开关：False 时回落
    # LLM 推理路径，用于观测 LLM 在清空目标下的真实工具选择行为。
    LEAD_CLEAR_DIRECT_EXEC: bool = True
    # 单次合并最多并入的消息条数：超出部分不会被丢弃，而是留到下一轮继续并（水位驱动）。
    LEAD_MEMORY_MAX_MERGE_MESSAGES: int = 24
    # 连续失败熔断阈值：达到后暂停合并，避免每轮白烧一次摘要 LLM 调用（借鉴 Claude Code
    # 的 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES；其线上观测到 1279 个会话曾连续失败 50+ 次）。
    LEAD_MEMORY_MAX_FAILURES: int = 3
    # 长期记忆摘要的目标字数（分区化结构化记忆，四节：口径/数字与结论/数据源/未决问题）。
    # 旧值 200 字实测会被"新信息挤旧信息"，口径容易丢；放宽到 600 且分节后口径有固定位置。
    LEAD_MEMORY_SUMMARY_CHARS: int = 600
    # 摘要输出的 max_tokens（须显著大于字数的 token 上限，留出 <analysis> 草稿的空间）
    LEAD_MEMORY_SUMMARY_MAX_TOKENS: int = 900
    # 上下文装配预算（digest 注入给子任务的总字符上限）
    LEAD_CTX_MAX_CHARS: int = 4000
    # 其中为"最近几轮活记忆"预留的字符数：长期记忆再长也不能把它挤没
    LEAD_CTX_LIVE_RESERVE_CHARS: int = 800
    # "上一轮已生成图表"摘要注入的总字符预算（按图逐条累加，超预算即停）
    LEAD_CTX_CHART_SUMMARY_CHARS: int = 2000
    # 答案生成时喂入的历史轮次总字符预算（取最近若干"整条"消息，超预算即停）
    LEAD_CTX_ANSWER_HISTORY_CHARS: int = 2000
    # 是否输出细粒度进度汇报（False 仅关键节点汇报，减少 SSE 噪声）
    LEAD_PROGRESS_VERBOSE: bool = False
    # Supervisor 主管循环：一轮对话最多派发子任务的轮次上限（防主管无限转圈）
    LEAD_MAX_SUPERVISOR_ROUNDS: int = 3

    # Insight 调度（dashboard-scheduler-and-insight-activation Task 5）
    INSIGHT_ENABLED: bool = True
    INSIGHT_INTERVAL_MINUTES: int = 5

    # 上下文压缩
    CONTEXT_MAX_CHARS: int = 150000
    CONTEXT_KEEP: int = 60
    CONTEXT_MIN_ROUNDS: int = 3
    CONTEXT_KEEP_ROUNDS: int = 3

    # 单工具结果压缩（rows 数据行保留前 N 行；insights 等建议列表不按条数截断，
    # 完整保留，仅受上方 RESULT_MAX_CHARS 总字符上限兜底）。
    # 2000/20：1500 字符对 executed_sql+summary+明细偏紧；20 行覆盖常见分析报表。
    # 仍保留"工具侧 sample 50 行 → 注入侧 20 行 → 字符 2000 兜底"三层防线，
    # 全量数据按需走 executed_sql 续查（LIMIT/OFFSET），不放开上限。
    RESULT_MAX_CHARS: int = 2000
    RESULT_MAX_ROWS: int = 20
    # 元数据类结果（无 rows 键，如 list_datasources 的完整列名/描述）保底上限。
    # 这类结果必须完整进入上下文才能让 LLM 拿到全量列名，不能用 1500 一刀切，
    # 仅用一个较大的上限防 pathological 超大响应。
    RESULT_MAX_META_CHARS: int = 8000

    @property
    def is_ai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def is_langfuse_configured(self) -> bool:
        """Langfuse 启用且 PUBLIC_KEY/SECRET_KEY 均已配置。"""
        return bool(
            self.LANGFUSE_ENABLED
            and self.LANGFUSE_PUBLIC_KEY
            and self.LANGFUSE_SECRET_KEY
        )

    def model_for_task(self, task_type: str) -> str:
        """根据任务复杂度路由模型。

        简单任务：list_datasources / polish_text / clean_suggest / recommend_charts
        复杂任务：agent_stream / generate_insights / chart_agent / planner_agent

        未配置分级模型时回退到默认 openai_model。
        """
        simple_tasks = {"simple", "polish", "clean", "recommend"}
        if task_type in simple_tasks:
            return self.LLM_MODEL_SIMPLE or self.openai_model
        return self.LLM_MODEL_COMPLEX or self.openai_model

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
