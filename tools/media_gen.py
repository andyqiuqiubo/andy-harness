#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
免费生图 / 生视频 生成服务（纯云端，零本地推理）
=================================================

按 `docs/reports/免费生图能力落地解决方案-2026-10-22.md` 实现，针对本机硬件
（MX150 2GB + 驱动 398.35 / CUDA 9.2，无法跑本地扩散模型）选择全云端方案。

能力
----
- 文生图   : 主链路 智谱 CogView-3-Flash（免费）→ 兜底 Pollinations（零门槛，无需 key）
- 图生视频 / 文生视频 : 智谱 CogVideoX-Flash（免费，异步任务 + 轮询）

设计约束（均为实测/官方口径，勿随意放宽）
------------------------------------------
1. Pollinations 匿名档：**1 张 / 45 秒 / IP**，且 1024² 实测超时，分辨率必须 ≤768²；
   限流返回 **402 FREE_LIMIT 或 503 SERVICE_UNAVAILABLE**（两种都要退避）。
2. 智谱视频为**异步**：提交拿 id → 轮询 /async-result/{id} → 取 video_result[0].url，
   图片结果链接有效期 30 天，视频链接亦为临时链接，必须及时转存。
3. 所有产物必须做 **magic bytes 校验**，杜绝把错误 JSON 存成图片/视频。
4. 免费档并发受限（公开口径 1~5，以控制台为准），本实现**默认串行 + 节流**。

用法
----
    python tools/media_gen.py image "水墨远山" --size 1024x1024 --out out.png
    python tools/media_gen.py video "一只猫在玩球" --size 1024x1024 --out out.mp4
    python tools/media_gen.py video "让画面动起来" --image first.png --out out.mp4
    python tools/media_gen.py providers          # 查看当前可用链路

Key: 环境变量 ZHIPU_API_KEY 优先，其次从项目根 key.txt 解析「智谱key：xxx」。
依赖：仅标准库（urllib / argparse / json），无需 pip 安装。
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
VIDEO_SUBMIT = ZHIPU_BASE + "/videos/generations"
IMAGE_SUBMIT = ZHIPU_BASE + "/images/generations"
ASYNC_RESULT = ZHIPU_BASE + "/async-result/{id}"

IMAGE_MODEL = "cogview-3-flash"
VIDEO_MODEL = "cogvideox-flash"

# CogView-3-Flash 官方枚举分辨率
COGVIEW_SIZES = [
    "1024x1024", "768x1344", "864x1152", "1344x768",
    "1152x864", "1440x720", "720x1440",
]
# CogVideoX-Flash 官方枚举分辨率（注意：与 cogvideox-3 的 1280x720 系列不同）
COGVIDEO_FLASH_SIZES = [
    "720x480", "1024x1024", "1280x960", "960x1280",
    "1920x1080", "1080x1920", "2048x1080", "3840x2160",
]
FPS_ALLOWED = (30, 60)

# Pollinations：实测 1 张/45 秒/IP，1024² 会超时 → 上限 768
POLLINATIONS_MAX_SIZE = 768
POLLINATIONS_MIN_INTERVAL = 46.0

MAGIC = {
    "image": [(b"\xff\xd8", "jpeg"), (b"\x89PNG\r\n\x1a\n", "png")],
}
HTTP_TIMEOUT = 60
POLL_INTERVAL = 10
POLL_MAX_WAIT = 900          # 视频最长等 15 分钟
DOWNLOAD_TIMEOUT = 180

UA = "andy-harness-media-gen/1.0 (stdlib urllib)"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _detect_project_root() -> Path:
    """从本文件位置向上找项目根（含 key.txt 或 backend 目录）。"""
    p = Path(__file__).resolve().parent
    for _ in range(4):
        if (p / "key.txt").exists() or (p / "backend").is_dir():
            return p
        p = p.parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _detect_project_root()


def load_zhipu_key() -> str:
    """读取智谱 key：环境变量 > 项目根 key.txt（解析『智谱key：xxx』）。"""
    key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if key:
        return key
    kt = PROJECT_ROOT / "key.txt"
    if kt.exists():
        for line in kt.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "智谱" in line and ("：" in line or ":" in line):
                sep = "：" if "：" in line else ":"
                cand = line.split(sep, 1)[1].strip()
                if cand:
                    return cand
    return ""


def _http_json(url: str, payload: Optional[dict] = None,
               headers: Optional[dict] = None, timeout: int = HTTP_TIMEOUT) -> dict:
    """发 JSON 请求（POST 带 body / GET 不带）。返回解析后的 dict。"""
    data = None
    hdr = {"User-Agent": UA}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdr["Content-Type"] = "application/json"
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdr, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            return {"error": {"code": str(e.code), "message": raw[:300].decode("utf-8", "ignore")}}
    except Exception as e:                                  # 超时 / 连接错误
        return {"error": {"code": "NETWORK_ERROR", "message": str(e)}}
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return {"_raw": raw}


