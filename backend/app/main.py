import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (
    admin_agent_config_router,
    admin_approval_router,
    admin_conversation_router,
    admin_data_pipeline_router,
    admin_datalake_router,
    admin_eval_agent_router,
    admin_eval_kb_router,
    admin_feedback_router,
    admin_health_router,
    admin_hotel_config_router,
    admin_lock_router,
    admin_log_router,
    admin_mcp_router,
    admin_memory_router,
    admin_quality_router,
    admin_report_router,
    admin_router,
    auth_router,
    chat_router,
    guest_auth_router,
    guest_order_router,
    guest_room_router,
    guest_room_type_router,
    guest_service_router,
    health,
    knowledge_router,
    staff_attendance_router,
    staff_router,
    wechat_webhook_router,
)
from app.security import create_session_service
from app.security.rate_limiter import setup_rate_limiter
from app.services.run_dispatch import ArqRunDispatcher, NullRunDispatcher, RunDispatcher
from app.services.run_event_bus import create_run_event_bus
from app.services.task_dispatch import ArqTaskDispatcher, NullTaskDispatcher, TaskDispatcher
from app.wechat import WechatClient

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    dispatcher: RunDispatcher
    try:
        dispatcher = await ArqRunDispatcher.create()
    except Exception as exc:
        logger.warning("ARQ 投递池不可用，Run 保持 pending 待补发：%s", exc)
        dispatcher = NullRunDispatcher()
    app.state.run_dispatcher = dispatcher
    # 后台任务走独立队列：投递失败时任务保持 pending，由 worker 启动恢复与周期 publisher 补发
    task_dispatcher: TaskDispatcher
    try:
        task_dispatcher = await ArqTaskDispatcher.create()
    except Exception as exc:
        logger.warning("ARQ 任务队列不可用，后台任务保持 pending 待补发：%s", exc)
        task_dispatcher = NullTaskDispatcher()
    app.state.task_dispatcher = task_dispatcher
    # 运行事件总线不可用时返回空实现：SSE 退回数据库状态投影
    app.state.run_event_bus = await create_run_event_bus()
    # 会话存储不可用时登录 503、令牌认证 401（失败关闭）
    session_service = await create_session_service()
    app.state.session_service = session_service
    # 微信渠道客户端：懒建连，失败在调用点按 502/503 显式暴露
    app.state.wechat_client = WechatClient.from_settings()
    try:
        yield
    finally:
        await session_service.aclose()
        await app.state.run_event_bus.aclose()
        await app.state.wechat_client.aclose()
        await dispatcher.aclose()
        await task_dispatcher.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    setup_rate_limiter(app)
    app.include_router(health.router, prefix="/api")
    app.include_router(auth_router.router, prefix="/api")
    app.include_router(admin_router.router, prefix="/api")
    app.include_router(admin_agent_config_router.router, prefix="/api")
    app.include_router(admin_approval_router.router, prefix="/api")
    app.include_router(admin_conversation_router.router, prefix="/api")
    app.include_router(admin_eval_agent_router.router, prefix="/api")
    app.include_router(admin_eval_kb_router.router, prefix="/api")
    app.include_router(admin_feedback_router.router, prefix="/api")
    app.include_router(admin_health_router.router, prefix="/api")
    app.include_router(admin_hotel_config_router.router, prefix="/api")
    app.include_router(admin_lock_router.router, prefix="/api")
    app.include_router(admin_log_router.router, prefix="/api")
    app.include_router(admin_mcp_router.router, prefix="/api")
    app.include_router(admin_memory_router.router, prefix="/api")
    app.include_router(admin_data_pipeline_router.router, prefix="/api")
    app.include_router(admin_datalake_router.router, prefix="/api")
    app.include_router(admin_quality_router.router, prefix="/api")
    app.include_router(admin_report_router.router, prefix="/api")
    app.include_router(staff_attendance_router.router, prefix="/api")
    app.include_router(staff_router.router, prefix="/api")
    app.include_router(guest_room_router.router, prefix="/api")
    app.include_router(guest_room_type_router.router, prefix="/api")
    app.include_router(guest_auth_router.router, prefix="/api")
    app.include_router(guest_order_router.router, prefix="/api")
    app.include_router(chat_router.router, prefix="/api")
    app.include_router(knowledge_router.router, prefix="/api")
    app.include_router(guest_service_router.router, prefix="/api")
    app.include_router(wechat_webhook_router.router, prefix="/api")
    return app


app = create_app()
