"""P2 FormatAdapter 内置实现 —— 音频转写（ASR）。

方案 5.1.3：Phase 2 通过 FormatAdapter 插件机制接入音频 / 视频 / 特殊格式，
以「**先转标准格式，再走通用链路**」为原则——转写为文本后复用 Phase 1 的
分段 / 清洗 / 索引链路，不为每种格式单独造索引。

内置实现：

- :class:`AudioAdapter` —— 音频文件经 **OpenAI 兼容** ``/audio/transcriptions``
  端点（whisper 系模型）转写为文本。配置走环境变量（可写入 backend/.env.local）：

      HARNESS_KB_ASR_ENDPOINT   转写端点。可给 base（自动补 /audio/transcriptions）
                                或完整 URL；**未配置时上传音频返回明确错误**。
      HARNESS_KB_ASR_API_KEY    密钥（可选；本地服务如 faster-whisper 通常不需要）。
      HARNESS_KB_ASR_MODEL      模型名，默认 whisper-1。

  说明：该端点形态不返回时间戳（方案 5.1.3 的「时间戳切片」需自建 ASR 服务时
  再扩展）；文本进入通用分段/清洗链路后与普通文档等价。

失败隔离（方案 5.1.5）：转写失败抛 :class:`ParseError`，仅该文档
``status=failed``，不影响知识库其余文档与其它格式。

注册：``register_builtin_adapters()`` 由 main.py 在启动时调用；
也可自行 ``register_adapter(AudioAdapter(transcribe=自定义函数))`` 接入。
"""

from __future__ import annotations

import os
from collections.abc import Callable

from .chunking import Asset
from .parsers import FormatAdapter, ParseError, register_adapter


class AudioAdapter(FormatAdapter):
    """音频 → 文本（ASR）。``transcribe`` 可注入（测试/自建 ASR 用）。"""

    extensions = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}

    def __init__(self, transcribe: Callable[[bytes, str], str] | None = None) -> None:
        self._transcribe = transcribe

    def parse(
        self,
        data: bytes,
        filename: str,
        assets_dir: str,  # noqa: ARG002 —— 音频无图片资产；保持契约一致
        doc_id: str,  # noqa: ARG002
    ) -> tuple[str, list[Asset]]:
        """转写音频为文本，返回 (正文, 空资产列表)。"""
        fn = self._transcribe or _transcribe_via_openai
        text = fn(data, filename)
        if not (text or "").strip():
            raise ParseError(f"音频转写结果为空: {filename}")
        return text, []


def _transcribe_via_openai(data: bytes, filename: str) -> str:
    """经 OpenAI 兼容 /audio/transcriptions 端点转写（httpx 同步调用）。

    注意：本函数在摄取线程中执行（service 用 ``asyncio.to_thread`` 调 parse），
    阻塞调用不会卡住事件循环。
    """
    endpoint = (os.environ.get("HARNESS_KB_ASR_ENDPOINT") or "").strip()
    if not endpoint:
        raise ParseError(
            "音频转写未配置：请设置 HARNESS_KB_ASR_ENDPOINT（OpenAI 兼容 /audio/transcriptions 端点）后重新上传"
        )
    url = endpoint.rstrip("/")
    if not url.endswith("/audio/transcriptions"):
        url = f"{url}/audio/transcriptions"
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    api_key = (os.environ.get("HARNESS_KB_ASR_API_KEY") or "").strip()
    model = (os.environ.get("HARNESS_KB_ASR_MODEL") or "").strip() or "whisper-1"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    import httpx

    try:
        resp = httpx.post(
            url,
            headers=headers,
            files={"file": (filename, data)},
            data={"model": model},
            timeout=120.0,
        )
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"音频转写请求失败: {e}") from e
    if resp.status_code != 200:
        raise ParseError(f"音频转写端点返回 {resp.status_code}: {resp.text[:200]}")
    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"音频转写端点返回非 JSON: {e}") from e
    text = payload.get("text") or ""
    if not text:
        data_field = payload.get("data")
        if isinstance(data_field, dict):
            text = data_field.get("text") or ""
    return str(text)


def register_builtin_adapters() -> None:
    """注册内置 P2 适配器（目前：AudioAdapter）。

    由 main.py 在应用启动时调用；未配置 ASR 端点时不影响任何现有格式，
    仅上传音频时才报「未配置」错误。
    """
    register_adapter(AudioAdapter())
