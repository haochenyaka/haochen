"""清理数据湖中所有 Parquet 文件（用于修复后重跑）。"""

import asyncio
import logging

from minio import Minio
from minio.error import S3Error

from app.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clean_datalake")

BUCKET = "haochen-datalake"


async def main() -> None:
    settings = get_settings()
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    def _clear() -> None:
        try:
            objects = list(client.list_objects(BUCKET, recursive=True))
            if not objects:
                logger.info("Bucket already empty")
                return
            for obj in objects:
                client.remove_object(BUCKET, obj.object_name)
                logger.info("Removed: %s", obj.object_name)
        except S3Error as e:
            if e.code == "NoSuchBucket":
                logger.info("Bucket does not exist, nothing to clean")
            else:
                raise

    await asyncio.to_thread(_clear)
    logger.info("Cleanup complete")


if __name__ == "__main__":
    asyncio.run(main())
