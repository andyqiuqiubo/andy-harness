"""图像 / 视频生成模块（media_gen）。

针对本机硬件（MX150 2GB / CUDA 9.2 无法本地推理）采用**纯云端异步生成**：
智谱 BigModel 免费模型 CogView-3-Flash（文生图）与 CogVideoX-Flash（文生/图生视频）。

上游为**异步任务制**（提交拿 id → 轮询结果），链接为临时链接，必须转存本地。
"""

from harness.modules.media_gen.service import MediaGenService, MediaGenError

__all__ = ["MediaGenService", "MediaGenError"]
