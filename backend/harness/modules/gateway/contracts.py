"""gateway 契约层 —— 归一化请求/响应、适配器接口、统一错误。

设计要点见 docs/andy-gateway-设计方案.md §4.1 / §4.2。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# 免费通道 id → 会话 provider id（provider_gateway 插件据此把三家注册为可选厂商）。
#: 键为网关 ChannelConfig.id，值为 ProviderRegistry 里的 provider_id。
CHANNEL_PROVIDER_IDS: dict[str, str] = {
    "cline": "gw-cline",
    "zen": "gw-opencode",
    "workbuddy": "gw-workbuddy",
    "qoder-cn": "gw-qoder",
}

#: provider_id → channel（反查）。
PROVIDER_ID_CHANNEL: dict[str, str] = {v: k for k, v in CHANNEL_PROVIDER_IDS.items()}

#: 会话选择器里展示的厂商名（provider_id → 展示名）。
CHANNEL_PROVIDER_LABELS: dict[str, str] = {
    "cline": "Cline",
    "zen": "OpenCode",
    "workbuddy": "WorkBuddy",
    "qoder-cn": "Qoder CN",
}


@dataclass(slots=True)
class Credential:
    """一条已解析的上游凭据（明文只存在于请求生命周期内）。"""

    id: str
    secret: str
    kind: str = "bearer"  # bearer | none
    meta: dict[str, Any] = field(default_factory=dict)


# ---- 探测判定（adapters.probe 的返回值） ------------------------------ #
#: 语义分层依据见 classify.py 模块头与竞品分析 §4-P0-3：网关自身故障不是对模型的判定。
PROBE_OK = "ok"  # 可达
PROBE_MODEL_DENIED = "model_denied"  # 网关点名拒绝（400/404/422 或点名文案）→ 长冷却
PROBE_GATEWAY_TROUBLE = "gateway_trouble"  # 5xx/429/超时/断网 → 短冷却，模型保持可用
PROBE_REGION_BLOCKED = "region_blocked"  # 地区门 → 单独分组，不冷却
PROBE_UNKNOWN = "unknown"  # 无法判定（如未配置探测地址）


@dataclass(slots=True)
class StreamOutcome:
    """aggregate_stream 的产出：归一化响应 + 供恢复判定用的流内事实。"""

    payload: dict[str, Any]
    saw_reasoning: bool = False
    saw_text: bool = False
    saw_tool_call: bool = False
    saw_finish: bool = False
    reasoning_text: str = ""


@dataclass(slots=True)
class UpstreamRequest:
    """适配器产出的上游请求。

    raw_body 用于非 JSON 形态的请求体（Qoder COSY 通道要求把 body 编成私有
    base64 的 wire 串，不是 JSON），与 json_body 二选一。
    """

    method: str
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any] | None = None
    raw_body: bytes | None = None


@dataclass(slots=True)
class AdapterCapabilities:
    """适配器能力声明；Router 据此过滤候选。"""

    streaming: bool = True
    tools: bool = False
    vision: bool = False
    reasoning: bool = False


class GatewayError(Exception):
    """统一错误模型：决定 HTTP 状态、是否可故障转移、冷却范围。

    kind / backoff / retry_after_ms 由 classify.classify_failure 填充，供
    HealthTracker 按 scope 与退避等级实施冷却（见 router.py）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 502,
        *,
        retryable: bool = False,
        cooldown_scope: str = "none",
        cooldown_seconds: int = 0,
        upstream_status: int | None = None,
        kind: str = "",
        backoff: bool = False,
        retry_after_ms: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.cooldown_scope = cooldown_scope
        self.cooldown_seconds = cooldown_seconds
        self.upstream_status = upstream_status
        self.kind = kind
        self.backoff = backoff
        self.retry_after_ms = retry_after_ms

    def to_openai(self, trace_id: str = "") -> dict[str, Any]:
        """OpenAI 兼容错误体（消息已由调用方脱敏）。"""
        return {
            "error": {
                "message": self.message,
                "type": self.code.lower(),
                "param": None,
                "code": self.code,
                "upstream_status": self.upstream_status,
                "trace_id": trace_id,
            }
        }


class ProviderAdapter(ABC):
    """上游协议适配器：Canonical ⇄ 上游形态。无状态，可共享。"""

    name: str = "base"

    @abstractmethod
    def capabilities(self) -> AdapterCapabilities: ...

    @abstractmethod
    def build_request(
        self,
        channel: Any,
        credential: Credential,
        body: dict[str, Any],
        *,
        session_key: str,
        force_stream: bool = True,
    ) -> UpstreamRequest:
        """把客户端 chat.completions 请求翻译成上游请求。"""

    @abstractmethod
    def parse_response(self, channel: Any, data: dict[str, Any]) -> dict[str, Any]:
        """上游非流式 JSON → Canonical（OpenAI 形状）。"""

    @abstractmethod
    async def aggregate_stream(
        self,
        channel: Any,
        lines: AsyncIterator[str],
        model: str,
        *,
        suppress_tool_names: frozenset[str] = frozenset(),
    ) -> StreamOutcome:
        """把上游 SSE 行聚合为一条非流式响应 + 流内事实（供空响应恢复判定）。"""

    def normalize_stream_line(self, line: str) -> list[str]:
        """把一条上游 SSE 行归一化为标准 OpenAI SSE 行（默认原样透传）。

        网关对外的 ``/v1`` 与进程内 provider 都假定流式载荷是 OpenAI 形状的
        ``data: {chunk}``。OpenAI 兼容上游天然满足；私有信封协议（Qoder COSY
        的 ``data:{"statusCodeValue":..,"body":"<chunk>"}``）必须在此翻译，否则
        客户端会拿到无法解析的信封——表现为"提问后没有任何反馈"。

        返回 0..n 行：空列表表示丢弃该行（如上游的自定义 event 行）。
        """
        return [line]

    @abstractmethod
    def map_error(self, status: int, body: bytes, retry_after_ms: int = 0) -> GatewayError:
        """上游错误 → 统一错误模型（含 kind/scope/退避语义）。"""

    async def probe(self, channel: Any, credential: Credential) -> str:
        """主动探测；返回 PROBE_* 判定。默认不探测（由 Channel 配置决定）。"""
        return PROBE_UNKNOWN

    async def prepare(self, channel: Any, credential: Credential) -> None:
        """发请求前的准备钩子（默认空实现）。

        需要先取上游元信息的协议在此预热缓存——Qoder COSY 的 model_config
        来自模型清单，缺了它上游会挂起不返回（实测：最小 model_config 会 read
        timeout）。网关在解析凭据后、构建请求前 await 本钩子。
        """
        return None

    async def catalog_request(self, channel: Any, credential: Credential) -> tuple[str, dict[str, str]] | None:
        """清单探测用的 (url, headers)。

        默认复用 build_request 的请求头——对 OpenAI 兼容通道，目录端点与聊天端点
        同源同鉴权，头可复用。签名类协议（Qoder COSY）必须覆写：签名覆盖路径，
        用聊天端点的头去请求目录端点会被判为签名不匹配。
        """
        if not getattr(channel, "probe_url", None):
            return None
        req = self.build_request(
            channel,
            credential,
            {"model": "", "messages": [], "stream": False},
            session_key="catalog",
            force_stream=False,
        )
        return str(channel.probe_url), req.headers
