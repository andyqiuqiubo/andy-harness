"""DeepSeek V4.1 Flash 模型客户端。

为什么自己实现而非复用 OpenAICompatibleProvider.chat？
该 provider 的 chat 是**流式**接口，tool_calls 以 SSE 分片下发，需自行累积；
且 stream=False 时其底层仍走流式解析，对单体 JSON 响应解析不可靠。
本插件需要「一次性拿到完整 tool_calls」以驱动 Agent 循环，因此实现轻量、
独立的**非流式** /chat/completions 调用：复用项目既有的 httpx 风格，自带
指数退避重试与超时，便于在沙箱/受限网络下稳定运行。

所有连接参数（base_url / model / timeout）均来自配置，不硬编码。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("harness.computer_use.client")


class ComputerUseModelError(RuntimeError):
    """模型调用错误（统一异常）。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class DeepSeekClient:
    """非流式 function-calling 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """发起一次非流式对话，返回解析后的响应。

        Returns:
            {"content": str, "tool_calls": list[dict], "usage": dict|None}
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
            "stream": False,
        }

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=body,
                        headers=self._headers(),
                    )
                    if resp.status_code != 200:
                        text = resp.text
                        # 401 是密钥问题，不可重试，直接抛出
                        if resp.status_code == 401:
                            raise ComputerUseModelError(f"API Key 无效或已过期: {text}", status_code=401)
                        # 429 / 5xx 可重试
                        raise ComputerUseModelError(
                            f"API 请求失败({resp.status_code}): {text}",
                            status_code=resp.status_code,
                        )
                    data = resp.json()
                return self._parse(data)
            except ComputerUseModelError as e:
                if e.status_code == 401:
                    raise  # 不可重试
                last_error = e
                logger.warning(
                    "模型调用失败(尝试 %d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    e,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                logger.warning(
                    "模型网络错误(尝试 %d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    e,
                )

            # 指数退避（最后一次不等待）
            if attempt < self._max_retries - 1:
                await asyncio.sleep(2**attempt)

        raise ComputerUseModelError(f"模型调用失败，已重试 {self._max_retries} 次: {last_error}")

    @staticmethod
    def _parse(data: dict[str, Any]) -> dict[str, Any]:
        """从非流式响应中提取 content / tool_calls / usage。"""
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise ComputerUseModelError(f"模型响应结构异常: {data}") from e

        content = message.get("content") or ""
        raw_tool_calls = message.get("tool_calls") or []
        usage = data.get("usage")

        # 非流式下 tool_calls 已是完整结构，无需分片累积
        tool_calls: list[dict[str, Any]] = []
        for tc in raw_tool_calls:
            fn = tc.get("function", {})
            tool_calls.append(
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "") or "",
                    },
                }
            )
        return {"content": content, "tool_calls": tool_calls, "usage": usage}


class FakeModelClient:
    """测试替身：按预设脚本返回 tool_calls / 终答，不发起任何网络请求。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        # script: 每个元素 {"tool_calls": [...]} 或 {"content": "..."}
        self._script = list(script)
        self._idx = 0
        # 记录每次调用收到的消息，便于测试断言（如多模态截图回填）
        self.captured: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.captured.append(list(messages))
        if self._idx >= len(self._script):
            # 脚本耗尽，返回空终答，避免死循环
            return {"content": "（测试脚本结束）", "tool_calls": [], "usage": None}
        step = self._script[self._idx]
        self._idx += 1
        return {
            "content": step.get("content", ""),
            "tool_calls": step.get("tool_calls", []),
            "usage": step.get("usage"),
        }
