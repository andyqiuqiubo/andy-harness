"""FastAPI 入口 —— 装配所有组件。

只做装配，不含业务逻辑。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from harness.api.errors import register_error_handlers
from harness.api.rest.models import router as models_router
from harness.api.rest.models import setup_model_routes
from harness.api.rest.plugins import router as plugins_router
from harness.api.rest.plugins import setup_plugin_routes
from harness.api.rest.providers import router as providers_router
from harness.api.rest.providers import setup_provider_routes
from harness.api.rest.sessions import router as sessions_router
from harness.api.rest.sessions import setup_session_routes
from harness.api.rest.settings import router as settings_router
from harness.api.rest.settings import setup_settings_routes
from harness.api.ws.chat import router as ws_router
from harness.api.ws.chat import setup_ws_routes
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.eventbus import EventBus
from harness.kernel.hooks import HookManager
from harness.kernel.loader import PluginLoader
from harness.kernel.services import ServiceRegistry

logger = logging.getLogger("harness.main")

# 全局组件
_services = ServiceRegistry()
_hooks = HookManager()
_tool_registry = ToolRegistry()
_loader = PluginLoader(
    events=EventBus(),
    services=_services,
    hooks=_hooks,
)


async def _init_components() -> None:
    """初始化核心组件。"""
    _services.register(ToolRegistry, _tool_registry, owner="kernel")
    _services.register(PluginLoader, _loader, owner="kernel")

    db = Database()
    _services.register(Database, db, owner="kernel")

    from harness.modules.session_manager.service import (
        SessionService,
        SessionServiceImpl,
    )

    session_service = SessionServiceImpl(db)
    _services.register(SessionService, session_service, owner="session_manager")

    from harness.modules.context_manager.service import (
        ContextService,
        ContextServiceImpl,
    )

    context_service = ContextServiceImpl(services=_services)
    _services.register(ContextService, context_service, owner="context_manager")

    from harness.engine.builtin_tools import CalculatorTool, CurrentTimeTool

    _tool_registry.register(CalculatorTool(), owner="builtin")
    _tool_registry.register(CurrentTimeTool(), owner="builtin")

    # 注册 ProviderRegistry（即使无 provider 插件，API 也不报 503）
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    pr = ProviderRegistry()
    _services.register(ProviderRegistry, pr, owner="kernel")

    # 从数据库加载自定义 provider
    pr.load_from_db(db)

    logger.info("核心组件已初始化")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """应用生命周期管理。"""
    await _init_components()

    # 加载并激活所有插件
    import pathlib

    plugins_dir = pathlib.Path(__file__).resolve().parent.parent / "plugins"
    activated = await _loader.load_and_activate_all(plugins_dir)
    logger.info("已加载并激活 %d 个插件: %s", len(activated), activated)

    logger.info("andy-harness 已启动")
    yield

    # 优雅关闭：停用所有非核心插件，关闭数据库
    logger.info("andy-harness 正在关闭...")
    for plugin_info in _loader.list_plugins():
        if plugin_info["activated"] and not plugin_info["core"]:
            try:
                await _loader.deactivate(plugin_info["id"])
            except Exception as e:
                logger.warning("停用插件 %s 失败: %s", plugin_info["id"], e)
    try:
        db = _services.get(Database)  # type: ignore[assignment]
        db.close()
    except Exception:
        pass
    logger.info("andy-harness 已关闭")


app = FastAPI(
    title="andy-harness",
    description="插件化 Agent Harness 智能体底座",
    version="0.1.0",
    lifespan=lifespan,
)

# 注册错误处理
register_error_handlers(app)

# 注册 REST 路由
setup_session_routes(_services)
setup_provider_routes(_services)
setup_model_routes(_services)
setup_plugin_routes(_services)
setup_settings_routes()

app.include_router(sessions_router)
app.include_router(providers_router)
app.include_router(models_router)
app.include_router(plugins_router)
app.include_router(settings_router)

# 注册 WebSocket 路由
setup_ws_routes(_services, _hooks, _tool_registry)

app.include_router(ws_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """健康检查端点。"""
    return {"status": "ok"}
