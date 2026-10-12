import logging

import redis.asyncio as redis
from fastapi import APIRouter, Response
from sqlalchemy import text

from app.config import get_settings
from app.db import engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/system/ready")
async def ready(response: Response) -> dict[str, object]:
    settings = get_settings()
    checks: dict[str, str] = {}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        logger.warning("postgres readiness check failed: %s", exc)
        checks["postgres"] = f"error: {type(exc).__name__}"

    client = redis.Redis.from_url(settings.redis_url)
    try:
        await client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("redis readiness check failed: %s", exc)
        checks["redis"] = f"error: {type(exc).__name__}"
    finally:
        await client.aclose()

    status = "ready" if all(value == "ok" for value in checks.values()) else "degraded"
    if status != "ready":
        response.status_code = 503
    return {"status": status, "checks": checks}
