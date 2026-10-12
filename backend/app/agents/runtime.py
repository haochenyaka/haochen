"""运行期上下文与 PostgreSQL checkpointer 生命周期。

检查点只落 PostgreSQL：建表 DDL 由 Alembic 迁移纳管（运行期不执行建表），连接池
按进程复用一个实例，随 worker 关闭释放。resume 时重建 Context，不从相邻 Run 猜测结果。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.context import ResourceSnapshot
from app.agents.skills import SkillCatalog
from app.config import get_settings
from app.db import SessionFactory
from app.services.audit_service import AuditRecorder
from app.services.run_events import RunEventEmitter

if TYPE_CHECKING:  # 真实类型仅供静态检查
    from app.agents.toolkits.registry import ToolRegistry
else:  # 运行期占位：pydantic 解析字段注解只要求名字可解析，导入真身会与 toolkits 包初始化成环
    ToolRegistry = object

logger = logging.getLogger(__name__)


CHECKPOINT_POOL_MIN_SIZE = 1
CHECKPOINT_POOL_MAX_SIZE = 4
CHECKPOINT_POOL_OPEN_TIMEOUT_SECONDS = 10.0


class RetrievalRefCollector:
    """Run 内检索引用收集器：工具写入、模型调用时取走，同一 Run 共享一个实例。

    不用 ContextVar：图把工具调用派发到子任务执行，子任务里的写入不会回到父上下文，
    显式对象随 RuntimeContext 传递才可靠。drain 取走后即清空，审计记的是"本次模型
    调用所见的新增引用"，不会把上一轮的引用重复计入。
    """

    def __init__(self) -> None:
        self._refs: list[dict[str, object]] = []

    def add(self, refs: Iterable[Mapping[str, object]]) -> None:
        self._refs.extend(dict(ref) for ref in refs)

    def drain(self) -> list[dict[str, object]]:
        drained, self._refs = self._refs, []
        return drained


@dataclass(frozen=True)
class RuntimeContext:
    """一次 Run 的运行期上下文：身份、装配资源、审计写入口与检查点句柄。"""

    agent_slug: str
    user_id: UUID
    thread_id: UUID
    run_id: UUID
    actor_id: str
    resources: ResourceSnapshot
    config: Mapping[str, Any]
    prompt_docs: str
    checkpointer: BaseCheckpointSaver | None
    recorder: AuditRecorder
    workdir: Path | None = None
    skills: SkillCatalog | None = None
    """Skill 内容目录：由运行入口按配置装载（声明缺失在装配期显式失败）。"""
    middleware_config: Mapping[str, Any] = field(default_factory=dict)
    tool_registry: ToolRegistry | None = None
    """工具注册表覆盖（测试注入用）；缺省取进程级内置注册表。"""
    events: RunEventEmitter | None = None
    """运行事件发射器：None 时事件不发射（投影可缺失，事实不受影响）。"""
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory
    """工具侧数据库会话工厂：口径与审计写入口一致，缺省取进程级工厂。"""
    retrieval_refs: RetrievalRefCollector = field(default_factory=RetrievalRefCollector)
    """检索引用收集器：检索工具写入，模型调用取走并落 model_audit.retrieval_refs。"""

    def thread_config(self) -> RunnableConfig:
        """检查点按会话线程隔离：thread_id 取线程主键，namespace 保持默认。"""
        return {"configurable": {"thread_id": str(self.thread_id)}}


_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None
_saver: AsyncPostgresSaver | None = None
_lock = asyncio.Lock()


async def get_shared_checkpointer() -> AsyncPostgresSaver:
    """进程级检查点实例：连接池惰性建立，DDL 不在运行期执行。"""
    global _pool, _saver
    async with _lock:
        if _saver is None:
            pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
                conninfo=get_settings().psycopg_dsn,
                min_size=CHECKPOINT_POOL_MIN_SIZE,
                max_size=CHECKPOINT_POOL_MAX_SIZE,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await pool.open(wait=True, timeout=CHECKPOINT_POOL_OPEN_TIMEOUT_SECONDS)
            _pool = pool
            _saver = AsyncPostgresSaver(pool)
            logger.info("checkpoint store ready")
        return _saver


async def close_shared_checkpointer() -> None:
    global _pool, _saver
    async with _lock:
        if _pool is not None:
            await _pool.close()
            logger.info("checkpoint store closed")
        _pool = None
        _saver = None
