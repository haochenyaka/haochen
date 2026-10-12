"""知识库对象存储：原件与解析产物走 MinIO，数据库只存 object_key。

不变量：
- 正文不落对象存储的「事实」以外内容；bucket 在首次使用时确保存在；
- 同步 SDK 一律 `asyncio.to_thread` 包装，不阻塞事件循环；
- 删除是幂等操作：对象不存在不报错（补偿路径可重复执行）。
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from minio import Minio
from minio.error import S3Error

from app.config import get_settings

logger = logging.getLogger(__name__)


class ObjectStorage:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool,
        bucket: str,
    ) -> None:
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        self._bucket_ready = False

    @classmethod
    def from_settings(cls) -> ObjectStorage:
        settings = get_settings()
        return cls(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
            bucket=settings.minio_bucket_knowledge,
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ensure_bucket(self) -> None:
        if self._bucket_ready:
            return

        def _ensure() -> None:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)

        await asyncio.to_thread(_ensure)
        self._bucket_ready = True

    async def put_bytes(
        self, object_key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        await self.ensure_bucket()

        def _put() -> None:
            self._client.put_object(
                self._bucket,
                object_key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )

        await asyncio.to_thread(_put)

    async def get_bytes(self, object_key: str) -> bytes:
        await self.ensure_bucket()

        def _get() -> bytes:
            response: Any = self._client.get_object(self._bucket, object_key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_get)

    async def remove(self, object_key: str) -> None:
        await self.ensure_bucket()

        def _remove() -> None:
            try:
                self._client.remove_object(self._bucket, object_key)
            except S3Error as exc:
                if exc.code not in {"NoSuchKey", "NoSuchBucket"}:
                    raise
                logger.info("object already absent: %s", object_key)

        await asyncio.to_thread(_remove)
