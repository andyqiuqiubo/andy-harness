"""gateway 凭据解析 —— 只读本机已有登录态，明文不落日志。

支持的 source：
- ``store:qoder-cn``    → 网关自存凭据（管理台「登录」授权，免装 Magpie）；无则回落
- ``local:qoder-cn``    → Magpie ``plugin-auth.json`` 中的 Qoder CN device token（兜底）
- ``store:workbuddy``   → 网关自存凭据；无则回落
- ``local:workbuddy``   → Magpie ``plugin-auth.json`` 中的 WorkBuddy 登录态（兜底）
- ``local:cline``      → Cline CLI 登录态（过期自动刷新并原子写回）
- ``public``           → OpenCode Zen 的共享 public 凭据
- ``env:VAR``          → 环境变量中的 Bearer token
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from harness.modules.gateway.config import ChannelConfig, CredentialRef
from harness.modules.gateway.contracts import Credential, GatewayError
from harness.modules.gateway.credential_store import GatewayCredentialStore

HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))

CLINE_VERSION = "3.0.68"
CLINE_CORE = "0.0.90"
WB_VERSION = "5.5.6"
ZEN_VERSION = "1.19.0"
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 配置；任何读取/解析失败都转为 GatewayError（不向数据面泄漏 500）。"""
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as e:
        raise GatewayError("CRED_INVALID", f"登录态文件不存在: {path}", 502) from e
    except PermissionError as e:
        raise GatewayError("CRED_INVALID", f"登录态文件无权限读取: {path}", 502) from e
    except json.JSONDecodeError as e:
        raise GatewayError("CRED_INVALID", f"登录态文件不是合法 JSON: {path}", 502) from e


def _node(data: dict[str, Any], path: Path, *keys: str) -> dict[str, Any]:
    """按键安全下钻；结构不符时给出明确错误而非 KeyError。"""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise GatewayError("CRED_INVALID", f"登录态文件结构异常（缺少 {key}）: {path}", 502)
        current = current[key]
    if not isinstance(current, dict):
        raise GatewayError("CRED_INVALID", f"登录态文件结构异常（{keys[-1]} 不是对象）: {path}", 502)
    return cast(dict[str, Any], current)


