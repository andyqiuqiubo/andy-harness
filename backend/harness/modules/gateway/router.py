"""路由与健康 —— 候选过滤、策略排序、熔断冷却。

两阶段模型（设计文档 §4.3.1）：
1) 硬性过滤：启用 / 模型匹配 / 租户白名单 / 熔断状态；
2) 软性排序：weighted(SWRR) / round_robin / least_inflight / least_latency / sticky。

熔断为 (channel, model) 双粒度（借鉴 9router `modelLock_${model}` 扁平字段与
dsh route/classify.js 的 scope）：一个模型撞了配额墙只锁该模型在该通道上的路由，
同通道其他模型不受牵连；provider 级故障（全站过载）才冷却整条通道。退避等级
按 429 逐级升、成功后归零（classify.backoff_for）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from harness.modules.gateway.classify import FailureVerdict, backoff_for
from harness.modules.gateway.config import ConfigSnapshot

# 熔断键：("channel", cid) 为通道级；("model", cid, model) 为模型级。
_Key = tuple[str, ...]


@dataclass(slots=True)
class Candidate:
    channel: Any  # ChannelConfig
    upstream_model: str


class HealthTracker:
    """轻量熔断器：双粒度冷却 + 退避等级 + 在途计数 + EWMA 时延（进程内，重启即恢复）。"""

    def __init__(self, default_cooldown: int = 30) -> None:
        self.default_cooldown = default_cooldown
        self._cooldown_until: dict[_Key, float] = {}
        self._backoff: dict[_Key, int] = {}
        self._inflight: dict[str, int] = {}
        self._latency: dict[str, float] = {}
        self._lock = threading.Lock()

    # ---- 查询 ---------------------------------------------------------- #
    def available(self, channel_id: str, model: str = "") -> bool:
        """通道级放行，且（给了 model 时）该模型也未冷却。"""
        now = time.time()
        with self._lock:
            if self._cooldown_until.get(("channel", channel_id), 0.0) > now:
                return False
            if model and self._cooldown_until.get(("model", channel_id, model), 0.0) > now:
                return False
            return True

    def any_available(self, channel_id: str, models: list[str]) -> bool:
        """通道级放行且至少一个模型可用（供 /v1/models 与通道列表判断"是否整体不可用"）。

        注意：必须在本锁内直接查模型键，不能调用 available()——threading.Lock
        不可重入，嵌套获取会自死锁（任何模型的冷却都会把 channels_public 挂死）。
        """
        now = time.time()
        with self._lock:
            if self._cooldown_until.get(("channel", channel_id), 0.0) > now:
                return False
            if not models:
                return True
            return any(self._cooldown_until.get(("model", channel_id, m), 0.0) <= now for m in models)

    def cooldown_remaining(self, channel_id: str, model: str = "") -> int:
        """剩余冷却秒数（0 = 未冷却）。

        只统计已存在的冷却键：缺失键不是"很久以前就该冷却"，而是"从未冷却"，
        直接用 0.0 作默认值做差会得到一个巨大的负数（真机曾把 -1.79e9 显示给前端）。
        """
        now = time.time()
        with self._lock:
            keys: list[_Key] = [("channel", channel_id)]
            if model:
                keys.append(("model", channel_id, model))
            remaining = [int(self._cooldown_until[k] - now) for k in keys if k in self._cooldown_until]
            positive = [v for v in remaining if v > 0]
            return max(positive, default=0)

    def inflight(self, channel_id: str) -> int:
        with self._lock:
            return self._inflight.get(channel_id, 0)

    def latency(self, channel_id: str) -> float:
        with self._lock:
            return self._latency.get(channel_id, 0.0)

    # ---- 状态变更 ------------------------------------------------------ #
    def mark_failure(self, channel_id: str, cooldown_seconds: int | None = None, *, model: str = "") -> None:
        """显式冷却（传输层错误等未分类路径）；model 为空 = 通道级。"""
        seconds = cooldown_seconds if cooldown_seconds is not None else self.default_cooldown
        if seconds <= 0:
            return
        key: _Key = ("model", channel_id, model) if model else ("channel", channel_id)
        with self._lock:
            self._cooldown_until[key] = time.time() + seconds

    def mark_classified(self, channel_id: str, error: Any, *, model: str = "") -> None:
        """按 FailureVerdict 的 scope 与退避语义冷却。

        - scope=request：不冷却任何东西（重放同一 body 会复现拒绝，冷却健康
          账号只会隐藏真实原因）；
        - scope=account：冷却 (channel, model)——一个模型的墙不推倒同通道的
          其他模型；
        - scope=provider/transient：冷却整条通道；
        - backoff：429/容量类按等级指数退避，成功后由 mark_success 归零；
          provider 自报的 retry-after 取 max 后叠加（已在 classify 内钳制 30min）。
        """
        verdict = error if isinstance(error, FailureVerdict) else _verdict_of(error)
        if verdict is None or verdict.scope == "request":
            return

        if verdict.scope == "account":
            keys: list[_Key] = [("model", channel_id, model)] if model else [("channel", channel_id)]
        else:  # provider / transient
            keys = [("channel", channel_id)]

        with self._lock:
            seconds = verdict.cooldown_ms / 1000
            if verdict.backoff:
                for key in keys:
                    self._backoff[key] = self._backoff.get(key, 0) + 1
                seconds = max(seconds, backoff_for(self._backoff[keys[0]]) / 1000)
            if verdict.retry_after_ms:
                seconds = max(seconds, verdict.retry_after_ms / 1000)
            if seconds <= 0:
                return
            until = time.time() + seconds
            for key in keys:
                self._cooldown_until[key] = until

    def mark_success(self, channel_id: str, model: str = "", latency_ms: int = 0) -> None:
        with self._lock:
            self._cooldown_until.pop(("channel", channel_id), None)
            if model:
                self._cooldown_until.pop(("model", channel_id, model), None)
                self._backoff.pop(("model", channel_id, model), None)
            self._backoff.pop(("channel", channel_id), None)
            if latency_ms > 0:
                prev = self._latency.get(channel_id)
                self._latency[channel_id] = latency_ms if prev is None else prev * 0.8 + latency_ms * 0.2

    def begin(self, channel_id: str) -> None:
        with self._lock:
            self._inflight[channel_id] = self._inflight.get(channel_id, 0) + 1

    def end(self, channel_id: str) -> None:
        with self._lock:
            self._inflight[channel_id] = max(0, self._inflight.get(channel_id, 0) - 1)


def _verdict_of(error: Any) -> FailureVerdict | None:
    """从 GatewayError 还原判定（适配器在 map_error 里已做初步分类）。"""
    kind = getattr(error, "kind", "") or ""
    scope = {
        "credential": "account",
        "channel": "provider",
        "none": "request",
    }.get(getattr(error, "cooldown_scope", ""), "")
    if not kind and not scope:
        return None
    return FailureVerdict(
        kind=kind or "unknown",
        scope=scope or "transient",
        retryable=bool(getattr(error, "retryable", False)),
        cooldown_ms=int(getattr(error, "cooldown_seconds", 0) or 0) * 1000,
        backoff=bool(getattr(error, "backoff", False)),
        retry_after_ms=int(getattr(error, "retry_after_ms", 0) or 0),
    )


class Router:
    """无状态路由（除策略计数器与健康状态）。"""

    def __init__(self, health: HealthTracker) -> None:
        self.health = health
        self._rr = 0
        self._swrr: dict[str, int] = {}
        self._lock = threading.Lock()

    def candidates(
        self,
        snapshot: ConfigSnapshot,
        model: str,
        *,
        allowed_models: list[str] | None = None,
        allowed_channels: list[str] | None = None,
        strategy: str | None = None,
        session_key: str = "",
    ) -> list[Candidate]:
        """返回有序候选；空列表表示无可用通道。"""
        wanted = self.expand(snapshot, model)
        out: list[Candidate] = []
        seen: set[tuple[str, str]] = set()
        for ch, upstream_model, requested in wanted:
            if not ch.enabled:
                continue
            if not self.tenant_allows(ch, requested, allowed_models, allowed_channels):
                continue
            # 双粒度熔断：通道级或该上游模型级冷却都不可用
            if not self.health.available(ch.id, upstream_model):
                continue
            key = (ch.id, upstream_model)
            if key in seen:
                continue
            seen.add(key)
            out.append(Candidate(ch, upstream_model))

        if len(out) <= 1 or model in snapshot.aliases:
            return out  # 别名本身自带优先级，不再重排

        chosen = strategy or snapshot.defaults.strategy
        return self._order(out, chosen, session_key or model)

    # ---- 内部 ---------------------------------------------------------- #
    def expand(self, snapshot: ConfigSnapshot, model: str) -> list[tuple[Any, str, str]]:
        if model in snapshot.aliases:
            out: list[tuple[Any, str, str]] = []
            for member in snapshot.aliases[model]:
                out.extend(self.expand(snapshot, member))
            return out

        if "/" in model:
            head, rest = model.split("/", 1)
            ch = snapshot.channels.get(head)
            if ch is not None and rest in ch.models:
                return [(ch, ch.model_map.get(rest, rest), model)]

        for ch in snapshot.channel_list():
            if model in ch.models:
                return [(ch, ch.model_map.get(model, model), model)]
        return []

    @staticmethod
    def tenant_allows(
        ch: Any,
        model: str,
        allowed_models: list[str] | None,
        allowed_channels: list[str] | None,
    ) -> bool:
        models = allowed_models or ["*"]
        channels = allowed_channels or ["*"]
        if "*" not in channels and ch.id not in channels:
            return False
        if "*" in models:
            return True
        bare = model.split("/", 1)[1] if "/" in model and model.split("/", 1)[0] == ch.id else model
        return model in models or bare in models or f"{ch.id}/{bare}" in models

    def _order(self, items: list[Candidate], strategy: str, session_key: str) -> list[Candidate]:
        if strategy == "round_robin":
            with self._lock:
                self._rr += 1
                shift = self._rr % len(items)
            return items[shift:] + items[:shift]
        if strategy == "least_inflight":
            return sorted(items, key=lambda c: self.health.inflight(c.channel.id))
        if strategy == "least_latency":
            return sorted(items, key=lambda c: self.health.latency(c.channel.id))
        if strategy == "sticky":
            idx = hash(session_key) % len(items)
            return items[idx:] + items[:idx]
        # weighted：平滑加权轮询（nginx SWRR）
        with self._lock:
            total = sum(max(1, c.channel.weight) for c in items)
            for c in items:
                self._swrr[c.channel.id] = self._swrr.get(c.channel.id, 0) + max(1, c.channel.weight)
            best = max(items, key=lambda c: self._swrr.get(c.channel.id, 0))
            self._swrr[best.channel.id] = self._swrr.get(best.channel.id, 0) - total
        return [best] + [c for c in items if c is not best]
