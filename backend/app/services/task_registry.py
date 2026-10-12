"""后台任务 Handler 注册表：静态清单 + 惰性导入，未知 handler_version 一律失败关闭。

不变量：
- 注册表只登记「模块路径 + 处理器引用」，模块在首次解析时导入——worker 启动不为
  未用到的领域付出导入成本；
- 解析按 (task_type, handler_version) 精确匹配：版本不匹配视为未知处理器（fail-closed），
  执行器据此把任务置 failed，不做版本回退、不做静默降级；
- 领域模块导入失败不抛出，只记录日志并视为未注册（同样失败关闭），避免单个模块
  的依赖问题拖垮整个 worker 启动。
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.task_dispatch import TaskDispatcher

logger = logging.getLogger(__name__)

# 领域 Handler 模块静态清单：新增领域在此登记，导入发生在首次解析
HANDLER_MODULES: tuple[str, ...] = (
    "app.knowledge.task_handlers",
    "app.lock.task_handlers",
    "app.lock.rotation_task",
    "app.data_pipeline.task_handlers",
)


@dataclass(frozen=True)
class TaskContext:
    """Handler 执行上下文：任务身份、载荷、独立会话工厂与后续任务投递口。"""

    task_id: UUID
    task_type: str
    handler_version: int
    payload: Mapping[str, Any]
    attempt_no: int
    owner_token: str
    session_factory: async_sessionmaker[AsyncSession]
    dispatcher: TaskDispatcher | None = None

    def payload_value(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


class TaskHandlerFn(Protocol):
    async def __call__(self, ctx: TaskContext) -> Mapping[str, Any] | None: ...


class FailureHookFn(Protocol):
    async def __call__(self, session: AsyncSession, ctx: TaskContext, error: str) -> None: ...


@dataclass(frozen=True)
class HandlerSpec:
    """处理器规格：执行体 + 可选的失败收敛钩子（在终态事务内调用）。"""

    task_type: str
    handler_version: int
    run: TaskHandlerFn
    on_failure: FailureHookFn | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.task_type, self.handler_version)


_REGISTRY: dict[tuple[str, int], HandlerSpec] = {}
_LOADED_MODULES: set[str] = set()


def task_handler(
    task_type: str,
    handler_version: int,
    *,
    on_failure: FailureHookFn | None = None,
) -> Callable[[TaskHandlerFn], TaskHandlerFn]:
    """注册处理器：`@task_handler("kb.parse", 1)`。

    同一 (task_type, handler_version) 重复注册直接报错——版本号即不可变契约，
    变更逻辑必须递增 handler_version。
    """

    def _decorator(fn: TaskHandlerFn) -> TaskHandlerFn:
        key = (task_type, handler_version)
        if key in _REGISTRY:
            raise RuntimeError(f"处理器重复注册：{task_type} v{handler_version}")
        _REGISTRY[key] = HandlerSpec(
            task_type=task_type,
            handler_version=handler_version,
            run=fn,
            on_failure=on_failure,
        )
        return fn

    return _decorator


def register_module_handlers(module_name: str) -> int:
    """导入模块以触发其注册（幂等）；导入失败记为未注册并返回 0。"""
    if module_name in _LOADED_MODULES:
        return 0
    _LOADED_MODULES.add(module_name)
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError:
        logger.warning("task handler module missing, treated as unregistered: %s", module_name)
        return 0
    except Exception:
        logger.exception("task handler module import failed: %s", module_name)
        return 0
    return 1


def load_handlers() -> None:
    for module_name in HANDLER_MODULES:
        register_module_handlers(module_name)


def resolve_handler(task_type: str, handler_version: int) -> HandlerSpec | None:
    """解析处理器；未知组合返回 None（调用方必须失败关闭）。"""
    load_handlers()
    return _REGISTRY.get((task_type, handler_version))


def registered_handlers() -> Mapping[tuple[str, int], HandlerSpec]:
    load_handlers()
    return dict(_REGISTRY)


def reset_registry() -> None:
    """测试用：清空注册表与导入标记。"""
    _REGISTRY.clear()
    _LOADED_MODULES.clear()
