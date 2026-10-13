"""OortCloud（OortCodex）第三方系统集成。

对接 OortCloud 平台，让用户通过统一登录页授权后，自动领取平台下发的免费
NewAPI 令牌，并据此在 harness 内创建一个 OpenAI 兼容 Provider（custom_oortcloud），
从而在会话里直接选用免费额度模型。

数据流（Web 部署）：
1. 前端 GET /api/integrations/oortcloud/login-url 拿到组装好的登录页地址
   （后端用 env 的平台/网关/服务名/ appname 拼装，绝不带 appid/secretkey）。
2. 浏览器跳转到该地址，用户登录授权后，登录页重定向到 redirect_uri
   并附带 access_token / tenant_id / state。
3. 前端回调页把 access_token 等 POST 给 /api/integrations/oortcloud/connect
   （mode='oauth'）。
4. 本类 authenticate 串联：
   令牌列表（同时校验 access_token）→ 取完整密钥（规范化为 sk-）→
   模型列表（校验密钥并取可用模型）→ 创建/更新 custom_oortcloud Provider。
   管理接口仅需用户登录的 access_token（多租户路径不补 appid/secretkey），
   appid/secretkey 为可选的应用凭证，仅当平台要求时才需配置（见 .env.local）。

密钥隔离：appid/secretkey（可选，NewAPI 应用凭证）只存在于后端 env；
状态文件只存用户级 access_token/tenant_id/state 与缓存的积分信息，不存运营方密钥。
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any

import httpx

from harness.infra.crypto import redact_secrets
from harness.modules.model_manager.openai_compatible import OpenAICompatibleProvider

from .integration_base import ActionSpec, AuthResult, IntegrationProvider, IntegrationResult
from .state_store import load_state, needs_upgrade, save_state

logger = logging.getLogger("harness.plugin.integration_hub.oortcloud")

# 落盘前必须加密的敏感字段（SEC-09：access_token 曾明文落盘 644 权限）
_SENSITIVE_FIELDS = ("access_token",)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# httpx 中「连接/传输」类异常（str() 常为空，需单独识别以给出友好提示）
_NET_ERROR_NAMES = {
    "ConnectError",
    "ReadError",
    "WriteError",
    "CloseError",
    "RemoteProtocolError",
    "ProxyError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
}


class OortCloudProvider(OpenAICompatibleProvider):
    """OortCloud NewAPI 网关的 Provider（相对通用基类有两处必要差异）。

    1. 对话流接口需带 ``X-Roo-App-Version`` 标识头（见对接文档 3.2 节，该头
       「仅主对话调用添加」）。缺该头时多租户网关可能**不予响应 / 长时间挂起**，
       表现为：授权时 ``/v1/models`` 正常，但 ``/v1/chat/completions`` 发出去
       迟迟不回，前端 30s 看门狗误报「连接中断」。
    2. 流式增量推理正文字段为 ``reasoning``（``reasoning_content`` 是兼容别名，
       见文档 10.2）。基类只读 ``reasoning_content``，此处补上 ``reasoning``，
       避免推理模型在长思考阶段只发 ``reasoning`` 时前端长时间零帧。
    """

    provider_name = "OortCloud"
    default_models: list[str] = []

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["X-Roo-App-Version"] = _env("OORTCLOUD_APP_VERSION", "1.2.3")
        return headers

    def _parse_stream_chunk(self, data: dict[str, Any]) -> dict[str, Any] | None:
        result = super()._parse_stream_chunk(data) or {}
        choices = data.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            if delta.get("reasoning") and not result.get("reasoning_content"):
                result["reasoning_content"] = delta["reasoning"]
        return result or None


class OortCloudIntegration(IntegrationProvider):
    """OortCloud 集成实现（oauth 登录领取免费令牌 → 自动建 Provider）。"""

    provider_id = "oortcloud"
    provider_name = "OortCloud 免费额度"
    supported_modes = ["oauth"]

    def __init__(self, services: Any = None, state_dir: str | None = None) -> None:
        self._services = services
        self._state: dict[str, Any] = {}
        self._state_dir = state_dir or _env(
            "INTEGRATION_HUB_STATE_DIR",
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data",
                "integration_hub",
            ),
        )
        self._state_file = os.path.join(self._state_dir, "oortcloud_state.json")
        self._load_state()
        # SEC-21：state 文件缺失时，从 providers 表回填的密文自愈恢复。
        # 注：services 可能在构造后才注入（PluginContext 时序），此处做一次
        # 尝试即可——若尚未注入则静默跳过，恢复入口也存在于下次 load 前。
        self._restore_token_from_provider()

    # ── 平台配置（从 env 读取，与 feishu 同机制） ──────
    @property
    def _platform(self) -> str:
        return _env("OORTCLOUD_PLATFORM_URL", "https://workup.oortcloudsmart.com:2443").rstrip("/")

    @property
    def _gateway(self) -> str:
        return _env("OORTCLOUD_GATEWAY", "bus")

    @property
    def _api_service(self) -> str:
        return _env("OORTCLOUD_API_SERVICE", "apaas-newapi")

    @property
    def _login_service(self) -> str:
        return _env("OORTCLOUD_LOGIN_SERVICE", "apaas-web")

    @property
    def _app_id(self) -> str:
        return _env("OORTCLOUD_APP_ID")

    @property
    def _secret_key(self) -> str:
        return _env("OORTCLOUD_SECRET_KEY")

    @property
    def _appname(self) -> str:
        return _env("OORTCLOUD_APPNAME", "andy-harness")

    def _new_api_base(self) -> str:
        """NewAPI 服务根地址（不含 /v1）。"""
        return f"{self._platform}/{self._gateway}/{self._api_service}"

    # ── 状态持久化 ──────────────────────────────────
    def _load_state(self) -> None:
        """读取状态文件；敏感字段（access_token）自动解密。

        兼容旧版明文文件：读取时按明文透传，随后 :func:`needs_upgrade`
        检测到明文即触发一次重写，把明文无缝升级为密文。
        """
        self._state = load_state(self._state_file, _SENSITIVE_FIELDS)
        if needs_upgrade(self._state_file, _SENSITIVE_FIELDS):
            logger.info("检测到旧版明文 access_token，自动升级为加密存储")
            self._save_state()

    def _save_state(self) -> None:
        """持久化状态：access_token 加密后原子落盘，权限收敛 0o600。"""
        save_state(self._state_dir, self._state_file, self._state, _SENSITIVE_FIELDS)

    def disconnect(self) -> None:
        """断开并清除本地认证状态（删 Provider + 清状态文件）。"""
        try:
            from harness.infra.database import Database
            from harness.modules.model_manager.provider_registry import ProviderRegistry

            pr = self._services.get(ProviderRegistry)
            db = self._services.get(Database)
            if pr is not None:
                pr.delete_provider("custom_oortcloud", db)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除 OortCloud Provider 失败: %s", e)
        self._state = {}
        try:
            if os.path.exists(self._state_file):
                os.remove(self._state_file)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除 OortCloud 状态文件失败: %s", e)

    # ── 登录页 URL 拼装 ─────────────────────────────
    def build_login_url(self, redirect_uri: str) -> str:
        """拼装统一登录页地址（仅此 URL 暴露给前端，绝不含 appid/secretkey）。"""
        from urllib.parse import urlencode

        state = secrets.token_urlsafe(16)
        # 把 state 存起来，回调时由 authenticate 比对，防重放 / CSRF
        self._state["state"] = state
        self._save_state()
        q = urlencode(
            {
                "appname": self._appname,
                "redirect_uri": redirect_uri or "",
                "state": state,
            }
        )
        return f"{self._platform}/{self._gateway}/{self._login_service}/loginPage/index.html?{q}"

    # ── 动作清单 ────────────────────────────────────
    def list_actions(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                name="usage",
                description="刷新并查看当前免费令牌的积分 / 额度信息",
                parameters_schema={"type": "object", "properties": {}},
            ),
        ]

    # ── 认证授权（领取免费额度并建 Provider） ────────
    def is_authenticated(self) -> bool:
        return bool(self._state.get("access_token"))

    def auth_instructions(self, mode: str) -> str:
        if mode == "oauth":
            return "OortCloud 模式：点击「登录领取免费额度」跳转到统一登录页，授权后自动创建 Provider。"
        return super().auth_instructions(mode)

    async def authenticate(self, mode: str, credentials: dict[str, Any]) -> AuthResult:
        if mode != "oauth":
            return AuthResult(ok=False, mode=mode, error=f"不支持的接入方式: {mode}，仅支持 oauth")
        access_token = credentials.get("access_token") or credentials.get("accessToken")
        tenant_id = credentials.get("tenant_id") or credentials.get("tenantId")
        state = credentials.get("state")
        if not access_token:
            return AuthResult(ok=False, mode=mode, error="缺少 access_token")

        # 可选 state 防重放比对
        saved_state = self._state.get("state")
        if saved_state and state and saved_state != state:
            return AuthResult(ok=False, mode=mode, error="state 校验失败，可能存在重放攻击")

        try:
            base = self._new_api_base()
            mgmt_headers = self._mgmt_headers(access_token)

            # ① 令牌列表（同时校验 access_token 有效性）
            token_list = await self._http_get_json(f"{base}/api/token/?p=1&size=10", mgmt_headers)
            if not isinstance(token_list, dict) or not token_list.get("success"):
                return AuthResult(ok=False, mode=mode, error="access_token 校验失败或令牌列表获取失败")
            items = (token_list.get("data") or {}).get("items") or []
            token = None
            for it in items:
                if it.get("name") == "我的令牌" and it.get("status") == 1:
                    token = it
                    break
            if token is None:
                for it in items:
                    if it.get("status") == 1:
                        token = it
                        break
            if token is None:
                return AuthResult(
                    ok=False,
                    mode=mode,
                    error="未找到可用令牌（请确认已登录且令牌已启用；若刚创建账号，请稍候几秒后重试）",
                )
            token_id = token.get("id")

            # ② 取完整密钥（规范化为 sk-）
            sk = await self._fetch_key(base, mgmt_headers, token_id)
            if not sk:
                return AuthResult(ok=False, mode=mode, error="获取完整令牌密钥失败")

            # ③ 模型列表（校验密钥 + 取可用模型）
            models = await self._fetch_models(base, sk)

            # ④ base_url（含 /v1）
            provider_base_url = f"{base}/v1"

            # ⑤ 创建 / 更新 Provider（SEC-21：授权令牌一并回填 providers 表）
            await self._upsert_provider(provider_base_url, sk, models, access_token=access_token)

            # ⑥ 持久化用户态 + 可选用量
            self._state["access_token"] = access_token
            self._state["tenant_id"] = tenant_id
            usage = await self._fetch_usage(base, sk)
            if isinstance(usage, dict):
                # 用量接口返回结构为 {"data": {...}}，需解包（与 _call_usage 一致）
                udata = usage.get("data") or {}
                self._state["remaining_credits"] = udata.get("remaining_credits")
                self._state["unlimited_quota"] = udata.get("unlimited_quota", False)
            self._save_state()

            if self._state.get("unlimited_quota"):
                detail = "已领取并创建 Provider：OortCloud 免费额度（无限额度）"
            else:
                detail = f"已领取并创建 Provider：OortCloud 免费额度（剩余积分 {self._state.get('remaining_credits')}）"
            return AuthResult(ok=True, mode=mode, detail=detail)
        except Exception as e:  # noqa: BLE001
            # SEC-32：异常文本可能携带 URL / 令牌，日志与回显前统一脱敏。
            logger.error("OortCloud authenticate 失败: %s", redact_secrets(str(e)))
            return AuthResult(ok=False, mode=mode, error=f"领取失败: {self._describe_error(e)}")

    # ── 请求封装 ────────────────────────────────────
    def _mgmt_headers(self, access_token: str) -> dict[str, str]:
        """管理接口公共头（令牌列表 / 取完整密钥）。

        最小必备头为 ``accesstoken``（用户登录令牌）+ ``requesttype=app``。
        ``appid`` / ``secretkey`` 是平台分配的 NewAPI 应用凭证：官方文档 3.1 将其
        列为必填，但 3.2 明确「多租户环境不补充这些平台网关头」——即本部署路径
        （``bus/apaas-newapi``）仅靠用户 access_token 即可调用。因此仅在显式配置
        时才附带这两个头，缺失不阻断领取流程（与你方对接人确认：无需填写即可使用）。
        """
        headers: dict[str, str] = {
            "accept": "application/json, text/plain, */*",
            "accesstoken": access_token,
            "requesttype": "app",
            "cache-control": "no-store",
            "pragma": "no-cache",
        }
        app_id = self._app_id
        secret_key = self._secret_key
        if app_id:
            headers["appid"] = app_id
        if secret_key:
            headers["secretkey"] = secret_key
        return headers

    @staticmethod
    def _bearer_headers(sk_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {sk_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _describe_error(e: Exception) -> str:
        """把异常转成「永不为空」且可定位的错误文案。

        坑：httpx 的超时 / 连接类异常（ConnectTimeout / ReadTimeout / ReadError
        / RemoteProtocolError 等）以及内置 ``TimeoutError`` 的 ``str()`` 均为空串，
        直接插值会得到「领取失败: 」这种零信息量的空白提示（用户实测：首次调用
        Oort 接口瞬时超时即复现）。这里统一补上异常类名，并对网络类异常给出可操作
        的提示，避免界面出现「领取失败:」后什么都没说。
        """
        name = type(e).__name__
        detail = str(e).strip()
        if isinstance(e, (httpx.TimeoutException, TimeoutError)) or name in _NET_ERROR_NAMES:
            return f"连接 OortCloud 平台超时或中断（{name}），请稍后重试；首次调用可能较慢"
        # SEC-32：异常文本可能携带 URL 查询串令牌 / 头凭据，回显客户端前脱敏。
        return redact_secrets(f"{detail}（{name}）" if detail else name)

    async def _request_json(
        self, method: str, url: str, headers: dict[str, str], *, json_body: Any = None, retries: int = 2
    ) -> Any:
        """发起 GET/POST 并解析 JSON，对网络 / HTTP 瞬时错误自动重试。

        用于领取流程中对 Oort 侧首次调用未就绪（令牌尚未下发、冷启动抖动、
        瞬时 5xx 等）的容错：一次瞬时失败不再整条中断（表现为「第一次失败、
        第二次成功」），最多重试 ``retries`` 次，每次间隔 1s。
        """
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    if method.upper() == "POST":
                        resp = await client.post(url, headers=headers, json=json_body)
                    else:
                        resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    return resp.json()
            except Exception as e:  # noqa: BLE001
                last_exc = e
                # 4xx 是明确的客户端错误（凭据无效 / 参数错误 / 资源不存在），
                # 重试不会改变结果，直接抛出交由上层处理。
                if isinstance(e, httpx.HTTPStatusError) and 400 <= e.response.status_code < 500:
                    raise
                if attempt < retries:
                    logger.warning(
                        "OortCloud 请求 %s 失败（第 %d 次，%s），1s 后重试",
                        url,
                        attempt + 1,
                        type(e).__name__,
                    )
                    await asyncio.sleep(1.0)
                    continue
                raise
        # 理论上不会到达（循环至少 raise 一次）；保险兜底
        raise last_exc if last_exc is not None else RuntimeError("unknown OortCloud request error")

    async def _http_get_json(self, url: str, headers: dict[str, str]) -> Any:
        return await self._request_json("GET", url, headers)

    async def _fetch_key(self, base: str, mgmt_headers: dict[str, str], token_id: Any) -> str | None:
        data = await self._request_json("POST", f"{base}/api/token/{token_id}/key", mgmt_headers)
        if not isinstance(data, dict) or not data.get("success"):
            return None
        key = (data.get("data") or {}).get("key")
        if not key:
            return None
        # 文档明确原始 key 不带 sk- 前缀，需补成 sk-<key>
        return key if key.startswith("sk-") else f"sk-{key}"

    async def _fetch_models(self, base: str, sk_key: str) -> list[str]:
        try:
            data = await self._request_json("GET", f"{base}/v1/models", self._bearer_headers(sk_key))
            models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            if models:
                return models
        except Exception as e:  # noqa: BLE001
            logger.warning("OortCloud /v1/models 失败，尝试 /api/pricing: %s", e)
        # 回退：公开定价接口（匿名可访问）
        try:
            data = await self._request_json("GET", f"{base}/api/pricing", self._bearer_headers(sk_key))
            models = [m.get("model_name") for m in (data.get("data") or []) if m.get("model_name")]
            if models:
                return models
        except Exception as e:  # noqa: BLE001
            logger.warning("OortCloud /api/pricing 失败: %s", e)
        return []

    async def _fetch_usage(self, base: str, sk_key: str) -> Any:
        # 用量非致命：仅尝试一次（retries=0），失败不影响领取结果
        try:
            return await self._request_json("GET", f"{base}/api/usage/token/", self._bearer_headers(sk_key), retries=0)
        except Exception as e:  # noqa: BLE001
            logger.warning("OortCloud 用量获取失败（非致命）: %s", e)
            return None

    async def _resolve_key(self, access_token: str) -> str | None:
        """重新走令牌列表 → 取密钥，解析出当前可用令牌的 sk- 密钥（供用量刷新）。"""
        base = self._new_api_base()
        mgmt = self._mgmt_headers(access_token)
        try:
            tl = await self._http_get_json(f"{base}/api/token/?p=1&size=10", mgmt)
        except Exception as e:  # noqa: BLE001
            logger.warning("OortCloud 令牌列表失败: %s", e)
            return None
        if not isinstance(tl, dict) or not tl.get("success"):
            return None
        items = (tl.get("data") or {}).get("items") or []
        token_id = None
        for it in items:
            if it.get("name") == "我的令牌" and it.get("status") == 1:
                token_id = it.get("id")
                break
        if token_id is None:
            for it in items:
                if it.get("status") == 1:
                    token_id = it.get("id")
                    break
        if token_id is None:
            return None
        return await self._fetch_key(base, mgmt, token_id)

    async def _upsert_provider(self, base_url: str, api_key: str, models: list[str], access_token: str = "") -> None:
        from harness.infra.crypto import APIKeyEncryptor
        from harness.infra.database import Database
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        pr = self._services.get(ProviderRegistry)
        db = self._services.get(Database)
        provider_id = "custom_oortcloud"  # 由 name="OortCloud" 派生

        encryptor = APIKeyEncryptor()
        encrypted_key = encryptor.encrypt(api_key)
        # SEC-21：授权令牌回填 providers 表。旧实现 access_token 只写本地集成
        # 状态文件（oortcloud_state.json），与 providers 表状态分裂——state 文件
        # 损坏/删除后令牌即永久丢失，用量刷新失效且必须重新走完整 oauth。
        # 回填后 providers 表成为规范存储（与 api_key_encrypted 同样的密文惯例），
        # state 文件仅作运行时缓存（_load_state 后可从表内密文自愈恢复）。
        # 存放位置为 extra_params（providers 表唯一可持久化的扩展 JSON 列）；
        # 请求体合并层会排除 *_encrypted 私有键（openai_compatible SEC-21 过滤），
        # 不会把令牌密文发给上游。
        extra_params: dict[str, Any] = dict(
            (pr.get_provider_config(provider_id) or {}).get("extra_params") or {}
        )
        if access_token:
            extra_params["oortcloud_access_token_encrypted"] = encryptor.encrypt(access_token)
        config = {
            "name": "OortCloud",
            "api_key_encrypted": encrypted_key,
            "base_url": base_url,
            "models": models or [],
            "extra_params": extra_params,
            "enabled": True,
        }
        # 统一用模块级 OortCloudProvider（带对话流必需头），并显式落地 enabled=True：
        # register_provider 会合并历史 DB override（可能为「停用」），
        # 而「重新领取免费额度」在语义上应视为启用，故随后用 update_provider_config
        # 以原始 config 覆盖（绕过 override），再持久化。
        pr.register_provider(provider_id, OortCloudProvider, config)
        pr.update_provider_config(provider_id, config)
        if db is not None:
            pr.persist_provider(db, provider_id, config)

    def _restore_token_from_provider(self) -> None:
        """SEC-21：state 文件缺失/损坏时，从 providers 表回填的密文恢复 access_token。

        使本地状态文件退化为纯缓存：删掉它重启后端即可自愈，无需重新走 oauth。
        """
        if self._state.get("access_token") or self._services is None:
            return
        try:
            from harness.infra.crypto import APIKeyEncryptor
            from harness.modules.model_manager.provider_registry import ProviderRegistry

            pr = self._services.get(ProviderRegistry)
            if pr is None or not pr.has_provider("custom_oortcloud"):
                return
            cfg = pr.get_provider_config("custom_oortcloud") or {}
            enc = (cfg.get("extra_params") or {}).get("oortcloud_access_token_encrypted")
            if not enc:
                return
            self._state["access_token"] = APIKeyEncryptor().decrypt(str(enc))
            self._save_state()
            logger.info("已从 providers 表恢复 OortCloud access_token（SEC-21）")
        except Exception as e:  # noqa: BLE001 - 恢复失败不应阻断启动
            logger.debug("无可恢复的 OortCloud access_token: %s", e)

    # ── 动作实现 ────────────────────────────────────
    async def call(self, action: str, params: dict[str, Any]) -> IntegrationResult:
        if action == "usage":
            return await self._call_usage()
        return IntegrationResult(ok=False, error=f"不支持的动作: {action}")

    async def _call_usage(self) -> IntegrationResult:
        token = self._state.get("access_token")
        if not token:
            # SEC-21：state 文件丢失/后建时自愈——先尝试从 providers 表回填的
            # 密文恢复，再报「尚未领取」（插件 activate 可能早于表加载）。
            self._restore_token_from_provider()
            token = self._state.get("access_token")
        if not token:
            return IntegrationResult(ok=False, error="尚未领取，请先登录领取免费额度")
        try:
            sk = await self._resolve_key(token)
            if not sk:
                # 无法重新取密钥时，返回已缓存的积分信息
                return IntegrationResult(
                    ok=True,
                    data={
                        "remaining_credits": self._state.get("remaining_credits"),
                        "unlimited_quota": self._state.get("unlimited_quota", False),
                        "cached": True,
                    },
                )
            base = self._new_api_base()
            usage = await self._fetch_usage(base, sk)
            if isinstance(usage, dict):
                data = usage.get("data") or {}
                self._state["remaining_credits"] = data.get("remaining_credits")
                self._state["unlimited_quota"] = data.get("unlimited_quota", False)
                self._save_state()
                return IntegrationResult(ok=True, data=data)
            return IntegrationResult(ok=False, error="获取用量失败")
        except Exception as e:  # noqa: BLE001
            return IntegrationResult(ok=False, error=self._describe_error(e))

    # ── 描述（追加积分信息） ────────────────────────
    def describe(self) -> dict[str, Any]:
        desc = super().describe()
        desc["credits"] = self._state.get("remaining_credits")
        desc["unlimited"] = self._state.get("unlimited_quota", False)
        return desc
