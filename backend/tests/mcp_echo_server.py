"""最小 MCP server（仅用于测试 MCP 客户端）。

实现 stdio 传输的最小协议子集：initialize / tools/list / tools/call。
提供一个 echo 工具：把收到的参数原样回显为文本。
"""

import json
import sys


def main() -> None:
    caps = {"tools": {"listChanged": False}}
    server_info = {"name": "echo-test", "version": "0.0.1"}
    tools = [
        {
            "name": "echo",
            "description": "回显传入的参数",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "add",
            "description": "把 a 与 b 相加",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        },
    ]

    def respond(msg: dict) -> None:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        msg_id = req.get("id")
        if method == "initialize":
            respond(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": caps,
                        "serverInfo": server_info,
                    },
                }
            )
        elif method == "tools/list":
            respond({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                text = json.dumps(args, ensure_ascii=False)
                result = {
                    "content": [{"type": "text", "text": f"echo: {text}"}],
                    "isError": False,
                }
            elif name == "add":
                try:
                    val = args.get("a", 0) + args.get("b", 0)
                    result = {
                        "content": [{"type": "text", "text": str(val)}],
                        "isError": False,
                    }
                except Exception as e:  # noqa: BLE001
                    result = {
                        "content": [{"type": "text", "text": f"error: {e}"}],
                        "isError": True,
                    }
            else:
                result = {
                    "content": [{"type": "text", "text": f"unknown tool {name}"}],
                    "isError": True,
                }
            respond({"jsonrpc": "2.0", "id": msg_id, "result": result})
        # 通知类（无 id）忽略


if __name__ == "__main__":
    main()
