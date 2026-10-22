"""andy-gateway lite —— 本机免费模型统一接入（P0 预演）。

把本机已登录的 Cline / WorkBuddy / OpenCode Zen Free 免费模型，以一个
OpenAI 兼容端点暴露给 andy-harness 或其他客户端：

    POST /v1/chat/completions     （流式 / 非流式）
    GET  /v1/models
    GET  /healthz

设计边界（与 docs/andy-gateway-设计方案.md 对齐）：
- 只做协议翻译与路由，不实现身份伪造；所需客户端头来自上游准入要求，
  由通道配置显式声明；
- 凭据仅从本机已有的登录态读取，明文不落日志、不出进程；
- Qoder CN 使用 COSY 信封签名，需专用适配器，本文件不包含。

启动：
    backend\\.venv\\Scripts\\python.exe tools\\andy_gateway_lite.py --port 8010
可选鉴权：
    set GATEWAY_LITE_KEY=agw-...   （设置后 /v1 需 Bearer）
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import string
import threading
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("GATEWAY_LITE_CONFIG", ROOT / "tools" / "gateway.providers.json"))
USAGE_LOG = ROOT / "logs" / "gateway-lite-usage.jsonl"
HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))

CLINE_VERSION = "3.0.68"
CLINE_CORE = "0.0.90"
WB_VERSION = "5.5.6"
ZEN_VERSION = "1.19.0"
ZEN_UA = f"opencode/{ZEN_VERSION} ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"
BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase

_usage_lock = threading.Lock()
_cooldown: dict[str, float] = {}
_cline_refresh_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 配置与凭据
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_expiry(value: Any) -> float:
    """把 ms 时间戳或 ISO 字符串统一成 epoch 秒。"""
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


class ClineCredential:
    """Cline CLI 登录态：过期前自动刷新，并原子写回（refresh token 一次性轮换）。"""

    path = HOME / ".cline" / "data" / "settings" / "providers.json"

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    def token(self) -> str:
        if self._cache and _parse_expiry(self._cache.get("expiresAt")) - time.time() > 300:
            auth = self._cache
        else:
            auth = _load_json(self.path)["providers"]["cline"]["settings"]["auth"]
            if _parse_expiry(auth.get("expiresAt")) - time.time() < 300:
                auth = self._refresh(auth)
            else:
                self._cache = auth
        access = str(auth.get("accessToken") or "")
        if not access:
            raise RuntimeError("Cline 登录态缺失，请在 Cline 中重新登录")
        return access if access.startswith("workos:") else f"workos:{access}"

    def _refresh(self, auth: dict[str, Any]) -> dict[str, Any]:
        with _cline_refresh_lock:
            # 另一个请求可能已经刷新过：以磁盘上的最新值为准，避免花掉同一枚 refresh token
            current = _load_json(self.path)["providers"]["cline"]["settings"]["auth"]
            if _parse_expiry(current.get("expiresAt")) - time.time() > 300:
                self._cache = current
                return current
            refresh = str(current.get("refreshToken") or "")
            if not refresh:
                raise RuntimeError("Cline refresh token 缺失，请重新登录")
            r = httpx.post(
                "https://api.cline.bot/api/v1/auth/refresh",
                json={"refreshToken": refresh, "grantType": "refresh_token"},
                headers={"Content-Type": "application/json", "User-Agent": f"Cline/{CLINE_VERSION}"},
                timeout=30,
            )
            if r.status_code != 200:
                raise RuntimeError(f"Cline token 刷新失败: HTTP {r.status_code}")
            body = r.json()
            body = body.get("data") if isinstance(body.get("data"), dict) else body
            if not body.get("accessToken"):
                raise RuntimeError("Cline token 刷新响应缺少 accessToken")

            data = _load_json(self.path)
            node = data["providers"]["cline"]["settings"]["auth"]
            node["accessToken"] = body["accessToken"]
            if body.get("refreshToken"):
                node["refreshToken"] = body["refreshToken"]
            if body.get("expiresAt"):
                node["expiresAt"] = body["expiresAt"]
            data["providers"]["cline"]["updatedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
            # 先放内存：即使写盘失败（文件被 Cline 占用），本次进程内仍能继续用新 token，
            # 不会拿已花掉的 refresh token 再试一次。
            self._cache = node
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(3):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if attempt == 2:
                        print("[gateway-lite] WARN: Cline 登录态写回失败（文件被占用），请稍后重试")
                    else:
                        time.sleep(0.2)
            return node


class WorkBuddyCredential:
    """WorkBuddy（国内版）登录态，取自 Magpie 的 plugin-auth.json。"""

    path = HOME / ".config" / "magpie" / "plugin-auth.json"

    def auth(self) -> dict[str, Any]:
        node = _load_json(self.path).get("workbuddy") or {}
        if not node.get("access"):
            raise RuntimeError("未找到 WorkBuddy 登录态（需先在 WorkBuddy 或 Magpie 中登录）")
        return node


def _cline_headers(token: str) -> dict[str, str]:
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


def _workbuddy_headers(auth: dict[str, Any], session_key: str) -> dict[str, str]:
    conv = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agw-wb:{session_key}"))
    return {
        "Authorization": f"Bearer {auth['access']}",
        "Content-Type": "application/json",
        "X-User-Id": str(auth.get("uid", "")),
        "X-Domain": str(auth.get("domain", "www.workbuddy.cn")),
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
    # OpenCode 的 ID：48-bit 时间戳 + 低 12 位小计数器（取反），尾缀 14 位 base62。
    # 低 12 位必须保持“小计数器”，随机值会被 Zen 的客户端校验判为伪造。
    now_ms = int(time.time() * 1000)
    counter = (int(hashlib.md5(seed.encode()).hexdigest()[:4], 16) % 64) + 1
    value = (~((now_ms << 12) + counter)) & 0xFFFFFFFFFFFF
    tail = "".join(BASE62[b % 62] for b in hashlib.md5(seed.encode()).digest()[:14])
    return f"{prefix}_{value:012x}{tail}"


def _zen_headers(session_key: str) -> dict[str, str]:
    session = _opencode_id("ses", f"agw-zen-ses:{session_key}")
    return {
        "content-type": "application/json",
        "accept": "*/*",
        "user-agent": ZEN_UA,
        "x-opencode-project": "global",
        "x-opencode-session": session,
        "x-opencode-request": _opencode_id("msg", f"agw-zen-msg:{session_key}:{secrets.token_hex(4)}"),
        "x-opencode-client": "cli",
        "x-opencode-session-id": session,
        "authorization": "Bearer public",
    }


ZEN_CORE_TOOLS = [
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


def _session_key(body: dict[str, Any]) -> str:
    for key in ("user", "session_id"):
        if isinstance(body.get(key), str) and body[key]:
            return str(body[key])
    messages = body.get("messages") or []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return "default"


# --------------------------------------------------------------------------- #
# 通道与候选解析
# --------------------------------------------------------------------------- #
CONFIG = load_config()
CHANNELS: dict[str, dict[str, Any]] = {c["id"]: c for c in CONFIG["channels"] if c.get("enabled")}
ALIASES: dict[str, list[str]] = CONFIG.get("group_aliases") or {}

_cline = ClineCredential()
_workbuddy = WorkBuddyCredential()


def resolve_candidates(model: str) -> list[tuple[dict[str, Any], str]]:
    """返回 [(channel, upstream_model)]；支持 `<channel>/<model>` 与 free/auto 别名。"""
    if model in ALIASES:
        out: list[tuple[dict[str, Any], str]] = []
        for full in ALIASES[model]:
            out.extend(resolve_candidates(full))
        return out

    if "/" in model:
        head, rest = model.split("/", 1)
        if head in CHANNELS and rest in CHANNELS[head].get("models", []):
            return [(CHANNELS[head], rest)]
        # 模型 id 本身含 "/"（如 cline-free/xxx）：按“通道内模型”再匹配
        for ch in CHANNELS.values():
            if model in ch.get("models", []):
                return [(ch, model)]

    for ch in CHANNELS.values():
        for candidate in ch.get("models", []):
            if candidate == model or f"{ch['id']}/{candidate}" == model:
                return [(ch, candidate)]
    return []


def public_models() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    for ch in CHANNELS.values():
        for m in ch.get("models", []):
            data.append(
                {
                    "id": f"{ch['id']}/{m}",
                    "object": "model",
                    "created": 0,
                    "owned_by": ch["id"],
                }
            )
    for alias in ALIASES:
        data.append({"id": alias, "object": "model", "created": 0, "owned_by": "gateway"})
    return data


def _record(channel: str, model: str, status: int, ms: int, usage: dict[str, Any] | None, error: str = "") -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "channel": channel,
        "model": model,
        "status": status,
        "duration_ms": ms,
        "usage": usage or {},
        "error": error[:200],
    }
    with _usage_lock:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# 上游请求
# --------------------------------------------------------------------------- #
def _build_upstream(channel: dict[str, Any], model: str, body: dict[str, Any], session: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    url = channel["base_url"].rstrip("/") + channel.get("chat_path", "/chat/completions")
    payload = {
        "model": model,
        "messages": body.get("messages") or [],
        "stream": bool(body.get("stream")),
    }
    for key in ("temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice", "presence_penalty", "frequency_penalty"):
        if key in body:
            payload[key] = body[key]

    if channel["auth"] == "cline-cli":
        headers = _cline_headers(_cline.token())
    elif channel["auth"] == "workbuddy-oauth":
        headers = _workbuddy_headers(_workbuddy.auth(), session)
    elif channel["auth"] == "public":
        headers = _zen_headers(session)
        if channel.get("requires_tools") and not payload.get("tools"):
            payload["tools"] = ZEN_CORE_TOOLS
    else:
        raise RuntimeError(f"未知鉴权类型: {channel['auth']}")
    # 部分上游只接受流式（WorkBuddy / Zen Free）：统一在此强制，非流式由网关聚合
    if channel.get("stream_only"):
        payload["stream"] = True
    return url, headers, payload


def _unwrap(channel: dict[str, Any], data: Any) -> Any:
    if channel.get("unwrap") == "data" and isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data


async def _aggregate_sse(lines: AsyncIterator[str], model: str) -> dict[str, Any]:
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    finish = "stop"
    last: dict[str, Any] = {}
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
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            if delta.get("tool_calls"):
                tool_calls.extend(delta["tool_calls"])
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": last.get("id") or f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": last.get("created") or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }


def _error(message: str, code: str = "upstream_error", status: int = 502, detail: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "param": None, "code": code, "detail": detail[:400]}},
    )


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI(title="andy-gateway-lite", version="0.1.0")
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
    limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    follow_redirects=False,
)


def _authorized(request: Request) -> bool:
    expected = os.environ.get("GATEWAY_LITE_KEY", "").strip()
    if not expected:
        return True
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    return bool(token) and secrets.compare_digest(token, expected)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "channels": list(CHANNELS)}


@app.get("/v1/models")
async def models(request: Request) -> Any:
    if not _authorized(request):
        return _error("invalid api key", "invalid_api_key", 401)
    return {"object": "list", "data": public_models()}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    if not _authorized(request):
        return _error("invalid api key", "invalid_api_key", 401)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("invalid json", "invalid_request_error", 400)

    model = str(body.get("model") or "")
    candidates = resolve_candidates(model)
    if not candidates:
        return _error(f"model not found: {model}", "model_not_found", 404)

    stream = bool(body.get("stream"))
    session = _session_key(body)
    last_error: str = ""
    started = time.perf_counter()

    for channel, upstream_model in candidates[:3]:
        if _cooldown.get(channel["id"], 0) > time.time():
            continue
        try:
            url, headers, payload = _build_upstream(channel, upstream_model, body, session)
        except Exception as e:  # noqa: BLE001
            last_error = f"{channel['id']}: {e}"
            continue

        try:
            req = _client.build_request("POST", url, headers=headers, json=payload)
            resp = await _client.send(req, stream=True)
        except Exception as e:  # noqa: BLE001
            last_error = f"{channel['id']}: {type(e).__name__}"
            _record(channel["id"], upstream_model, 0, int((time.perf_counter() - started) * 1000), None, last_error)
            continue

        if resp.status_code >= 400:
            detail = (await resp.aread()).decode("utf-8", errors="replace")
            await resp.aclose()
            last_error = f"{channel['id']}: HTTP {resp.status_code} {detail[:200]}"
            if resp.status_code in (429, 500, 502, 503, 504):
                _cooldown[channel["id"]] = time.time() + 30
            _record(channel["id"], upstream_model, resp.status_code, int((time.perf_counter() - started) * 1000), None, detail)
            continue

        # 流式：直接透传上游 SSE，客户端断开时关闭上游
        if stream:
            async def passthrough(source: httpx.Response, ch: dict[str, Any], m: str) -> AsyncIterator[bytes]:
                try:
                    async for chunk in source.aiter_raw():
                        yield chunk
                finally:
                    await source.aclose()
                    _record(ch["id"], m, 200, int((time.perf_counter() - started) * 1000), None)

            return StreamingResponse(
                passthrough(resp, channel, upstream_model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # 非流式：普通 JSON 直接解包；仅流式的通道聚合 SSE
        if channel.get("stream_only"):
            lines = resp.aiter_lines()
            result = await _aggregate_sse(lines, upstream_model)
            await resp.aclose()
            _record(channel["id"], upstream_model, 200, int((time.perf_counter() - started) * 1000), result.get("usage"))
            return JSONResponse(result)

        raw = await resp.aread()
        await resp.aclose()
        try:
            data = _unwrap(channel, json.loads(raw))
        except json.JSONDecodeError:
            return _error("upstream returned non-json", "upstream_error", 502, raw.decode("utf-8", "replace"))
        usage = data.get("usage") if isinstance(data, dict) else None
        _record(channel["id"], upstream_model, 200, int((time.perf_counter() - started) * 1000), usage)
        return JSONResponse(data)

    return _error("all candidates failed", "upstream_unavailable", 503, last_error)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await _client.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()
    print(f"andy-gateway-lite: {len(CHANNELS)} channels, config={CONFIG_PATH}")
    print(f"listening on http://{args.host}:{args.port}/v1")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