def _download(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _validate(data: bytes, kind: str) -> bool:
    """产物 magic bytes 校验：防止把错误 JSON / HTML 当图片视频存盘。"""
    if not data:
        return False
    if kind == "image":
        return any(data.startswith(m) for m, _ in MAGIC["image"])
    if kind == "video":
        return data[4:8] == b"ftyp" or data[:4] in (b"\x1a\x45\xdf\xa3", b"RIFF")
    return bool(data)


def _ext_of(data: bytes, default: str) -> str:
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if len(data) > 8 and data[4:8] == b"ftyp":
        return ".mp4"
    return default


# --------------------------------------------------------------------------- #
# 提交重试：免费档瞬时限流/过载可退避重试（1302 并发超限 / 1305 访问量过大等）
# --------------------------------------------------------------------------- #

RETRYABLE_CODES = {"1302", "1305", "1113", "429", "429001"}


def submit_with_retry(api_key: str, payload: dict, attempts: int = 4,
                      base_wait: float = 20.0) -> dict:
    url = VIDEO_SUBMIT if payload["model"].startswith("cogvideox") else IMAGE_SUBMIT
    headers = {"Authorization": f"Bearer {api_key}"}
    last: dict = {}
    for attempt in range(attempts):
        resp = _http_json(url, payload, headers, timeout=90)
        err = resp.get("error")
        if not err:
            return resp
        last = err
        code = str(err.get("code", ""))
        if code in RETRYABLE_CODES and attempt < attempts - 1:
            wait = base_wait * (attempt + 1)
            print(f"[retry] {err.get('message', '')}（code={code}），{wait:.0f}s 后第 "
                  f"{attempt + 2}/{attempts} 次重试", file=sys.stderr)
            time.sleep(wait)
    return {"error": last}


# --------------------------------------------------------------------------- #
# Provider 1：智谱 CogView-3-Flash（主链路，免费）
# --------------------------------------------------------------------------- #

class ZhipuImage:
    name = "zhipu_cogview3flash"
    needs_key = True

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or load_zhipu_key()

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(self, prompt: str, size: str = "1024x1024",
                 watermark: bool = True) -> bytes:
        """文生图。兼容同步返回 data[0].url 与异步返回 id 两种形态。"""
        if size not in COGVIEW_SIZES:
            raise ValueError(f"size 必须是 {COGVIEW_SIZES}，收到 {size}")
        payload = {"model": IMAGE_MODEL, "prompt": prompt, "size": size,
                   "watermark_enabled": watermark}
        resp = submit_with_retry(self.api_key, payload)
        if "error" in resp:
            raise RuntimeError(f"zhipu image error: {resp['error']}")

        # 形态 A：同步返回（OpenAI 风格）
        url = None
        try:
            url = (resp.get("data") or [{}])[0].get("url")
        except Exception:
            url = None

        # 形态 B：异步任务 → 轮询 async-result 的 image_result
        if not url and resp.get("id"):
            url = self._poll(resp["id"], "image_result", "image")
        if not url:
            raise RuntimeError(f"zhipu image: 无返回 URL，resp={json.dumps(resp, ensure_ascii=False)[:300]}")

        data = _download(url)
        if not _validate(data, "image"):
            raise RuntimeError(f"zhipu image: 产物格式非法（前 32B={data[:32]!r}）")
        return data

    def _poll(self, task_id: str, result_key: str, kind: str) -> Optional[str]:
        waited = 0
        while waited < POLL_MAX_WAIT:
            time.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
            r = _http_json(ASYNC_RESULT.format(id=task_id),
                           headers={"Authorization": f"Bearer {self.api_key}"})
            if "error" in r:
                raise RuntimeError(f"poll error: {r['error']}")
            status = r.get("task_status", "")
            if status in ("SUCCESS", "success"):
                items = r.get(result_key) or []
                if not items:
                    raise RuntimeError(f"task {task_id} 成功但无 {result_key}")
                return items[0].get("url")
            if status in ("FAIL", "fail"):
                raise RuntimeError(f"task {task_id} 失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        raise TimeoutError(f"task {task_id} 轮询超时（{POLL_MAX_WAIT}s）")


# --------------------------------------------------------------------------- #
# Provider 2：Pollinations（零门槛兜底，无需 key）
# --------------------------------------------------------------------------- #

class PollinationsImage:
    """零门槛兜底。三条实测硬约束：≤768²、≥45s/IP、402 与 503 都退避。"""
    name = "pollinations"
    needs_key = False
    _last_call = 0.0

    def available(self) -> bool:
        return True

    def generate(self, prompt: str, size: str = "768x768") -> bytes:
        side = POLLINATIONS_MAX_SIZE
        if size and "x" in size:
            try:
                side = min(int(size.split("x")[0]), POLLINATIONS_MAX_SIZE)
            except ValueError:
                side = POLLINATIONS_MAX_SIZE
        url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
               + f"?width={side}&height={side}&nologo=true&seed=42")

        last_err = "unknown"
        for attempt in range(3):
            wait = POLLINATIONS_MIN_INTERVAL - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            PollinationsImage._last_call = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                    data = r.read()
                if _validate(data, "image"):
                    return data
                last_err = f"非图片响应: {data[:120]!r}"
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")
                try:
                    last_err = json.loads(body)["error"]["message"]
                except Exception:
                    last_err = body[:160]
            except Exception as e:
                last_err = str(e)
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"pollinations 失败: {last_err}")    # 402 / 503 / 超时 均走这里


