"""GatewayService —— 装配、请求管线、故障转移、用量与指标。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from harness.kernel.eventbus import EventBus
from harness.kernel.services import ServiceRegistry
from harness.modules.gateway import catalog_probe
from harness.modules.gateway.adapters import AdapterRegistry, OpenAILikeAdapter
from harness.modules.gateway.adapters.openai_like import fingerprint_tool_names
from harness.modules.gateway.adapters.qoder_cosy_adapter import QoderCosyAdapter
from harness.modules.gateway.auth import GatewayAuthService, KeyContext
from harness.modules.gateway.config import ConfigError, ConfigService, ConfigSnapshot
from harness.modules.gateway.contracts import (
    CHANNEL_PROVIDER_IDS,
    CHANNEL_PROVIDER_LABELS,
    PROBE_GATEWAY_TROUBLE,
    PROBE_MODEL_DENIED,
    PROBE_OK,
    PROBE_REGION_BLOCKED,
    PROBE_UNKNOWN,
    PROVIDER_ID_CHANNEL,
    Credential,
    GatewayError,
    ProviderAdapter,
)
from harness.modules.gateway.credential_store import GatewayCredentialStore
from harness.modules.gateway.credentials import CredentialResolver
from harness.modules.gateway.login import DeviceLoginManager, QoderCNDeviceLogin
from harness.modules.gateway.router import HealthTracker, Router
from harness.modules.gateway.store import GatewayStore, SqlDB, UsageWriter, now_iso
from harness.modules.gateway.stream_rewrite import ToolCallRewriter

logger = logging.getLogger("harness.gateway")

# 流式透传：等待上游首字节超过该秒数则发 SSE keepalive 注释
KEEPALIVE_SECONDS = 15.0

# 纯思考空停恢复：检查点注入的最大字符数（保守余量，非精确 token 计数）
RECOVERY_CHECKPOINT_CHARS = 32_000


@dataclass(slots=True)
class ChatResult:
    stream: bool
    payload: dict[str, Any] | None = None
    iterator: AsyncIterator[bytes] | None = None
    status: int = 200


class GatewayService:
    """LLM 网关中心门面。"""

    def __init__(
        self,
        db: SqlDB,
        services: ServiceRegistry | None = None,
        *,
        config_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        bootstrap_key: str | None = None,
        key_file: Path | None = None,
        watch_config: bool = True,
    ) -> None:
        self.services = services
        self.store = GatewayStore(db)
        self.auth = GatewayAuthService(db)
        self.config = ConfigService(config_path)
        self.adapters = AdapterRegistry()
        self.health = HealthTracker()
        self.router = Router(self.health)
        # 自有凭据库（自建登录，免装 Magpie）→ 凭据解析器优先用它，Magpie 文件兜底
        self.credential_store = GatewayCredentialStore(db)
        self.credentials = CredentialResolver(store=self.credential_store)
        self.login = DeviceLoginManager(
            self.credential_store,
            [QoderCNDeviceLogin()],
            transport=transport,
        )
        self.usage = UsageWriter(self.store)
        self._snapshot: ConfigSnapshot | None = None
        self._client: httpx.AsyncClient | None = None
        self._transport = transport
        self._bootstrap_key = bootstrap_key
        self._key_file = key_file
        self._watch = watch_config
        self._watcher: asyncio.Task[None] | None = None
        self._prober: asyncio.Task[None] | None = None
        self._span_tasks: set[asyncio.Task[None]] = set()
        self._last_checksum = ""
        self._metrics: dict[tuple[str, ...], int] = defaultdict(int)
        self._inflight: dict[str, int] = defaultdict(int)
        # 清单探测结论：(channel_id, model) -> 是否仍在上游免费目录中。
        # 缺席 = True 未知（保留可见）；明确不在目录里才置 False。见 catalog_probe。
        self._catalog_present: dict[tuple[str, str], bool] = {}
        #: 配置生效后的回调（provider 插件用它重同步注册表：模型增删/通道开关
        #: 即时反映到会话下拉，不必重启；热更新曾因缺这一步显示已删模型）
        self.on_config_applied: Any = None
        self.started = False

    # ---- 生命周期 ------------------------------------------------------ #
    async def start(self) -> None:
        if self.started:
            return
        self.store.ensure_schema()
        self.credential_store.ensure_schema()
        self.adapters.register(OpenAILikeAdapter())
        self.adapters.register(QoderCosyAdapter())
        snapshot = self.config.load()
        self._validate_adapters(snapshot)
        self._snapshot = snapshot
        self.auth.bootstrap(self._bootstrap_key, self._key_file)
        read_timeout = max(30, snapshot.defaults.read_timeout_seconds)
        self._client = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
        )
        self.usage.start()
        pruned = self.store.prune_usage(snapshot.defaults.usage_retention_days)
        self._apply_snapshot(snapshot, actor="system", source="startup", force=True)
        if self._watch:
            self._watcher = asyncio.create_task(self._watch_loop(), name="gateway-config-watch")
        if any(ch.probe_url for ch in snapshot.channel_list()):
            self._prober = asyncio.create_task(self._probe_loop(), name="gateway-probe")
        self.started = True
        logger.info(
            "gateway 已启动：%d 个通道（清理过期用量 %d 行）",
            len(snapshot.channel_list()),
            pruned,
        )

    async def stop(self) -> None:
        self.started = False
        for task in (self._watcher, self._prober):
            if task is not None:
                task.cancel()
        self._watcher = None
        self._prober = None
        await self.usage.stop()
        await self.login.stop()
        if self._span_tasks:
            await asyncio.wait(set(self._span_tasks), timeout=2)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- 查询 ---------------------------------------------------------- #
    @property
    def snapshot(self) -> ConfigSnapshot:
        if self._snapshot is None:
            self._snapshot = self.config.load()
        return self._snapshot

    def channels_public(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ch in self.snapshot.channel_list():
            out.append(
                {
                    "id": ch.id,
                    "name": ch.name or ch.id,
                    "adapter": ch.adapter,
                    "base_url": ch.base_url,
                    "auth": ch.auth,
                    "stream_only": ch.stream_only,
                    "requires_tools": ch.requires_tools,
                    "weight": ch.weight,
                    "strategy": ch.strategy,
                    "models": list(ch.models),
                    # 双粒度熔断：通道级冷却、或其全部模型都被模型级冷却，才算不可用
                    "available": self.health.any_available(ch.id, list(ch.models)),
                }
            )
        return out

    def models(self, ctx: KeyContext) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = []
        for ch in self.snapshot.channel_list():
            for model in ch.models:
                if not self.router.tenant_allows(ch, model, ctx.allowed_models, ctx.allowed_channels):
                    continue
                data.append(
                    {
                        "id": f"{ch.id}/{model}",
                        "object": "model",
                        "created": 0,
                        "owned_by": ch.id,
                        # 模型级可用性：长冷却（点名拒绝/配额墙）的模型保留在列表里
                        # 但标注不可用——用户看得到"有这个东西，只是此刻连不上"
                        "available": self.health.available(ch.id, ch.model_map.get(model, model)),
                    }
                )
        for alias in self.snapshot.aliases:
            visible = any(
                self.router.tenant_allows(ch, requested, ctx.allowed_models, ctx.allowed_channels)
                for ch, _upstream, requested in self.router.expand(self.snapshot, alias)
            )
            if visible:
                data.append({"id": alias, "object": "model", "created": 0, "owned_by": "gateway"})
        return data

    def all_models(self) -> list[dict[str, Any]]:
        """管理面用：全部通道的全部模型 + 模型级可用性（不受租户白名单限制）。

        `/v1/models` 是数据面（要网关 Key），前端只有管理员身份；长冷却的模型
        保留在列表里并带 cooldown_remaining，让"有这个东西、只是此刻连不上"可见。
        """
        out: list[dict[str, Any]] = []
        for ch in self.snapshot.channel_list():
            for model in ch.models:
                upstream = ch.model_map.get(model, model)
                out.append(
                    {
                        "id": f"{ch.id}/{model}",
                        "channel_id": ch.id,
                        "model": model,
                        "available": self.health.available(ch.id, upstream),
                        "cooldown_remaining": self.health.cooldown_remaining(ch.id, upstream),
                    }
                )
        return out

    def metrics_text(self) -> str:
        requests: list[str] = []
        tokens: list[str] = []
        failovers: list[str] = []
        for key, value in sorted(self._metrics.items()):
            kind = key[0]
            if kind == "requests" and len(key) == 3:
                requests.append(f'agw_requests_total{{channel="{key[1]}",status="{key[2]}"}} {value}')
            elif kind == "tokens" and len(key) == 3:
                tokens.append(f'agw_tokens_total{{tenant="{key[1]}",type="{key[2]}"}} {value}')
            elif kind == "failovers" and len(key) == 2:
                failovers.append(f'agw_failovers_total{{channel="{key[1]}"}} {value}')
        lines = [
            "# HELP agw_requests_total Gateway requests by channel and status",
            "# TYPE agw_requests_total counter",
            *requests,
            "# HELP agw_tokens_total Gateway token usage",
            "# TYPE agw_tokens_total counter",
            *tokens,
            "# HELP agw_failovers_total Failover attempts",
            "# TYPE agw_failovers_total counter",
            *failovers,
            "# HELP agw_inflight_requests In-flight requests per channel",
            "# TYPE agw_inflight_requests gauge",
        ]
        for channel, value in sorted(self._inflight.items()):
            lines.append(f'agw_inflight_requests{{channel="{channel}"}} {value}')
        return "\n".join(lines) + "\n"

    # ---- 配置 ---------------------------------------------------------- #
    def apply_config(self, payload: dict[str, Any], *, actor: str = "admin", source: str = "api") -> dict[str, Any]:
        ConfigService.validate_payload(payload)  # 不落盘预检，失败则磁盘保持不变
        snapshot = self.config.save(payload)
        self._validate_adapters(snapshot)
        version = self._apply_snapshot(snapshot, actor=actor, source=source, force=True)
        self.store.audit("config.apply", actor=actor, target_type="config", detail={"version": version})
        return {"version": version, "checksum": snapshot.checksum}

    def _apply_snapshot(self, snapshot: ConfigSnapshot, *, actor: str, source: str, force: bool = False) -> int:
        if not force and snapshot.checksum == self._last_checksum:
            return self.store.latest_config_version() or snapshot.version
        self._snapshot = snapshot
        self._last_checksum = snapshot.checksum
        if self.store.has_config_checksum(snapshot.checksum):
            version = self.store.latest_config_version()
        else:
            version = max(self.store.latest_config_version() + 1, snapshot.version)
            self.store.save_config_version(
                version=version,
                payload=snapshot.raw,
                checksum=snapshot.checksum,
                actor=actor,
                source=source,
            )
        for seed in snapshot.tenants:
            try:
                self.auth.create_tenant(seed.id, seed.name, seed.allowed_models, seed.allowed_channels)
            except Exception:  # noqa: BLE001 —— 已存在则忽略
                pass
        callback = self.on_config_applied
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 —— 回调失败不影响配置生效
                logger.exception("配置生效回调（provider 重同步）执行失败")
        return version

    def _validate_adapters(self, snapshot: ConfigSnapshot) -> None:
        for ch in snapshot.channels.values():
            if ch.enabled and ch.adapter not in self.adapters.names():
                raise ConfigError(f"channel {ch.id} 引用了未注册的适配器: {ch.adapter}")

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                if not self.config.path.exists():
                    continue
                mtime = self.config.path.stat().st_mtime
                if getattr(self, "_last_mtime", None) == mtime:
                    continue
                self._last_mtime = mtime
                snapshot = self.config.load()
                self._validate_adapters(snapshot)
                self._apply_snapshot(snapshot, actor="file", source="file")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 配置错误不打断现网
                logger.warning("gateway 配置热更新失败（保持现网快照）: %s", e)

    async def _probe_loop(self) -> None:
        """主动健康探测（设计 §4.3.4）：按判定分层冷却，非一刀切。

        语义分层依据见 contracts.PROBE_* 与竞品分析 §4-P0-3：网关自身故障
        （5xx/429/超时/断网）只短冷却且模型保持可用；点名拒绝才长冷却；
        地区门不冷却（换出口即解）。刻意不做出口 IP 轮询——那要求后端定期
        调用外部 echo 服务，5 分钟的探测周期已能覆盖 VPN 切换场景。
        """
        while True:
            snapshot = self.snapshot
            for channel in snapshot.channel_list():
                if channel.probe_url:
                    await self._probe_channel(channel, snapshot)
            await asyncio.sleep(max(30, snapshot.defaults.probe_interval_seconds))

    async def probe_now(self, channels: list[str] | None = None) -> None:
        """按需立即探测（会话页点「刷新可用性」时调用，管理面触发）。

        channels 传 provider id（gw-*）或通道 id 均可；None = 全部有 probe_url 的通道。
        """
        snapshot = self.snapshot
        wanted = channels
        if wanted is not None:
            wanted = [PROVIDER_ID_CHANNEL.get(c, c) for c in wanted]
        for channel in snapshot.channel_list():
            if not channel.probe_url:
                continue
            if wanted is not None and channel.id not in wanted:
                continue
            await self._probe_channel(channel, snapshot)

    async def _probe_channel(self, channel: Any, snapshot: ConfigSnapshot) -> None:
        """探测单通道：健康判定分层冷却 + 清单在线性刷新。"""
        interval = channel.probe_interval_seconds or snapshot.defaults.probe_interval_seconds
        verdict = PROBE_UNKNOWN
        credentials = None
        adapter = None
        try:
            credentials = await asyncio.to_thread(self.credentials.resolve, channel)
            adapter = self.adapters.get(channel.adapter)
            verdict = await adapter.probe(channel, credentials[0])
        except (GatewayError, KeyError):
            verdict = PROBE_GATEWAY_TROUBLE
        if verdict == PROBE_OK:
            self.health.mark_success(channel.id)
        elif verdict == PROBE_MODEL_DENIED:
            # 点名拒绝：长冷却（默认 10 分钟），期间该通道整体退出路由
            self.health.mark_failure(channel.id, 600)
        elif verdict == PROBE_REGION_BLOCKED:
            self.health.mark_failure(channel.id, interval)
        else:
            # 网关自身故障 / 无法判定：短冷却，模型保持可用
            self.health.mark_failure(channel.id, min(interval, 60))

        # 清单探测（GET 目录端点，不发起生成、不消耗额度）：核对模型
        # 是否仍在免费目录中。仅在成功拿到目录时更新判定；抓取失败保留
        # 上次结论（见 catalog_probe 的非对称原则）。
        # channel.catalog_probe=False 时跳过：某些通道的 probe_url 是配置端点
        # 而非模型目录（WorkBuddy /v3/config 不含 step-5-preview，但后者可用），
        # 拿它当目录会把能用的模型误判下架 → 界面置灰。
        if adapter is not None and credentials and getattr(channel, "catalog_probe", True):
            await self._refresh_catalog(channel, adapter, credentials[0])

    async def _refresh_catalog(self, channel: Any, adapter: Any, credential: Credential) -> None:
        """用适配器给的目录请求，GET 目录端点抽取在线 id，更新清单判定。

        目录请求由适配器自己给（catalog_request）：签名类协议（Qoder COSY）
        的签名覆盖路径，不能沿用聊天端点的头。
        """
        try:
            request = await adapter.catalog_request(channel, credential)
        except (GatewayError, KeyError, IndexError, ValueError):
            return
        if request is None:
            return
        url, headers = request
        present = await catalog_probe.fetch_catalog_ids(url, headers, transport=self._transport)
        online = catalog_probe.present_ids(present, list(channel.models))
        if online is None:
            return  # 无从判定：保留上次
        for model in channel.models:
            self._catalog_present[(channel.id, model)] = model in online

    # ---- 会话 provider 侧支持 ----------------------------------------- #
    def internal_context(self) -> KeyContext:
        """进程内 provider（provider_gateway 插件）用的免鉴权上下文。

        default 租户、全模型全通道、无 RPM/TPM——真实免费额度约束由上游与
        HealthTracker 承担，这里只需通过 has_scope("chat") 检查。
        """
        return KeyContext(
            key_id="internal-gateway",
            tenant_id="default",
            name="internal",
            scopes=["chat", "models", "admin"],
            rpm=0,
            tpm=0,
        )

    def model_available(self, channel_id: str, model: str) -> bool:
        """模型当前是否可用：模型级/通道级健康 ∧ 未被清单判定下架。"""
        bare = model.split("/", 1)[1] if "/" in model and model.split("/", 1)[0] == channel_id else model
        upstream = model
        ch = self.snapshot.channels.get(channel_id)
        if ch is not None:
            upstream = ch.model_map.get(bare, bare)
        # catalog_probe=False 的通道（目录端点不可靠，如 WorkBuddy /v3/config）
        # 连读取端也跳过清单判定——同时清掉历史遗留的假阴性，避免热更新后仍置灰。
        catalog_trusted = ch is None or getattr(ch, "catalog_probe", True)
        if catalog_trusted and self._catalog_present.get((channel_id, bare)) is False:
            return False
        return self.health.available(channel_id, upstream)

    def availability_snapshot(self, channels: list[str] | None = None) -> dict[str, Any]:
        """供前端轮询的可用性快照（开放端点，仅结构，不含凭据）。

        channels 为 None 时返回全部免费通道；否则只返回指定通道（provider id 经
        PROVIDER_ID_CHANNEL 映射为通道 id）。
        """
        if channels is None:
            wanted = list(CHANNEL_PROVIDER_IDS.keys())
        else:
            wanted = [PROVIDER_ID_CHANNEL.get(c, c) for c in channels]
        result: dict[str, Any] = {"channels": {}, "checked_at": now_iso()}
        snap = self.snapshot
        for channel_id in wanted:
            ch = snap.channels.get(channel_id)
            if ch is None or not ch.enabled:
                result["channels"][channel_id] = {"models": [], "enabled": False}
                continue
            models: dict[str, Any] = {}
            catalog_trusted = getattr(ch, "catalog_probe", True)
            for model in ch.models:
                models[model] = {
                    "id": f"{channel_id}/{model}",
                    "available": self.model_available(channel_id, model),
                    "in_catalog": (not catalog_trusted) or self._catalog_present.get(
                        (channel_id, model), True
                    ) is not False,
                    "cooldown_remaining": self.health.cooldown_remaining(
                        channel_id, ch.model_map.get(model, model)
                    ),
                }
            result["channels"][channel_id] = {
                "enabled": True,
                "provider_id": CHANNEL_PROVIDER_IDS.get(channel_id, channel_id),
                "label": CHANNEL_PROVIDER_LABELS.get(channel_id, channel_id),
                "models": models,
                "available": any(m["available"] for m in models.values()),
            }
        return result

    # ---- 对话 ---------------------------------------------------------- #
    async def chat(self, body: dict[str, Any], ctx: KeyContext, trace_id: str = "") -> ChatResult:
        if not ctx.has_scope("chat"):
            raise GatewayError("GW_FORBIDDEN", "该 Key 无 chat 权限", 403)
        model = str(body.get("model") or "")
        if not model:
            raise GatewayError("GW_INVALID_REQUEST", "model 必填", 400)

        snapshot = self.snapshot
        session_key = _session_key(body)
        # 全部候选同属一个通道时沿用该通道的策略；跨通道用默认策略
        strategies = {ch.strategy for ch, _up, _req in self.router.expand(snapshot, model)}
        strategy = strategies.pop() if len(strategies) == 1 else None
        candidates = self.router.candidates(
            snapshot,
            model,
            allowed_models=ctx.allowed_models,
            allowed_channels=ctx.allowed_channels,
            strategy=strategy,
            session_key=session_key,
        )
        if not candidates:
            raise GatewayError("GW_NO_CAPACITY", f"没有可用通道: {model}", 503)

        estimated = _estimate_tokens(body.get("messages"))
        self.auth.limiter.check(ctx, estimated)

        stream = bool(body.get("stream"))
        started = time.perf_counter()
        deadline = time.monotonic() + max(1, snapshot.defaults.request_budget_seconds)
        last_error: GatewayError | None = None
        max_attempts = 1 + max(0, snapshot.defaults.max_failovers)

        for attempt, candidate in enumerate(candidates[:max_attempts], 1):
            if time.monotonic() >= deadline:
                last_error = GatewayError("UPSTREAM_TIMEOUT", "请求总预算耗尽", 504)
                break
            channel = candidate.channel
            if not self.health.available(channel.id, candidate.upstream_model):
                continue
            try:
                # 凭据解析可能触发 Cline token 刷新（网络 I/O），放到线程里执行，
                # 避免阻塞事件循环导致其他会话一起卡住。
                credentials = await asyncio.to_thread(self.credentials.resolve, channel, last_error)
                adapter = self.adapters.get(channel.adapter)
            except (GatewayError, KeyError) as e:
                last_error = e if isinstance(e, GatewayError) else GatewayError("GW_CONFIG_ERROR", str(e), 500)
                continue

            credential = credentials[0]
            # 协议预热：部分上游（如 Qoder COSY）需要先取元信息才能构造请求
            try:
                await adapter.prepare(channel, credential)
            except (GatewayError, KeyError, ValueError):
                pass
            payload = dict(body)
            payload["model"] = candidate.upstream_model
            # 仅当调用方没自带 tools、而我们注入了指纹工具时，转发侧才抑制对
            # 这些占位工具的调用（客户端从未注册它们，回去是个无力处理的错误）。
            suppress = (
                fingerprint_tool_names()
                if channel.requires_tools and not payload.get("tools")
                else frozenset()
            )
            try:
                upstream = adapter.build_request(
                    channel, credential, payload, session_key=session_key, force_stream=True
                )
            except GatewayError as e:
                last_error = e
                continue

            client = self._client
            if client is None:
                raise GatewayError("GW_CONFIG_ERROR", "网关尚未启动", 503)
            self.health.begin(channel.id)
            self._inflight[channel.id] += 1
            try:
                request = client.build_request(
                    upstream.method,
                    upstream.url,
                    headers=upstream.headers,
                    # Qoder COSY 通道的 body 是私有 base64 的 wire 串，不是 JSON
                    content=upstream.raw_body if upstream.raw_body is not None else None,
                    json=upstream.json_body if upstream.raw_body is None else None,
                )
                response = await client.send(request, stream=True)
            except httpx.HTTPError as e:
                self._release(channel.id)
                # 连接层失败：通道级短冷却（不是账号的错）
                self.health.mark_failure(channel.id, snapshot.defaults.health_cooldown_seconds)
                last_error = GatewayError(
                    "UPSTREAM_UNREACHABLE",
                    f"上游连接失败: {type(e).__name__}",
                    502,
                    retryable=True,
                    cooldown_scope="channel",
                    cooldown_seconds=snapshot.defaults.health_cooldown_seconds,
                )
                self._record(
                    ctx, channel.id, credential.id, model, 0, started, attempt, trace_id, last_error.code
                )
                continue

            if response.status_code >= 400:
                detail = await response.aread()
                retry_after = _retry_after_ms(response.headers)
                await response.aclose()
                self._release(channel.id)
                error = adapter.map_error(response.status_code, detail, retry_after)
                # 按 kind×scope 冷却：账号级锁 (channel, model)，provider 级锁整条通道，
                # request 级不锁（分类规则见 classify.py）
                self.health.mark_classified(channel.id, error, model=candidate.upstream_model)
                last_error = error
                self._record(
                    ctx,
                    channel.id,
                    credential.id,
                    model,
                    response.status_code,
                    started,
                    attempt,
                    trace_id,
                    error.code,
                )
                if error.retryable and attempt < max_attempts:
                    self._metrics[("failovers", channel.id)] += 1
                    continue
                raise error

            if stream:
                iterator = self._stream(
                    response,
                    ctx,
                    channel.id,
                    credential.id,
                    model,
                    trace_id,
                    started,
                    attempt,
                    suppress,
                    adapter=adapter,
                    upstream_model=candidate.upstream_model,
                )
                return ChatResult(stream=True, iterator=iterator)

            try:
                outcome = await asyncio.wait_for(
                    adapter.aggregate_stream(
                        channel,
                        response.aiter_lines(),
                        model,
                        suppress_tool_names=suppress,
                    ),
                    timeout=max(10, snapshot.defaults.read_timeout_seconds),
                )
            except TimeoutError:
                await response.aclose()
                self._release(channel.id)
                # 单模型读取超时只冷却该模型：zen/cline 这类聚合通道里一个慢
                # 模型超时不该把 big-pickle 等健康模型连坐成"没有可用通道"。
                self.health.mark_failure(
                    channel.id,
                    snapshot.defaults.health_cooldown_seconds,
                    model=candidate.upstream_model,
                )
                last_error = GatewayError(
                    "UPSTREAM_TIMEOUT",
                    f"上游读取超时（>{snapshot.defaults.read_timeout_seconds}s）",
                    504,
                    retryable=True,
                    cooldown_scope="channel",
                    cooldown_seconds=snapshot.defaults.health_cooldown_seconds,
                )
                self._record(
                    ctx, channel.id, credential.id, model, 0, started, attempt, trace_id, last_error.code
                )
                continue
            except GatewayError:
                # 适配器已给出语义化错误（状态码/可重试/冷却范围）：原样上抛，
                # 不被下面的泛化分支改写成 502——否则"模型排队中"这类可重试的
                # 503 会退化成不可重试的 502，白白丢掉 Retry-After 语义。
                await response.aclose()
                self._release(channel.id)
                raise
            except Exception as e:  # noqa: BLE001 —— 流中断统一转为可转移错误
                try:
                    await response.aclose()
                except Exception:  # noqa: BLE001
                    pass
                self._release(channel.id)
                # 流中断同理：故障多在模型侧（上游断流），冷却该模型即可
                self.health.mark_failure(
                    channel.id,
                    snapshot.defaults.health_cooldown_seconds,
                    model=candidate.upstream_model,
                )
                last_error = GatewayError(
                    "UPSTREAM_ERROR",
                    f"上游流中断: {type(e).__name__}",
                    502,
                    retryable=True,
                    cooldown_scope="channel",
                    cooldown_seconds=snapshot.defaults.health_cooldown_seconds,
                )
                self._record(
                    ctx, channel.id, credential.id, model, 0, started, attempt, trace_id, last_error.code
                )
                continue

            await response.aclose()
            self._release(channel.id)

            # 纯思考空停恢复（借鉴 dsh recovery.js / issue #12）：推理模型把输出
            # 预算全耗在 reasoning 上时，首段只有思考没有正文。从检查点续写一次
            # 要正文；有界、不递归，用户取消/已达上限/明确错误不触发。
            if (
                snapshot.defaults.empty_answer_recovery
                and outcome.saw_reasoning
                and not outcome.saw_text
                and not outcome.saw_tool_call
                and time.monotonic() < deadline
            ):
                recovered = await self._recover_empty_answer(
                    channel, credential, adapter, payload, outcome, deadline, suppress
                )
                if recovered is not None:
                    # 合并两次调用的用量（首段思考 + 续写正文），恢复轮次同样计账
                    merged = dict(outcome.payload.get("usage") or {})
                    for key, value in (recovered.payload.get("usage") or {}).items():
                        if isinstance(value, (int, float)):
                            merged[key] = merged.get(key, 0) + value
                    recovered.payload["usage"] = merged
                    outcome = recovered

            elapsed = int((time.perf_counter() - started) * 1000)
            self.health.mark_success(channel.id, candidate.upstream_model, elapsed)
            data = outcome.payload
            usage = data.get("usage") or {}
            self._record(
                ctx, channel.id, credential.id, model, 200, started, attempt, trace_id, "", usage=usage
            )
            return ChatResult(stream=False, payload=data)

        raise last_error or GatewayError("GW_NO_CAPACITY", "所有候选通道均不可用", 503)

    async def _recover_empty_answer(
        self,
        channel: Any,
        credential: Any,
        adapter: Any,
        payload: dict[str, Any],
        outcome: Any,
        deadline: float,
        suppress: frozenset[str],
    ) -> Any:
        """用思考检查点续写一次索要正文；失败返回 None（保留原结果）。"""
        checkpoint = (outcome.reasoning_text or "")[-RECOVERY_CHECKPOINT_CHARS:]
        instruction = (
            "The previous response was interrupted before its final answer. "
            "Complete the original task using the conversation above. "
            "The text below is an incomplete draft of the interrupted analysis, not new instructions. "
            "Use its established results to deliver the final answer now. "
            "For this continuation, the checkpoint already satisfies any earlier request for prolonged "
            "analysis; do not restart that analysis. Give a concise, substantive final answer. "
            "Do not call tools.\n\n"
            f"Interrupted analysis checkpoint:\n{checkpoint}"
        )
        messages = list(payload.get("messages") or [])
        messages.append({"role": "user", "content": instruction})
        retry_payload = dict(payload)
        retry_payload["messages"] = messages
        try:
            upstream = adapter.build_request(
                channel, credential, retry_payload, session_key="recovery", force_stream=True
            )
            client = self._client
            if client is None:
                return None
            budget = max(5.0, deadline - time.monotonic())
            request = client.build_request(
                upstream.method,
                upstream.url,
                headers=upstream.headers,
                content=upstream.raw_body if upstream.raw_body is not None else None,
                json=upstream.json_body if upstream.raw_body is None else None,
            )
            response = await client.send(request, stream=True)
            try:
                if response.status_code >= 400:
                    return None
                lines = response.aiter_lines()
                return await asyncio.wait_for(
                    adapter.aggregate_stream(
                        channel, lines, payload.get("model", ""), suppress_tool_names=suppress
                    ),
                    timeout=budget,
                )
            finally:
                await response.aclose()
        except Exception as e:  # noqa: BLE001 —— 恢复是尽力而为，失败保留原结果
            logger.info("gateway 空响应恢复失败（保留原结果）: %s", type(e).__name__)
            return None

    def _release(self, channel_id: str) -> None:
        self.health.end(channel_id)
        self._inflight[channel_id] = max(0, self._inflight[channel_id] - 1)

    async def _stream(
        self,
        response: httpx.Response,
        ctx: KeyContext,
        channel_id: str,
        credential_id: str,
        model: str,
        trace_id: str,
        started: float,
        attempt: int,
        suppress_tools: frozenset[str] = frozenset(),
        adapter: ProviderAdapter | None = None,
        upstream_model: str = "",
    ) -> AsyncIterator[bytes]:
        """透传上游 SSE；首字节等待期发 keepalive，客户端断开立即关闭上游。

        先经适配器 ``normalize_stream_line`` 把私有信封翻译成标准 OpenAI SSE
        （Qoder COSY 的 ``data:{"statusCodeValue":..,"body":"<chunk>"}`` 若原样
        透传，provider 只认 ``data: `` 会零块输出 → 页面无反馈）。
        tool_calls 帧经 ToolCallRewriter 改写：索引重编号为 0..n 连续值
        （上游与 reasoning/text 共享编号空间时客户端会拿到稀疏数组），并抑制
        对注入的指纹工具的调用。详见 stream_rewrite.py 模块头。
        """
        lines = response.aiter_lines()
        rewriter = ToolCallRewriter(suppress_tools)
        ttfb = 0
        usage: dict[str, Any] = {}
        status = 200
        error_code = ""
        # 注意：不能用 asyncio.wait_for(lines.__anext__()) 做心跳——超时会取消
        # __anext__，把整个上游 async generator 一起关掉（流会被掐死）。改为
        # 「取下一行任务 + 心跳定时任务」竞争，超时只发注释、不取消取行任务。
        pending: asyncio.Task[str] | None = None
        ticker: asyncio.Task[None] | None = None

        async def _next_line() -> str:
            return await lines.__anext__()

        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(_next_line())
                if ticker is None:
                    ticker = asyncio.ensure_future(asyncio.sleep(KEEPALIVE_SECONDS))
                done, _rest = await asyncio.wait({pending, ticker}, return_when=asyncio.FIRST_COMPLETED)
                if pending in done:
                    if ticker is not None:
                        ticker.cancel()
                    ticker = None
                    try:
                        line = pending.result()
                    except StopAsyncIteration:
                        pending = None
                        for out in rewriter.flush():
                            yield (out + "\n").encode("utf-8")
                        break
                    pending = None
                    # 适配器把私有信封归一化成标准 OpenAI SSE（默认原样透传）
                    normalized = (
                        adapter.normalize_stream_line(line) if adapter is not None else [line]
                    )
                    for norm in normalized:
                        if norm.startswith("data: "):
                            raw = norm[6:].strip()
                            fatal = False
                            if raw != "[DONE]":
                                try:
                                    chunk = json.loads(raw)
                                    if not ttfb and (chunk.get("choices") or []):
                                        ttfb = int((time.perf_counter() - started) * 1000)
                                    if chunk.get("usage"):
                                        usage = chunk["usage"]
                                    if isinstance(chunk, dict) and chunk.get("error"):
                                        # 流内错误信封（如 Qoder 排队）：发给客户端后终止。
                                        # 上游出错后往往不关连接，继续等会无限 keepalive，
                                        # 客户端表现为"提问后永远转圈、没有任何反馈"。
                                        fatal = True
                                        status = 502
                                        err_obj = chunk.get("error") or {}
                                        error_code = str(
                                            err_obj.get("code") or "UPSTREAM_STREAM_ERROR"
                                        )
                                except json.JSONDecodeError:
                                    pass
                            for out in rewriter.rewrite_line(norm):
                                yield (out + "\n").encode("utf-8")
                            if fatal:
                                return
                        else:
                            # 上游可能以 HTTP 200 + 非 SSE JSON 报错（如 {"error": ...}）。
                            # 直接透传会被 provider 当作无效行丢弃 → 零块 → 页面无反馈。
                            stripped = norm.strip()
                            if stripped.startswith("{"):
                                try:
                                    obj = json.loads(stripped)
                                except json.JSONDecodeError:
                                    obj = None
                                if isinstance(obj, dict) and obj.get("error"):
                                    err_obj = obj.get("error")
                                    payload = {
                                        "error": err_obj
                                        if isinstance(err_obj, dict)
                                        else {"message": str(err_obj)}
                                    }
                                    yield (
                                        f"data: {json.dumps(payload, ensure_ascii=False)}\n"
                                    ).encode()
                                    status = 502
                                    error_code = str(
                                        payload["error"].get("code") or "UPSTREAM_ERROR_BODY"
                                    )
                                    return
                            yield (norm + "\n").encode("utf-8")
                else:
                    ticker = None
                    yield b": keepalive\n\n"
        except asyncio.CancelledError:
            status = 499
            error_code = "CLIENT_ABORTED"
            raise
        except httpx.HTTPError:
            status = 502
            error_code = "UPSTREAM_STREAM_ERROR"
            raise
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
            try:
                await response.aclose()
            except Exception:  # noqa: BLE001 —— 关闭失败不影响记账
                pass
            self._release(channel_id)
            elapsed = int((time.perf_counter() - started) * 1000)
            if status == 200:
                self.health.mark_success(channel_id, upstream_model, elapsed)
            elif status == 499:
                # 客户端主动停止 / 提前断开：不是模型或通道的错。
                # 若在此冷却，用户点「停止生成」会把同通道健康模型连坐成
                # GW_NO_CAPACITY（真机踩过：step-5 正常回答被标不可用）。
                pass
            else:
                # 流式出错只冷却**这个模型**（不是整条通道）：一条通道里某个
                # 模型坏了，不该牵连其它模型的路由——否则 flint 的 503 会让
                # 同 zen 通道的 big-pickle 一起"没有可用通道"。
                self.health.mark_failure(channel_id, 60, model=upstream_model)
            self._record(
                ctx,
                channel_id,
                credential_id,
                model,
                status,
                started,
                attempt,
                trace_id,
                error_code,
                usage=usage,
                ttfb=ttfb,
            )

    # ---- 记账/指标 ------------------------------------------------------ #
    def _record(
        self,
        ctx: KeyContext,
        channel_id: str,
        credential_id: str,
        model: str,
        status: int,
        started: float,
        attempt: int,
        trace_id: str,
        error_code: str,
        *,
        usage: dict[str, Any] | None = None,
        ttfb: int = 0,
    ) -> None:
        usage = usage or {}
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total = int(usage.get("total_tokens") or prompt + completion)
        elapsed = int((time.perf_counter() - started) * 1000)
        self.usage.enqueue(
            {
                "tenant_id": ctx.tenant_id,
                "key_id": ctx.key_id,
                "channel_id": channel_id,
                "credential_id": credential_id,
                "model": model,
                "status": status,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "duration_ms": elapsed,
                "ttfb_ms": ttfb,
                "attempt": attempt,
                "trace_id": trace_id,
                "error_code": error_code,
            }
        )
        self._metrics[("requests", channel_id, str(status))] += 1
        if prompt:
            self._metrics[("tokens", ctx.tenant_id, "prompt")] += prompt
        if completion:
            self._metrics[("tokens", ctx.tenant_id, "completion")] += completion
        self._emit_span(trace_id, ctx, channel_id, model, status, elapsed, ttfb, prompt, completion, total, error_code)

    def _emit_span(
        self,
        trace_id: str,
        ctx: KeyContext,
        channel_id: str,
        model: str,
        status: int,
        elapsed: int,
        ttfb: int,
        prompt: int,
        completion: int,
        total: int,
        error_code: str,
    ) -> None:
        if self.services is None or not self.services.has(EventBus):
            return
        payload = {
            "trace_id": trace_id or f"gw-{uuid.uuid4().hex[:12]}",
            "name": f"gateway {channel_id}/{model}",
            "kind": "gateway",
            "status": "ok" if status == 200 else "error",
            "duration_ms": elapsed,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "error": error_code,
            "channel": channel_id,
            "model": model,
            "tenant_id": ctx.tenant_id,
            "ttfb_ms": ttfb,
        }
        try:
            bus = self.services.get(EventBus)
            task = asyncio.create_task(bus.publish("trace.span", payload))
            self._span_tasks.add(task)
            task.add_done_callback(self._span_tasks.discard)
        except Exception:  # noqa: BLE001 —— 可观测是旁路
            pass


def _estimate_tokens(messages: Any) -> int:
    if not isinstance(messages, list):
        return 0
    chars = 0
    for m in messages:
        if isinstance(m, dict):
            chars += len(str(m.get("content") or ""))
    return max(1, chars // 4)


def _retry_after_ms(headers: Any) -> int:
    """响应头 Retry-After（秒）→ 毫秒；缺失/非法返回 0。钳制在 classify 内完成。"""
    if headers is None:
        return 0
    getter = getattr(headers, "get", None)
    if getter is None:
        return 0
    raw = getter("retry-after") or getter("Retry-After")
    if raw is None:
        return 0
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, seconds) * 1000


def _session_key(body: dict[str, Any]) -> str:
    for key in ("user", "session_id"):
        if isinstance(body.get(key), str) and body[key]:
            return str(body[key])
    messages = body.get("messages") or []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            return hashlib.sha256(str(m.get("content") or "").encode("utf-8")).hexdigest()[:16]
    return "default"
