"""飞书（Lark）第三方系统集成。

基于飞书官方两篇文档重新设计接入能力：

1. **CLI（出方向，主路径）**：飞书官方命令行 ``lark-cli``——文档明确它是给 AI Agent
   操作飞书的那双手，OpenClaw 等官方插件底层都基于它。覆盖完整能力地图
   （消息/云文档/多维表格/日历/邮件/任务/知识库/OKR/审批…），并支持：
   - 用户身份 OAuth（``lark-cli auth login``，可访问个人数据）
   - 应用身份（免授权直接用，凭证走 ``LARKSUITE_CLI_APP_ID/APP_SECRET`` 环境变量）
   - 多 Profile 隔离（``lark-cli profile add/use``，并发安全）
   - ``lark-cli api GET/POST <任意开放API路径>`` 直达任意开放接口
   因此 CLI 模式的 ``raw`` 动作可直接透传 ``lark-cli`` 子命令，解锁全部能力；
   无需为每个业务域手搓端点。

2. **Channel（入方向）**：基于飞书官方服务端 SDK（lark-oapi）的「事件订阅 / 长连接」
   （WebSocket）能力，让 Agent 在飞书群聊/单聊里被 @ 对话调用，免公网回调，回复走应用
   消息 API（reply）。SDK 为可选依赖，未安装时优雅降级。

3. **API（兜底）**：仅当本机无 ``lark-cli`` 时使用，手搓 ``tenant_access_token`` 并调用
   少量核心开放接口（发消息/机器人信息/用户信息）。

曾有过「把 lark-cli 注册为 MCP server」的 mcp 模式，**已移除**：官方 CLI 至今未提供
``mcp`` 子命令（1.0.97 实测 ``lark-cli mcp --help`` → unknown command），该模式点了必然失败；
CLI 模式的 ``raw`` 动作已能覆盖同样能力。

说明：app_id/app_secret 以本地明文存于状态文件，与 permission.json 同级风险，
仅适用于本机单用户桌面环境。所有对飞书的写操作经由本 harness 的权限层
（integration_call 为 write 工具，高风险动作可要求人工确认），等价于文档所述治理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from harness.infra import subproc

from .feishu_channel import FeishuChannelConnector
from .integration_base import ActionSpec, AuthResult, IntegrationProvider, IntegrationResult

logger = logging.getLogger("harness.plugin.integration_hub.feishu")

# 飞书开放平台 API 根地址（api 兜底模式使用）
FEISHU_OPEN_API_BASE = "https://open.feishu.cn/open-apis"
# 官方 CLI 二进制名（优先 lark-cli，兼容旧别名）
FEISHU_CLI_CANDIDATES = ("lark-cli", "lark", "feishu", "larksuite")
# 本机已知安装位置（兜底，最高优先级之一）：用户按约定把 CLI 装在 F:\tools\larkcli。
# 作用：即便状态文件丢失、也没设 HARNESS_FEISHU_CLI_PATH 环境变量，也能优先命中正确的二进制，
# 而不是被 PATH 上别的 lark-cli（如 connector 包里的旧 shim、trae/doubao 自带的副本）劫持。
FEISHU_CLI_KNOWN_LOCATIONS = (
    r"F:\tools\larkcli\node_modules\@larksuite\cli\bin",
    r"F:\tools\larkcli\node_modules\@larksuite\cli\bin\lark-cli.exe",
)
# 一键安装命令（官方）
FEISHU_CLI_INSTALL = "npx @larksuite/cli@latest install"
# lark-cli 读取应用身份的环境变量（官方 CLI 文档明确）
LARK_CLI_APP_ID_ENV = "LARKSUITE_CLI_APP_ID"
LARK_CLI_APP_SECRET_ENV = "LARKSUITE_CLI_APP_SECRET"


def _resolve_lark_cli_home() -> str:
    """解析飞书 CLI 应使用的用户主目录（定位 ``.lark-cli/config.json``）。

    lark-cli 是 Go 二进制，按 ``USERPROFILE`` / ``HOME`` 找配置。后端若由某些启动器
    拉起，这些变量可能**缺失**，但更常见是**被设成了不含 ``.lark-cli`` 的错误路径**
    （例如沙箱/IDE 注入的临时 HOME）——此时仅「缺失才补」的守卫会被跳过，CLI 子进程
    继承错误路径而报 ``not_configured``。

    本函数优先用 Windows Shell API 拿真实用户配置目录（不受启动环境 env 污染影响），
    再逐级兜底，并以「目录下确实存在 ``.lark-cli/config.json``」作为命中判据，确保
    无论后端如何被拉起，CLI 总能定位到同一份配置。
    """
    candidates: list[str] = []
    # 1) Windows Shell API（最权威，绕过被污染的 env）
    try:
        import ctypes
        from ctypes import wintypes

        fid = "{5E6C858F-0E22-4760-9AFE-EA3317B67173}"  # FOLDERID_Profile
        sh = ctypes.windll.shell32.SHGetKnownFolderPath
        sh.argtypes = [
            ctypes.c_wchar_p,
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        sh.restype = ctypes.c_int
        buf = ctypes.c_wchar_p()
        if sh(fid, 0, None, ctypes.byref(buf)) == 0 and buf.value:
            candidates.append(buf.value)
    except Exception:  # noqa: BLE001 - 非 Windows 或 API 不可用，走兜底
        pass
    # 2) 环境变量里「确实包含 .lark-cli」的那一个
    for env_key in ("USERPROFILE", "HOME"):
        v = os.environ.get(env_key)
        if v:
            candidates.append(v)
    # 3) Path.home()
    try:
        candidates.append(str(Path.home()))
    except Exception:  # noqa: BLE001
        pass
    # 4) 常见兜底路径
    candidates.append(r"C:\Users\Administrator")
    # 命中真正包含 .lark-cli/config.json 的目录
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, ".lark-cli", "config.json")):
            return c
    # 未命中则回退到第一个非空候选（多数是真实 profile）
    for c in candidates:
        if c:
            return c
    return r"C:\Users\Administrator"


class FeishuIntegration(IntegrationProvider):
    """飞书集成实现（CLI 主路径 + Channel 入站 + API 兜底）。"""

    provider_id = "feishu"
    provider_name = "飞书 (Lark)"
    # 注意：曾经有过 mcp 模式（把 lark-cli 注册为 MCP server），
    # 但官方 CLI 至今**没有 mcp 子命令**（1.0.97 实测 `lark-cli mcp --help` → unknown command），
    # 该模式点了必然失败，故摘除。CLI 模式的 raw 动作已能覆盖同样能力。
    supported_modes = ["cli", "channel", "api"]
    cli_install_command = FEISHU_CLI_INSTALL
    channel_capabilities = [
        {"name": "群聊", "description": "在飞书群里 @ 机器人，Agent 实时回复（走应用消息 API）"},
        {"name": "单聊", "description": "用户与机器人私聊，Agent 直接应答"},
    ]

    def __init__(self, state_dir: str | None = None) -> None:
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        self._app_id: str | None = None
        self._app_secret: str | None = None
        self._auth_mode: str | None = None
        self._cli_binary: str | None = None
        self._cli_profile: str | None = None
        # CLI 授权就绪状态快照（60s TTL，避免设置页每次刷新都拉起子进程）
        self._cli_auth_cache: dict[str, Any] | None = None
        # 自定义群机器人 Webhook（仅出站发送，不接收消息）
        self._webhook_url: str | None = None
        self._webhook_secret: str | None = None
        self._message_handler: Any = None
        self._channel = FeishuChannelConnector()
        self._state_dir = state_dir or os.environ.get(
            "INTEGRATION_HUB_STATE_DIR",
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data",
                "integration_hub",
            ),
        )
        self._state_file = os.path.join(self._state_dir, "feishu_state.json")
        self._load_state()

    # ── 依赖注入（由插件 activate 时设置） ─────────────
    def set_message_handler(self, handler: Any) -> None:
        """注入入站消息处理器：channel 收到用户消息后调用它获取 Agent 回复。"""
        self._message_handler = handler
        self._channel.set_message_handler(handler)

    # ── 状态持久化 ──────────────────────────────────
    def _load_state(self) -> None:
        """从本地状态文件恢复 token / 认证模式 / 凭证 / CLI 信息（用于重启后自动刷新）。"""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, encoding="utf-8") as f:
                    data = json.load(f)
                self._token = data.get("token")
                self._token_expire_at = float(data.get("expire_at", 0.0))
                self._auth_mode = data.get("auth_mode")
                self._cli_binary = data.get("cli_binary")
                self._cli_profile = data.get("cli_profile")
                # API 模式恢复 app_secret，使 token 过期后可自动静默刷新；
                # CLI 模式恢复 app_id/app_secret，用于以应用身份注入环境变量
                self._app_id = data.get("app_id")
                self._app_secret = data.get("app_secret")
                self._webhook_url = data.get("webhook_url")
                self._webhook_secret = data.get("webhook_secret")
        except Exception as e:  # noqa: BLE001
            logger.warning("读取飞书状态文件失败: %s", e)

    def _save_state(self) -> None:
        """持久化当前认证状态（含 app_id/app_secret，供重启后静默刷新/应用身份注入）。"""
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "token": self._token,
                        "expire_at": self._token_expire_at,
                        "auth_mode": self._auth_mode,
                        "cli_binary": self._cli_binary,
                        "cli_profile": self._cli_profile,
                        "app_id": self._app_id,
                        "app_secret": self._app_secret,
                        "webhook_url": self._webhook_url,
                        "webhook_secret": self._webhook_secret,
                    },
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("写入飞书状态文件失败: %s", e)

    def disconnect(self) -> None:
        """断开并清除本地认证状态（删除状态文件、撤销 MCP 注册、停止渠道）。"""
        self._token = None
        self._token_expire_at = 0.0
        self._app_id = None
        self._app_secret = None
        self._auth_mode = None
        self._cli_binary = None
        self._cli_profile = None
        self._webhook_url = None
        self._webhook_secret = None
        self._cli_auth_cache = None
        try:
            if os.path.exists(self._state_file):
                os.remove(self._state_file)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除飞书状态文件失败: %s", e)
        # 渠道停止（若运行）
        try:
            if self._channel.status().get("running"):
                asyncio.create_task(self._channel.stop())
        except Exception as e:  # noqa: BLE001
            logger.warning("停止飞书渠道失败: %s", e)

    # ── 动作清单 ────────────────────────────────────
    def list_actions(self) -> list[ActionSpec]:
        if self._auth_mode == "cli":
            return [
                ActionSpec(
                    name="install",
                    description="（可选）一键安装飞书 CLI：npx @larksuite/cli@latest install",
                    parameters_schema={"type": "object", "properties": {}},
                ),
                ActionSpec(
                    name="auth_status",
                    description="查看当前 lark-cli 登录/授权状态（auth status）",
                    parameters_schema={"type": "object", "properties": {}},
                ),
                ActionSpec(
                    name="whoami",
                    description=(
                        "查看当前 CLI 身份：profile / appId / 身份类型(bot|user) / token 状态。"
                        "排查「到底以谁的身份在操作飞书」时优先调用。"
                    ),
                    parameters_schema={"type": "object", "properties": {}},
                ),
                ActionSpec(
                    name="doctor",
                    description=(
                        "CLI 健康检查：配置、认证、连通性逐项给出 pass/warn/fail。"
                        "连接成功但调用报 token/权限错误时先跑它。"
                    ),
                    parameters_schema={"type": "object", "properties": {}},
                ),
                ActionSpec(
                    name="skills",
                    description=(
                        "读取 CLI 内置的能力说明书（与 CLI 版本严格同步，比网上的文档可靠）。"
                        "不带 name → 列出全部技能域（输出较长，仅用于挑名字）；"
                        "带 name（lark-doc / lark-calendar / lark-sheets / lark-mail / lark-task …）→ "
                        "读取该业务域的完整用法，拿到正确命令与参数后再用 raw 执行。"
                        "推荐流程：skills（列） → skills（读） → raw（执行）。"
                    ),
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "技能名，如 lark-doc；留空则列出全部技能",
                            },
                            "path": {
                                "type": "string",
                                "description": "可选，技能下的子文件路径（read 时用）",
                            },
                            "profile": {"type": "string", "description": "可选，指定 profile"},
                        },
                    },
                ),
                ActionSpec(
                    name="schema",
                    description=(
                        "查看某个开放 API 方法的参数、类型与所需权限 scope，"
                        "method 形如 mail.user_mailbox.messages.list 或 im.messages.patch"
                        "（资源名与方法见各域 --help 末尾的 resources 列表）。"
                        "调用 raw / api 前先查它，避免参数猜错。"
                    ),
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "method": {
                                "type": "string",
                                "description": "service.resource.method，如 im.message.create",
                            },
                            "profile": {"type": "string", "description": "可选，指定 profile"},
                        },
                        "required": ["method"],
                    },
                ),
                ActionSpec(
                    name="send_message",
                    description="通过 lark-cli 发送消息：im send-message --chat-id ... --text ...",
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "chat_id": {"type": "string", "description": "会话 ID（群/单聊）"},
                            "text": {"type": "string", "description": "消息文本"},
                            "profile": {
                                "type": "string",
                                "description": "可选，指定 lark-cli profile（多应用隔离）",
                            },
                        },
                        "required": ["chat_id", "text"],
                    },
                ),
                ActionSpec(
                    name="create_doc",
                    description=(
                        "（CLI 模式）在飞书云文档创建一篇文档并写入正文，默认以用户身份落在"
                        "「我的文档库」（个人空间）。参数 title 为标题；可选 content 为正文；"
                        "可选 identity 指定身份（user|bot，默认 user）；"
                        "可选 parent_position（默认 my_library）或 parent_token 指定父目录/知识库节点；"
                        "可选 doc_format（markdown|xml，默认 markdown）。返回 document_id 与访问链接。"
                        "注意：首次使用前需先完成用户授权（lark-cli auth login），否则 user 身份不可用。"
                    ),
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "文档标题"},
                            "content": {
                                "type": "string",
                                "description": "可选，创建后写入的正文（markdown）",
                            },
                            "identity": {
                                "type": "string",
                                "description": "可选，操作身份 user|bot，默认 user",
                                "enum": ["user", "bot"],
                                "default": "user",
                            },
                            "parent_position": {
                                "type": "string",
                                "description": "可选，父位置（如 my_library），默认 my_library",
                                "default": "my_library",
                            },
                            "parent_token": {
                                "type": "string",
                                "description": "可选，父文件夹/知识库节点 token（与 parent_position 互斥）",
                            },
                            "doc_format": {
                                "type": "string",
                                "description": "可选，内容格式 markdown|xml，默认 markdown",
                                "enum": ["markdown", "xml"],
                                "default": "markdown",
                            },
                            "profile": {
                                "type": "string",
                                "description": "可选，指定 lark-cli profile",
                            },
                        },
                        "required": ["title"],
                    },
                ),
                ActionSpec(
                    name="get_bot_info",
                    description=(
                        "（CLI 模式）获取当前应用/机器人基本信息与授权状态："
                        "appId、brand，以及 bot / user 两种身份的准备情况（auth status）。"
                    ),
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "profile": {
                                "type": "string",
                                "description": "可选，指定 lark-cli profile",
                            },
                        },
                    },
                ),
                ActionSpec(
                    name="raw",
                    description=(
                        "直接透传参数给 lark-cli，解锁飞书全部业务对象与任意开放 API。"
                        "优先用 +-shortcut（高层任务），其次 typed 命令，最后 api 逃逸口："
                        "建文档 ['docs','+create','--title','X','--content','Y','--doc-format','markdown']；"
                        "读日程 ['calendar','+agenda','--as','bot']；"
                        "读任务 ['task','+get-my-tasks']；"
                        "读邮件 ['mail','+messages','--message-id','...']；"
                        "写单元格 ['sheets','+cells-set',...]；"
                        "任意接口 ['api','GET','/open-apis/xxx']。"
                        "注意：写操作常需 --as 指定身份，高风险写需 --yes（须先向用户确认）。"
                        "不确定语法时先用 skills / schema 动作查说明书，再执行。"
                    ),
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "lark-cli 参数列表，如 ['im','send','--help']",
                            },
                            "profile": {
                                "type": "string",
                                "description": "可选，指定 lark-cli profile",
                            },
                        },
                        "required": ["args"],
                    },
                ),
            ]
        if self._auth_mode == "api":
            return [
                ActionSpec(
                    name="create_doc",
                    description=(
                        "在飞书云文档创建一篇文档。参数 title 为文档标题；"
                        "可选 content 为要写入的正文（创建后自动写入）；"
                        "可选 space_id 把文档加入指定知识库（我的文档库）。"
                        "返回 document_id 与访问链接。"
                    ),
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "文档标题"},
                            "content": {"type": "string", "description": "可选，创建后写入的正文"},
                            "space_id": {
                                "type": "string",
                                "description": "可选，知识库/我的文档库 space_id，写入后自动归入",
                            },
                        },
                        "required": ["title"],
                    },
                ),
                ActionSpec(
                    name="write_doc",
                    description="向已有云文档追加正文。参数 document_id 与 content（文本，支持多行）。",
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "document_id": {"type": "string", "description": "文档 ID"},
                            "content": {"type": "string", "description": "要写入的正文"},
                        },
                        "required": ["document_id", "content"],
                    },
                ),
                ActionSpec(
                    name="send_webhook",
                    description=(
                        "通过已配置的自定义群机器人 Webhook 发送消息到群（仅出站，不接收）。"
                        "参数 text 为消息文本。连接时需在 credentials 中提供 webhook_url。"
                    ),
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "要发送到群的消息文本"},
                        },
                        "required": ["text"],
                    },
                ),
                ActionSpec(
                    name="send_message",
                    description="发送文本消息到指定会话/用户（应用消息 API）",
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "receive_id": {"type": "string", "description": "接收方 ID（群/用户）"},
                            "receive_id_type": {
                                "type": "string",
                                "description": "接收方类型",
                                "enum": ["chat_id", "user_id", "union_id", "email", "phone"],
                                "default": "chat_id",
                            },
                            "msg_type": {
                                "type": "string",
                                "description": "消息类型",
                                "enum": ["text", "post"],
                                "default": "text",
                            },
                            "content": {"type": "string", "description": "消息内容（文本或 post JSON 字符串）"},
                        },
                        "required": ["receive_id", "content"],
                    },
                ),
                ActionSpec(
                    name="get_bot_info",
                    description="获取当前机器人（应用）基本信息（API 兜底模式）",
                    parameters_schema={"type": "object", "properties": {}},
                ),
                ActionSpec(
                    name="get_user",
                    description="按用户 ID 获取用户信息（API 兜底模式）",
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string", "description": "用户 ID"},
                            "user_id_type": {
                                "type": "string",
                                "description": "用户 ID 类型",
                                "enum": ["open_id", "union_id", "user_id"],
                                "default": "open_id",
                            },
                        },
                        "required": ["user_id"],
                    },
                ),
            ]
        return []

    # ── 认证授权 ────────────────────────────────────
    def is_authenticated(self) -> bool:
        if self._auth_mode == "cli":
            return self._cli_binary is not None
        if self._auth_mode == "api":
            return bool(self._token) and time.time() < self._token_expire_at
        return False

    def auth_instructions(self, mode: str) -> str:
        if mode == "cli":
            return (
                "飞书 CLI 模式（推荐）：\n"
                f"1) 安装：{self.cli_install_command}\n"
                "2) 授权（用户身份，可访问个人日历/消息/文档）：lark-cli auth login\n"
                "   也可仅用应用身份（免授权，但无法访问个人数据）：连接时填入 app_id/app_secret，"
                "或执行 lark-cli config init 创建应用。\n"
                "3) 多应用隔离：lark-cli profile add --name <n> --app-id <id> --app-secret-stdin；"
                "调用时加 --profile <n>。"
            )
        if mode == "channel":
            return (
                "飞书 Channel 模式（入方向）：在飞书开放平台创建应用并开启「机器人」能力，"
                "订阅 im.message.receive_v1 事件、开启长连接（无需公网回调）；填入 app_id/"
                "app_secret 后启动，Agent 即可在飞书群聊（@ 机器人）/单聊里被对话调用，"
                "回复走应用消息 API。\n"
                "（需先安装官方 SDK：pip install lark-oapi；未安装时启动会给出安装提示。）"
            )
        if mode == "api":
            return (
                "飞书 API 模式（无 CLI 兜底）：在飞书开放平台创建应用，"
                "获取 app_id 与 app_secret 后填入本页对应字段即可。"
            )
        return super().auth_instructions(mode)

    async def authenticate(self, mode: str, credentials: dict[str, Any]) -> AuthResult:
        if mode not in self.supported_modes:
            return AuthResult(ok=False, mode=mode, error=f"不支持的接入方式: {mode}，可选: {self.supported_modes}")
        if mode == "api":
            return await self._auth_api(credentials)
        if mode == "cli":
            return self._auth_cli(credentials)
        # channel 模式不走 authenticate，由 start_channel 处理
        return AuthResult(ok=False, mode=mode, error=f"未知接入方式: {mode}")

    async def _auth_api(self, credentials: dict[str, Any]) -> AuthResult:
        app_id = credentials.get("app_id") or credentials.get("appId")
        app_secret = credentials.get("app_secret") or credentials.get("appSecret")
        if not app_id or not app_secret:
            return AuthResult(ok=False, mode="api", error="API 模式需要 app_id 与 app_secret")
        # 自定义群机器人 Webhook（仅出站发送）；可选签名密钥
        self._webhook_url = credentials.get("webhook_url") or credentials.get("webhookUrl")
        self._webhook_secret = credentials.get("webhook_secret") or credentials.get("webhookSignSecret") or None
        if await self._fetch_token(app_id, app_secret):
            self._auth_mode = "api"
            self._app_id = app_id
            self._app_secret = app_secret
            self._save_state()
            detail = "tenant_access_token 已获取"
            if self._webhook_url:
                detail += "；群机器人 Webhook 已记录"
            return AuthResult(ok=True, mode="api", detail=detail)
        return AuthResult(ok=False, mode="api", error="获取 tenant_access_token 失败（检查 app_id/app_secret 或网络）")

    def _auth_cli(self, credentials: dict[str, Any] | None = None) -> AuthResult:
        credentials = credentials or {}
        # 允许在连接表单里显式指定 CLI 路径（非 PATH 安装场景，如装在 F:\tools\larkcli）
        explicit = credentials.get("cli_path") or credentials.get("cliPath")
        binary: str | None = None
        if explicit:
            binary = self._resolve_cli_path(str(explicit))
            if not binary:
                return AuthResult(
                    ok=False,
                    mode="cli",
                    error=f"指定的飞书 CLI 路径不可用: {explicit}",
                )
        else:
            binary = self._detect_cli()
        if not binary:
            return AuthResult(
                ok=False,
                mode="cli",
                detail="未检测到飞书 CLI",
                error=(
                    f"未检测到飞书 CLI，请先执行「{self.cli_install_command}」安装，或检查 PATH 后重试。"
                    "（CLI 模式可选：建云文档/发群通知请改用「开放 API」；"
                    "在群里 @机器人请改用「飞书频道（入站）」，两者都不需要 CLI。）"
                ),
            )
        # 可选：连接时填入 app_id/app_secret → 以应用身份注入环境变量（免授权直接用）
        app_id = credentials.get("app_id") or credentials.get("appId")
        app_secret = credentials.get("app_secret") or credentials.get("appSecret")
        profile = credentials.get("profile")
        self._cli_binary = binary
        self._auth_mode = "cli"
        self._app_id = app_id or self._app_id
        self._app_secret = app_secret or self._app_secret
        self._cli_profile = profile or self._cli_profile
        self._cli_auth_cache = None
        self._save_state()
        # 尽力获取授权状态作为详情返回（不影响连接成败）
        detail = f"已检测到飞书 CLI: {binary}"
        status = self._cli_auth_status_sync()
        if status is not None:
            detail += f"\n授权状态：\n{status}"
        return AuthResult(ok=True, mode="cli", detail=detail)

    @staticmethod
    def _resolve_cli_path(raw: str) -> str | None:
        """把「可执行文件路径」或「所在目录」规范化为可执行文件的绝对路径。"""
        target = (raw or "").strip().strip('"')
        if not target:
            return None
        if os.path.isdir(target):
            for cand in FEISHU_CLI_CANDIDATES:
                for suffix in ("", ".exe", ".cmd", ".bat"):
                    f = os.path.join(target, cand + suffix)
                    if os.path.isfile(f):
                        return f
            return None
        return target if os.path.isfile(target) else None

    def _detect_cli(self) -> str | None:
        """按候选名检测本机已安装的飞书 CLI 二进制绝对路径（优先 lark-cli）。

        检测顺序（先确定、后模糊）：
        1. 已持久化的 ``self._cli_binary``（上次连接时写入，文件仍存在）——
           CLI 常被装在非 PATH 目录（如本项目装在 ``F:\\tools\\larkcli``），
           持久化后重启服务无需再配环境变量。
        2. 环境变量 ``HARNESS_FEISHU_CLI_PATH``（可指向可执行文件本身，或其所在目录）。
           **实测坑**：终端里 ``which lark-cli`` 能找到，不代表后端进程找得到——
           两者 PATH 可能不同（后端由 IDE/服务方式启动时尤为常见，曾出现终端有、
           后端报“未检测到飞书 CLI”）。显式路径是唯一可靠的兜底。
        3. 本机已知安装位置 ``FEISHU_CLI_KNOWN_LOCATIONS``（F:\\tools\\larkcli）。
           状态文件丢失或没设环境变量时的最后保险，避免误选 PATH 上的其它副本。
        4. ``shutil.which`` 按候选名查找（Windows 下会按 PATHEXT 匹配 .exe/.cmd）。
        """
        if self._cli_binary:
            persisted = self._resolve_cli_path(self._cli_binary)
            if persisted:
                return persisted
        explicit = os.environ.get("HARNESS_FEISHU_CLI_PATH", "").strip()
        if explicit:
            resolved = self._resolve_cli_path(explicit)
            if resolved:
                return resolved
            logger.warning("HARNESS_FEISHU_CLI_PATH 指向的路径不存在，回退到 PATH 查找: %s", explicit)
        for loc in FEISHU_CLI_KNOWN_LOCATIONS:
            resolved = self._resolve_cli_path(loc)
            if resolved:
                return resolved
        for name in FEISHU_CLI_CANDIDATES:
            path = shutil.which(name)
            if path:
                return path
        return None

    def cli_available(self) -> bool | None:
        """真实检测本机 CLI 是否可用（前端据此在 CLI 页签直接提示未安装）。"""
        return self._detect_cli() is not None

    def cli_path(self) -> str | None:
        """供前端展示「已检测到：<绝对路径>」，便于用户确认实际生效的是哪个二进制。"""
        return self._detect_cli()

    # ── CLI 授权就绪状态（供设置页展示，减少「已连接却报错」的困惑） ──
    _CLI_AUTH_TTL = 60.0

    def _cli_auth_snapshot(self, force: bool = False) -> dict[str, Any] | None:
        """CLI 授权就绪状态快照（带 TTL 缓存）。

        返回 ``{"ready": bool|None, "summary": str}``；仅 CLI 模式下有意义，
        其余模式（api/未连接/未检测到 CLI）返回 None。``ready=None`` 表示
        无法判断（如 auth status 输出非 JSON）。
        """
        if self._auth_mode != "cli" or not self._cli_binary:
            return None
        now = time.time()
        cached = self._cli_auth_cache
        if not force and cached and now - float(cached.get("ts", 0)) < self._CLI_AUTH_TTL:
            return cached
        out = self._cli_auth_status_sync() or ""
        data = self._parse_cli_json(out)
        ready: bool | None
        summary: str
        if data:
            identities = data.get("identities") or {}
            bot = identities.get("bot") or {}
            user = identities.get("user") or {}
            bot_ok = bool(bot.get("available"))
            user_ok = bool(user.get("available"))
            ready = bot_ok or user_ok
            parts = []
            if bot_ok:
                parts.append("应用身份（bot）可用")
            if user_ok:
                name = user.get("userName") or ""
                parts.append(f"用户身份（user）可用{('：' + str(name)) if name else ''}")
            summary = "；".join(parts) if parts else "尚未完成任何身份授权（请执行 lark-cli auth login）"
        else:
            # 输出非 JSON：多半是 not_configured / CLI 报错原文
            ready = False if out else None
            summary = " ".join(out.split())[:200]
        snap = {"ts": now, "ready": ready, "summary": summary}
        self._cli_auth_cache = snap
        return snap

    def describe(self) -> dict[str, Any]:
        """在通用描述上追加 CLI 授权就绪状态（前端据此提示未授权）。"""
        desc = super().describe()
        snap = self._cli_auth_snapshot()
        if snap is not None:
            desc["cli_auth"] = {"ready": snap["ready"], "summary": snap["summary"]}
        return desc

    # ── 入站渠道（Channel SDK） ──────────────────────
    def channel_status(self) -> dict[str, Any]:
        return self._channel.status()

    async def start_channel(self, credentials: dict[str, Any]) -> AuthResult:
        if self._message_handler is None:
            return AuthResult(ok=False, error="未配置消息处理器（Agent 未就绪），无法启动飞书渠道")
        res = await self._channel.start(credentials)
        if res.ok:
            # 渠道回复（im.message.receive_v1 → reply_message）以应用身份调用开放 API，
            # 需要 tenant_access_token；故把凭证注入内存，供 _ensure_token 静默换取。
            # 注意：仅内存，不写状态文件（避免渠道凭证落盘）。
            self._app_id = credentials.get("app_id") or credentials.get("appId")
            self._app_secret = credentials.get("app_secret") or credentials.get("appSecret") or None
        return res

    async def stop_channel(self) -> AuthResult:
        return await self._channel.stop()

    # ── HTTP 封装（api 兜底模式，可被测替换） ─────────
    async def _api_call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """调用飞书开放 API，返回解析后的 JSON（api 兜底模式）。"""
        headers = {"Authorization": f"Bearer {self._token}"} if auth else {}
        async with httpx.AsyncClient(timeout=15) as client:
            if method == "GET":
                resp = await client.get(f"{FEISHU_OPEN_API_BASE}{path}", params=params, headers=headers)
            else:
                resp = await client.post(
                    f"{FEISHU_OPEN_API_BASE}{path}",
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            result: dict[str, Any] = resp.json()
            return result

    async def _fetch_token(self, app_id: str, app_secret: str) -> bool:
        """换取 tenant_access_token（api 兜底模式）。"""
        try:
            payload = await self._api_call(
                "POST",
                "/auth/v3/tenant_access_token/internal",
                json_body={"app_id": app_id, "app_secret": app_secret},
                auth=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("飞书 token 请求失败: %s", e)
            return False
        if payload.get("code") != 0:
            logger.error("飞书 token 错误: %s", payload)
            return False
        self._token = payload.get("tenant_access_token")
        expire = float(payload.get("expire", 7200))
        self._token_expire_at = time.time() + expire - 60  # 提前 60s 刷新
        return True

    async def _ensure_token(self) -> bool:
        """确保 token 有效；若已过期且仍有凭证则静默刷新（api 兜底模式）。"""
        if self._token and time.time() < self._token_expire_at:
            return True
        if self._app_id and self._app_secret:
            return await self._fetch_token(self._app_id, self._app_secret)
        return False

    # ── CLI 封装（主路径，可被测替换） ───────────────
    def _cli_auth_status_sync(self) -> str | None:
        """同步获取 lark-cli auth status 的输出（用于连接时展示，失败返回 None）。"""
        if not self._cli_binary:
            return None
        try:
            cmd = [self._cli_binary]
            # 必须带上 profile：否则会展示 CLI 当前默认 profile（可能是别的应用），误导用户
            if self._cli_profile:
                cmd += ["--profile", self._cli_profile]
            cmd += ["auth", "status"]
            # 强制校正 HOME / USERPROFILE，确保 CLI 能定位 .lark-cli/config.json
            env = dict(os.environ)
            home = _resolve_lark_cli_home()
            env["HOME"] = home
            env["USERPROFILE"] = home
            appdata = os.path.join(home, "AppData", "Roaming")
            if os.path.isdir(appdata):
                env["APPDATA"] = appdata
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            out = (proc.stdout or proc.stderr or "").strip()
            return out or None
        except Exception as e:  # noqa: BLE001
            logger.warning("获取飞书 CLI 授权状态失败: %s", e)
            return None

    async def _cli_auth_status(self) -> str | None:
        """运行 lark-cli auth status，尽力返回输出（用于详情展示）。"""
        if not self._cli_binary:
            return None
        rc, out, err = await self._run_cli(["auth", "status"])
        if rc != 0 and not out:
            return err or "(无法获取授权状态)"
        return out or err

    def _cli_env(self) -> dict[str, str]:
        """构造子进程环境变量：注入应用身份（若已配置），使 CLI 以应用身份免授权调用；

        并**强制校正** ``HOME`` / ``USERPROFILE`` / ``APPDATA``，确保 CLI 子进程总能
        定位到 ``.lark-cli/config.json``（避免被启动环境的错误 HOME 拖累而报
        ``not_configured``）。详见 :func:`_resolve_lark_cli_home`。
        """
        env = dict(os.environ)
        home = _resolve_lark_cli_home()
        env["HOME"] = home
        env["USERPROFILE"] = home
        appdata = os.path.join(home, "AppData", "Roaming")
        if os.path.isdir(appdata):
            env["APPDATA"] = appdata
        if self._app_id:
            env[LARK_CLI_APP_ID_ENV] = self._app_id
        if self._app_secret:
            env[LARK_CLI_APP_SECRET_ENV] = self._app_secret
        return env

    async def _run_npx_install(self) -> tuple[int, str, str]:
        """执行 ``npx @larksuite/cli@latest install`` 一键安装飞书 CLI。"""
        result = await subproc.run(
            ["npx", "@larksuite/cli@latest", "install"],
            env={**dict(os.environ), "npm_config_yes": "true"},
        )
        if result.returncode == -1 and not result.stderr:
            return (-1, "", "未找到 npx，请先安装 Node.js")
        return (result.returncode, result.stdout, result.stderr)

    # CLI 配置缺失类错误特征：出现在请求发出**之前**（无副作用），可安全降级重试
    _NOT_CONFIGURED_RE = re.compile(r"not[\s_-]?configured", re.IGNORECASE)

    async def _exec_cli(
        self, cmd: list[str], env: dict[str, str], stdin_data: bytes | None = None
    ) -> tuple[int, str, str]:
        """执行已拼装好的 CLI 命令并收集输出。

        走 :mod:`harness.infra.subproc`（线程内同步子进程）：Windows 上
        ``uvicorn --reload`` 的 SelectorEventLoop 不支持 asyncio 子进程
        （抛裸 NotImplementedError 且消息为空），同步执行与事件循环种类无关。
        """
        result = await subproc.run(cmd, env=env, stdin_data=stdin_data)
        return result.returncode, result.stdout, result.stderr

    async def _run_cli(
        self,
        args: list[str],
        profile: str | None = None,
        env: dict[str, str] | None = None,
        stdin_data: bytes | None = None,
    ) -> tuple[int, str, str]:
        """执行飞书 CLI 命令，返回 (返回码, stdout, stderr)。

        profile 为空时回落到已配置的 ``self._cli_profile``（连接时写入），
        保证任何调用路径都作用在正确的应用上；仍为空则不加 flag，由 CLI 自行决定。
        以 ``--profile <name>`` 全局 flag 注入（并发安全，避免改进程级配置）。

        自愈：若因注入的 profile 在 CLI 配置里不存在而报 not_configured
        （该错误发生在请求发出前，无副作用），自动去掉 profile 按默认应用重试一次，
        避免配置漂移后所有调用集体失败。
        """
        if not self._cli_binary:
            return (-1, "", "CLI 未初始化")
        cmd = [self._cli_binary]
        profile = profile or self._cli_profile
        if profile:
            cmd += ["--profile", str(profile)]
        cmd += [str(a) for a in args]
        rc, out, err = await self._exec_cli(cmd, env or self._cli_env(), stdin_data)
        if rc != 0 and profile and self._NOT_CONFIGURED_RE.search(f"{out}\n{err}"):
            logger.warning("lark-cli profile '%s' 不可用（not_configured），降级为默认应用重试", profile)
            retry_cmd = [self._cli_binary] + [str(a) for a in args]
            rc2, out2, err2 = await self._exec_cli(retry_cmd, env or self._cli_env(), stdin_data)
            if rc2 == 0 or not self._NOT_CONFIGURED_RE.search(f"{out2}\n{err2}"):
                return (
                    rc2,
                    out2,
                    (err2 + f"\n[提示] profile '{profile}' 在 lark-cli 配置中不存在，已自动按默认应用执行。").strip(),
                )
        return rc, out, err

    # ── 动作实现 ────────────────────────────────────
    async def call(self, action: str, params: dict[str, Any]) -> IntegrationResult:
        # install 与 send_webhook（自定义群机器人，无需应用 token）无需先认证即可执行
        if action == "install":
            return await self._call_cli_action(action, params)
        if action == "send_webhook":
            return await self._send_webhook(params)
        if not self.is_authenticated():
            return IntegrationResult(ok=False, error="尚未认证，请先调用 integration_connect 完成授权")
        if self._auth_mode == "cli":
            return await self._call_cli_action(action, params)
        if self._auth_mode == "api":
            return await self._call_api(action, params)
        return IntegrationResult(ok=False, error=f"未知模式: {self._auth_mode}")

    async def _call_cli_action(self, action: str, params: dict[str, Any]) -> IntegrationResult:
        profile = params.get("profile") or self._cli_profile
        if action == "install":
            rc, out, err = await self._run_npx_install()
            if rc != 0:
                return IntegrationResult(ok=False, error=f"安装失败 ({rc}): {err or out}")
            self._cli_binary = self._detect_cli() or self._cli_binary
            self._save_state()
            return IntegrationResult(ok=True, data={"stdout": out, "stderr": err})
        if action == "auth_status":
            rc, out, err = await self._run_cli(["auth", "status"])
            if rc != 0 and not out:
                return IntegrationResult(ok=False, error=f"auth status 失败: {err or out}")
            return IntegrationResult(ok=True, data={"stdout": out or err})
        if action in ("whoami", "doctor"):
            rc, out, err = await self._run_cli([action], profile=profile)
            if rc != 0 and not out:
                return IntegrationResult(ok=False, error=f"{action} 失败: {err or out}")
            return IntegrationResult(ok=True, data={"stdout": out or err, "stderr": err})
        if action == "skills":
            name = params.get("name")
            if name:
                args = ["skills", "read", str(name)]
                sub = params.get("path")
                if sub:
                    args.append(str(sub))
            else:
                args = ["skills", "list"]
            rc, out, err = await self._run_cli(args, profile=profile)
            if rc != 0 and not out:
                return IntegrationResult(ok=False, error=f"skills 失败: {err or out}")
            return IntegrationResult(ok=True, data={"stdout": out or err, "stderr": err})
        if action == "schema":
            method = params.get("method")
            if not method:
                return IntegrationResult(ok=False, error="schema 需要 method（service.resource.method）")
            rc, out, err = await self._run_cli(["schema", str(method)], profile=profile)
            if rc != 0 and not out:
                return IntegrationResult(ok=False, error=f"schema 失败: {err or out}")
            return IntegrationResult(ok=True, data={"stdout": out or err, "stderr": err})
        if action == "send_message":
            chat_id = params.get("chat_id")
            text = params.get("text")
            if not chat_id or text is None:
                return IntegrationResult(ok=False, error="send_message 需要 chat_id 与 text")
            rc, out, err = await self._run_cli(
                ["im", "send-message", "--chat-id", str(chat_id), "--text", str(text)],
                profile=profile,
            )
            if rc == 0:
                return IntegrationResult(ok=True, data={"stdout": out, "stderr": err})
            return IntegrationResult(ok=False, error=f"CLI 发送失败 (exit {rc}): {err or out}")
        if action == "raw":
            args = params.get("args", [])
            if not isinstance(args, list):
                return IntegrationResult(ok=False, error="raw 动作的 args 必须是字符串列表")
            rc, out, err = await self._run_cli([str(a) for a in args], profile=profile)
            if rc == 0:
                return IntegrationResult(ok=True, data={"stdout": out, "stderr": err})
            return IntegrationResult(ok=False, error=f"CLI 返回非零退出码 {rc}: {err or out}")
        if action == "create_doc":
            return await self._create_doc_cli(params)
        if action == "get_bot_info":
            return await self._get_bot_info_cli(params)
        return IntegrationResult(ok=False, error=f"CLI 模式下不支持动作: {action}")

    # ── CLI 模式下的高层动作实现 ──────────────────────
    @staticmethod
    def _parse_cli_json(text: str) -> dict[Any, Any] | None:
        """从 CLI 输出（可能夹带日志/警告）中截取第一个 JSON 对象。"""
        text = (text or "").strip()
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            data: dict[Any, Any] = json.loads(text[start : end + 1])
            return data
        except json.JSONDecodeError:
            return None

    async def _create_doc_cli(self, params: dict[str, Any]) -> IntegrationResult:
        """CLI 模式创建文档：默认以用户身份落到「我的文档库」。

        多行 / 超长正文改走 stdin（``--content -``）：CLI 官方建议，避免
        Windows argv 转义损坏换行与 32K 命令行长度上限。
        """
        title = params.get("title")
        if not title:
            return IntegrationResult(ok=False, error="create_doc 需要 title")
        content = params.get("content") or ""
        identity = params.get("identity") or params.get("as") or "user"
        parent_position = params.get("parent_position") or params.get("parentPosition")
        parent_token = params.get("parent_token") or params.get("parentToken")
        doc_format = params.get("doc_format") or params.get("docFormat") or "markdown"
        # 多行或超长正文：用 `-` 占位、正文从 stdin 喂入
        content_text = str(content)
        stdin_data: bytes | None = None
        if "\n" in content_text or len(content_text) > 2000:
            stdin_data = content_text.encode("utf-8")
            content_arg = "-"
        else:
            content_arg = content_text
        args: list[str] = [
            "docs",
            "+create",
            "--as",
            str(identity),
            "--title",
            str(title),
            "--content",
            content_arg,
            "--doc-format",
            str(doc_format),
        ]
        if parent_token:
            args += ["--parent-token", str(parent_token)]
        else:
            args += ["--parent-position", str(parent_position or "my_library")]
        rc, out, err = await self._run_cli(
            args,
            profile=params.get("profile") or self._cli_profile,
            stdin_data=stdin_data,
        )
        if rc != 0:
            detail = (err or out or "").strip()
            hint = ""
            if self._NOT_CONFIGURED_RE.search(detail):
                hint = "（lark-cli 未完成配置或授权：请先在本机执行 lark-cli auth login 完成用户身份授权）"
            return IntegrationResult(ok=False, error=f"CLI 创建文档失败 (exit {rc}): {detail}{hint}")
        data = self._parse_cli_json(out)
        if not data or not data.get("ok"):
            return IntegrationResult(ok=False, error=f"CLI 创建文档返回失败: {out}")
        doc = (data.get("data") or {}).get("document") or {}
        document_id = doc.get("document_id")
        if not document_id:
            return IntegrationResult(ok=False, error=f"CLI 创建文档未返回 document_id: {out}")
        return IntegrationResult(
            ok=True,
            data={
                "document_id": document_id,
                "url": doc.get("url"),
                "identity": data.get("identity"),
            },
        )

    async def _get_bot_info_cli(self, params: dict[str, Any]) -> IntegrationResult:
        """CLI 模式获取应用/机器人信息与授权状态（auth status）。"""
        rc, out, err = await self._run_cli(["auth", "status"], profile=params.get("profile") or self._cli_profile)
        if rc != 0 and not out:
            return IntegrationResult(ok=False, error=f"auth status 失败: {err or out}")
        data = self._parse_cli_json(out)
        if not data:
            return IntegrationResult(ok=False, error=f"auth status 输出非 JSON: {out or err}")
        identities = data.get("identities") or {}
        bot = identities.get("bot") or {}
        user = identities.get("user") or {}
        return IntegrationResult(
            ok=True,
            data={
                "appId": data.get("appId"),
                "brand": data.get("brand"),
                "bot": {"status": bot.get("status"), "available": bot.get("available")},
                "user": {
                    "status": user.get("status"),
                    "available": user.get("available"),
                    "userName": user.get("userName"),
                    "openId": user.get("openId"),
                },
            },
        )

    async def _call_api(self, action: str, params: dict[str, Any]) -> IntegrationResult:
        if action == "create_doc":
            return await self._create_doc(params)
        if action == "write_doc":
            return await self._write_doc(params)
        if action == "send_message":
            return await self._send_message(params)
        if action == "get_bot_info":
            return await self._get_bot_info()
        if action == "get_user":
            return await self._get_user(params)
        return IntegrationResult(ok=False, error=f"未知动作: {action}")

    # ── 云文档（docx）操作 ──────────────────────────
    async def _create_doc(self, params: dict[str, Any]) -> IntegrationResult:
        title = params.get("title")
        if not title:
            return IntegrationResult(ok=False, error="create_doc 需要 title")
        if not await self._ensure_token():
            return IntegrationResult(ok=False, error="token 已过期且无法刷新，请重新连接")
        try:
            payload = await self._api_call("POST", "/docx/v1/documents", json_body={"title": title})
        except Exception as e:  # noqa: BLE001
            return IntegrationResult(ok=False, error=f"创建文档失败: {e}")
        if payload.get("code") != 0:
            return IntegrationResult(ok=False, error=f"创建文档失败: {payload.get('msg')}")
        # 飞书返回结构：data.document.document_id
        document = (payload.get("data") or {}).get("document") or {}
        document_id = document.get("document_id")
        if not document_id:
            return IntegrationResult(ok=False, error=f"创建文档未返回 document_id: {payload}")
        doc_url = f"https://bytedance.feishu.cn/docx/{document_id}"
        result: dict[str, Any] = {"document_id": document_id, "url": doc_url}

        # 可选：创建后写入正文
        content = params.get("content")
        if content is not None and str(content) != "":
            write = await self._write_doc({"document_id": document_id, "content": content})
            result["write"] = write.to_dict()

        # 可选：归入知识库（我的文档库）
        space_id = params.get("space_id")
        if space_id:
            wiki = await self._add_to_wiki(document_id, title, space_id)
            result["wiki"] = wiki
        else:
            # 尽力自动定位「我的文档库」个人知识库
            auto = await self._try_auto_wiki(document_id, title)
            if auto:
                result["wiki"] = auto
        return IntegrationResult(ok=True, data=result)

    async def _write_doc(self, params: dict[str, Any]) -> IntegrationResult:
        document_id = params.get("document_id")
        content = params.get("content")
        if not document_id or content is None:
            return IntegrationResult(ok=False, error="write_doc 需要 document_id 与 content")
        if not await self._ensure_token():
            return IntegrationResult(ok=False, error="token 已过期且无法刷新，请重新连接")
        # 按行切分为多个段落块（block_type=2 为文本段落）
        lines = str(content).split("\n")
        children = [
            {
                "block_type": 2,
                "text": {
                    "elements": [{"text_run": {"content": (ln or "") + "\n"}}],
                    "style": {},
                },
            }
            for ln in lines
        ]
        try:
            payload = await self._api_call(
                "POST",
                f"/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                json_body={"children": children, "index": 0},
            )
        except Exception as e:  # noqa: BLE001
            return IntegrationResult(ok=False, error=f"写入文档失败: {e}")
        if payload.get("code") != 0:
            return IntegrationResult(ok=False, error=f"写入文档失败: {payload.get('msg')}")
        return IntegrationResult(ok=True, data=payload.get("data", payload))

    async def _add_to_wiki(self, document_id: str, title: str, space_id: str) -> dict[str, Any]:
        """把已创建的文档加入指定知识库（我的文档库）空间。"""
        if not await self._ensure_token():
            return {"ok": False, "error": "token 已过期"}
        # 取空间根节点 token
        try:
            space = await self._api_call("GET", f"/wiki/v2/spaces/{space_id}/nodes")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"获取知识库根节点失败: {e}"}
        if space.get("code") != 0:
            return {"ok": False, "error": f"获取知识库根节点失败: {space.get('msg')}"}
        items = (space.get("data") or {}).get("items") or []
        root_token = items[0].get("node_token") if items else None
        if not root_token:
            return {"ok": False, "error": "未找到知识库根节点"}
        try:
            add = await self._api_call(
                "POST",
                f"/wiki/v2/spaces/{space_id}/nodes",
                json_body={
                    "node_type": "doc",
                    "obj_type": "doc",
                    "parent_node_token": root_token,
                    "node_name": title,
                    "obj_token": document_id,
                },
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"加入知识库失败: {e}"}
        if add.get("code") != 0:
            return {"ok": False, "error": f"加入知识库失败: {add.get('msg')}"}
        return {"ok": True, "node": (add.get("data") or {}).get("node")}

    async def _try_auto_wiki(self, document_id: str, title: str) -> dict[str, Any] | None:
        """尽力自动定位「我的文档库」个人知识库并把文档归入（找不到则返回 None）。"""
        if not await self._ensure_token():
            return None
        try:
            spaces = await self._api_call("GET", "/wiki/v2/spaces")
        except Exception:  # noqa: BLE001
            return None
        if spaces.get("code") != 0:
            return None
        items = (spaces.get("data") or {}).get("items") or []
        target = None
        for s in items:
            name = (s.get("name") or "").lower()
            if "我的文档库" in name or "my wiki" in name or s.get("type") == "personal":
                target = s
                break
        if target is None and items:
            target = items[0]  # 兜底：取首个空间
        if target is None:
            return None
        return await self._add_to_wiki(document_id, title, target.get("space_id"))

    # ── 自定义群机器人 Webhook（仅出站） ────────────
    async def _send_webhook(self, params: dict[str, Any]) -> IntegrationResult:
        text = params.get("text")
        if not text:
            return IntegrationResult(ok=False, error="send_webhook 需要 text")
        if not self._webhook_url:
            return IntegrationResult(
                ok=False,
                error="未配置群机器人 Webhook（连接时请在 credentials 提供 webhook_url）",
            )
        body: dict[str, Any] = {"msg_type": "text", "content": {"text": str(text)}}
        # 若配置了签名密钥，按飞书自定义机器人规则附加 timestamp + sign
        if self._webhook_secret:
            try:
                import base64
                import hashlib
                import hmac
                import time as _time

                timestamp = str(int(_time.time()))
                string_to_sign = f"{timestamp}\n{self._webhook_secret}"
                hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
                sign = base64.b64encode(hmac_code).decode("utf-8")
                body["timestamp"] = timestamp
                body["sign"] = sign
            except Exception as e:  # noqa: BLE001
                logger.warning("生成 Webhook 签名失败: %s", e)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(self._webhook_url, json=body)
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            return IntegrationResult(ok=False, error=f"发送群机器人消息失败: {e}")
        if data.get("code") != 0:
            return IntegrationResult(
                ok=False, error=f"发送群机器人消息失败: {data.get('msg')} (code={data.get('code')})"
            )
        return IntegrationResult(ok=True, data={"msg": data.get("msg", "success")})

    async def reply_message(self, message_id: str, text: str) -> IntegrationResult:
        """通过应用消息 API 回复某条消息（入站渠道 @ 机器人后回复到原会话）。"""
        if not await self._ensure_token():
            return IntegrationResult(ok=False, error="token 已过期且无法刷新，请重新连接")
        try:
            payload = await self._api_call(
                "POST",
                f"/im/v1/messages/{message_id}/reply",
                json_body={
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
        except Exception as e:  # noqa: BLE001
            return IntegrationResult(ok=False, error=f"回复消息失败: {e}")
        if payload.get("code") != 0:
            return IntegrationResult(ok=False, error=f"回复消息失败: {payload.get('msg')}")
        return IntegrationResult(ok=True, data=payload.get("data", payload))

    async def _send_message(self, params: dict[str, Any]) -> IntegrationResult:
        receive_id = params.get("receive_id")
        if not receive_id:
            return IntegrationResult(ok=False, error="send_message 需要 receive_id")
        receive_id_type = params.get("receive_id_type", "chat_id")
        msg_type = params.get("msg_type", "text")
        content = params.get("content", "")
        if not await self._ensure_token():
            return IntegrationResult(ok=False, error="token 已过期且无法刷新，请重新调用 integration_connect")
        content_obj = content if msg_type == "post" else {"text": content}
        try:
            payload = await self._api_call(
                "POST",
                "/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                json_body={
                    "receive_id": receive_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content_obj, ensure_ascii=False),
                },
            )
        except Exception as e:  # noqa: BLE001
            return IntegrationResult(ok=False, error=f"发送失败: {e}")
        if payload.get("code") != 0:
            return IntegrationResult(ok=False, error=f"发送失败: {payload.get('msg')}")
        return IntegrationResult(ok=True, data=payload.get("data", payload))

    async def _get_bot_info(self) -> IntegrationResult:
        if not await self._ensure_token():
            return IntegrationResult(ok=False, error="token 已过期且无法刷新，请重新调用 integration_connect")
        try:
            payload = await self._api_call("GET", "/bot/v3/info")
        except Exception as e:  # noqa: BLE001
            return IntegrationResult(ok=False, error=f"获取机器人信息失败: {e}")
        if payload.get("code") != 0:
            return IntegrationResult(ok=False, error=f"获取机器人信息失败: {payload.get('msg')}")
        return IntegrationResult(ok=True, data=payload.get("bot", payload))

    async def _get_user(self, params: dict[str, Any]) -> IntegrationResult:
        user_id = params.get("user_id")
        if not user_id:
            return IntegrationResult(ok=False, error="get_user 需要 user_id")
        user_id_type = params.get("user_id_type", "open_id")
        if not await self._ensure_token():
            return IntegrationResult(ok=False, error="token 已过期且无法刷新，请重新调用 integration_connect")
        try:
            payload = await self._api_call(
                "GET",
                f"/contact/v3/users/{user_id}",
                params={"user_id_type": user_id_type},
            )
        except Exception as e:  # noqa: BLE001
            return IntegrationResult(ok=False, error=f"获取用户信息失败: {e}")
        if payload.get("code") != 0:
            return IntegrationResult(ok=False, error=f"获取用户信息失败: {payload.get('msg')}")
        return IntegrationResult(ok=True, data=payload.get("data", payload))
