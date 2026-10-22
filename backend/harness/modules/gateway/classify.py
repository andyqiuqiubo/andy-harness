"""上游失败分类与冷却策略（纯函数）。

合并两处成熟来源（依据见 docs/reports/andy-gateway-竞品与借鉴点分析-2026-10-22.md §4-P0-1）：

- **dsh-our-free-model `src/route/classify.js`**：kind（这是什么故障）与
  scope（故障在哪里）分离的词汇表；
- **9router `open-sse/config/errorConfig.js` ERROR_RULES**：文本规则优先于
  状态码规则的冷却策略表，以及指数退避。

两条核心经验：

1. **文本规则优先于状态码**。一条 400 的 message 说 "rate limit" 就是限流；
   一条 429 说 "no credentials" 就是凭据没了。只看状态码会把这两类判反。
2. **未匹配的 4xx 不冷却任何东西**（scope=request）。重放同一 body 会复现
   拒绝，冷却一个健康账号只会隐藏真实原因，并让窗口内每个后续请求都带着
   同一份拷贝失败。

纯函数：无时钟、无 IO、无随机数。唯一的时间形状输入是 provider 自报的
retry-after，钳制在 30 分钟——provider 确实会报多小时的重置，照单全收会让
链路静默清空（9router MAX_RATE_LIMIT_COOLDOWN_MS 同值）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---- 词汇表 ---------------------------------------------------------- #

#: 故障类别：这是什么问题（决定给用户看什么、是否值得重放）。
KIND_AUTH = "auth"
KIND_PERMISSION = "permission"
KIND_BILLING = "billing"
KIND_RATE_LIMIT = "rate_limit"
KIND_OVERLOADED = "overloaded"
KIND_UNAVAILABLE = "unavailable"
KIND_INVALID_REQUEST = "invalid_request"
KIND_UPSTREAM = "upstream"
KIND_TIMEOUT = "timeout"
KIND_TRANSPORT = "transport"
KIND_UNKNOWN = "unknown"

#: 故障范围：该冷却谁（决定熔断粒度）。
SCOPE_REQUEST = "request"  # 请求自身有问题：不冷却任何账号/通道
SCOPE_ACCOUNT = "account"  # 该凭据/账号有问题：冷却 (channel, model)
SCOPE_PROVIDER = "provider"  # 上游整体过载：冷却整个 channel
SCOPE_TRANSIENT = "transient"  # 短暂抖动：短冷却

# ---- 冷却常量（毫秒） ------------------------------------------------ #
COOLDOWN_LONG_MS = 120_000  # 凭据级拒绝，不会自行恢复
COOLDOWN_SHORT_MS = 5_000  # 等一次刷新即可（9router 给 request-not-allowed 的值）
TRANSIENT_COOLDOWN_MS = 30_000  # 未匹配错误的兜底（9router TRANSIENT_COOLDOWN_MS）
BACKOFF_BASE_MS = 2_000  # 指数退避基步长
BACKOFF_MAX_MS = 300_000  # 退避上限 5 分钟
BACKOFF_MAX_LEVEL = 15  # 等级上限，防止 runaway
MAX_PROVIDER_COOLDOWN_MS = 1_800_000  # provider 自报 retry-after 的硬钳制（30 分钟）


def backoff_for(level: int) -> int:
    """某一退避等级对应的冷却毫秒数。

    level 0 与 1 都返回基步长——新账号首次撞限流必须得到一个不小于 2s 的
    冷却，否则会立刻重新进入轮换（9router getQuotaCooldown 的 Math.max(0,
    level-1) 守卫）。
    """
    numeric = int(level) if isinstance(level, int) else 0
    clamped = min(max(numeric, 1), BACKOFF_MAX_LEVEL)
    return int(min(BACKOFF_BASE_MS * 2 ** (clamped - 1), BACKOFF_MAX_MS))


@dataclass(frozen=True, slots=True)
class FailureVerdict:
    """一次上游失败的完整判定。"""

    kind: str
    scope: str
    retryable: bool  # 是否值得换下一个候选重放（我们的 failover 语义）
    cooldown_ms: int  # 固定冷却；backoff=True 时仅作下限
    backoff: bool = False  # 是否按等级指数退避（429/容量类）
    code: str = "UPSTREAM_ERROR"  # 对外错误码（GatewayError.code）
    http_status: int = 502  # 对客户端暴露的状态
    retry_after_ms: int = 0  # provider 自报的重置时间（已钳制 30min）


def _retry_after_ms(headers: Any) -> int:
    """从响应头取 Retry-After（秒），钳制到 30 分钟；缺失/非法返回 0。"""
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
    if seconds <= 0:
        return 0
    return min(seconds * 1000, MAX_PROVIDER_COOLDOWN_MS)


# 文本规则：大小写不敏感子串匹配，按顺序求值，先命中者赢。
# (模式, kind, scope, retryable, cooldown_ms, backoff, code, http_status)
_TEXT_RULES: tuple[tuple[str, str, str, bool, int, bool, str, int], ...] = (
    # 凭据彻底没了：之后每个请求同样失败，冷却账号而非模型
    ("no credentials", KIND_AUTH, SCOPE_ACCOUNT, True, COOLDOWN_LONG_MS, False, "CRED_INVALID", 502),
    # 刷新一次即可清除的拒绝——给 5s 而不是 2min，因为"等"就是解药
    ("request not allowed", KIND_PERMISSION, SCOPE_ACCOUNT, True, COOLDOWN_SHORT_MS, False, "CRED_INVALID", 502),
    # provider 把自己建的 payload 判坏：账号路由坏了，不是用户请求坏了
    (
        "improperly formed request",
        KIND_INVALID_REQUEST,
        SCOPE_ACCOUNT,
        False,
        COOLDOWN_LONG_MS,
        False,
        "UPSTREAM_ERROR",
        502,
    ),
    ("rate limit", KIND_RATE_LIMIT, SCOPE_ACCOUNT, True, 0, True, "CRED_RATE_LIMITED", 429),
    ("too many requests", KIND_RATE_LIMIT, SCOPE_ACCOUNT, True, 0, True, "CRED_RATE_LIMITED", 429),
    ("quota exceeded", KIND_RATE_LIMIT, SCOPE_ACCOUNT, True, 0, True, "CRED_RATE_LIMITED", 429),
    ("usage limit", KIND_RATE_LIMIT, SCOPE_ACCOUNT, True, 0, True, "CRED_RATE_LIMITED", 429),
    # 容量措辞是全站压力而非凭据耗尽：冷却 provider 范围
    ("capacity", KIND_OVERLOADED, SCOPE_PROVIDER, True, 0, True, "UPSTREAM_5XX", 502),
    ("overloaded", KIND_OVERLOADED, SCOPE_PROVIDER, True, 0, True, "UPSTREAM_5XX", 502),
)

# 状态码规则：仅当没有文本规则命中时求值。
_STATUS_RULES: dict[int, tuple[str, str, bool, int, bool, str, int]] = {
    401: (KIND_AUTH, SCOPE_ACCOUNT, True, COOLDOWN_LONG_MS, False, "CRED_INVALID", 502),
    402: (KIND_BILLING, SCOPE_ACCOUNT, True, COOLDOWN_LONG_MS, False, "CRED_INVALID", 502),
    403: (KIND_PERMISSION, SCOPE_ACCOUNT, True, COOLDOWN_LONG_MS, False, "CRED_INVALID", 502),
    404: (KIND_UNAVAILABLE, SCOPE_ACCOUNT, True, COOLDOWN_LONG_MS, False, "UPSTREAM_MODEL_GONE", 502),
    408: (KIND_TIMEOUT, SCOPE_TRANSIENT, True, TRANSIENT_COOLDOWN_MS, False, "UPSTREAM_TIMEOUT", 504),
    429: (KIND_RATE_LIMIT, SCOPE_ACCOUNT, True, 0, True, "CRED_RATE_LIMITED", 429),
}

#: 网关"点名拒绝该模型"的措辞（借鉴 dsh probe.js stateOf）。
#: ⚠️ 仅在状态码 < 500 时采信：503 的 reason phrase "Service Unavailable" 会被
#: 每个反代拼出来，把它当模型判定曾把一个可用模型踢出选择器整整一轮。
_NAMED_MODEL_RE = re.compile(
    r"unavailable|not supported|no such model|unknown model|invalid model|model is unavailable",
    re.IGNORECASE,
)

#: 地区门措辞（单独分组，不冷却——换出口即解）。
_REGION_RE = re.compile(r"not available in your country|region|geo", re.IGNORECASE)

#: 网关 HTML 错误页（WAF/body-size 限制在前面的反代），不是凭据问题。
_HTML_PAGE_RE = re.compile(r"^\s*<(!doctype|html[\s>])", re.IGNORECASE)


def _text_of(body: bytes | str | None) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")[:600]
    return str(body)[:600]


def classify_failure(status: int, body: bytes | str | None = None, retry_after_ms: int = 0) -> FailureVerdict:
    """把一次上游失败判定为 (kind, scope, retryable, cooldown)。

    判定顺序（dsh/9router 一致）：地区门 → 文本规则 → 点名文案 → 状态码规则
    → 按状态类别的兜底。retry_after_ms 在此统一钳制到 30 分钟。
    """
    retry_after_ms = min(max(0, int(retry_after_ms)), MAX_PROVIDER_COOLDOWN_MS)
    text = _text_of(body)
    flat = text.lower()

    # 地区门优先：它是出口的属性，不是账号或模型的故障，冷却没有意义。
    if _REGION_RE.search(flat):
        return FailureVerdict(KIND_PERMISSION, SCOPE_REQUEST, False, 0, code="REGION_BLOCKED", http_status=403)

    # 网关自身故障（5xx）时绝不采信文案——见 _NAMED_MODEL_RE 上的警告。
    gateway_trouble = status >= 500

    # 文本规则优先于状态码。
    for pattern, kind, scope, retryable, cooldown, backoff, code, http in _TEXT_RULES:
        if pattern in flat:
            verdict = FailureVerdict(kind, scope, retryable, cooldown, backoff, code, http)
            return _apply_retry_after(verdict, retry_after_ms)

    # 点名拒绝该模型：账号/通道不再路由它，长冷却（模型级）。
    if not gateway_trouble and _NAMED_MODEL_RE.search(flat):
        return FailureVerdict(
            KIND_UNAVAILABLE, SCOPE_ACCOUNT, True, COOLDOWN_LONG_MS, code="UPSTREAM_MODEL_GONE", http_status=status
        )

    if status in _STATUS_RULES:
        kind, scope, retryable, cooldown, backoff, code, http = _STATUS_RULES[status]
        verdict = FailureVerdict(kind, scope, retryable, cooldown, backoff, code, http)
        return _apply_retry_after(verdict, retry_after_ms)

    # 兜底：5xx 是上游抖动（短冷却、可转移）；其余 4xx 是请求自身的问题
    # （不冷却任何东西——重放同一 body 会复现拒绝）。
    if status >= 500:
        return FailureVerdict(
            KIND_UPSTREAM, SCOPE_TRANSIENT, True, TRANSIENT_COOLDOWN_MS, code="UPSTREAM_5XX", http_status=status
        )
    if _HTML_PAGE_RE.search(text):
        # 前置反代的 WAF/大小限制：既不是凭据也不是用户请求
        return FailureVerdict(
            KIND_UPSTREAM, SCOPE_TRANSIENT, True, TRANSIENT_COOLDOWN_MS, code="UPSTREAM_5XX", http_status=status
        )
    return FailureVerdict(
        KIND_INVALID_REQUEST, SCOPE_REQUEST, False, 0, code="UPSTREAM_ERROR", http_status=status
    )


def _apply_retry_after(verdict: FailureVerdict, retry_after_ms: int) -> FailureVerdict:
    """provider 自报的重置时间叠加到判定上（仍受 30 分钟钳制）。"""
    if retry_after_ms <= 0:
        return verdict
    return FailureVerdict(
        verdict.kind,
        verdict.scope,
        verdict.retryable,
        max(verdict.cooldown_ms, retry_after_ms),
        verdict.backoff,
        verdict.code,
        verdict.http_status,
        retry_after_ms,
    )
