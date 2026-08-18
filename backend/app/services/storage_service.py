import io
from datetime import timedelta

import structlog
from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = structlog.get_logger("storage")


class MinIOStorage:
    def __init__(self):
        self._client: Minio | None = None
        self._available = False
        self._init_client()

    def _init_client(self):
        if not settings.minio_endpoint:
            logger.warning("minio_not_configured", message="MinIO endpoint not configured, using local filesystem")
            return
        try:
            self._client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=False,
            )
            self._available = True
            logger.info("minio_connected", endpoint=settings.minio_endpoint)
        except Exception as e:
            logger.warning("minio_init_failed", error=str(e))

    def ensure_bucket(self, bucket: str):
        if not self._client or not self._available:
            return
        try:
            if not self._client.bucket_exists(bucket):
                self._client.make_bucket(bucket)
                logger.info("bucket_created", bucket=bucket)
        except S3Error as e:
            logger.warning("bucket_ensure_failed", bucket=bucket, error=str(e))

    def put_object(self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str | None:
        """Upload data to MinIO. Returns None if MinIO unavailable (caller should fall back to local)."""
        if not self._client or not self._available:
            return None
        try:
            self.ensure_bucket(bucket)
            self._client.put_object(bucket, key, io.BytesIO(data), len(data), content_type=content_type)
            logger.info("object_uploaded", bucket=bucket, key=key, size=len(data))
            return key
        except (S3Error, Exception) as e:
            logger.warning("upload_failed", bucket=bucket, key=key, error=str(e))
            return None

    def get_presigned_url(self, bucket: str, key: str, expires: int = 600) -> str | None:
        """Generate presigned GET URL. Returns None if MinIO unavailable."""
        if not self._client or not self._available:
            return None
        try:
            return self._client.presigned_get_object(bucket, key, expires=timedelta(seconds=expires))
        except S3Error as e:
            logger.warning("presigned_url_failed", bucket=bucket, key=key, error=str(e))
            return None

    @property
    def is_available(self) -> bool:
        return self._available


storage = MinIOStorage()