def _parse_expiry(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


class ClineTokenManager:
    """Cline CLI 登录态：到期前 5 分钟刷新；refresh token 一次性轮换必须写回。"""

    path = HOME / ".cline" / "data" / "settings" / "providers.json"

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def present(self) -> bool:
        """登录态是否就绪（只查文件与结构，不刷新 token、不发请求）。"""
        try:
            auth = _node(_load_json(self.path), self.path, "providers", "cline", "settings", "auth")
        except GatewayError:
            return False
        return bool(auth.get("accessToken") or auth.get("refreshToken"))

    def token(self) -> str:
        if self._cache and _parse_expiry(self._cache.get("expiresAt")) - time.time() > 300:
            auth = self._cache
        else:
            auth = _node(_load_json(self.path), self.path, "providers", "cline", "settings", "auth")
            if _parse_expiry(auth.get("expiresAt")) - time.time() < 300:
                auth = self._refresh(auth)
            else:
                self._cache = auth
        access = str(auth.get("accessToken") or "")
        if not access:
            raise GatewayError("CRED_INVALID", "Cline 登录态缺失，请重新登录", 502)
        return access if access.startswith("workos:") else f"workos:{access}"

    def _refresh(self, auth: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = _node(_load_json(self.path), self.path, "providers", "cline", "settings", "auth")
            if _parse_expiry(current.get("expiresAt")) - time.time() > 300:
                self._cache = current
                return current
            refresh = str(current.get("refreshToken") or "")
            if not refresh:
                raise GatewayError("CRED_INVALID", "Cline refresh token 缺失，请重新登录", 502)
            try:
                r = httpx.post(
                    "https://api.cline.bot/api/v1/auth/refresh",
                    json={"refreshToken": refresh, "grantType": "refresh_token"},
                    headers={"Content-Type": "application/json", "User-Agent": f"Cline/{CLINE_VERSION}"},
                    timeout=30,
                )
            except httpx.HTTPError as e:
                raise GatewayError("CRED_REFRESH_FAILED", f"Cline token 刷新网络错误: {e}", 502) from e
            if r.status_code != 200:
                raise GatewayError("CRED_INVALID", f"Cline token 刷新失败: HTTP {r.status_code}", 502)
            body = r.json()
            body = body.get("data") if isinstance(body.get("data"), dict) else body
            if not body.get("accessToken"):
                raise GatewayError("CRED_INVALID", "Cline token 刷新响应缺少 accessToken", 502)

            data = _load_json(self.path)
            node = _node(data, self.path, "providers", "cline", "settings", "auth")
            node["accessToken"] = body["accessToken"]
            if body.get("refreshToken"):
                node["refreshToken"] = body["refreshToken"]
            if body.get("expiresAt"):
                node["expiresAt"] = body["expiresAt"]
            data["providers"]["cline"]["updatedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
            # 先放内存：写盘失败也不会拿已花掉的 refresh token 再试
            self._cache = node
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(3):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise GatewayError(
                            "CRED_WRITE_FAILED",
                            "Cline 登录态写回失败（文件被占用），请关闭 Cline 后重试",
                            502,
                        )
                    # 注意：不要在此处 sleep——本方法可能运行在事件循环线程中
            return node


class WorkBuddyTokenManager:
    """WorkBuddy 国内版登录态：优先网关自存凭据（自建登录），Magpie 文件兜底。"""

    path = HOME / ".config" / "magpie" / "plugin-auth.json"

    def __init__(self, store: GatewayCredentialStore | None = None) -> None:
        self._store = store

    def _from_store(self) -> Credential | None:
        if self._store is None:
            return None
        data = self._store.load("workbuddy")
        if not data:
            return None
        access = str(data.get("access") or "")
        if not access:
            return None
        return Credential(
            id="workbuddy",
            secret=access,
            meta={
                "uid": str(data.get("uid", "")),
                "domain": str(data.get("domain", "www.workbuddy.cn")),
            },
        )

    def present(self) -> bool:
        """登录态是否就绪（只查自存库与文件，不刷新、不发请求）。"""
        if self._from_store() is not None:
            return True
        try:
            node = _load_json(self.path).get("workbuddy") or {}
        except GatewayError:
            return False
        return isinstance(node, dict) and bool(node.get("access"))

    def credential(self) -> Credential:
        stored = self._from_store()
        if stored is not None:
            return stored
        node = _load_json(self.path).get("workbuddy") or {}
        if not isinstance(node, dict):
            raise GatewayError("CRED_INVALID", "Magpie 登录态结构异常（workbuddy 不是对象）", 502)
        access = str(node.get("access") or "")
        if not access:
            raise GatewayError("CRED_INVALID", "未找到 WorkBuddy 登录态，请先在网关管理台登录", 502)
        return Credential(
            id="workbuddy",
            secret=access,
            meta={"uid": str(node.get("uid", "")), "domain": str(node.get("domain", "www.workbuddy.cn"))},
        )


class QoderDeviceTokenManager:
    """Qoder CN 登录态：优先网关自存凭据（自建登录），Magpie 文件兜底。

    Qoder CN 的 CLI 不申请 job token，**device token 本身就是签名用的 token**
    （magpie 插件的 `deviceChat: true` 分支），所以这里直接取 access/deviceToken。
    COSY 信封还需要 uid 与 machineId（缺 machineId 无法签名，视为登录态失效）。
    """

    path = HOME / ".config" / "magpie" / "plugin-auth.json"

    def __init__(self, store: GatewayCredentialStore | None = None) -> None:
        self._store = store

    def _from_store(self) -> Credential | None:
        if self._store is None:
            return None
        data = self._store.load("qoder-cn")
        if not data:
            return None
        access = str(data.get("access") or data.get("deviceToken") or "")
        machine_id = str(data.get("machine_id") or "")
        if not access or not machine_id:
            return None
        return Credential(
            id="qoder-cn",
            secret=access,
            meta={
                "uid": str(data.get("uid") or ""),
                "name": str(data.get("name") or ""),
                "email": str(data.get("email") or ""),
                "machine_id": machine_id,
            },
        )

    def present(self) -> bool:
        """登录态是否就绪（自存库或 Magpie 文件任一处有完整凭据即可）。"""
        if self._from_store() is not None:
            return True
        try:
            node = _load_json(self.path).get("qoder-cn") or {}
        except GatewayError:
            return False
        if not isinstance(node, dict):
            return False
        return bool(node.get("access") or node.get("deviceToken")) and bool(node.get("machineId"))

    def credential(self) -> Credential:
        stored = self._from_store()
        if stored is not None:
            return stored
        node = _load_json(self.path).get("qoder-cn") or {}
        if not isinstance(node, dict):
            raise GatewayError("CRED_INVALID", "登录态结构异常（qoder-cn 不是对象）", 502)
        access = str(node.get("access") or node.get("deviceToken") or "")
        if not access:
            raise GatewayError(
                "CRED_INVALID", "未找到 Qoder CN 登录态，请在网关管理台点「登录」完成授权", 502
            )
        machine_id = str(node.get("machineId") or "")
        if not machine_id:
            raise GatewayError("CRED_INVALID", "Qoder CN 登录态缺少 machineId，请重新登录", 502)
        return Credential(
            id="qoder-cn",
            secret=access,
            meta={
                "uid": str(node.get("uid") or node.get("accountId") or ""),
                "name": str(node.get("name") or ""),
                "email": str(node.get("email") or ""),
                "machine_id": machine_id,
            },
        )


def cline_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://cline.bot",
        "X-Title": "Cline",
        "User-Agent": f"Cline/{CLINE_VERSION}",
        "X-CLIENT-TYPE": "cline-cli",
        "X-CLIENT-VERSION": CLINE_VERSION,
        "X-PLATFORM": "cli",
        "X-PLATFORM-VERSION": CLINE_VERSION,
        "X-CORE-VERSION": CLINE_CORE,
        "X-IS-MULTIROOT": "false",
    }


def workbuddy_headers(credential: Credential, session_key: str) -> dict[str, str]:
    conv = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agw-wb:{session_key}"))
    return {
        "Authorization": f"Bearer {credential.secret}",
        "Content-Type": "application/json",
        "X-User-Id": str(credential.meta.get("uid", "")),
        "X-Domain": str(credential.meta.get("domain", "www.workbuddy.cn")),
        "X-Product": "SaaS",
        "X-IDE-Type": "WorkBuddy",
        "User-Agent": f"WorkBuddy/{WB_VERSION}",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": WB_VERSION,
        "X-Agent-Intent": "craft",
        "X-Requested-With": "XMLHttpRequest",
        "X-Conversation-ID": conv,
        "X-Conversation-Request-ID": str(uuid.uuid4()),
        "X-Conversation-Message-ID": str(uuid.uuid4()),
        "X-Request-ID": str(uuid.uuid4()),
    }


def _opencode_id(prefix: str, seed: str) -> str:
    now_ms = int(time.time() * 1000)
    counter = (int(hashlib.md5(seed.encode()).hexdigest()[:4], 16) % 64) + 1
    value = (~((now_ms << 12) + counter)) & 0xFFFFFFFFFFFF
    tail = "".join(BASE62[b % 62] for b in hashlib.md5(seed.encode()).digest()[:14])
    return f"{prefix}_{value:012x}{tail}"


def zen_headers(session_key: str) -> dict[str, str]:
    session = _opencode_id("ses", f"agw-zen-ses:{session_key}")
    return {
        "content-type": "application/json",
        "accept": "*/*",
        "user-agent": f"opencode/{ZEN_VERSION} ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
        "x-opencode-project": "global",
        "x-opencode-session": session,
        "x-opencode-request": _opencode_id("msg", f"agw-zen-msg:{session_key}:{time.time_ns()}"),
        "x-opencode-client": "cli",
        "x-opencode-session-id": session,
        "authorization": "Bearer public",
    }


class CredentialResolver:
    """按 Channel 配置解析出可用凭据列表（按权重 SWRR 排序，首个可用者被选中）。"""

    def __init__(
        self,
        cline: ClineTokenManager | None = None,
        workbuddy: WorkBuddyTokenManager | None = None,
        qoder: QoderDeviceTokenManager | None = None,
        store: GatewayCredentialStore | None = None,
    ) -> None:
        self.cline = cline or ClineTokenManager()
        self.workbuddy = workbuddy or WorkBuddyTokenManager(store=store)
        self.qoder = qoder or QoderDeviceTokenManager(store=store)
        self._swrr: dict[str, int] = {}
        self._lock = threading.Lock()

    def _order(self, channel_id: str, refs: list[CredentialRef]) -> list[CredentialRef]:
        if len(refs) <= 1:
            return list(refs)
        total = sum(max(1, r.weight) for r in refs)
        with self._lock:
            for r in refs:
                key = f"{channel_id}:{r.id}"
                self._swrr[key] = self._swrr.get(key, 0) + max(1, r.weight)
            best = max(refs, key=lambda r: self._swrr.get(f"{channel_id}:{r.id}", 0))
            key = f"{channel_id}:{best.id}"
            self._swrr[key] = self._swrr.get(key, 0) - total
        return [best] + [r for r in refs if r is not best]

    def resolve(self, channel: ChannelConfig, last_error: GatewayError | None = None) -> list[Credential]:
        refs = self._order(channel.id, channel.credentials or self._default_refs(channel))
        out: list[Credential] = []
        for ref in refs:
            try:
                out.append(self._resolve_one(channel, ref))
            except GatewayError as e:
                last_error = e
                continue
        if not out:
            raise last_error or GatewayError("GW_NO_CAPACITY", f"channel {channel.id} 无可用凭据", 503)
        return out

    def _default_refs(self, channel: ChannelConfig) -> list[CredentialRef]:
        if channel.auth == "cline-cli":
            return [CredentialRef(id="default", source="local:cline")]
        if channel.auth == "workbuddy-oauth":
            return [CredentialRef(id="default", source="store:workbuddy")]
        if channel.auth == "qoder-device":
            return [CredentialRef(id="default", source="store:qoder-cn")]
        return [CredentialRef(id="default", source="public")]

    def present(self, channel: ChannelConfig) -> bool:
        """凭据是否就绪（只查存在性，不刷新 token、不发请求）。

        用途：provider 注册门控。未登录 / 未配置 Key 的通道不注册成会话
        Provider，避免用户「选得到、发出去、只收到报错」的割裂体验；
        配好 Key / 登录后重启即自动出现。
        """
        refs = channel.credentials or self._default_refs(channel)
        for ref in refs:
            try:
                if self._present_one(ref):
                    return True
            except Exception:  # noqa: BLE001 —— 探测异常一律按「未就绪」
                continue
        return False

    def _present_one(self, ref: CredentialRef) -> bool:
        source = ref.source
        if source == "public":
            return True
        if source.startswith("env:"):
            return bool(os.environ.get(source[4:]))
        if source in ("local:cline",):
            return self.cline.present()
        if source in ("store:workbuddy", "local:workbuddy"):
            return self.workbuddy.present()
        if source in ("store:qoder-cn", "local:qoder-cn"):
            return self.qoder.present()
        return False

    def _resolve_one(self, channel: ChannelConfig, ref: CredentialRef) -> Credential:
        source = ref.source
        if source.startswith("env:"):
            name = source[4:]
            secret = os.environ.get(name, "")
            if not secret:
                raise GatewayError("CRED_INVALID", f"环境变量 {name} 为空", 502)
            return Credential(id=ref.id, secret=secret)
        if source == "public":
            return Credential(id=ref.id, secret="public")
        if source == "local:cline":
            return Credential(id=ref.id, secret=self.cline.token())
        if source in ("store:workbuddy", "local:workbuddy"):
            cred = self.workbuddy.credential()
            cred.id = ref.id
            return cred
        if source in ("store:qoder-cn", "local:qoder-cn"):
            cred = self.qoder.credential()
            cred.id = ref.id
            return cred
        raise GatewayError("CRED_INVALID", f"未知凭据来源: {source}", 500)
