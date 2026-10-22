"""OpenAI 兼容上游适配器 —— 覆盖 Cline / WorkBuddy / Zen Free 等 profile。

设计：上游若仍是 OpenAI Chat Completions 形态，只差路径/鉴权头/模型名/流式
约束时，用 Channel 配置解决，不新增适配器类（见设计文档 §4.2.2 成本控制点）。

协议要点均有实测日期与来源，记录在 docs/andy-gateway-本机免费模型接入-v1.1.md；
失败分类与冷却语义见 classify.py（移植自 dsh-our-free-model / 9router）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from harness.modules.gateway.classify import (
    SCOPE_ACCOUNT,
    SCOPE_PROVIDER,
    SCOPE_REQUEST,
    SCOPE_TRANSIENT,
    classify_failure,
)
from harness.modules.gateway.config import ChannelConfig
from harness.modules.gateway.contracts import (
    PROBE_GATEWAY_TROUBLE,
    PROBE_MODEL_DENIED,
    PROBE_OK,
    PROBE_REGION_BLOCKED,
    PROBE_UNKNOWN,
    AdapterCapabilities,
    Credential,
    GatewayError,
    ProviderAdapter,
    StreamOutcome,
    UpstreamRequest,
)
from harness.modules.gateway.credentials import cline_headers, workbuddy_headers, zen_headers

#: requires_tools 通道注入的指纹工具（Zen 免费档闸门要求声明）。
#: ⚠️ 与上游当前要求对齐：2026-10 实测 Zen v2 只校验 read/shell 两个名字的
#: 存在，五个全带是兼容余量；上游若收窄可在此删减，勿随意改名为"更好看"。
ZEN_CORE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": "Not available in this client.",
            "parameters": {
                "type": "object",
                "properties": {param: {"type": "string"}},
                "required": [param],
            },
        },
    }
    for name, param in (
        ("bash", "command"),
        ("edit", "filePath"),
        ("glob", "pattern"),
        ("grep", "pattern"),
        ("read", "filePath"),
    )
]


def fingerprint_tool_names() -> frozenset[str]:
    """requires_tools 通道注入的指纹工具名（流式改写据此抑制对这些占位工具的调用）。"""
    return frozenset(t["function"]["name"] for t in ZEN_CORE_TOOLS)


_COPY_FIELDS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "tools",
    "tool_choice",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "seed",
)


class OpenAILikeAdapter(ProviderAdapter):
    name = "openai-like"

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(streaming=True, tools=True, vision=True, reasoning=True)

    def build_request(
        self,
        channel: ChannelConfig,
        credential: Credential,
        body: dict[str, Any],
        *,
        session_key: str,
        force_stream: bool = True,
    ) -> UpstreamRequest:
        payload: dict[str, Any] = {
            "model": body.get("model", ""),
            "messages": body.get("messages") or [],
            "stream": True if force_stream else bool(body.get("stream")),
        }
        for key in _COPY_FIELDS:
            if key in body and body[key] is not None:
                payload[key] = body[key]

        if channel.requires_tools and not payload.get("tools"):
            payload["tools"] = ZEN_CORE_TOOLS

        headers: dict[str, str] = {"Content-Type": "application/json", **channel.headers}
        if channel.auth == "cline-cli":
            headers.update(cline_headers(credential.secret))
        elif channel.auth == "workbuddy-oauth":
            headers.update(workbuddy_headers(credential, session_key))
        elif channel.auth == "public":
            headers.update(zen_headers(session_key))
        elif channel.auth == "bearer":
            headers["Authorization"] = f"Bearer {credential.secret}"
        else:
            raise GatewayError("GW_INVALID_REQUEST", f"未知鉴权类型: {channel.auth}", 500)

        url = channel.base_url.rstrip("/") + channel.chat_path
        return UpstreamRequest(method="POST", url=url, headers=headers, json_body=payload)

    def parse_response(self, channel: ChannelConfig, data: dict[str, Any]) -> dict[str, Any]:
        if channel.unwrap and isinstance(data.get(channel.unwrap), dict):
            return cast(dict[str, Any], data[channel.unwrap])
        return data

    async def aggregate_stream(
        self,
        channel: ChannelConfig,
        lines: AsyncIterator[str],
        model: str,
        *,
        suppress_tool_names: frozenset[str] = frozenset(),
    ) -> StreamOutcome:
        content: list[str] = []
        reasoning: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish = "stop"
        last: dict[str, Any] = {}
        saw_tool_call = False

        async for line in lines:
            if not line or not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if raw == "[DONE]":
                break
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue
            last = chunk
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(str(delta["content"]))
                if delta.get("reasoning_content"):
                    reasoning.append(str(delta["reasoning_content"]))
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    name = str(fn.get("name") or "")
                    # 指纹工具抑制：客户端从未注册这些名字，回过去是个无力处理的错误
                    if name and name in suppress_tool_names:
                        saw_tool_call = True  # 仍记为"见过工具调用"（不触发空响应恢复）
                        continue
                    saw_tool_call = True
                    idx = int(tc.get("index", 0))
                    slot = tool_calls.setdefault(
                        idx,
                        {"id": tc.get("id", ""), "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if fn.get("name"):
                        slot["function"]["name"] += str(fn["name"])
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += str(fn["arguments"])
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        reasoning_text = "".join(reasoning)
        if reasoning_text:
            message["reasoning_content"] = reasoning_text
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        payload = {
            "id": last.get("id") or "chatcmpl-gateway",
            "object": "chat.completion",
            "created": last.get("created") or 0,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
        }
        return StreamOutcome(
            payload=payload,
            saw_reasoning=bool(reasoning_text),
            saw_text=bool(content),
            saw_tool_call=saw_tool_call,
            saw_finish=finish != "",
            reasoning_text=reasoning_text,
        )

    def map_error(self, status: int, body: bytes, retry_after_ms: int = 0) -> GatewayError:
        """上游错误 → 统一错误模型（分类规则见 classify.py）。"""
        verdict = classify_failure(status, body, retry_after_ms)
        scope = {
            SCOPE_REQUEST: "none",
            SCOPE_ACCOUNT: "credential",
            SCOPE_PROVIDER: "channel",
            SCOPE_TRANSIENT: "channel",
        }.get(verdict.scope, "none")
        text = body.decode("utf-8", errors="replace")[:300] if body else ""
        return GatewayError(
            verdict.code,
            f"上游返回 HTTP {status}: {text}" if text else f"上游返回 HTTP {status}",
            verdict.http_status,
            retryable=verdict.retryable,
            cooldown_scope=scope,
            cooldown_seconds=verdict.cooldown_ms // 1000,
            upstream_status=status,
            kind=verdict.kind,
            backoff=verdict.backoff,
            retry_after_ms=retry_after_ms,
        )

    async def probe(self, channel: ChannelConfig, credential: Credential) -> str:
        """主动探测，返回 PROBE_* 判定（语义分层见 contracts 与竞品分析 §4-P0-3）。

        判定的非对称性是故意的：一条 404 的探测地址说明配置过期（长冷却），而
        一次 503/超时只说明网关此刻过载（短冷却，模型保持可用）——把后者当前
        者会把可用模型无谓地藏起来。
        """
        url = channel.probe_url
        if not url and channel.probe_path:
            url = channel.base_url.rstrip("/") + channel.probe_path
        if not url:
            return PROBE_UNKNOWN  # 未配置探测地址：健康度由真实流量决定
        try:
            req = self.build_request(
                channel,
                credential,
                {"model": channel.models[0] if channel.models else "", "messages": [], "stream": False},
                session_key="probe",
                force_stream=False,
            )
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                r = await client.get(url, headers=req.headers)
        except Exception:  # noqa: BLE001 —— 探测失败只影响健康分，不抛给请求
            return PROBE_GATEWAY_TROUBLE  # 连不上是网关/网络问题，不是模型被拒

        body = r.content[:600]
        if r.status_code < 400:
            return PROBE_OK
        # 先看文案：地区门与"点名拒绝"都在 body 里，且与状态码无关
        verdict = classify_failure(r.status_code, body)
        if verdict.code == "REGION_BLOCKED":
            return PROBE_REGION_BLOCKED
        if verdict.code == "UPSTREAM_MODEL_GONE":
            return PROBE_MODEL_DENIED
        if r.status_code in (400, 404, 422):
            # 探测端点自身被拒（配置过期/路径变更）：按模型级拒绝处理
            return PROBE_MODEL_DENIED
        # 429 / 5xx / 其它：网关自身故障，短冷却
        return PROBE_GATEWAY_TROUBLE
