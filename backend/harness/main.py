"""FastAPI 入口 —— 装配所有组件。

只做装配，不含业务逻辑。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harness.api.errors import register_error_handlers
from harness.api.middleware.auth import AuthMiddleware
from harness.api.rest.artifacts import router as artifacts_router
from harness.api.rest.artifacts import setup_artifact_routes
from harness.api.rest.attachments import router as attachments_router
from harness.api.rest.attachments import setup_attachment_routes
from harness.api.rest.auth import router as auth_router
from harness.api.rest.auth import setup_auth_routes
from harness.api.rest.channels import router as channels_router
from harness.api.rest.channels import setup_channel_routes
from harness.api.rest.devkit import router as devkit_router
from harness.api.rest.evals_lab import router as evals_lab_router
from harness.api.rest.insights import router as insights_router
from harness.api.rest.integrations import router as integrations_router
from harness.api.rest.mcp import router as mcp_router
from harness.api.rest.mcp import setup_mcp_routes
from harness.api.rest.memories import router as memories_router
from harness.api.rest.memories import setup_memory_routes
from harness.api.rest.models import router as models_router
from harness.api.rest.models import setup_model_routes
from harness.api.rest.permission_audit import router as permission_audit_router
from harness.api.rest.permissions import router as permissions_router
from harness.api.rest.permissions import setup_permission_routes
from harness.api.rest.plugins import router as plugins_router
from harness.api.rest.plugins import setup_plugin_routes
from harness.api.rest.providers import router as providers_router
from harness.api.rest.providers import setup_provider_routes
from harness.api.rest.runs import router as runs_router
from harness.api.rest.runs import setup_runs_routes
from harness.api.rest.schedules import router as schedules_router
from harness.api.rest.schedules import setup_schedule_routes
from harness.api.rest.sessions import router as sessions_router
from harness.api.rest.sessions import setup_session_routes
from harness.api.rest.settings import router as settings_router
from harness.api.rest.settings import setup_settings_routes
from harness.api.rest.skills import router as skills_router
from harness.api.rest.skills import setup_skill_routes
from harness.api.rest.templates_hub import router as templates_hub_router
from harness.api.rest.traces import router as traces_router
from harness.api.rest.traces import setup_trace_routes
from harness.api.rest.workflows import router as workflows_router
from harness.api.ws.chat import router as ws_router
from harness.api.ws.chat import setup_ws_routes
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.eventbus import EventBus
from harness.kernel.hooks import HookManager
from harness.kernel.loader import PluginLoader
from harness.kernel.services import ServiceRegistry

logger = logging.getLogger("harness.main")


def _apply_local_proxy_env() -> None:
    """后端启动时注入出网代理配置（飞书 CLI 等 Go 子进程依赖环境变量）。

    飞书官方 CLI 是 Go 二进制，只认 ``HTTPS_PROXY`` / ``HTTP_PROXY`` 等环境变量，
    不读取 Windows 系统代理；而后端子进程（lark-cli）继承后端进程的环境，
    因此只要后端进程有这些变量，CLI 即可出网。

    读取 ``backend/.env.local``（已被 ``.gitignore`` 忽略，可本地持久化填写、不入库）：:

        HTTPS_PROXY=http://127.0.0.1:7890
        HTTP_PROXY=http://127.0.0.1:7890
        NO_PROXY=localhost,127.0.0.1

    仅当对应变量在进程环境中尚不存在时才写入（启动终端已 ``export`` 的优先级更高）。
    无论是否读到文件，都会确保 ``NO_PROXY`` 含 ``localhost`` / ``127.0.0.1``，
    避免本机环回、MCP、健康检查等内部调用被误送外部代理。
    """
    backend_dir = Path(__file__).resolve().parent.parent
    local_env = backend_dir / ".env.local"
    proxy_keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")

    if local_env.is_file():
        try:
            for raw in local_env.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if not key or key not in proxy_keys:
                    continue
                # 不覆盖已存在的变量：shell 中显式 export 的优先级更高
                if key not in os.environ and key.lower() not in os.environ:
                    os.environ[key] = value
                    if key.isupper() and key.lower() not in os.environ:
                        os.environ[key.lower()] = value
                    logger.info("从 .env.local 注入代理变量 %s", key)
        except OSError as e:
            logger.warning("读取 .env.local 失败: %s", e)

    # 确保环回地址不走代理（内部 / MCP / 健康检查等）
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if "127.0.0.1" not in no_proxy:
        no_proxy = (no_proxy + "," if no_proxy else "") + "localhost,127.0.0.1"
        os.environ["NO_PROXY"] = no_proxy
        os.environ["no_proxy"] = no_proxy

    effective = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "(未设置)"
    logger.info("后端出网代理: HTTPS_PROXY=%s, NO_PROXY=%s", effective, os.environ.get("NO_PROXY"))


def _ensure_cli_home_env() -> None:
    """确保 USERPROFILE / HOME 存在（飞书 CLI 定位配置文件的依据）。

    lark-cli 是 Go 二进制，按 ``USERPROFILE``（Windows）/ ``HOME``（类 Unix）查找
    配置文件（``.lark-cli/config.json``）。某些受限启动环境（如精简沙箱）会缺失
    这些变量，导致 CLI 找不到配置而报 ``not_configured``。

    用 ``Path.home()`` 推算真实用户主目录（不依赖环境变量本身），缺失时补上，
    使后端拉起的 CLI 子进程始终能定位到同一份配置。
    """
    try:
        home = str(Path.home())
    except Exception as e:  # pragma: no cover - 极少见
        logger.warning("无法确定用户主目录: %s", e)
        return
    if not home:
        return
    if not os.environ.get("USERPROFILE"):
        os.environ["USERPROFILE"] = home
        logger.info("补充 USERPROFILE=%s（供 lark-cli 定位配置）", home)
    if not os.environ.get("HOME"):
        os.environ["HOME"] = home
        logger.info("补充 HOME=%s（供 lark-cli 定位配置）", home)


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
    # 暴露事件总线与钩子管理器（供引擎 / 定时任务复用同一份；幂等，避免重复启动时栈无限增长）
    if not _services.has(EventBus):
        _services.register(EventBus, _loader.events, owner="kernel")
    if not _services.has(HookManager):
        _services.register(HookManager, _hooks, owner="kernel")

    db = Database()
    _services.register(Database, db, owner="kernel")

    # Insights 插件指标扩展点：注册内置贡献者，否则
    # GET /api/insights/plugins 恒返回空数组（扩展点定义了却没接线）。
    try:
        from harness.modules.insights.builtin_metrics import register_builtin_contributors

        register_builtin_contributors()
    except Exception as e:  # noqa: BLE001 —— 指标是旁路能力，失败不影响启动
        logger.warning("注册内置 Insights 指标贡献者失败: %s", e)

    # E12：认证与多用户（默认关闭；启用后按用户隔离会话与记忆）。
    from harness.modules.auth_manager.service import (
        AuthService,
        AuthServiceImpl,
    )

    auth_service = AuthServiceImpl(db)
    auth_service.bootstrap()
    _services.register(AuthService, auth_service, owner="auth_manager")

    from harness.modules.session_manager.service import (
        SessionService,
        SessionServiceImpl,
    )

    session_service = SessionServiceImpl(db, services=_services)
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
    # 先补齐用户主目录环境变量（lark-cli 定位配置用），再注入出网代理
    # （lark-cli 等 Go 子进程依赖环境变量），最后初始化组件并加载插件，
    # 保证任何后续子进程都能拿到正确的网络出口与配置文件。
    _ensure_cli_home_env()
    _apply_local_proxy_env()

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
        db = _services.get(Database)
        db.close()
    except Exception:
        pass
    logger.info("andy-harness 已关闭")


app = FastAPI(
    title="andy-harness",
    description="插件化 Agent Harness 智能体底座",
    version="1.0.0",
    lifespan=lifespan,
)


# E12：认证中间件（须在 CORS 之前加入，使 CORS 位于最外层，
# 预检 / 401 仍带正确的 CORS 头）。认证未启用时直接放行。
def _resolve_auth_service() -> Any:
    from harness.modules.auth_manager.service import AuthService

    if not _services.has(AuthService):
        return None
    return _services.get(AuthService)


app.add_middleware(AuthMiddleware, get_auth_service=_resolve_auth_service)

# E11：Tauri 桌面壳以自定义协议源（tauri://localhost / http://tauri.localhost）
# 直接访问后端，属于跨源；开发期 Vite 走代理不需要 CORS，直连也一并放行。
# 可用 HARNESS_ALLOWED_ORIGINS（逗号分隔）追加来源。
_DEFAULT_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]
_extra_origins = [o.strip() for o in os.environ.get("HARNESS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[*_DEFAULT_ORIGINS, *_extra_origins],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册错误处理
register_error_handlers(app)

# 注册 REST 路由
setup_session_routes(_services)
setup_provider_routes(_services)
setup_model_routes(_services)
setup_plugin_routes(_services)
setup_settings_routes()
setup_skill_routes(_services)
setup_permission_routes(_services)
setup_mcp_routes(_services)
setup_artifact_routes(_services)
setup_memory_routes(_services)
setup_trace_routes(_services)
setup_schedule_routes(_services)
setup_attachment_routes(_services)
setup_runs_routes(_services, _hooks, _tool_registry)
setup_channel_routes(_services)
setup_auth_routes(_services)

app.include_router(sessions_router)
app.include_router(providers_router)
app.include_router(models_router)
app.include_router(plugins_router)
app.include_router(settings_router)
app.include_router(skills_router)
app.include_router(permissions_router)
app.include_router(permission_audit_router)
app.include_router(insights_router)
app.include_router(evals_lab_router)
app.include_router(templates_hub_router)
app.include_router(workflows_router)
app.include_router(devkit_router)
app.include_router(mcp_router)
app.include_router(artifacts_router)
app.include_router(memories_router)
app.include_router(traces_router)
app.include_router(schedules_router)
app.include_router(attachments_router)
app.include_router(runs_router)
app.include_router(channels_router)
app.include_router(auth_router)
app.include_router(integrations_router)

# 注册 WebSocket 路由
setup_ws_routes(_services, _hooks, _tool_registry)

app.include_router(ws_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """健康检查端点。"""
    return {"status": "ok"}
