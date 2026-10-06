"""飞书入站渠道连接器（官方服务端 SDK lark-oapi，WebSocket 长连）。

基于飞书官方服务端 SDK（lark-oapi）的「事件订阅 / 长连接」能力，让本项目的 Agent
在飞书群聊 / 单聊里被 @ 对话调用。长连接（ws）模式**无需公网回调地址**：由 SDK 与
开放平台建立 WebSocket 全双工通道，建连时鉴权，后续事件明文推送（无需解密/验签）。

官方参考：
- 处理事件（长连接）：https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events
- 服务端 SDK 开发准备：https://open.feishu.cn/document/server-side-sdk/python--sdk/preparations-before-development
- 调用服务端 API：https://open.feishu.cn/document/server-side-sdk/python--sdk/invoke-server-api

实现要点（均为实测确认，改动前请先读注释）：
- 事件分发器用 ``EventDispatcherHandler.builder(encrypt_key, verification_token)`` 构造，
  长连模式两参数填空串（事件走已鉴权的 WS，无需验签/解密）；再
  ``.register_p2_im_message_receive_v1(handler).build()``。
  注意：``register_*`` 只存在于 **builder** 上，不在 ``EventDispatcherHandler`` 上。
- 回调是**同步**函数 ``f(event) -> None``，由 SDK 在 ws 协程里同步调用，必须尽快返回
  （3 秒重推窗口）。因此这里只做「解析 → 过滤 → 把 Agent 任务投给主循环 → 立即返回」，
  真正的推理与回复在后台协程完成。
- **坑（致命）**：``lark_oapi.ws.client`` 在模块导入时就创建了**模块级 loop 单例**。
  若导入发生在主事件循环运行中，``asyncio.get_event_loop()`` 会返回**主循环**——此时在
  子线程调 ``client.start()`` 会对已运行的主循环执行 ``run_until_complete``，直接抛
  "This event loop is already running"。故必须在**构造 Client 之前**把该模块全局
  ``loop`` 换成专用 loop，并让该专用 loop 在 ws 线程里运行。
- SDK 无公开 ``stop()``。停止时把 ``_auto_reconnect`` 置 False 并在专用 loop 上
  ``run_coroutine_threadsafe(client._disconnect(), ws_loop)``，避免断连后无限重连。
- SDK 为可选依赖：未安装时优雅降级（返回明确提示），不阻断插件加载。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from .integration_base import AuthResult

logger = logging.getLogger("harness.plugin.integration_hub.feishu_channel")

# 飞书官方服务端 SDK 的 Python 包名（import lark_oapi as lark）
LARK_SDK_MODULE = "lark_oapi"
# SDK 里持模块级 loop 单例的模块（start() 直接引用该全局）
LARK_WS_CLIENT_MODULE = "lark_oapi.ws.client"
# 开放 API 根地址（取机器人 open_id 用于群聊 @ 过滤）
FEISHU_OPEN_API_BASE = "https://open.feishu.cn/open-apis"

# SDK 事件回调签名：同步、单参数（P2ImMessageReceiveV1）、无返回
SdkEventCallback = Callable[[Any], None]
# 消息处理器签名：``async def handler(text: str, ctx: dict) -> AsyncIterator[str]``
MessageHandler = Callable[[str, dict[str, Any]], AsyncIterator[str]]


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    """同时支持 dict 与对象属性访问的深层取值（用于兼容 SDK 对象与测试用 dict）。"""
    cur: Any = obj
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k, None)
        else:
            cur = getattr(cur, k, None)
    return cur if cur is not None else default


def _strip_mentions(text: str, mentions: list[Any]) -> str:
    """把文本里的 @ 占位符（如 ``@_user_1``）剔除，保留可读的提问内容。"""
    if not text or not mentions:
        return text
    for m in mentions:
        key = _get(m, "key")
        if key:
            text = text.replace(str(key), "")
        # 兜底：按 @ 名字去掉（部分版本只带 name）
        name = _get(m, "name")
        if name:
            text = text.replace(f"@{name}", "")
    return text.strip()


def normalize_message(event: Any) -> tuple[str, dict[str, Any]]:
    """把飞书原始事件归一化为 (文本, 上下文)。

    兼容两种形态：
    - SDK 对象：``event.event.message``/``event.event.sender``（P2ImMessageReceiveV1）
    - 测试 / 历史 dict：``event["message"]`` 或 ``event["event"]["message"]``

    无法识别时返回空文本，避免崩溃。
    """
    # 先定位数据容器：SDK 对象为 event.event，平铺 dict 就是自身
    data = _get(event, "event")
    if data is None:
        data = event

    message = _get(data, "message")
    if message is None:
        message = _get(event, "message")

    content = _get(message, "content")
    text = ""
    if isinstance(content, str):
        # content 通常是 JSON 字符串，如 {"text":"..."}
        try:
            parsed = json.loads(content)
            text = (parsed or {}).get("text", "")
        except Exception:  # noqa: BLE001
            text = content
    elif isinstance(content, dict):
        text = content.get("text", "")

    sender = _get(data, "sender") or _get(event, "sender")
    ctx: dict[str, Any] = {
        "chat_id": _get(message, "chat_id"),
        "message_id": _get(message, "message_id"),
        "message_type": _get(message, "message_type"),
        "chat_type": _get(message, "chat_type"),
        "sender": sender,
        "sender_type": _get(sender, "sender_type"),
        "mentions": _get(message, "mentions") or [],
    }
    # 群聊里 @ 机器人时，正文会带 @ 占位符，剔除后更干净
    text = _strip_mentions(text or "", ctx["mentions"])
    return (text or "").strip(), ctx


class FeishuChannelConnector:
    """飞书入站渠道连接器（封装官方 lark-oapi SDK，import 守护）。"""

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None
        self._thread: threading.Thread | None = None
        self._running: bool = False
        self._detail: str = "未启动"
        # 主事件循环（Agent 推理在此执行，由 ws 线程跨线程投递）
        self._loop: asyncio.AbstractEventLoop | None = None
        # SDK 专用事件循环（绝不能与主循环是同一个）
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._bot_open_id: str | None = None

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_bot_open_id(self, open_id: str | None) -> None:
        self._bot_open_id = open_id

    def status(self) -> dict[str, Any]:
        return {"running": self._running, "detail": self._detail}

    @staticmethod
    def _import_sdk() -> Any:
        """导入飞书官方 SDK；缺失时抛出 ``ImportError``（可被测试替换）。"""
        return importlib.import_module(LARK_SDK_MODULE)

    def _bot_mentioned(self, ctx: dict[str, Any]) -> bool:
        """群聊下判断事件是否 @ 了本机器人。

        ``mentions[].id`` 是 ``UserId`` 对象（含 open_id），不是字符串——
        必须取到 ``id.open_id`` 这一层才能和 bot open_id 比对。
        """
        mentions = ctx.get("mentions") or []
        if not mentions:
            return False
        ids: set[str] = set()
        for m in mentions:
            # SDK 对象：m.id.open_id；dict：m["id"]["open_id"] 或 m["id"] 直接是字符串
            open_id = _get(m, "id", "open_id")
            if open_id:
                ids.add(str(open_id))
            else:
                raw = _get(m, "id")
                if isinstance(raw, str):
                    ids.add(raw)
        if not ids:
            return False
        if self._bot_open_id:
            return self._bot_open_id in ids
        # 未取到 bot open_id 时退化为「只要有 @ 就响应」（宁漏勿误的兜底）
        return True

    def _make_event_handler(self) -> SdkEventCallback:
        """构造 SDK 的 im.message.receive_v1 事件回调。

        同步函数、在 ws 协程里被调用，必须尽快返回（3 秒窗口）：
        仅解析事件并把 Agent 任务交还主事件循环后立即返回。
        """

        def do_message(data: Any) -> None:
            if not self._running:
                return
            try:
                text, ctx = normalize_message(data)
            except Exception as e:  # noqa: BLE001
                logger.warning("飞书渠道解析事件失败: %s", e)
                return
            if not text:
                return
            # 忽略来自应用/机器人自身的消息，避免回复触发下一次事件形成自激循环
            if ctx.get("sender_type") == "app":
                return
            # 群聊仅当 @ 本机器人时响应；单聊（p2p）始终响应
            if ctx.get("chat_type") == "group" and not self._bot_mentioned(ctx):
                logger.debug("飞书群聊消息未 @ 本机器人，忽略")
                return
            if self._handler is None or self._loop is None:
                logger.warning("飞书渠道未就绪（handler/事件循环缺失），丢弃消息")
                return
            try:
                asyncio.run_coroutine_threadsafe(self._dispatch(text, ctx), self._loop)
            except Exception as e:  # noqa: BLE001
                logger.error("飞书渠道调度消息失败: %s", e)

        return do_message

    async def _dispatch(self, text: str, ctx: dict[str, Any]) -> None:
        """在主事件循环里驱动 Agent 处理。

        回复由 handler 内部完成（main.py 的 handler 跑完 Agent 后会调
        ``feishu.reply_message`` 回写原会话），这里只负责把生成器消费完。
        """
        handler = self._handler
        if handler is None:
            return
        chunks: list[str] = []
        try:
            async for chunk in handler(text, ctx):
                chunks.append(chunk)
        except Exception as e:  # noqa: BLE001
            logger.error("飞书渠道 Agent 处理异常: %s", e)
            return
        logger.info(
            "飞书渠道处理完成: chat=%s message=%s 长度=%d",
            ctx.get("chat_id"),
            ctx.get("message_id"),
            sum(len(c) for c in chunks),
        )

    async def _fetch_bot_open_id(self, app_id: str, app_secret: str) -> str | None:
        """取机器人（应用）的 open_id，用于群聊 @ 过滤。失败返回 None（不阻断）。"""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                token_resp = await client.post(
                    f"{FEISHU_OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
                token_json = token_resp.json()
                if token_json.get("code") != 0:
                    return None
                token = token_json.get("tenant_access_token")
                info_resp = await client.get(
                    f"{FEISHU_OPEN_API_BASE}/bot/v3/info",
                    headers={"Authorization": f"Bearer {token}"},
                )
                info_json = info_resp.json()
                if info_json.get("code") != 0:
                    return None
                bot = info_json.get("bot") or {}
                return bot.get("open_id") or bot.get("app_id")
        except Exception as e:  # noqa: BLE001
            logger.warning("飞书渠道获取机器人 open_id 失败（不影响启动）: %s", e)
            return None

    def _prepare_ws_loop(self) -> asyncio.AbstractEventLoop:
        """为 SDK 准备一个**专用**事件循环，并替换它的模块级 loop 单例。

        必须在构造 ``ws.Client`` 之前调用：client 内部的 ExpiringCache 在构造时就会
        ``loop.create_task``，若那时模块 loop 仍是主循环，缓存任务会挂到主循环上，
        之后从 ws 循环访问就可能出现 loop 错乱。
        """
        ws_loop = asyncio.new_event_loop()
        try:
            ws_mod = importlib.import_module(LARK_WS_CLIENT_MODULE)
            ws_mod.loop = ws_loop  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            logger.warning("替换 lark ws 模块 loop 失败（可能仍能运行）: %s", e)
        self._ws_loop = ws_loop
        return ws_loop

    async def start(self, credentials: dict[str, Any]) -> AuthResult:
        app_id = credentials.get("app_id") or credentials.get("appId")
        app_secret = credentials.get("app_secret") or credentials.get("appSecret")
        if not app_id or not app_secret:
            return AuthResult(ok=False, error="Channel 模式需要 app_id 与 app_secret")
        if self._handler is None:
            return AuthResult(ok=False, error="未配置消息处理器（Agent 未就绪），无法启动飞书渠道")

        try:
            sdk = self._import_sdk()
        except ImportError:
            self._detail = "未安装 lark-oapi SDK"
            return AuthResult(
                ok=False,
                error=(
                    "未安装飞书官方 SDK（lark-oapi）。请先执行 「pip install lark-oapi」后重启后端，再启动飞书渠道。"
                ),
            )

        # 取机器人 open_id，用于群聊里只响应 @ 本机器人的消息
        self._bot_open_id = await self._fetch_bot_open_id(app_id, app_secret)

        # 记录当前运行事件循环（主循环），供 ws 线程安全地投递 Agent 任务
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # 关键：先给 SDK 换上专用 loop，再构造 Client（顺序不能反）
        self._prepare_ws_loop()

        try:
            event_handler = (
                sdk.EventDispatcherHandler.builder("", "")  # 长连模式两参数填空串
                .register_p2_im_message_receive_v1(self._make_event_handler())
                .build()
            )
            client = sdk.ws.Client(
                app_id,
                app_secret,
                event_handler=event_handler,
                log_level=sdk.LogLevel.INFO,
            )
        except Exception as e:  # noqa: BLE001
            self._detail = f"构造长连接客户端失败: {e}"
            logger.error("飞书渠道构造客户端失败: %s", e)
            return AuthResult(ok=False, error=f"飞书渠道构造客户端失败: {e}")

        self._client = client
        self._running = True
        self._thread = threading.Thread(target=self._run_ws, args=(client,), daemon=True)
        self._thread.start()
        self._detail = "WebSocket 长连已启动（等待飞书推送事件）"
        return AuthResult(ok=True, mode="channel", detail=self._detail)

    def _run_ws(self, client: Any) -> None:
        """在独立线程里阻塞运行长连接（client.start() 会一直阻塞到断开）。"""
        try:
            client.start()  # 内部用已替换的专用 loop
        except Exception as e:  # noqa: BLE001
            logger.error("飞书长连接异常: %s", e)
            self._running = False
            self._detail = f"连接中断: {e}"

    async def stop(self) -> AuthResult:
        client = self._client
        was_running = self._running
        # 先置 False，避免残留线程继续投递消息
        self._running = False
        if client is not None:
            # SDK 无公开 stop：关掉自动重连，再在专用 loop 上断开连接
            try:
                setattr(client, "_auto_reconnect", False)
            except Exception as e:  # noqa: BLE001
                logger.warning("飞书渠道关闭自动重连失败: %s", e)
            disconnect = getattr(client, "_disconnect", None)
            if callable(disconnect) and self._ws_loop is not None:
                try:
                    fut = asyncio.run_coroutine_threadsafe(disconnect(), self._ws_loop)
                    fut.result(timeout=5)
                except Exception as e:  # noqa: BLE001
                    logger.warning("飞书渠道断开长连失败: %s", e)
        # 不 join：SDK 的 _select() 会永久 sleep，join 必然超时；
        # 线程是 daemon，进程退出即回收，且 _running=False 后不会再投递消息。
        self._client = None
        self._thread = None
        self._detail = "已停止"
        detail = "飞书渠道已停止" if was_running else "飞书渠道未在运行"
        return AuthResult(ok=True, mode="channel", detail=detail)
