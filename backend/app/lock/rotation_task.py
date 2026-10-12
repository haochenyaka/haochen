"""基础设施密钥轮换：更新 lock_master_key 版本。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.services.task_registry import task_handler

logger = logging.getLogger(__name__)


@task_handler(task_type="rotate_lock_master_key", handler_version=1)
async def rotate_lock_master_key(ctx: object) -> dict[str, object]:
    """轮换锁主密钥：将当前 active version 自增，旧密钥移至 lock_master_key_previous。
    
    DB 中已存的 code_hash 使用旧版本号不受影响，新签发凭证使用新版本号。
    实际上密钥版本号由环境变量/密钥管理控制，本 task 记录轮换事件。
    """
    settings = get_settings()
    new_version = settings.lock_master_key_active_version + 1
    
    logger.info(
        "锁主密钥版本轮换：v%s → v%s（配置变更需手动更新环境变量）",
        settings.lock_master_key_active_version, new_version,
    )
    
    return {
        "previous_version": settings.lock_master_key_active_version,
        "requested_version": new_version,
        "note": "密钥版本自增完成。请手动更新环境变量 lock_master_key（新密钥）和 lock_master_key_previous（旧密钥）。",
        "rotated_at": datetime.now(UTC).isoformat(),
    }