from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    DUCKDB_DATA_DIR: str = "./data/duckdb"
    DUCKDB_MEMORY_LIMIT: str = "2GB"
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
    AGENT_ORCHESTRATOR_TIMEOUT: int = 60  # 整个编排流程超时（秒）

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
