"""Qoder CN 适配器 —— COSY 信封 + 私有 base64 body + agent SSE 解析。

协议移植自本机 magpie 插件
`~/.config/magpie/plugins/node_modules/@magpie-community/opencode-qoder-auth`
（v0.2.27，MIT），实测于 2026-10-22：`GET /algo/api/v2/model/list?Encode=1` 携带
COSY 信封返回 200 与完整模型清单（12 个 key，其中 qfmodel / dfmodel 的
price_factor=0 即免费）。

与 OpenAI 兼容通道的三处关键差异（踩坑记录）：
1. **URL 形态**：`base_url` 是站点网关根（如 https://gateway.qoder.com.cn），
   聊天端点是 `/algo/api/v2/service/pro/sse/agent_chat_generation` 加查询参数
   `FetchKeys=llm_model_result&AgentId=agent_common&Encode=1`；
2. **body 不是 JSON**：明文 JSON 先经 Qoder 私有 base64（打乱字母表 + `$` 补位 +
   首尾三段互换）编码成 wire 串再发，签名也对这个 wire 串计算；
3. **SSE 形态**：每行 `data:` 的载荷是信封 `{statusCodeValue, body}`，`body` 才是
   OpenAI 形状的 chunk（且 body 可能是字符串）；外层 statusCodeValue≠200 是失败。

模型 key → 网关 model 名：通道配置里写 `qoder-cn/qfmodel` 这样的前缀形式，
适配器取出裸 key（qfmodel）作为 `X-Model-Key`，并把模型清单里的原始配置
（model_config）带给上游。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from harness.modules.gateway.adapters.qoder_cosy import (
    CosyAccount,
    build_agent_body,
    cosy_headers,
    decode_body,
    encode_body,
)
from harness.modules.gateway.classify import classify_failure
from harness.modules.gateway.config import ChannelConfig
from harness.modules.gateway.contracts import (
    PROBE_GATEWAY_TROUBLE,
    PROBE_MODEL_DENIED,
    PROBE_OK,
    AdapterCapabilities,
    Credential,
    GatewayError,
    ProviderAdapter,
    StreamOutcome,
    UpstreamRequest,
)

logger = logging.getLogger("harness.gateway.qoder")

#: 聊天端点（Qoder CN 网关）。
CHAT_PATH = (
    "/algo/api/v2/service/pro/sse/agent_chat_generation"
    "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
)
#: 模型清单端点。
MODELS_PATH = "/algo/api/v2/model/list?Encode=1"

#: 免费模型（price_factor == 0）。会话页默认只暴露这些。
FREE_KEYS = ("qfmodel", "dfmodel")

#: Qoder 的排队码（参考实现的 QUEUED）：模型排队中，稍后重试即可。
QUEUED_CODE = "10605"

#: Qoder 的额度类错误码（参考实现的 QUOTA_CODES）。
QUOTA_CODES = frozenset({"110", "112", "113", "114", "115", "116", "117", "118", "119", "122"})


class QoderCosyAdapter(ProviderAdapter):
    """Qoder CN（qoder.com.cn）适配器。"""

    name = "qoder-cosy"

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(streaming=True, tools=True, vision=True, reasoning=True)

    # ---- 请求构建 ------------------------------------------------------ #
    def build_request(
        self,
        channel: ChannelConfig,
        credential: Credential,
        body: dict[str, Any],
        *,
        session_key: str,
        force_stream: bool = True,
    ) -> UpstreamRequest:
        account = _account_from(credential)
        model_raw = str(body.get("model") or "")
        model_key = model_raw.split("/", 1)[1] if "/" in model_raw else model_raw
        path = channel.chat_path or CHAT_PATH
        url = channel.base_url.rstrip("/") + path

        messages = cast(list[dict[str, Any]], body.get("messages") or [])
        model_config = self._model_config(channel, model_key)
        agent_body = build_agent_body(
            messages,
            model_config,
            model_key=model_key,
            max_tokens=int(body.get("max_tokens") or body.get("max_completion_tokens") or 32000),
            enable_thinking=True,
            session_id=session_key or "",
            tools=cast(list[dict[str, Any]] | None, body.get("tools")),
        )
        plain = json.dumps(agent_body, ensure_ascii=False, separators=(",", ":"))
        wire = encode_body(plain)
        headers = cosy_headers(
            url,
            account,
            wire,
            extra={
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Model-Key": model_key,
                "X-Model-Source": str(model_config.get("source") or "system"),
            },
        )
        return UpstreamRequest(method="POST", url=url, headers=headers, raw_body=wire.encode("utf-8"))

    def _model_config(self, channel: ChannelConfig, model_key: str) -> dict[str, Any]:
        """取模型清单里该 key 的原始配置（上游要求带 model_config）。"""
        cached = getattr(self, "_listing", None)
        if isinstance(cached, dict):
            for model in _scene_models(cached):
                if str(model.get("key")) == model_key:
                    return model
        # 清单未就绪：退回最小配置（上游会自行纠正）
        return {"key": model_key, "source": "system", "enable": True}

    def parse_response(self, channel: ChannelConfig, data: dict[str, Any]) -> dict[str, Any]:
        return data

    # ---- SSE 聚合 ------------------------------------------------------ #
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
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                env = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(env, dict):
                continue
            status = env.get("statusCodeValue")
            if status is not None and _as_int(status) != 200:
                # 外层信封报错：Qoder 的语义在 body 里（排队/配额/签名错…）
                inner = env.get("body")
                detail = inner if isinstance(inner, str) else json.dumps(env, ensure_ascii=False)
                raise self._envelope_error(_as_int(status), detail)
            inner = env.get("body")
            if not isinstance(inner, str):
                continue
            text = inner.strip()
            if text == "[DONE]":
                break
            try:
                chunk = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
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
                    if name and name in suppress_tool_names:
                        saw_tool_call = True
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
            "id": last.get("id") or "chatcmpl-qoder",
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
            saw_finish=True,
            reasoning_text=reasoning_text,
        )

    def normalize_stream_line(self, line: str) -> list[str]:
        """Qoder 信封 SSE → 标准 OpenAI SSE。

        实测上游形态（2026-10-22）：
        ``data:{"headers":{...},"body":"{\\"choices\\":[...]}","statusCodeValue":200}``
        —— `data:` 后**没有空格**，且真正的 chunk 是 `body` 里的一层 JSON 字符串。
        网关若原样透传，进程内 provider（只认 `data: `）会跳过所有行，
        页面表现为"提问后没有任何反馈"；外部 `/v1` 客户端也解析不了信封。
        这里统一剥壳：`body` 原样作为 `data: {chunk}` 输出。
        """
        if not line.startswith("data:"):
            # 上游自定义事件（event:finish 等）：对 OpenAI 客户端无意义，丢弃
            return []
        raw = line[5:].strip()
        if not raw:
            return []
        if raw == "[DONE]":
            return ["data: [DONE]"]
        try:
            env = json.loads(raw)
        except json.JSONDecodeError:
            # 已是裸 OpenAI chunk（少数路径）：补规范前缀
            return [f"data: {raw}"]
        if not isinstance(env, dict):
            return []
        status = env.get("statusCodeValue")
        if status is not None and _as_int(status) != 200:
            err = self._envelope_error(_as_int(status), _envelope_detail(env))
            payload = {"error": {"message": err.message, "code": err.code, "type": err.code}}
            return [f"data: {json.dumps(payload, ensure_ascii=False)}"]
        inner = env.get("body")
        if not isinstance(inner, str):
            return []  # 纯元信息行（firstTokenDuration 等）
        text = inner.strip()
        if not text:
            return []
        if text == "[DONE]":
            return ["data: [DONE]"]
        try:
            chunk = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(chunk, dict):
            return []
        return [f"data: {json.dumps(chunk, ensure_ascii=False, separators=(',', ':'))}"]

    def _envelope_error(self, status: int, detail: str) -> GatewayError:
        """把 Qoder 的信封错误翻译成语义化 GatewayError。

        关键形态（实测 2026-10-22）：排队中时外层 status=403，body 里的 message
        又是嵌套 JSON：
        `{"code":"10605","message":"{\\"isQueued\\":true,...,\\"retryAfterSeconds\\":30}"}`
        —— 这是"稍后重试"的信号（503 + Retry-After），不是账号或请求的错；
        把它当 403 会误冷却账号，也会让前端显示永久不可用。
        """
        # 实测形态（2026-10-22 抓包）：Qoder 的 message 是**多层嵌套 JSON 字符串**，
        # 排队码不在第一层——第一层 code 是 "403"，第二层才是 "10605"，第三层才有
        # isQueued。只解一层会把排队误判成 403 凭据错（长冷却账号 + 前端标不可用）。
        chain: list[dict[str, Any]] = []
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict):
                chain = _unwrap_chain(parsed)
        except (json.JSONDecodeError, AttributeError, TypeError):
            chain = []
        codes = {str(n.get("code")) for n in chain if n.get("code") is not None}
        queue = next((n for n in chain if n.get("isQueued") is True), None)
        # 最深层的可读文案（跳过仍是 JSON 字符串的 message）
        message = detail
        for node in reversed(chain):
            msg = node.get("message")
            if isinstance(msg, str) and not msg.strip().startswith("{"):
                message = msg
                break

        if QUEUED_CODE in codes or queue is not None:
            wait = (queue or {}).get("retryAfterSeconds")
            try:
                seconds = max(1, int(wait or 0))
            except (TypeError, ValueError):
                seconds = 30
            return GatewayError(
                "UPSTREAM_BUSY",
                f"Qoder 模型排队中，约 {seconds}s 后可重试",
                503,
                retryable=True,
                cooldown_scope="none",
                retry_after_ms=seconds * 1000,
            )

        if codes & QUOTA_CODES:
            return GatewayError(
                "CRED_RATE_LIMITED",
                f"Qoder 额度/配额用尽: {message[:200]}",
                429,
                retryable=True,
                cooldown_scope="credential",
                cooldown_seconds=120,
            )
        if status in (401, 403):
            return GatewayError(
                "CRED_INVALID",
                f"Qoder 拒绝该请求（登录态或权限）: {message[:200]}",
                502,
                retryable=False,
                cooldown_scope="credential",
                cooldown_seconds=600,
            )
        return GatewayError(
            "UPSTREAM_ERROR",
            f"Qoder 返回错误: {message[:200]}",
            502 if status >= 500 else status,
            retryable=True,
            cooldown_scope="channel",
        )

    def map_error(self, status: int, body: bytes, retry_after_ms: int = 0) -> GatewayError:
        # Qoder 排队/配额/签名错常以 HTTP 4xx（实测 403）承载「扁平信封」
        # `{"code":"10605","message":"..."}` —— 此时必须解析信封再翻译，否则
        # 排队（10605）会被误判成 403 凭据错并长冷却账号，前端会把它标成
        # "永久不可用"。SSE 信封（HTTP 200 + {statusCodeValue}）走 aggregate_stream
        # 已在内部交给 _envelope_error；这里补齐 HTTP 错误态的同款解析。
        text = body.decode("utf-8", errors="replace")[:2000] if body else ""
        parsed = _try_envelope(text)
        if parsed is not None:
            return self._envelope_error(status, text)
        verdict = classify_failure(status, body, retry_after_ms)
        scope = {
            "request": "none",
            "account": "credential",
            "provider": "channel",
            "transient": "channel",
        }.get(verdict.scope, "none")
        text = body.decode("utf-8", errors="replace")[:300] if body else ""
        return GatewayError(
            verdict.code,
            f"Qoder 返回 HTTP {status}: {text}" if text else f"Qoder 返回 HTTP {status}",
            verdict.http_status,
            retryable=verdict.retryable,
            cooldown_scope=scope,
            cooldown_seconds=verdict.cooldown_ms // 1000,
            upstream_status=status,
            kind=verdict.kind,
            backoff=verdict.backoff,
            retry_after_ms=retry_after_ms,
        )

    # ---- 准备 ---------------------------------------------------------- #
    async def prepare(self, channel: ChannelConfig, credential: Credential) -> None:
        """预热模型清单（model_config 的来源）。

        实测：model_config 缺字段时上游不返回内容（客户端 read timeout），
        所以聊天前必须先拿到清单里的原始模型配置。
        """
        if isinstance(getattr(self, "_listing", None), dict):
            return
        listing = await self.fetch_listing(channel, credential)
        if listing is not None:
            self._listing = listing

    # ---- 探测 ---------------------------------------------------------- #
    async def catalog_request(self, channel: ChannelConfig, credential: Credential) -> tuple[str, dict[str, str]]:
        """清单探测请求：COSY 签名覆盖路径，必须按目录端点重新签名。"""
        account = _account_from(credential)
        url = channel.base_url.rstrip("/") + MODELS_PATH
        return url, cosy_headers(url, account, "")

    async def probe(self, channel: ChannelConfig, credential: Credential) -> str:
        """用模型清单做探测：拿到清单 = 网关可达且凭据有效。"""
        try:
            listing = await self.fetch_listing(channel, credential)
        except Exception:  # noqa: BLE001 —— 探测失败只影响健康分
            return PROBE_GATEWAY_TROUBLE
        if listing is None:
            return PROBE_GATEWAY_TROUBLE
        self._listing = listing
        keys = {str(m.get("key")) for m in _scene_models(listing)}
        if keys and not (keys & set(FREE_KEYS)):
            # 清单里连免费模型都没有：这个账号看不到免费档
            return PROBE_MODEL_DENIED
        return PROBE_OK

    async def fetch_listing(self, channel: ChannelConfig, credential: Credential) -> dict[str, Any] | None:
        """拉模型清单（带 COSY 信封）。返回 None 表示无从判定。"""
        account = _account_from(credential)
        url = channel.base_url.rstrip("/") + MODELS_PATH
        headers = cosy_headers(url, account, "")
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                response = await client.get(url, headers=headers)
        except Exception:  # noqa: BLE001
            return None
        if response.status_code >= 400:
            return None
        text = response.text
        try:
            # Encode=1 时 Qoder 返回私有 base64；有些路径直接给 JSON
            if text.lstrip()[:1] in "{[":
                data = json.loads(text)
            else:
                data = json.loads(decode_body(text))
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None


def _unwrap_chain(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """沿 ``message`` 的嵌套 JSON 字符串逐层解包，返回各层 dict。

    Qoder 的排队错误形如（实测）：
    ``{code:"403", message:"{code:\"10605\", message:\"{\\\\\"isQueued\\\\\":true,...}\"}"}``
    —— 排队码/``isQueued`` 在第三层，只解一层会漏判。
    """
    chain: list[dict[str, Any]] = []
    node: Any = parsed
    for _ in range(8):  # 防御性上限，避免恶意深嵌套
        if not isinstance(node, dict):
            break
        chain.append(node)
        msg = node.get("message")
        if isinstance(msg, str) and msg.strip().startswith("{"):
            try:
                node = json.loads(msg.strip())
            except json.JSONDecodeError:
                break
        else:
            break
    return chain


def _envelope_detail(env: dict[str, Any]) -> str:
    """取信封里的错误正文（body 是字符串则用它，否则整体序列化）。"""
    inner = env.get("body")
    return inner if isinstance(inner, str) else json.dumps(env, ensure_ascii=False)


def _try_envelope(text: str) -> dict[str, Any] | None:
    """若 body 是 Qoder 扁平信封 `{"code":..., "message":...}` 则返回其 dict，否则 None。"""
    text = (text or "").strip()
    if not text or text[:1] not in "{":
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and "code" in parsed:
        return parsed
    return None


def _as_int(value: Any) -> int:
    """宽松转 int（Qoder 的 statusCodeValue 可能是数字或数字字符串）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _account_from(credential: Credential) -> CosyAccount:
    meta = credential.meta or {}
    return CosyAccount(
        uid=str(meta.get("uid") or ""),
        name=str(meta.get("name") or ""),
        email=str(meta.get("email") or ""),
        access=credential.secret,
        machine_id=str(meta.get("machine_id") or ""),
    )


def _scene_models(listing: dict[str, Any]) -> list[dict[str, Any]]:
    """app 段 + chat 段合并（chat 段补齐 app 没有的 key）。"""
    out: list[dict[str, Any]] = [m for m in (listing.get("app") or []) if isinstance(m, dict)]
    for model in listing.get("chat") or []:
        if not isinstance(model, dict):
            continue
        if not any(m.get("key") == model.get("key") for m in out):
            out.append(model)
    return out


def free_model_keys(listing: dict[str, Any]) -> list[str]:
    """清单里的免费模型 key（price_factor == 0）。"""
    out: list[str] = []
    for model in _scene_models(listing):
        raw_factor = model.get("price_factor")
        if raw_factor is None:
            continue
        try:
            factor = float(raw_factor)
        except (TypeError, ValueError):
            continue
        if factor == 0.0:
            key = str(model.get("key") or "")
            if key:
                out.append(key)
    return out
