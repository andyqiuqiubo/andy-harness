"""网关自建登录（设备授权流）——让用户不必安装 Magpie。

协议移植自本机 Magpie 插件 `@magpie-community/opencode-qoder-auth`（deviceSignIn），
流程与官方 CLI 一致：

1. 本地生成 PKCE 参数：``verifier``（64 随机字节 base64url）、
   ``challenge = base64url(sha256(verifier))``、``nonce``、``machine_id``（均为 UUID）；
2. 用户在浏览器打开授权页
   ``https://qoder.cn/device/selectAccounts?challenge&challenge_method=S256&nonce&machine_id&client_id``
   完成厂商账号登录并授权本设备；
3. 网关每 2 秒轮询 ``openapi.qoder.com.cn/api/v1/deviceToken/poll`` 换取 device token；
4. ``/api/v1/me/jobToken`` 对 Qoder CN 返回 4xx —— 该站 CLI 本身不申请 job token，
   **device token 即对话 token**（COSY 签名用的就是它）；
5. ``/api/v1/userinfo`` 取回 uid / name / email，与 token 一起加密落库。

成功后凭据进入 `gw_credentials`（见 credential_store.py），`local:qoder-cn`
（读 Magpie 文件）退化为可选兜底。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from harness.modules.gateway.contracts import GatewayError
from harness.modules.gateway.credential_store import GatewayCredentialStore

logger = logging.getLogger("harness.gateway.login")

#: Qoder CN（与官方 CLI `@qodercn-ai/qoderclicn` 一致的生产 client id）
QODER_CN_CLIENT_ID = "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"
QODER_CN_WEB = "https://qoder.cn"
QODER_CN_OPENAPI = "https://openapi.qoder.com.cn"

#: 授权等待上限（与插件 SIGN_IN_TIMEOUT 一致）
LOGIN_TIMEOUT_SECONDS = 15 * 60
#: 轮询间隔
POLL_INTERVAL_SECONDS = 2.0
#: 已结束会话的保留时长（供前端最后读一次状态；超期清理防泄漏）
FINISHED_SESSION_TTL_SECONDS = 10 * 60


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _expiry_at(data: dict[str, Any]) -> str:
    """把上游的 expires_at / expires_in 归一成 ISO 串。

    上游没给过期信息时返回空串 = "长期有效"（由请求失败时的 classify 兜底），
    绝不臆造一个短过期时间——否则长期有效的 device token 会被误标过期、
    逼用户天天重登。
    """
    raw = str(data.get("expires_at") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    seconds = data.get("expires_in")
    if isinstance(seconds, int | float) and seconds > 0:
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
    return ""


@dataclass(slots=True)
class LoginSession:
    """一次进行中的设备授权登录。"""

    session_id: str
    channel_id: str
    login_url: str
    params: dict[str, str]
    deadline: float
    state: str = "pending"  # pending | ok | error | timeout
    message: str = ""
    payload: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: float = 0.0  # 结束时刻（monotonic），供过期清理

    def public(self) -> dict[str, Any]:
        """给 API/前端的视图（不含任何 token）。"""
        return {
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "state": self.state,
            "message": self.message,
            "login_url": self.login_url,
            "created_at": self.created_at,
        }


class DeviceLoginFlow(ABC):
    """一个通道的设备授权登录流程（无状态，可共享）。"""

    channel_id: str = ""
    display_name: str = ""

    @abstractmethod
    def begin(self) -> dict[str, Any]:
        """生成本次登录的授权 URL 与私有参数（不发网络请求）。"""

    @abstractmethod
    async def poll(
        self, session: LoginSession, client: httpx.AsyncClient
    ) -> dict[str, Any] | None:
        """轮询一次；返回凭据载荷 = 登录成功，None = 继续等待。"""


class QoderCNDeviceLogin(DeviceLoginFlow):
    """Qoder CN 设备授权登录（协议移植自 Magpie opencode-qoder-auth）。"""

    channel_id = "qoder-cn"
    display_name = "Qoder CN"

    def begin(self) -> dict[str, Any]:
        verifier = _b64url(os.urandom(64))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        nonce = str(uuid.uuid4())
        machine_id = str(uuid.uuid4())
        query = urlencode(
            {
                "challenge": challenge,
                "challenge_method": "S256",
                "nonce": nonce,
                "machine_id": machine_id,
                "client_id": QODER_CN_CLIENT_ID,
            }
        )
        return {
            "login_url": f"{QODER_CN_WEB}/device/selectAccounts?{query}",
            "params": {"verifier": verifier, "nonce": nonce, "machine_id": machine_id},
        }

    async def poll(
        self, session: LoginSession, client: httpx.AsyncClient
    ) -> dict[str, Any] | None:
        params = session.params
        response = await client.get(
            f"{QODER_CN_OPENAPI}/api/v1/deviceToken/poll",
            params={
                "nonce": params["nonce"],
                "verifier": params["verifier"],
                "challenge_method": "S256",
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            return None  # 用户还没授权：继续等
        try:
            data = response.json()
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        token = str(data.get("token") or "").strip()
        if not token:
            return None
        return await self._exchange(session, data, token, client)

    async def _exchange(
        self,
        session: LoginSession,
        device: dict[str, Any],
        token: str,
        client: httpx.AsyncClient,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        access = token
        refresh = str(device.get("refresh_token") or "")
        expires = _expiry_at(device)

        job = await client.post(
            f"{QODER_CN_OPENAPI}/api/v1/me/jobToken",
            headers=headers,
            json={"clientId": QODER_CN_CLIENT_ID},
        )
        if job.status_code == 200:
            try:
                body = job.json()
            except json.JSONDecodeError as exc:
                raise GatewayError("CRED_INVALID", "Qoder jobToken 响应不是 JSON", 502) from exc
            job_token = str(body.get("token") or "").strip() if isinstance(body, dict) else ""
            if not job_token:
                raise GatewayError("CRED_INVALID", "Qoder jobToken 响应缺少 token", 502)
            access = job_token
            refresh = str(body.get("refresh_token") or "") or refresh
            expires = _expiry_at(body if isinstance(body, dict) else {})
        elif not 400 <= job.status_code < 500:
            # 5xx 等：换凭证重试即可，不当成登录失败
            raise GatewayError(
                "CRED_INVALID", f"Qoder jobToken 失败: HTTP {job.status_code}", 502
            )
        # 4xx：Qoder CN 的 CLI 同样不申请 job token，device token 即对话 token

        uid = str(device.get("user_id") or "")
        name = ""
        email = ""
        try:
            info_response = await client.get(
                f"{QODER_CN_OPENAPI}/api/v1/userinfo", headers=headers
            )
            if 200 <= info_response.status_code < 300:
                info = info_response.json()
                if isinstance(info, dict):
                    name = str(info.get("name") or "")
                    email = str(info.get("email") or "")
                    if not uid:
                        uid = str(
                            info.get("id") or info.get("user_id") or info.get("uid") or ""
                        )
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            logger.info("Qoder userinfo 获取失败（不阻塞登录）")

        return {
            "access": access,
            "refresh": refresh,
            "expires": expires,
            "uid": uid,
            "name": name,
            "email": email,
            "machine_id": session.params["machine_id"],
            "kind": "device",
        }


class DeviceLoginManager:
    """管理各通道的设备授权登录会话（后台轮询 + 成功后落库）。"""

    def __init__(
        self,
        store: GatewayCredentialStore,
        flows: list[DeviceLoginFlow] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_credential_saved: Any = None,
    ) -> None:
        self.store = store
        self.flows: dict[str, DeviceLoginFlow] = {f.channel_id: f for f in (flows or [])}
        self._transport = transport
        self._sessions: dict[str, LoginSession] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._client: httpx.AsyncClient | None = None
        #: 凭据落库后的回调（provider 插件用它即时刷新注册，免得重启后端）
        self.on_credential_saved = on_credential_saved

    # ---- 会话 ---------------------------------------------------------- #
    def start(self, channel_id: str) -> LoginSession:
        """发起一次登录；返回带授权 URL 的会话（URL 不含任何 token）。

        同通道已有进行中的会话时**自动取代**（取消旧轮询任务）——用户点了
        「取消」或换浏览器重开时不必等 15 分钟超时。
        """
        flow = self.flows.get(channel_id)
        if flow is None:
            raise ValueError(f"通道 {channel_id} 不支持自建登录")
        self._prune_finished()
        for old in list(self._sessions.values()):
            if old.channel_id == channel_id and old.state == "pending":
                old.state = "timeout"
                old.message = "已被新的登录请求取代"
                old.finished_at = time.monotonic()
                task = self._tasks.pop(old.session_id, None)
                if task is not None:
                    task.cancel()
        begun = flow.begin()
        session = LoginSession(
            session_id=uuid.uuid4().hex,
            channel_id=channel_id,
            login_url=str(begun["login_url"]),
            params={str(k): str(v) for k, v in dict(begun["params"]).items()},
            deadline=time.monotonic() + LOGIN_TIMEOUT_SECONDS,
        )
        self._sessions[session.session_id] = session
        self._tasks[session.session_id] = asyncio.create_task(
            self._poll_loop(session), name=f"gw-login-{channel_id}"
        )
        logger.info("通道 %s 发起自建登录 session=%s", channel_id, session.session_id)
        return session

    def status(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session.public()

    def channels(self) -> list[dict[str, Any]]:
        """各通道的登录能力与当前是否已存有凭据。"""
        out: list[dict[str, Any]] = []
        for channel_id, flow in self.flows.items():
            stored = self.store.load(channel_id)
            out.append(
                {
                    "channel_id": channel_id,
                    "display_name": flow.display_name,
                    "supports_login": True,
                    "has_stored_credential": stored is not None,
                    "account": _account_hint(stored),
                    "expires": str((stored or {}).get("expires") or ""),
                }
            )
        return out

    def logout(self, channel_id: str) -> bool:
        """删除自存凭据（Magpie 兜底仍然有效）；通知外部刷新 provider 注册。"""
        self.store.delete(channel_id)
        self._notify_saved(channel_id)
        return True

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- 内部 ---------------------------------------------------------- #
    def _prune_finished(self) -> None:
        """清理已结束且超时未被读取的会话，防内存泄漏（前端掉线也能回收）。"""
        now = time.monotonic()
        expired = [
            sid
            for sid, s in self._sessions.items()
            if s.state != "pending" and now - s.finished_at > FINISHED_SESSION_TTL_SECONDS
        ]
        for sid in expired:
            self._sessions.pop(sid, None)

    def _settle(self, session: LoginSession, state: str, message: str) -> None:
        """终结一个会话并记录结束时刻（供 _prune_finished 回收）。"""
        session.state = state
        session.message = message
        session.finished_at = time.monotonic()

    def _notify_saved(self, channel_id: str) -> None:
        """凭据落库后通知外部（失败只记日志，不影响登录结果）。"""
        callback = self.on_credential_saved
        if callback is None:
            return
        try:
            callback(channel_id)
        except Exception:  # noqa: BLE001 —— 回调失败不应让登录变成失败
            logger.exception("通道 %s 凭据保存回调执行失败", channel_id)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=20.0, follow_redirects=False, transport=self._transport
            )
        return self._client

    async def _poll_loop(self, session: LoginSession) -> None:
        flow = self.flows[session.channel_id]
        client = self._http()
        try:
            while session.state == "pending":
                if time.monotonic() > session.deadline:
                    self._settle(session, "timeout", "授权超时（15 分钟未完成），请重新发起登录")
                    return
                try:
                    payload = await flow.poll(session, client)
                except GatewayError as exc:
                    self._settle(session, "error", str(exc.message))
                    logger.warning("通道 %s 登录失败: %s", session.channel_id, exc.message)
                    return
                except httpx.HTTPError as exc:
                    logger.info("通道 %s 登录轮询网络错误（继续等）: %s", session.channel_id, exc)
                    payload = None
                if payload is not None:
                    self.store.save(session.channel_id, payload)
                    session.payload = payload
                    self._settle(session, "ok", "登录成功，凭据已加密保存")
                    logger.info("通道 %s 自建登录成功", session.channel_id)
                    self._notify_saved(session.channel_id)
                    return
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 轮询异常不能拖垮网关
            self._settle(session, "error", f"登录轮询异常: {exc}")
            logger.exception("通道 %s 登录轮询异常", session.channel_id)


def _account_hint(payload: dict[str, Any] | None) -> str:
    """给前端展示的账号提示（只露邮箱/昵称，不露 token）。"""
    if not payload:
        return ""
    return str(payload.get("email") or payload.get("name") or payload.get("uid") or "")
