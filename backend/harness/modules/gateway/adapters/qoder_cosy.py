"""Qoder CN COSY 信封与请求体编解码（纯函数，无 IO）。

移植自本机 `~/.config/magpie/plugins/node_modules/@magpie-community/opencode-qoder-auth`
（v0.2.27，MIT）的 `index.mjs`，只取协议必需部分：

1. **信封（cosyHeaders）**：
   - `info` = 账号信息 JSON 的 AES-128-CBC 加密（密钥兼作 IV），base64；
   - `key` = 该 AES 密钥用 Qoder 内置 RSA-1024 公钥（PKCS#1 v1.5）包裹，base64；
   - `payload` = base64(JSON{version,requestId,info,cosyVersion,ideVersion})；
   - `sig` = MD5(`payload\nkey\nts\nbody\npath`)；
   - 请求头 `Authorization: Bearer COSY.<payload>.<sig>` + 一组 `Cosy-*` 头。

2. **请求体编解码**：Qoder 的 `Encode=1` 通道用一套私有 base64（打乱的字母表 +
   `$` 补位 + 首尾三段互换）。请求体必须按此编码后再发；响应体同法解码。

⚠️ 协议要点（踩坑记录）：
- 签名的 `path` 是**去掉 `/algo` 前缀**后的路径（`new URL(url).pathname` 再
  `slice(5)`），不是完整路径；
- `body` 参与签名时是**编码后的 wire 串**，不是明文 JSON；
- `Cosy-ClientType` 必须是 10（桌面端），`Cosy-Version` 对应桌面端构建号；
- RSA 是 1024 位 + PKCS#1 v1.5（`cryptography` 的 `padding.PKCS1v15()`）。

对照来源：index.mjs 的 `userBlob` / `cosyHeaders` / `segmentEncode` /
`swapThirds` / `encodeBody` / `decodeBody`（2026-10-22 读本机 v0.2.27）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: Qoder 桌面端构建自报的协议版本（index.mjs COSY_VERSION）。
COSY_VERSION = "1.1.69"

#: Qoder 内置的 1024 位 RSA 公钥（包裹每请求的 AES 密钥）。
RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

#: 私有 base64 字母表（index.mjs ALPHABET）。
ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"

#: Qoder 的系统提示前缀（其 agent 协议要求）。
QODER_SYS = (
    "You are a Qoder agent. Use the instructions below and the tools available to you to assist the user."
)

#: 内联工具调用标记（模型把工具调用写成正文时的私有 XML）。
CALL_OPEN = "<\x3ctool_call\x3e"
CALL_CLOSE = "<\x3c/tool_call\x3e"


def _hex_id() -> str:
    return uuid.uuid4().hex


def machine_os() -> str:
    """machineOS：Qoder 桌面端对机器的命名方式（arch_platform）。"""
    import platform

    machine = platform.machine().lower()
    arch = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "i386": "x86",
        "i686": "x86",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine, machine)
    return f"{arch}_{platform.system().lower()}"


@dataclass(slots=True)
class CosyAccount:
    """COSY 信封携带的账号信息。"""

    uid: str
    name: str = ""
    email: str = ""
    access: str = ""  # device token / job token
    machine_id: str = ""


def user_blob(account: CosyAccount) -> tuple[str, str]:
    """账号信息的加密与密钥包裹。

    返回 (info_b64, key_b64)：
    - info：AES-128-CBC 加密的账号 JSON（密钥兼作 IV）的 base64；
    - key：该 AES 密钥用 Qoder RSA 公钥 PKCS#1 v1.5 包裹后的 base64。
    """
    raw = json.dumps(
        {
            "uid": account.uid,
            "aid": "",
            "name": account.name or "",
            "email": account.email or "",
            "security_oauth_token": account.access,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    key = _hex_id()[:16].encode("utf-8")
    # Node 的 createCipheriv('aes-128-cbc') 自动补 PKCS#7；Python 的 cryptography
    # 不补，必须手动补——否则密文长度不是 16 的倍数会直接抛 ValueError。
    pad = 16 - (len(raw) % 16)
    raw_padded = raw + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    info = base64.b64encode(encryptor.update(raw_padded) + encryptor.finalize()).decode("ascii")
    public_key = serialization.load_pem_public_key(RSA_PUBLIC_KEY.encode("ascii"))
    wrapped = public_key.encrypt(key, rsa_padding.PKCS1v15())  # type: ignore[union-attr]
    return info, base64.b64encode(wrapped).decode("ascii")


def sign_path(url: str) -> str:
    """参与签名的路径：URL 路径去掉 `/algo` 前缀。"""
    path = urlparse(url).path
    if path.startswith("/algo"):
        path = path[5:]
    return path


def cosy_headers(
    url: str,
    account: CosyAccount,
    body: str,
    *,
    ts: int | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """为一次调用生成 COSY 信封请求头。

    body 是**线上形态**的请求体（已按 Qoder 私有 base64 编码）；它参与 MD5 签名。
    """
    if not account.machine_id:
        raise ValueError("Qoder 账号缺少 machineId，需重新登录")
    moment = int(time.time()) if ts is None else ts
    info, key = user_blob(account)
    payload = base64.b64encode(
        json.dumps(
            {
                "version": "v1",
                "requestId": _hex_id(),
                "info": info,
                "cosyVersion": COSY_VERSION,
                "ideVersion": "",
            },
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    signature = hashlib.md5(
        f"{payload}\n{key}\n{moment}\n{body}\n{sign_path(url)}".encode()
    ).hexdigest()
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        "Authorization": f"Bearer COSY.{payload}.{signature}",
        "Cosy-Business-Product": "app",
        "Cosy-Business-Type": "agent",
        "Cosy-ClientIp": account.machine_id,
        "Cosy-ClientType": "10",
        "Cosy-Data-Policy": "disagree",
        "Cosy-Date": str(moment),
        "Cosy-Key": key,
        "Cosy-MachineId": account.machine_id,
        "Cosy-MachineToken": account.machine_id,
        "Cosy-MachineType": "5",
        "Cosy-MachineOS": machine_os(),
        "Cosy-Scene": "app",
        "Cosy-User": account.uid,
        "Cosy-Version": COSY_VERSION,
        "Login-Version": "v2",
    }
    if extra:
        headers.update(extra)
    return headers


# ---- 请求体私有 base64 编解码 -------------------------------------------- #
# 逐 6 bit 取字（与 index.mjs segmentEncode 同构），'$' 补位到 4 的倍数；
# 再把首尾三段互换（swapThirds）。

def segment_encode(data: bytes) -> str:
    out: list[str] = []
    acc = 0
    nbits = 0
    for byte in data:
        acc = ((acc << 8) | byte) & 0xFFFFFF
        nbits += 8
        while nbits >= 6:
            nbits -= 6
            out.append(ALPHABET[(acc >> nbits) & 63])
    if nbits > 0:
        out.append(ALPHABET[(acc << (6 - nbits)) & 63])
    text = "".join(out)
    while len(text) % 4:
        text += "$"
    return text


def swap_thirds(text: str) -> str:
    third = len(text) // 3
    if not third:
        return text
    return text[len(text) - third :] + text[third : len(text) - third] + text[:third]


def encode_body(text: str) -> str:
    """明文 JSON → Qoder 的线上请求体。"""
    return swap_thirds(segment_encode(text.encode("utf-8")))


def decode_body(wire: str) -> str:
    """Qoder 的线上响应体 → 明文文本。"""
    text = swap_thirds(wire)
    out = bytearray()
    index = 0
    while index + 4 <= len(text):
        group = text[index : index + 4]
        index += 4
        values = [ALPHABET.index(c) for c in group if c != "$"]
        if not values:
            continue
        number = 0
        for value in values:
            number = number * 64 + value
        length = (6 * len(values)) // 8
        for k in range(length):
            shift = 6 * len(values) - 8 * (k + 1)
            out.append((number // (2**shift)) & 255)
    return out.decode("utf-8", errors="replace")


def qoder_session_id(seed: str) -> str:
    """Qoder 的 session_id：对话种子的 sha256 前段，格式化成 uuid 形状。"""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def build_agent_body(
    messages: list[dict[str, Any]],
    model_config: dict[str, Any],
    *,
    model_key: str,
    max_tokens: int = 32000,
    enable_thinking: bool = True,
    reasoning_effort: str = "",
    session_id: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造 Qoder `agent_chat_generation` 的明文请求体（index.mjs qoderBody）。

    与 OpenAI 形状的差别（移植时必须注意）：
    - 消息是「块」数组：`content: [{type:'text', text:'...'}]`；
    - 系统提示同时放进 `system` 字段与首条 system 消息；
    - 顶层有 request_id / request_set_id / chat_record_id / session_id /
      parameters / business / agent_id / task_id / session_type / model_config。
    """
    system_text = "\n\n".join(
        str(m.get("content") or "")
        for m in messages
        if m.get("role") in ("system", "developer") and m.get("content")
    )
    system_block = {
        "type": "text",
        "text": (QODER_SYS + "\n\n" + system_text) if system_text else QODER_SYS,
    }
    params: dict[str, Any] = {"enable_thinking": enable_thinking, "max_tokens": max_tokens}
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort
    request_id = str(uuid.uuid4())
    body: dict[str, Any] = {
        "request_id": request_id,
        "request_set_id": request_id,
        "chat_record_id": request_id,
        "session_id": session_id or qoder_session_id("agw-qoder"),
        "parameters": params,
        "business": {
            "product": "app",
            "version": COSY_VERSION,
            "type": "agent",
            "id": _hex_id(),
            "name": "agw session",
            "begin_at": int(time.time() * 1000),
            "stage": "start",
        },
        "agent_id": "agent_common",
        "task_id": "common",
        "session_type": "app",
        "model_config": model_config,
        "system": [system_block],
        "messages": [
            {"role": "system", "content": [system_block]},
            *_qoder_messages(messages),
        ],
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("function", {}).get("name", ""),
                    "description": t.get("function", {}).get("description", ""),
                    "parameters": t.get("function", {}).get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
            if t.get("type") == "function" and t.get("function", {}).get("name")
        ]
    return body


