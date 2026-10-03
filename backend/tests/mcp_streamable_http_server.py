"""最小 MCP streamable-http 测试服务器 —— 仅用标准库。

用法: python mcp_streamable_http_server.py [port]

模拟「新版 / 只支持 POST」的远端 MCP server（真实形态参考 libgen）：
- `GET /`  → **405 Method Not Allowed**（不下发 SSE endpoint）
- `POST /` → 直接把 JSON-RPC 响应当作 SSE 事件流写在**本次响应体**里
            （`event: message` + `data: {...}`），并在 initialize 时下发
            `Mcp-Session-Id`。

暴露 `echo` 与 `add` 两个工具，与 SSE echo server 行为一致。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 记录收到的请求头快照，便于测试断言（如会话 ID 是否回传）
SEEN_HEADERS: list[dict[str, str]] = []
_LOCK = threading.Lock()

TOOLS = [
    {
        "name": "echo",
        "description": "回显 text 参数",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "计算 a + b",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def _handle_rpc(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 请求，返回响应（通知返回 None）。"""
    method = msg.get("method")
    req_id = msg.get("id")
    if req_id is None:  # 通知，无响应
        return None
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "streamable-http-echo", "version": "0.1.0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            result = {"content": [{"type": "text", "text": f"echo: {args.get('text', '')}"}]}
        elif name == "add":
            try:
                total = float(args.get("a", 0)) + float(args.get("b", 0))
                text = str(int(total)) if total.is_integer() else str(total)
            except (TypeError, ValueError):
                text = "invalid numbers"
            result = {"content": [{"type": "text", "text": f"sum: {text}"}]}
        else:
            result = {
                "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True,
            }
    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        """不支持 GET 建流：与 libgen 等服务一致返回 405。"""
        body = b"Method Not Allowed"
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path not in ("/", "/mcp"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400)
            return

        with _LOCK:
            SEEN_HEADERS.append(
                {
                    "method": str(msg.get("method", "")),
                    "mcp-session-id": self.headers.get("Mcp-Session-Id", ""),
                }
            )

        response = _handle_rpc(msg)
        if response is None:  # 通知（如 notifications/initialized）→ 202 空响应
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # 把响应当作 SSE 事件流写进本次 POST 响应体（streamable-http 典型形态）
        payload = json.dumps(response)
        body = f"event: message\ndata: {payload}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        if msg.get("method") == "initialize":
            self.send_header("Mcp-Session-Id", "test-session-1")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18993
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"streamable-http MCP echo server on http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
