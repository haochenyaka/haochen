"""数据湖对象存储：Parquet 文件写入/读取 MinIO，支持增量分区。

路径约定：{table_name}/year={yyyy}/month={mm}/day={dd}/part-{offset}.parquet
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import urllib3
from minio import Minio
from minio.error import S3Error

from app.config import get_settings

logger = logging.getLogger(__name__)

_DATA_LAKE_BUCKET = "haochen-datalake"

CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 5.0
HTTP_RETRIES = 1


def _date_path(table: str, dt: date, offset: int = 0) -> str:
    return f"{table}/year={dt.year}/month={dt.month:02d}/day={dt.day:02d}/part-{offset:06d}.parquet"


def _parse_date_path(path: str) -> tuple[str, date, int] | None:
    m = re.match(r"(.+)/year=(\d{4})/month=(\d{2})/day=(\d{2})/part-(\d+)\.parquet", path)
    if not m:
        return None
    return (
        m.group(1),
        date(int(m.group(2)), int(m.group(3)), int(m.group(4))),
        int(m.group(5)),
    )


class LakeStorage:
    """数据湖存储：基于 MinIO 的 Parquet 分层存储。

    与 ObjectStorage 共享同一 MinIO 客户端模式，但使用独立桶 haochen-datalake。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool,
    ) -> None:
        # 湖不可达时快速失败：客户端默认重试会耗到 ~30s，把报表降级与管道任务一起拖住。
        http_client = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT_SECONDS, read=READ_TIMEOUT_SECONDS),
            retries=urllib3.Retry(total=HTTP_RETRIES, backoff_factor=0.1),
        )
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            http_client=http_client,
        )
        self._bucket = _DATA_LAKE_BUCKET
        self._bucket_ready = False

    @classmethod
    def from_settings(cls) -> LakeStorage:
        settings = get_settings()
        return cls(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    async def ensure_bucket(self) -> None:
        if self._bucket_ready:
            return

        def _ensure() -> None:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)

        await asyncio.to_thread(_ensure)
        self._bucket_ready = True

    async def write_parquet(
        self,
        table: str,
        dt: date,
        offset: int,
        data: list[dict[str, Any]],
        schema: pa.Schema | None = None,
    ) -> str:
        """将一批数据作为 Parquet 写入 MinIO。"""
        if not data:
            return ""
        object_key = _date_path(table, dt, offset)
        table_arrow = pa.Table.from_pylist(data, schema=schema)
        buf = io.BytesIO()
        pq.write_table(table_arrow, buf, compression="zstd")
        buf.seek(0)
        raw = buf.read()

        await self.ensure_bucket()

        def _put() -> None:
            self._client.put_object(
                self._bucket,
                object_key,
                io.BytesIO(raw),
                length=len(raw),
                content_type="application/octet-stream",
            )

        await asyncio.to_thread(_put)
        logger.info("wrote %d rows to parquet: %s", len(data), object_key)
        return object_key

    async def read_parquet_to_temp(self, object_key: str) -> Path:
        """将 Parquet 文件下载到临时目录并返回路径。"""
        await self.ensure_bucket()

        def _get() -> bytes:
            response: Any = self._client.get_object(self._bucket, object_key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        data = await asyncio.to_thread(_get)
        tmp = Path(tempfile.mkdtemp()) / object_key.replace("/", "_")
        tmp.write_bytes(data)
        return tmp

    async def list_tables(self) -> list[str]:
        """列出数据湖中所有表名（扫描顶层前缀）。"""
        await self.ensure_bucket()
        tables: set[str] = set()

        def _list() -> None:
            for obj in self._client.list_objects(self._bucket, prefix="", recursive=False):
                if obj.is_dir and obj.object_name:
                    tables.add(obj.object_name.rstrip("/"))

        await asyncio.to_thread(_list)
        return sorted(tables)

    async def list_partitions(
        self, table: str, year: int | None = None, month: int | None = None
    ) -> list[str]:
        """列出指定表的分区文件路径。"""
        prefix = f"{table}/"
        if year is not None:
            prefix += f"year={year}/"
            if month is not None:
                prefix += f"month={month:02d}/"

        await self.ensure_bucket()
        files: list[str] = []

        def _list() -> None:
            for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
                if obj.object_name and obj.object_name.endswith(".parquet"):
                    files.append(obj.object_name)

        await asyncio.to_thread(_list)
        return sorted(files)

    async def latest_modification(self, table: str) -> datetime | None:
        """表下最新一个 Parquet 分片的最后修改时间（供报表新鲜度校验）。

        无分片或湖不可达时返回 None（调用方据此判为陈旧并回落 PG）。
        """
        prefix = f"{table}/"
        await self.ensure_bucket()
        latest: datetime | None = None

        def _scan() -> None:
            nonlocal latest
            for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
                if not (obj.object_name and obj.object_name.endswith(".parquet")):
                    continue
                ts = getattr(obj, "last_modified", None)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if latest is None or ts > latest:
                    latest = ts

        await asyncio.to_thread(_scan)
        return latest

    async def delete_partition(self, object_key: str) -> None:
        """删除指定 Parquet 文件（幂等）。"""
        await self.ensure_bucket()

        def _remove() -> None:
            try:
                self._client.remove_object(self._bucket, object_key)
            except S3Error as exc:
                if exc.code not in {"NoSuchKey", "NoSuchBucket"}:
                    raise

        await asyncio.to_thread(_remove)
