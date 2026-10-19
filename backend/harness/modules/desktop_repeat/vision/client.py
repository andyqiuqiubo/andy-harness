"""阿里云百炼视觉理解客户端（OpenAI 兼容 /chat/completions）。

设计约束：
- 纯 httpx（不引入 openai SDK 依赖），图像走 base64 Data URL（本机帧不外传
  原始文件，只传编码内容）；
- 密钥解析顺序：显式参数 → 环境变量 DASHSCOPE_API_KEY → 配置的 key 文件 →
  项目根 key.txt（用户约定位置）。密钥**绝不**写日志/落库/进报告；
- 失败一律返回 None 并记录原因（旁路语义：调用失败不影响主流程）。
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("harness.modules.desktop_repeat.vision")

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{20,}")


def _project_root() -> Path:
    # harness/modules/desktop_repeat/vision/client.py → 上 5 级为项目根
    return Path(__file__).resolve().parents[5]


def resolve_api_key(explicit: str = "", key_file: str = "") -> str:
    """密钥解析（显式 > env > 指定文件 > 项目根 key.txt）。"""
    if explicit:
        return explicit.strip()
    env = os.environ.get("DASHSCOPE_API_KEY", "")
    if env:
        return env.strip()
    candidates = [Path(key_file)] if key_file else \
        [_project_root() / "key.txt"]
    for p in candidates:
        try:
            if p.is_file():
                m = _KEY_PATTERN.search(p.read_text(encoding="utf-8", errors="ignore"))
                if m:
                    return m.group(0)
        except Exception:  # noqa: BLE001
            continue
    return ""


def image_data_url(path: Path) -> str | None:
    """本地帧 → base64 Data URL；读不到/为空返回 None。"""
    try:
        data = path.read_bytes()
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    suffix = path.suffix.lower().lstrip(".") or "jpeg"
    mime = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class BailianVisionClient:
    """最小可用的视觉理解客户端：多图输入 → 文本回答。"""

    def __init__(self, api_key: str = "", model: str = "qwen3.8-omni-flash",
                 base_url: str = DEFAULT_BASE_URL, timeout_s: int = 60,
                 key_file: str = "") -> None:
        self.api_key = resolve_api_key(api_key, key_file)
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def analyze_images(self, images: list[str | Path], prompt: str,
                             system: str | None = None) -> dict[str, Any] | None:
        """images: 本地路径或已编码 Data URL；返回 {"text", "usage"} 或 None。"""
        if not self.available:
            logger.info("vision: 未配置 API Key，跳过")
            return None
        content: list[dict[str, Any]] = []
        for img in images:
            url = img if isinstance(img, str) and img.startswith("data:") \
                else image_data_url(Path(img))
            if not url:
                continue
            content.append({"type": "image_url", "image_url": {"url": url}})
        if not content:
            return None
        content.append({"type": "text", "text": prompt})
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "temperature": 0.1}
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        last_err = ""
        for attempt in (1, 2):  # 旁路语义：最多两次，失败即放弃
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s) as cli:
                    resp = await cli.post(url, json=body, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    text = ((data.get("choices") or [{}])[0].get("message") or {}) \
                        .get("content", "")
                    return {"text": text or "",
                            "usage": data.get("usage") or {}}
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
            logger.info("vision: 调用失败（第 %d 次）%s", attempt, last_err)
        logger.info("vision: 放弃本次分析（%s）", last_err)
        return None


def parse_json_answer(text: str) -> dict[str, Any] | None:
    """解析模型回答中的 JSON（容忍 ```json 围栏与前后缀文本）。"""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    import json
    try:
        obj = json.loads(raw[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None
