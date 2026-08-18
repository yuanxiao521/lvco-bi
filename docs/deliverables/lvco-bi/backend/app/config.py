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

    @property
    def is_ai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
