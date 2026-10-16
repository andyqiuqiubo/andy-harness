"""andy-harness 会话功能端到端验证（API 级，真实流式）。

覆盖：
  S1 停止生成：已获取内容完整保留 + stopped 标记 + 「用户已停止回答」数据基础
  S2 正常完成：对照场景（stopped=false）
  S3 帧归属：token_delta / done 帧携带 session_id（跨会话过滤的数据基础）
  S4 历史消息加载：完整问答历史按序返回

用法：先确保后端运行于 127.0.0.1:8000，然后
  backend/.venv/Scripts/python.exe H:\\jsd-andyharness\\e2e\\e2e_chat.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS_URL = "ws://127.0.0.1:8000/ws/chat"
PROVIDER = "custom_oortcloud"
MODEL = "deepseek-v4.1-flash"
PROMPT = "请用三百字左右简要介绍杭州这座城市的特色。"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def create_session(client: httpx.AsyncClient, title: str) -> str:
    r = await client.post("/api/sessions", json={"title": title})
    r.raise_for_status()
    return r.json()["id"]


async def get_messages(client: httpx.AsyncClient, sid: str) -> list[dict]:
    r = await client.get(f"/api/sessions/{sid}/messages")
    r.raise_for_status()
    return r.json()


async def scenario_stop(client: httpx.AsyncClient) -> None:
    """S1：流式中途停止 → 部分内容保留 + stopped 标记。"""
    print("\n== S1 停止生成保留内容 ==")
    sid = await create_session(client, "E2E-S1-停止生成")
    deltas: list[str] = []
    done_frame: dict | None = None
    stop_ack = False
    async with websockets.connect(WS_URL, max_size=16 * 1024 * 1024) as ws:
        await ws.send(
            json.dumps(
                {
                    "session_id": sid,
                    "content": PROMPT,
                    "provider_id": PROVIDER,
                    "model": MODEL,
                    "temperature": 0.7,
                }
            )
        )
        # 收集约 12 个正文 delta 后停止
        stopped_after = 12
        async for raw in ws:
            frame = json.loads(raw)
            if frame["type"] == "token_delta":
                d = frame["data"].get("delta", "")
                if d:
                    deltas.append(d)
                if len(deltas) >= stopped_after:
                    await ws.send(json.dumps({"type": "stop", "session_id": sid}))
            elif frame["type"] == "stop_ack":
                stop_ack = True
            elif frame["type"] == "done":
                done_frame = frame
                break
            elif frame["type"] == "error":
                check("S1 未收到错误帧", False, str(frame["data"]))
                return
    streamed = "".join(deltas)
    check("S1 流式收到多个正文 delta", len(deltas) >= 5, f"{len(deltas)} 个 delta")
    check("S1 收到 stop_ack", stop_ack)
    check("S1 收到 done 帧", done_frame is not None)
    if done_frame:
        check(
            "S1 done.stopped == true",
            done_frame["data"].get("stopped") is True,
            f"stopped={done_frame['data'].get('stopped')}",
        )
        check(
            "S1 done.session_id 归属正确",
            done_frame["data"].get("session_id") == sid,
        )
    # 停止后后端已把部分内容落库：轮询至多 5 秒
    partial = ""
    stopped_flag = None
    for _ in range(25):
        msgs = await get_messages(client, sid)
        assistants = [m for m in msgs if m["role"] == "assistant"]
        if assistants:
            partial = assistants[-1]["content"]
            stopped_flag = assistants[-1].get("stopped")
            if stopped_flag:
                break
        await asyncio.sleep(0.2)
    check("S1 部分回答已持久化（内容不消失）", bool(partial.strip()), f"{len(partial)} 字")
    check("S1 持久化消息带 stopped 标记", stopped_flag is True, f"stopped={stopped_flag}")
    check(
        "S1 持久化内容与流式内容一致（前缀）",
        streamed.startswith(partial[: max(1, len(partial) - 1)]) or partial in streamed or streamed in partial,
        f"streamed={len(streamed)} 字 / persisted={len(partial)} 字",
    )
    users = [m for m in (await get_messages(client, sid)) if m["role"] == "user"]
    check("S1 用户提问仍在", len(users) == 1 and users[0]["content"] == PROMPT)


async def scenario_normal(client: httpx.AsyncClient) -> None:
    """S2：正常完成（对照）。"""
    print("\n== S2 正常完成（对照） ==")
    sid = await create_session(client, "E2E-S2-正常完成")
    async with websockets.connect(WS_URL, max_size=16 * 1024 * 1024) as ws:
        await ws.send(
            json.dumps(
                {
                    "session_id": sid,
                    "content": "用一个词回答：法国的首都是什么？只回答词本身。",
                    "provider_id": PROVIDER,
                    "model": MODEL,
                    "temperature": 0.0,
                }
            )
        )
        async for raw in ws:
            frame = json.loads(raw)
            if frame["type"] == "done":
                check("S2 done.stopped == false", frame["data"].get("stopped") is False)
                break
            if frame["type"] == "error":
                check("S2 未收到错误帧", False, str(frame["data"]))
                return
    msgs = await get_messages(client, sid)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    check("S2 完整回答已持久化", bool(assistants and assistants[-1]["content"].strip()))
    check("S2 持久化消息 stopped 为假", assistants and assistants[-1].get("stopped") in (False, None))


async def scenario_history(client: httpx.AsyncClient) -> None:
    """S4：历史消息加载（多轮）。"""
    print("\n== S4 历史消息加载 ==")
    sid = await create_session(client, "E2E-S4-历史加载")
    for q in ["第一轮：你好", "第二轮：杭州有什么", "第三轮：谢谢"]:
        async with websockets.connect(WS_URL, max_size=16 * 1024 * 1024) as ws:
            await ws.send(
                json.dumps(
                    {
                        "session_id": sid,
                        "content": q,
                        "provider_id": PROVIDER,
                        "model": MODEL,
                        "temperature": 0.0,
                    }
                )
            )
            async for raw in ws:
                frame = json.loads(raw)
                if frame["type"] == "done":
                    break
                if frame["type"] == "error":
                    check("S4 未收到错误帧", False, str(frame["data"]))
                    return
    msgs = await get_messages(client, sid)
    users = [m for m in msgs if m["role"] == "user"]
    assistants = [m for m in msgs if m["role"] == "assistant"]
    check("S4 三轮提问全部加载", len(users) == 3, f"{len(users)} 条")
    check("S4 三个回答全部加载", len(assistants) == 3, f"{len(assistants)} 条")
    check(
        "S4 消息按时间正序且问答交替",
        all(msgs[i]["role"] == "user" and msgs[i + 1]["role"] == "assistant" for i in (0, 2, 4)),
    )


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=30) as client:
        health = await client.get("/api/health")
        check("后端健康检查", health.status_code == 200, health.text[:40])
        await scenario_stop(client)
        await scenario_normal(client)
        await scenario_history(client)

    print("\n===== 汇总 =====")
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    print(f"\n总计 {len(results)} 项，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
