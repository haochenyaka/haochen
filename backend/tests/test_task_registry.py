"""后台任务注册表单元测试：静态清单惰性导入、版本精确匹配与失败关闭。"""

from __future__ import annotations

import pytest

from app.services.task_registry import (
    TaskContext,
    register_module_handlers,
    registered_handlers,
    reset_registry,
    resolve_handler,
    task_handler,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    reset_registry()
    yield
    reset_registry()


def test_register_and_resolve_exact_version() -> None:
    @task_handler("demo.echo", 1)
    async def _echo(ctx: TaskContext) -> dict[str, object]:
        return {"echo": ctx.payload_value("value")}

    spec = resolve_handler("demo.echo", 1)
    assert spec is not None
    assert spec.task_type == "demo.echo"
    assert spec.handler_version == 1
    assert spec.key == ("demo.echo", 1)
    assert spec.on_failure is None


def test_unknown_version_is_fail_closed() -> None:
    @task_handler("demo.echo", 2)
    async def _echo(ctx: TaskContext) -> dict[str, object] | None:
        return None

    assert resolve_handler("demo.echo", 1) is None
    assert resolve_handler("demo.echo", 3) is None
    assert resolve_handler("demo.unknown", 2) is None


def test_duplicate_registration_rejected() -> None:
    @task_handler("demo.dup", 1)
    async def _first(ctx: TaskContext) -> None:
        return None

    with pytest.raises(RuntimeError):

        @task_handler("demo.dup", 1)
        async def _second(ctx: TaskContext) -> None:
            return None


def test_failure_hook_registered_with_handler() -> None:
    async def _hook(session: object, ctx: TaskContext, error: str) -> None:
        return None

    @task_handler("demo.hooked", 1, on_failure=_hook)
    async def _body(ctx: TaskContext) -> None:
        return None

    spec = resolve_handler("demo.hooked", 1)
    assert spec is not None
    assert spec.on_failure is _hook


def test_missing_module_treated_as_unregistered() -> None:
    assert register_module_handlers("app.no.such.module") == 0
    assert resolve_handler("app.no.such.module", 1) is None
    # 重复调用不再尝试导入（幂等）
    assert register_module_handlers("app.no.such.module") == 0


def test_registered_handlers_snapshot() -> None:
    @task_handler("demo.a", 1)
    async def _a(ctx: TaskContext) -> None:
        return None

    @task_handler("demo.a", 2)
    async def _b(ctx: TaskContext) -> None:
        return None

    snapshot = registered_handlers()
    # reset 后首次解析会重放各领域模块，注册表同时含真实处理器与本用例的两个版本
    assert {("demo.a", 1), ("demo.a", 2)} <= set(snapshot)
    assert snapshot is not registered_handlers()
