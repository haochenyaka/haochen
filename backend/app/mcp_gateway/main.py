"""mcp-gateway：把内部渠道（门锁/机器人/微信）以远程 MCP 暴露。

设计（PR-M3，对齐 mechanism-baseline §2.7）：
- 每个渠道一个 FastMCP 应用（streamable_http），由同一进程按子路径挂载：
  `/lock/mcp`、`/robot/mcp`、`/wechat/mcp`；
- 仅做协议转换：工具实现复用既有渠道客户端（`app.lock` / `app.robot` / `app.wechat`），
  业务服务、幂等、失败语义**不变**；
- 真机联调留 P9；当前工具内部走 device-sim 契约模拟器。

运行（compose `mcp-gateway` 服务）：
    uvicorn app.mcp_gateway.main:app --host 0.0.0.0 --port 9200

子应用各自持有 streamable_http 的 session manager，父应用需组合它们的 lifespan，
否则挂载后的端点在请求期不可用。新增渠道时在 `_build_servers()` 加一项即可。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app.mcp_gateway.tools import (
    build_lock_server,
    build_robot_server,
    build_wechat_server,
)

MCP_PATH = "/mcp"


def _build_servers() -> list[tuple[str, Starlette]]:
    return [
        ("/lock", build_lock_server().streamable_http_app()),
        ("/robot", build_robot_server().streamable_http_app()),
        ("/wechat", build_wechat_server().streamable_http_app()),
    ]


def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app() -> Starlette:
    servers = _build_servers()

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for _prefix, sub in servers:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    return Starlette(
        routes=[Route("/healthz", _healthz)] + [Mount(prefix, app=sub) for prefix, sub in servers],
        lifespan=lifespan,
    )


app = build_app()


__all__ = ["MCP_PATH", "app", "build_app"]
