"""WS 参考客户端脚本。

用法:
    uv run python -m tests.test_ws_client --session-id <session_id> --provider deepseek

    或先创建会话:
    curl -X POST http://localhost:8000/api/sessions -H "Content-Type: application/json" -d '{"title":"测试"}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def ws_client(url: str, session_id: str, content: str, provider_id: str) -> None:
    """WebSocket 参考客户端。"""
    async with websockets.connect(url) as ws:
        # 发送消息
        await ws.send(
            json.dumps(
                {
                    "session_id": session_id,
                    "content": content,
                    "provider_id": provider_id,
                }
            )
        )

        # 接收帧
        while True:
            try:
                raw = await ws.recv()
                frame = json.loads(raw)
                frame_type = frame.get("type")
                data = frame.get("data", {})

                if frame_type == "token_delta":
                    if "delta" in data:
                        print(data["delta"], end="", flush=True)
                    if "reasoning_content" in data:
                        print(f"\n[思维链] {data['reasoning_content']}", end="", flush=True)
                elif frame_type == "tool_event":
                    print(f"\n[工具调用] {data}")
                elif frame_type == "context_snapshot":
                    print(f"\n[上下文快照] tokens={data.get('token_count', 0)}")
                elif frame_type == "error":
                    print(f"\n[错误] {data.get('message', '')}", file=sys.stderr)
                    break
                elif frame_type == "done":
                    print(f"\n[完成] iterations={data.get('iterations', 0)}, latency={data.get('latency_ms', 0)}ms")
                    break
            except websockets.exceptions.ConnectionClosed:
                print("\n连接已关闭")
                break


def main() -> None:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="andy-harness WS 参考客户端")
    parser.add_argument(
        "--url",
        default="ws://localhost:8000/ws/chat",
        help="WebSocket URL",
    )
    parser.add_argument("--session-id", required=True, help="会话 ID")
    parser.add_argument("--content", default="你好", help="对话内容")
    parser.add_argument("--provider", default="deepseek", help="Provider ID")

    args = parser.parse_args()
    asyncio.run(ws_client(args.url, args.session_id, args.content, args.provider))


if __name__ == "__main__":
    main()