# --------------------------------------------------------------------------- #
# Provider 3：智谱 CogVideoX-Flash（免费，异步）
# --------------------------------------------------------------------------- #

class ZhipuVideo:
    name = "zhipu_cogvideoxflash"
    needs_key = True

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or load_zhipu_key()

    def available(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _as_image_url(path_or_url: str) -> str:
        """本地图转 data URI（官方要求：png/jpeg/jpg，≤5MB，base64 带 content-type）。"""
        if path_or_url.startswith(("http://", "https://", "data:")):
            return path_or_url
        p = Path(path_or_url)
        if not p.exists():
            raise FileNotFoundError(f"图片不存在: {path_or_url}")
        raw = p.read_bytes()
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("image_url 超过 5MB 限制（CogVideoX）")
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        return "data:" + mime + ";base64," + base64.b64encode(raw).decode()

    def generate(self, prompt: str = "", image: str = "", size: str = "1024x1024",
                 fps: int = 30, quality: str = "speed", with_audio: bool = False,
                 watermark: bool = True, attempts: int = 4, base_wait: float = 20.0) -> bytes:
        if size not in COGVIDEO_FLASH_SIZES:
            raise ValueError(f"size 必须是 {COGVIDEO_FLASH_SIZES}，收到 {size}")
        if fps not in FPS_ALLOWED:
            raise ValueError(f"fps 必须是 30 或 60，收到 {fps}")
        if not prompt and not image:
            raise ValueError("prompt 与 image 不能同时为空")

        payload = {
            "model": VIDEO_MODEL,
            "prompt": prompt or "让画面动起来",
            "size": size,
            "fps": fps,
            "quality": quality,
            "with_audio": with_audio,
            "watermark_enabled": watermark,   # false 需先在「个人中心-安全管理-去水印管理」签署
        }
        if image:
            payload["image_url"] = self._as_image_url(image)   # 传图即切图生视频(I2V)

        resp = submit_with_retry(self.api_key, payload, attempts=attempts, base_wait=base_wait)
        if "error" in resp:
            raise RuntimeError(f"zhipu video error: {resp['error']}")
        task_id = resp.get("id")
        if not task_id:
            raise RuntimeError(f"zhipu video: 未返回任务 id，resp={json.dumps(resp, ensure_ascii=False)[:300]}")
        print(f"[video] 任务已提交 id={task_id}（{'图生视频' if image else '文生视频'}，{size}@{fps}fps）",
              file=sys.stderr)

        video_url = self._poll(task_id)
        data = _download(video_url)
        if not _validate(data, "video"):
            raise RuntimeError(f"zhipu video: 产物格式非法（前 32B={data[:32]!r}）")
        return data

    def _poll(self, task_id: str) -> str:
        waited = 0
        while waited < POLL_MAX_WAIT:
            time.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
            r = _http_json(ASYNC_RESULT.format(id=task_id),
                           headers={"Authorization": f"Bearer {self.api_key}"})
            if "error" in r:
                raise RuntimeError(f"poll error: {r['error']}")
            status = r.get("task_status", "")
            print(f"[video] {waited}s status={status}", file=sys.stderr)
            if status in ("SUCCESS", "success"):
                items = r.get("video_result") or []
                if not items or not items[0].get("url"):
                    raise RuntimeError(f"task {task_id} 成功但无 video_result")
                return items[0]["url"]
            if status in ("FAIL", "fail"):
                raise RuntimeError(f"task {task_id} 失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        raise TimeoutError(f"task {task_id} 轮询超时（{POLL_MAX_WAIT}s）")


# --------------------------------------------------------------------------- #
# 服务层：降级链
# --------------------------------------------------------------------------- #

def generate_image(prompt: str, size: str = "1024x1024",
                   out: Optional[str] = None, providers: Optional[list] = None,
                   watermark: bool = True) -> str:
    """文生图：智谱主链路 → Pollinations 零门槛兜底。"""
    providers = providers or ["zhipu_cogview3flash", "pollinations"]
    errors = []
    for name in providers:
        try:
            if name == "zhipu_cogview3flash":
                p = ZhipuImage()
                if not p.available():
                    errors.append(f"{name}: 未配置 ZHIPU_API_KEY")
                    continue
                data = p.generate(prompt, size, watermark=watermark)
            elif name == "pollinations":
                data = PollinationsImage().generate(prompt, size)
            else:
                errors.append(f"{name}: 未知 provider")
                continue
            return _save(data, out or "gen_image", "image")
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[warn] {name} 失败 -> {e}", file=sys.stderr)
            continue
    raise RuntimeError("全部 provider 失败 -> " + " | ".join(errors))


def generate_video(prompt: str = "", image: str = "", size: str = "1024x1024",
                   fps: int = 30, quality: str = "speed", with_audio: bool = False,
                   watermark: bool = True, out: Optional[str] = None,
                   attempts: int = 4, base_wait: float = 20.0) -> str:
    """文生/图生视频：智谱 CogVideoX-Flash（免费，异步）。"""
    p = ZhipuVideo()
    if not p.available():
        raise RuntimeError("未配置 ZHIPU_API_KEY（环境变量或 key.txt）")
    data = p.generate(prompt=prompt, image=image, size=size, fps=fps,
                      quality=quality, with_audio=with_audio, watermark=watermark,
                      attempts=attempts, base_wait=base_wait)
    return _save(data, out or "gen_video", "video")


def _save(data: bytes, out: str, kind: str) -> str:
    path = Path(out)
    if path.is_dir() or out.endswith(("/", "\\")):
        path.mkdir(parents=True, exist_ok=True)
        path = path / f"gen_{kind}_{int(time.time())}"
    if not path.suffix:
        path = path.with_suffix(_ext_of(data, ".png" if kind == "image" else ".mp4"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"[ok] 已保存 {path}（{len(data)} bytes）")
    return str(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="免费生图/生视频（纯云端）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("image", help="文生图")
    pi.add_argument("prompt")
    pi.add_argument("--size", default="1024x1024", choices=COGVIEW_SIZES)
    pi.add_argument("--out", default="outputs/gen_image")
    pi.add_argument("--provider", action="append", help="可多次指定，默认 zhipu -> pollinations")
    pi.add_argument("--no-watermark", action="store_true",
                    help="去水印（需已在智谱平台签署免责声明，否则可能报错）")

    pv = sub.add_parser("video", help="文生/图生视频（异步）")
    pv.add_argument("prompt", nargs="?", default="")
    pv.add_argument("--image", default="", help="本地路径或 URL，传图即为图生视频")
    pv.add_argument("--size", default="1024x1024", choices=COGVIDEO_FLASH_SIZES)
    pv.add_argument("--fps", type=int, default=30, choices=list(FPS_ALLOWED))
    pv.add_argument("--quality", default="speed", choices=["speed", "quality"])
    pv.add_argument("--audio", action="store_true", help="生成 AI 音效")
    pv.add_argument("--no-watermark", action="store_true", help="去水印（需已签署免责声明）")
    pv.add_argument("--attempts", type=int, default=4, help="1305 过载重试次数")
    pv.add_argument("--out", default="outputs/gen_video")

    sub.add_parser("providers", help="查看当前可用链路")

    a = ap.parse_args()
    if a.cmd == "providers":
        key = load_zhipu_key()
        print(f"zhipu_cogview3flash  : {'可用' if key else '不可用（缺 ZHIPU_API_KEY）'}")
        print(f"zhipu_cogvideoxflash : {'可用' if key else '不可用（缺 ZHIPU_API_KEY）'}")
        print("pollinations        : 可用（零门槛，≤768²，45s/IP）")
        print(f"本地推理            : 不可用（本机 MX150 2GB / CUDA 9.2，见解决方案文档 §1.2）")
        return 0

    if a.cmd == "image":
        generate_image(a.prompt, a.size, a.out, a.provider, watermark=not a.no_watermark)
    else:
        generate_video(prompt=a.prompt, image=a.image, size=a.size, fps=a.fps,
                       quality=a.quality, with_audio=a.audio,
                       watermark=not a.no_watermark, out=a.out,
                       attempts=a.attempts, base_wait=30.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
