"""最小 MCP SSE（HTTP + Server-Sent Events）测试服务器 —— 仅用标准库。

用法: python mcp_sse_echo_server.py [port]

- `GET /sse`              : 建立事件流，先下发 `event: endpoint`，之后推送响应
- `POST /messages/?s=<id>`: 接收 JSON-RPC 请求，把响应推给对应事件流

暴露 `echo` 与 `add` 两个工具，与 stdio 版 echo server 行为一致。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SESSIONS: dict[str, queue.Queue] = {}
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
    if req_id is None:  # 通知
        return None
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sse-echo", "version": "0.1.0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            text = str(args.get("text", ""))
            result = {"content": [{"type": "text", "text": f"echo: {text}"}]}
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

    # ── SSE 事件流 ──
    def do_GET(self):  # noqa: N802
        if not self.path.startswith("/sse"):
            self.send_error(404)
            return
        session_id = uuid.uuid4().hex[:8]
        q: queue.Queue[str] = queue.Queue()
        with _LOCK:
            _SESSIONS[session_id] = q

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self._write_event("endpoint", f"/messages/?session_id={session_id}")
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    self._write_event("ping", "{}")  # 心跳
                    continue
                if payload is None:
                    break
                self._write_event("message", payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _LOCK:
                _SESSIONS.pop(session_id, None)

    def _write_event(self, event: str, data: str) -> None:
        body = f"event: {event}\ndata: {data}\n\n".encode()
        self.wfile.write(body)
        self.wfile.flush()

    # ── JSON-RPC 请求入口 ──
    def do_POST(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        if not parsed.path.startswith("/messages"):
            self.send_error(404)
            return
        session_id = (parse_qs(parsed.query).get("session_id") or [""])[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400)
            return

        response = _handle_rpc(msg)
        if response is not None:
            with _LOCK:
                q = _SESSIONS.get(session_id)
            if q is not None:
                q.put(json.dumps(response))

        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18991
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"SSE MCP echo server on http://127.0.0.1:{port}/sse", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
