#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""duration=10 真机探针：cogvideox-flash 当前是否真正支持 10 秒。

背景（2026-10-23 上午实测，勿凭印象改动）：
上游对 duration=10 / 30 **不报错但静默钳位**——产物 mvhd 解析仅 5.11 秒
（timescale=1000, duration=5109）。官方「5/10 秒」是通用说明，10 秒对应
付费 cogvideox-2/3；media_gen 因此硬禁 flash=10 并默认 5 秒（勿回退）。

本脚本在**当前时刻**复测同一行为，用于判断「把默认时长改为 10 秒」是否
具备事实基础：

    提交 duration=10 → 退避重试抢过 1305 高峰 → 轮询到终态 →
    下载产物 → 解析 mvhd 实际秒数 → 输出结论（SUPPORTED / CLAMPED）

用法：
    python tools/duration_probe.py [--duration 10] [--attempts 12]
                                  [--base-wait 90] [--max-wait 2400]

结论写 stdout 与 outputs/duration_probe_<ts>.log；产物无论 5 秒 / 10 秒都
落 outputs/duration_probe_<ts>.mp4 供复核。仅标准库；智谱 key 解析与
提交重试复用 tools/media_gen.py（环境变量 ZHIPU_API_KEY > 项目根 key.txt）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_gen import (  # noqa: E402
    COGVIDEO_FLASH_SIZES,
    VIDEO_MODEL,
    _download,
    _http_json,
    _validate,
    load_zhipu_key,
    submit_with_retry,
)

ZHIPU_ASYNC_RESULT = "https://open.bigmodel.cn/api/paas/v4/async-result/{id}"


def _project_root() -> Path:
    p = Path(__file__).resolve().parent
    for _ in range(4):
        if (p / "key.txt").exists() or (p / "backend").is_dir():
            return p
        p = p.parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()


def mp4_duration_seconds(data: bytes) -> float | None:
    """解析 mp4 mvhd box 得到实际时长（秒）。

    version 0：version+flags(4B) → creation(4B) → modified(4B) → timescale(4B) → duration(4B)
    version 1：version+flags(4B) → creation(8B) → modified(8B) → timescale(4B) → duration(8B)
    返回 None 表示未找到 mvhd（非 mp4 或结构异常）。
    """
    idx = data.find(b"mvhd")
    if idx < 0 or idx + 32 > len(data):
        return None
    p = idx + 4  # 跳过 'mvhd' type
    version = data[p]
    p += 4  # version(1B) + flags(3B)
    if version == 1:
        p += 16  # creation + modified（各 8B）
        timescale = int.from_bytes(data[p:p + 4], "big")
        duration = int.from_bytes(data[p + 4:p + 12], "big")
    else:
        p += 8  # creation + modified（各 4B）
        timescale = int.from_bytes(data[p:p + 4], "big")
        duration = int.from_bytes(data[p + 4:p + 8], "big")
    if timescale <= 0:
        return None
    return duration / timescale


class Logger:
    """同时写 stdout 与日志文件（后台运行时靠文件回看进度）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh = path.open("w", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="cogvideox-flash duration 真机探针")
    ap.add_argument("--duration", type=int, default=10, help="要验证的时长（秒）")
    ap.add_argument("--size", default="720x480", choices=COGVIDEO_FLASH_SIZES,
                    help="最小档最快出片；时长结论与尺寸无关")
    ap.add_argument("--quality", default="speed", choices=["speed", "quality"])
    ap.add_argument("--attempts", type=int, default=12, help="1305 高峰提交重试次数")
    ap.add_argument("--base-wait", type=float, default=90.0, help="重试基础退避（秒）")
    ap.add_argument("--max-wait", type=int, default=2400, help="接单后最长等待（秒）")
    args = ap.parse_args()

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = PROJECT_ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)
    log = Logger(out_dir / f"duration_probe_{ts}.log")
    mp4_path = out_dir / f"duration_probe_{ts}.mp4"

    api_key = load_zhipu_key()
    if not api_key:
        log("FAIL: 未配置智谱 key（环境变量 ZHIPU_API_KEY 或 key.txt）")
        log.close()
        return 1

    payload = {
        "model": VIDEO_MODEL,
        "prompt": "一只小猫在草地上奔跑，固定机位",
        "size": args.size,
        "fps": 30,
        "quality": args.quality,
        "duration": args.duration,
        "with_audio": False,
        "watermark_enabled": False,
    }
    log(f"探针开始 | model={VIDEO_MODEL} duration={args.duration}s "
        f"size={args.size} quality={args.quality}")
    log(f"attempts={args.attempts} base_wait={args.base_wait}s max_wait={args.max_wait}s")

    # ── 提交（1305 高峰期退避重试） ──────────────────────────────
    resp = submit_with_retry(api_key, payload, attempts=args.attempts,
                             base_wait=args.base_wait)
    if "error" in resp:
        log(f"FAIL: 提交始终失败: {resp['error']}")
        log.close()
        return 1
    task_id = resp.get("id")
    if not task_id:
        log(f"FAIL: 上游未返回任务 id: {resp}")
        log.close()
        return 1
    log(f"已接单 upstream={task_id}")

    # ── 轮询到终态 ──────────────────────────────────────────────
    waited = 0
    video_url = ""
    while waited < args.max_wait:
        time.sleep(20)
        waited += 20
        r = _http_json(ZHIPU_ASYNC_RESULT.format(id=task_id),
                       headers={"Authorization": f"Bearer {api_key}"})
        status = r.get("task_status", "")
        log(f"等待 {waited}s | status={status}")
        if status in ("SUCCESS", "success"):
            items = r.get("video_result") or []
            video_url = items[0].get("url", "") if items else ""
            if not video_url:
                log(f"FAIL: 上游成功但无 video_result: {r}")
                log.close()
                return 1
            break
        if status in ("FAIL", "fail"):
            log(f"FAIL: 上游终止: {r}")
            log.close()
            return 1
    else:
        log(f"FAIL: 轮询超时（{args.max_wait}s），任务 {task_id} 未出终态")
        log.close()
        return 1

    # ── 下载 + mvhd 解析 ────────────────────────────────────────
    data = _download(video_url)
    if not _validate(data, "video"):
        log(f"FAIL: 产物格式非法（前 32B={data[:32]!r}）")
        log.close()
        return 1
    mp4_path.write_bytes(data)
    actual = mp4_duration_seconds(data)
    if actual is None:
        log(f"FAIL: 未解析到 mvhd（产物 {len(data)} bytes 已留存 {mp4_path}）")
        log.close()
        return 1

    log(f"产物已留存: {mp4_path}（{len(data)} bytes）")
    log(f"mvhd 实际时长 = {actual:.2f}s（请求 {args.duration}s）")
    if abs(actual - args.duration) < 0.5:
        log(f"VERDICT: SUPPORTED —— cogvideox-flash 当前支持 {args.duration} 秒，"
            f"可把默认时长改为 {args.duration}")
    else:
        log(f"VERDICT: CLAMPED —— 传 {args.duration}s 被上游静默钳为 {actual:.2f}s，"
            f"默认时长维持 5 秒（改为 {args.duration} 只会让界面显示与产物不符）")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
