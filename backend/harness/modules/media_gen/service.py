"""图像 / 视频生成服务。

上游：智谱 BigModel（OpenAI 兼容 `/paas/v4`）。

协议要点（均经真机实测 / 官方 OpenAPI 核实，勿凭印象改动）：
1. 文生图 `POST /paas/v4/images/generations`：
   同步返回 `data[0].url`，或异步返回 `id` → 轮询 `async-result` 的 `image_result`；
2. 视频 `POST /paas/v4/videos/generations`：恒为异步，返回 `id`；
   轮询 `GET /paas/v4/async-result/{id}`，`task_status` = PROCESSING/SUCCESS/FAIL，
   成功取 `video_result[0].url`；
3. `cogvideox-flash` 与 `cogvideox-3` 参数**不同**：flash **无** `duration`，
   且 size 枚举不含 `1280x720`/`720x1280`（那些是 cogvideox-3 的）；
4. 免费档高峰期必现 **1305「该模型当前访问量过大」** → 必须退避重试；
5. 上游链接为**临时链接**（图片 30 天）→ 必须下载转存；
6. 产物落盘前做 **magic bytes 校验**，杜绝把错误 JSON 存成图片/视频。
7. 停止语义：智谱异步任务**无取消 API**——「停止生成」是本地语义：任务置
   CANCELLED 并即刻断开与上游的轮询（后续 poll 不再查智谱、不下载落盘）；
   上游任务可能仍在排队，但其产物不会被本端获取。
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from harness.infra.database import Database

logger = logging.getLogger("harness.media_gen")

ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
IMAGE_SUBMIT = ZHIPU_BASE + "/images/generations"
VIDEO_SUBMIT = ZHIPU_BASE + "/videos/generations"
ASYNC_RESULT = ZHIPU_BASE + "/async-result/{id}"

# 图像 / 视频模型（免费档）
IMAGE_MODELS = {"cogview-3-flash"}
VIDEO_MODELS = {"cogvideox-flash"}

COGVIEW_SIZES = [
    "1024x1024", "768x1344", "864x1152", "1344x768",
    "1152x864", "1440x720", "720x1440",
]
COGVIDEO_FLASH_SIZES = [
    "720x480", "1024x1024", "1280x960", "960x1280",
    "1920x1080", "1080x1920", "2048x1080", "3840x2160",
]
FPS_ALLOWED = (30, 60)
# 官方 API 通用说明为「duration 默认 5 秒，支持 5 / 10」，但**按模型区分**：
# - cogvideox-flash（免费档）实测仅 5 秒：传 10 不报错、产物仍 5.11s（2026-10-23 实测）
# - cogvideox-2 / cogvideox-3（付费）支持 5 / 10 秒
# 越界值上游一律静默忽略（按 5 秒生成），故必须在前置校验层拦住。
DURATION_ALLOWED = (5, 10)
DURATION_BY_MODEL: dict[str, tuple[int, ...]] = {
    "cogvideox-flash": (5,),
    "cogvideox-2": (5, 10),
    "cogvideox-3": (5, 10),
}


def durations_for(model: str) -> tuple[int, ...]:
    """某模型支持的时长集合（未知模型按通用集合兜底）。"""
    return DURATION_BY_MODEL.get(model, DURATION_ALLOWED)


def default_duration(model: str) -> int:
    """某模型的默认时长 = 它支持的最大值。"""
    return max(durations_for(model))

# 免费档瞬时过载 / 限流错误码 → 可退避重试
RETRYABLE_CODES = {"1302", "1305", "1113", "429", "429001"}

HTTP_TIMEOUT = 90.0
POLL_INTERVAL = 10.0
POLL_MAX_WAIT = 900.0          # 单任务最长轮询 15 分钟
DOWNLOAD_TIMEOUT = 180.0
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # CogVideoX image_url ≤5MB

# 任务最长等待（含排队 + 生成）。超过后任务判 FAIL 并停止向上游查询，
# 避免免费档无限排队时前端永远停在「生成中」。默认 1 小时，可 env 覆盖。
DEFAULT_MAX_WAIT_SECONDS = int(os.environ.get("MEDIA_MAX_WAIT_SECONDS", "3600"))


def _waited_seconds(params: dict[str, Any]) -> int:
    """任务已等待秒数（服务端计算，避免前端解析 UTC 时间戳的时区偏差）。"""
    created = params.get("created_ts")
    if not created:
        deadline, max_wait = params.get("deadline_ts"), params.get("max_wait")
        if not deadline or not max_wait:
            return 0
        created = float(deadline) - float(max_wait)
    return max(0, int(time.time() - float(created)))


def _remaining_seconds(params: dict[str, Any]) -> int:
    """距最大等待截止还剩多少秒；-1 表示「不限等待」（等智谱主动结束）。"""
    deadline = params.get("deadline_ts")
    if deadline is not None and float(deadline) <= 0:
        return -1
    if not deadline:
        return 0
    return max(0, int(float(deadline) - time.time()))

_MAGIC_IMAGE = (b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF")


class MediaGenError(Exception):
    """媒体生成失败（参数错误 / 上游错误 / 产物非法）。"""


def _detect_project_root() -> Path:
    p = Path(__file__).resolve().parent
    for _ in range(6):
        if (p / "key.txt").exists() or (p / "backend").is_dir():
            return p
        p = p.parent
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT = _detect_project_root()


def load_zhipu_api_key(runtime_key: str = "") -> str:
    """按优先级解析智谱 API Key：环境变量 > 插件运行时注入 > key.txt 兜底。

    由 MediaGenService 与 provider_zhipu 插件共用，确保「生图/生视频接口」与
    「会话页模型下拉可用性（has_api_key）」看到的是同一把 key——否则会出现
    接口能调、下拉里却没有「智谱」的分裂（前端以 has_api_key 过滤 provider）。
    """
    return (
        os.environ.get("ZHIPU_API_KEY", "").strip()
        or (runtime_key or "").strip()
        or MediaGenService._from_key_txt()
    )


class MediaGenService:
    """图像 / 视频生成服务（异步任务 + 产物落盘）。"""

    def __init__(self, db: Database | None = None, data_dir: str | Path | None = None) -> None:
        self.db = db or Database()
        self.data_dir = Path(data_dir) if data_dir else PROJECT_ROOT / "backend" / "data" / "media"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_key = ""

    # ------------------------------------------------------------------ #
    # 凭据
    # ------------------------------------------------------------------ #
    def set_api_key(self, api_key: str) -> None:
        """由 provider 插件在激活时注入已配置的 key（优先于 key.txt）。"""
        if api_key:
            self._runtime_key = api_key.strip()

    @staticmethod
    def _from_key_txt() -> str:
        kt = PROJECT_ROOT / "key.txt"
        if not kt.exists():
            return ""
        for line in kt.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "智谱" in line and ("：" in line or ":" in line):
                sep = "：" if "：" in line else ":"
                cand = line.split(sep, 1)[1].strip()
                if cand:
                    return cand
        return ""

    def _api_key(self) -> str:
        key = load_zhipu_api_key(self._runtime_key)
        if not key:
            raise MediaGenError("未配置智谱 API Key（环境变量 ZHIPU_API_KEY / 插件配置 / key.txt）")
        return key

    def has_credentials(self) -> bool:
        try:
            return bool(self._api_key())
        except MediaGenError:
            return False

    # ------------------------------------------------------------------ #
    # 创建任务
    # ------------------------------------------------------------------ #
    async def create_task(
        self,
        kind: str,
        model: str,
        prompt: str = "",
        image: str = "",
        size: str | None = None,
        fps: int = 30,
        quality: str = "quality",
        # None → 按模型取默认（cogvideox-flash=5，cogvideox-2/3=10）
        duration: int | None = None,
        with_audio: bool = False,
        # 默认不带水印（智谱要求 watermark_enabled=false 需先在个人中心签
        # 「去水印管理」免责声明；未签时上游可能忽略或报错）
        watermark: bool = False,
        session_id: str = "",
        provider_id: str = "zhipu",
        attempts: int = 4,
        max_wait_seconds: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """提交生成任务。视频恒异步；图像同步返回时直接落盘并置 SUCCESS。

        - `max_wait_seconds`：最长等待（默认 1 小时）。超时后任务标记 FAIL 并
          停止向上游轮询（主动断开），前端据此显示失败原因。
        - `attachments`：参考图片元信息（id / filename / mime），落库供刷新后
          恢复问答区的参考图缩略图。
        """
        if kind not in ("image", "video"):
            raise MediaGenError(f"kind 必须是 image 或 video，收到 {kind}")
        if model not in IMAGE_MODELS and model not in VIDEO_MODELS:
            raise MediaGenError(f"不支持的模型: {model}")
        if model in VIDEO_MODELS and kind != "video":
            raise MediaGenError(f"{model} 仅支持视频生成")
        if not prompt and not image:
            raise MediaGenError("prompt 与 image 不能同时为空")

        task_id = uuid.uuid4().hex
        payload: dict[str, Any] = {"model": model}
        # 视频时长：image 分支不适用（dur=None）；video 分支按模型默认/用户指定
        dur: int | None = None

        if kind == "image":
            size = size or "1024x1024"
            if size not in COGVIEW_SIZES:
                raise MediaGenError(f"图像 size 必须是 {COGVIEW_SIZES}，收到 {size}")
            payload.update({"prompt": prompt, "size": size, "watermark_enabled": watermark})
            endpoint = IMAGE_SUBMIT
        else:
            # 默认取「免费档实际可用」的最大档：1080p 质量优先。
            # 4K（3840x2160）技术上也支持，但免费档排队极难成功，需显式传参。
            size = size or "1920x1080"
            if size not in COGVIDEO_FLASH_SIZES:
                raise MediaGenError(f"视频 size 必须是 {COGVIDEO_FLASH_SIZES}，收到 {size}")
            if fps not in FPS_ALLOWED:
                raise MediaGenError(f"fps 必须是 30 或 60，收到 {fps}")
            allowed = durations_for(model)
            dur = duration if duration is not None else default_duration(model)
            # 越界值上游静默忽略（实测传 10/30 都得 5.11s），必须前置拦
            if dur not in allowed:
                raise MediaGenError(
                    f"{model} 仅支持 {'/'.join(str(d) for d in allowed)} 秒，收到 {dur}")
            payload["duration"] = dur
            payload.update({
                "prompt": prompt or "让画面动起来",
                "size": size, "fps": fps, "quality": quality,
                "duration": dur,
                "with_audio": with_audio, "watermark_enabled": watermark,
            })
            if image:
                payload["image_url"] = self._as_image_url(image)
            endpoint = VIDEO_SUBMIT

        # max_wait_seconds=0 表示「不限等待」：一直等智谱给出终态（或上游断开），
        # 不触发超时熔断。默认 1 小时。
        if max_wait_seconds is None:
            max_wait = DEFAULT_MAX_WAIT_SECONDS
        else:
            max_wait = int(max_wait_seconds)
        now_ts = int(time.time())
        infinite = max_wait <= 0
        params = {"size": size, "fps": fps, "quality": quality,
                  "duration": dur,
                  "with_audio": with_audio, "watermark": watermark,
                  "has_image": bool(image),
                  "max_wait": max_wait,
                  "created_ts": now_ts,
                  "deadline_ts": 0 if infinite else now_ts + max_wait,
                  "attachments": attachments or []}

        headers = {"Authorization": f"Bearer {self._api_key()}"}

        # 提交前先落库（SUBMITTING）：此前任务要等上游接单后才 insert，
        # 提交阶段（含 1305 长退避）刷新页面是一片空白、看不到任何痕迹。
        self._insert(task_id, session_id, kind, model, provider_id, prompt, params,
                     "", "SUBMITTING", "", "", "", "", 0)

        try:
            resp = await self._post_with_retry(endpoint, payload, headers, attempts=attempts)
        except MediaGenError as e:
            # 上游拒绝 / 重试耗尽：落库 FAIL 并返回任务（不抛），
            # 让前端能展示具体原因，刷新后也能看到这条失败记录。
            self._update(task_id, status="FAIL", error=str(e)[:500])
            logger.warning("media task %s 提交失败: %s", task_id, e)
            return await self.get_task(task_id)

        # 图像同步返回形态：直接下载落盘
        url = self._extract_sync_url(resp)
        if url:
            file_path, mime, nbytes = await self._download_and_store(task_id, url, kind)
            self._update(task_id, status="SUCCESS", upstream_id=resp.get("id", ""),
                         result_url=url, file_path=file_path, mime=mime,
                         size_bytes=nbytes)
            return await self.get_task(task_id)

        upstream_id = resp.get("id") or resp.get("request_id")
        if not upstream_id:
            self._update(task_id, status="FAIL",
                         error=f"上游未返回任务 id: {json.dumps(resp, ensure_ascii=False)[:300]}")
            return await self.get_task(task_id)

        self._update(task_id, status="PROCESSING", upstream_id=upstream_id)
        logger.info("media task %s 已提交（%s/%s，upstream=%s）", task_id, kind, model, upstream_id)
        return await self.get_task(task_id)

    # ------------------------------------------------------------------ #
    # 查询任务（必要时轮询上游）
    # ------------------------------------------------------------------ #
    async def get_task(self, task_id: str, poll: bool = False) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM media_tasks WHERE id = ?", (task_id,))
        if not row:
            raise MediaGenError(f"任务不存在: {task_id}")
        if poll and row["status"] in ("SUBMITTING", "PROCESSING"):
            # 活跃态才向上游查；CANCELLED（用户手动停止）等终态一律不查
            # 统一在入口判超时：一旦超过最大等待时间即判 FAIL 并**不再向上游查询**
            # （主动断开与智谱的轮询），避免无限排队把界面卡在「生成中」。
            if not self._check_deadline(task_id, row):
                row = self.db.query_one(
                    "SELECT * FROM media_tasks WHERE id = ?", (task_id,)) or row
                if row["status"] == "PROCESSING":
                    await self._refresh(task_id)
            row = self.db.query_one(
                "SELECT * FROM media_tasks WHERE id = ?", (task_id,)) or row
        return self._to_dict(row)

    @staticmethod
    def _deadline_of(row: Any) -> float:
        """任务的最大等待截止时间（unix 秒）；inf 表示「不限等待」。"""
        try:
            params = json.loads(row["params_json"] or "{}")
        except Exception:  # noqa: BLE001
            params = {}
        if "deadline_ts" in params:
            deadline = float(params.get("deadline_ts") or 0)
            # 0 = 用户选择不限等待：一直等智谱给出终态，不触发超时熔断
            return float("inf") if deadline <= 0 else deadline
        return time.time() + DEFAULT_MAX_WAIT_SECONDS

    def _check_deadline(self, task_id: str, row: Any) -> bool:
        """超过最大等待时间则标记 FAIL 并停止后续轮询（返回是否已判超时）。"""
        if row["status"] not in ("SUBMITTING", "PROCESSING"):
            return False
        deadline = self._deadline_of(row)
        if time.time() < deadline:
            return False
        try:
            params = json.loads(row["params_json"] or "{}")
        except Exception:  # noqa: BLE001
            params = {}
        max_wait = int(params.get("max_wait") or DEFAULT_MAX_WAIT_SECONDS)
        self._update(
            task_id, status="FAIL",
            error=(f"生成超时：超过最大等待时间 {max_wait // 60} 分钟，"
                   f"已主动停止向智谱查询（上游任务 "
                   f"{row['upstream_id'] or '未接单'}，可稍后重试）"),
        )
        logger.warning("media task %s 超过最大等待 %ds，已判超时并停止轮询", task_id, max_wait)
        return True

    async def _refresh(self, task_id: str) -> None:
        """向上游查询一次并回写（未完成则保持 PROCESSING）。"""
        row = self.db.query_one("SELECT * FROM media_tasks WHERE id = ?", (task_id,))
        if not row or row["status"] != "PROCESSING":
            return
        if self._check_deadline(task_id, row):
            return
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            try:
                r = await client.get(ASYNC_RESULT.format(id=row["upstream_id"]), headers=headers)
                data = r.json()
            except Exception as e:  # noqa: BLE001
                logger.warning("media task %s 轮询异常: %s", task_id, e)
                return
        status = data.get("task_status", "")
        if status in ("SUCCESS", "success"):
            items = data.get("video_result") or data.get("image_result") or []
            url = items[0].get("url") if items else ""
            if not url:
                self._update(task_id, status="FAIL", error="上游成功但未返回结果链接")
                return
            try:
                file_path, mime, nbytes = await self._download_and_store(task_id, url, row["kind"])
            except Exception as e:  # noqa: BLE001
                self._update(task_id, status="FAIL", error=f"产物下载失败: {e}")
                return
            self._update(task_id, status="SUCCESS", result_url=url,
                         file_path=file_path, mime=mime, size_bytes=nbytes)
        elif status in ("FAIL", "fail"):
            self._update(task_id, status="FAIL",
                         error=json.dumps(data, ensure_ascii=False)[:500])

    async def wait_task(self, task_id: str, timeout: float = POLL_MAX_WAIT) -> dict[str, Any]:
        """阻塞轮询直到完成（供测试与脚本使用；HTTP 层请用短轮询）。"""
        waited = 0.0
        while waited < timeout:
            task = await self.get_task(task_id, poll=True)
            if task["status"] in ("SUCCESS", "FAIL"):
                return task
            await _sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
        raise MediaGenError(f"任务 {task_id} 轮询超时（{timeout}s）")

    # ------------------------------------------------------------------ #
    # 停止（用户主动取消）
    # ------------------------------------------------------------------ #
    def stop_task(self, task_id: str) -> dict[str, Any]:
        """用户主动停止生成：SUBMITTING/PROCESSING → CANCELLED。

        语义边界（勿改）：
        - 智谱异步任务**无取消 API**，上游任务可能仍在排队/生成；本端能做的
          是即刻断开——状态置 CANCELLED 后，``get_task(poll=True)`` 的活跃
          判定与 ``_refresh`` 都不再覆盖它：既不继续查询智谱，也不把上游
          迟到的成功产物下载落盘。
        - 幂等：已终态（SUCCESS/FAIL/CANCELLED）再停止为空操作，不改写既有
          状态与错误原因（刷新后仍能看到真实的成功/失败记录）。
        """
        row = self.db.query_one("SELECT * FROM media_tasks WHERE id = ?", (task_id,))
        if not row:
            raise MediaGenError(f"任务不存在: {task_id}")
        if row["status"] in ("SUBMITTING", "PROCESSING"):
            self._update(task_id, status="CANCELLED", error="用户已停止生成")
            logger.info("media task %s 被用户手动停止", task_id)
            row = self.db.query_one(
                "SELECT * FROM media_tasks WHERE id = ?", (task_id,)) or row
        return self._to_dict(row)

    def read_file(self, task_id: str) -> tuple[bytes, str]:
        row = self.db.query_one("SELECT * FROM media_tasks WHERE id = ?", (task_id,))
        if not row or not row["file_path"]:
            raise MediaGenError("任务无产物")
        p = Path(row["file_path"])
        if not p.exists():
            raise MediaGenError(f"产物文件缺失: {p}")
        return p.read_bytes(), row["mime"] or "application/octet-stream"

    def list_tasks(self, session_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if session_id:
            rows = self.db.query(
                "SELECT * FROM media_tasks WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT ?", (session_id, limit))
        else:
            rows = self.db.query(
                "SELECT * FROM media_tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._to_dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 内部：HTTP / 落盘 / DB
    # ------------------------------------------------------------------ #
    async def _post_with_retry(self, endpoint: str, payload: dict[str, Any],
                               headers: dict[str, str], attempts: int) -> dict[str, Any]:
        last: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for attempt in range(max(1, attempts)):
                try:
                    r = await client.post(endpoint, json=payload, headers=headers)
                    data = r.json()
                except Exception as e:  # noqa: BLE001
                    last = {"code": "NETWORK_ERROR", "message": str(e)}
                    if attempt < attempts - 1:
                        await _sleep(5.0 * (attempt + 1))
                    continue
                err = data.get("error")
                if not err:
                    return data
                last = err
                code = str(err.get("code", ""))
                if code in RETRYABLE_CODES and attempt < attempts - 1:
                    wait = 20.0 * (attempt + 1)
                    logger.warning("上游过载 %s（%s），%.0fs 后重试 %d/%d",
                                   err.get("message", ""), code, wait, attempt + 2, attempts)
                    await _sleep(wait)
        raise MediaGenError(f"上游错误: {last}")

    @staticmethod
    def _as_image_url(path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://", "data:")):
            return path_or_url
        p = Path(path_or_url)
        if not p.exists():
            raise MediaGenError(f"图片不存在: {path_or_url}")
        raw = p.read_bytes()
        if len(raw) > MAX_IMAGE_BYTES:
            raise MediaGenError("图生视频图片超过 5MB 限制")
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(raw).decode()

    @staticmethod
    def _extract_sync_url(resp: dict[str, Any]) -> str:
        try:
            return (resp.get("data") or [{}])[0].get("url", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    async def _download_and_store(self, task_id: str, url: str, kind: str) -> tuple[str, str, int]:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url)
            data = r.content
        if not self._validate(data, kind):
            raise MediaGenError(f"产物格式非法（前 32 字节={data[:32]!r}）")
        ext = ".jpg" if data.startswith(b"\xff\xd8") else ".png" if data.startswith(b"\x89PNG") \
            else ".mp4" if len(data) > 8 and data[4:8] == b"ftyp" else (".mp4" if kind == "video" else ".png")
        mime = "video/mp4" if ext == ".mp4" else ("image/jpeg" if ext == ".jpg" else "image/png")
        path = self.data_dir / f"{task_id}{ext}"
        path.write_bytes(data)
        logger.info("media task %s 产物已落盘: %s（%d bytes）", task_id, path, len(data))
        return str(path), mime, len(data)

    @staticmethod
    def _validate(data: bytes, kind: str) -> bool:
        if not data:
            return False
        if kind == "image":
            return data.startswith(_MAGIC_IMAGE)
        return data[4:8] == b"ftyp" or data[:4] in (b"\x1a\x45\xdf\xa3", b"RIFF")

    def _insert(self, task_id: str, session_id: str, kind: str, model: str, provider_id: str,
                prompt: str, params: dict[str, Any], upstream_id: str, status: str,
                error: str, result_url: str, file_path: str, mime: str, size_bytes: int) -> None:
        self.db.execute(
            "INSERT INTO media_tasks (id, session_id, kind, model, provider_id, prompt, "
            "params_json, upstream_id, status, error, result_url, file_path, mime, size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, session_id, kind, model, provider_id, prompt, json.dumps(params),
             upstream_id, status, error, result_url, file_path, mime, size_bytes),
        )

    def _update(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        sets += ", updated_at = datetime('now')"
        self.db.execute(f"UPDATE media_tasks SET {sets} WHERE id = ?",  # noqa: S608
                        (*fields.values(), task_id))

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        params = {}
        try:
            params = json.loads(row["params_json"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "kind": row["kind"],
            "model": row["model"],
            "provider_id": row["provider_id"],
            "prompt": row["prompt"],
            "params": params,
            # 由服务端计算，避免前端解析 created_at（SQLite 存 UTC）时的时区偏差
            "waited_seconds": _waited_seconds(params),
            "remaining_seconds": _remaining_seconds(params),
            "upstream_id": row["upstream_id"],
            "status": row["status"],
            "error": row["error"],
            "result_url": row["result_url"],
            "has_file": bool(row["file_path"]) and Path(row["file_path"]).exists(),
            "mime": row["mime"],
            "size_bytes": row["size_bytes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