def _blocks_of(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text") is not None:
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if url:
                blocks.append({"type": "image_url", "image_url": {"url": url}})
    return blocks


def _qoder_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI 形状的消息 → Qoder 的块消息（index.mjs qoderMessages 的核心）。

    规则：system/developer 提到 system 字段（此处已在调用方处理，这里跳过）；
    assistant 的 tool_calls 保留为 tool_calls 块；tool 消息按 tool_call_id 成对。
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            continue
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id") or ""),
                    "content": str(message.get("content") or ""),
                }
            )
            continue
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for index, call in enumerate(message["tool_calls"]):
                function = call.get("function", {}) or {}
                args = function.get("arguments")
                name = str(function.get("name") or "")
                call_id = str(call.get("id") or "").strip()
                if not call_id:
                    said = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
                    digest = hashlib.sha256(f"{index}\0{name}\0{said}".encode()).hexdigest()
                    call_id = f"call_{digest[:32]}"
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
                        },
                    }
                )
            out.append({"role": "assistant", "content": str(message.get("content") or ""), "tool_calls": calls})
            continue
        blocks = _blocks_of(message.get("content"))
        if role == "assistant":
            blocks = [b for b in blocks if b.get("type") == "text"]
        if not blocks:
            continue
        out.append({"role": role or "user", "content": blocks})
    return out
