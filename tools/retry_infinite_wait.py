"""复刻一条失败任务持续重试，直到抢到智谱"快窗口"出片。

用法：python tools/retry_infinite_wait.py <源任务id前8位> [base_url]

策略（由 2026-10-23 实测数据决定）：
  - 接单后 **<1 分钟**排队 → 成功（11:58 46s / 14:47 41s）
  - 接单后 **30 分钟以上**排队 → 智谱判 1234「网络错误」（12:26 / 19:56 / 20:55 三次同模式）

所以不能对已接单的任务无限等待：等待超过 QUEUE_CUTOFF_SECONDS 仍未终态，
就放弃这条（本地不取消上游，只是不再等），回到提交阶段重发，赌下一个快窗口。

阶段：
1. 提交循环：attempts=1 快速得知结果；被 1305/1302 拒则等 SUBMIT_RETRY_SECONDS 再试；
2. 已接单：每 POLL_SECONDS 轮询，SUCCESS 立即下载产物；FAIL 或等待超过
   QUEUE_CUTOFF_SECONDS 都回到阶段 1 重发；
3. 全局截止 TOTAL_MAX_HOURS，超时放弃。

只读源任务的 prompt / 尺寸 / 质量 / 参考图，不改动源任务记录。
"""

from __future__ import annotations

import base64
import json
import pathlib
import sqlite3
import sys
import time
import urllib.error
import urllib.request

SRC = sys.argv[1] if len(sys.argv) > 1 else "28c3cc08"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
DB = "backend/data/harness.db"
ATT_ROOT = pathlib.Path("backend/data/attachments")

SUBMIT_RETRY_SECONDS = 90   # 提交被限流后的重试间隔
QUEUE_CUTOFF_SECONDS = 600  # 接单后最多等 10 分钟；慢队列基本必死 1234，早点换发
POLL_SECONDS = 20
TOTAL_MAX_HOURS = 8


def http(method: str, path: str, payload=None, timeout: int = 180):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        # 产物是二进制（MP4/JPEG），只有 /content 端点返回原始字节
        return raw if path.endswith("/content") else json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        return {"__http_error": e.code, "body": e.read().decode("utf-8", "ignore")[:300]}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_source() -> tuple | None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM media_tasks WHERE id LIKE ?", (SRC + "%",)).fetchone()
    con.close()
    return row


def main() -> int:
    row = load_source()
    if row is None:
        log(f"源任务不存在: {SRC}")
        return 1

    src_id = row["id"]
    session_id = row["session_id"]
    prompt = row["prompt"]
    kind = row["kind"]
    model = row["model"]
    params = json.loads(row["params_json"] or "{}")
    size, quality = params.get("size"), params.get("quality", "quality")
    atts = params.get("attachments") or []
    log(f"源任务 {src_id[:8]} | {kind}/{model} | size={size} quality={quality} | prompt={prompt!r}")
    log(f"策略：提交限流重试 {SUBMIT_RETRY_SECONDS}s / 接单后最长等 {QUEUE_CUTOFF_SECONDS}s 换发 / 总时限 {TOTAL_MAX_HOURS}h")

    image = ""
    if atts:
        att = atts[0]
        files = list(ATT_ROOT.glob(f"{session_id}/{att['id']}__*")) if session_id else []
        if not files:
            files = list(ATT_ROOT.glob(f"*/{att['id']}__*"))
        if not files:
            log(f"⚠️ 未找到附件文件 {att['id']}，将以文生视频提交")
        else:
            raw = bytes(files[0].read_bytes())
            mime = att.get("mime") or "image/png"
            image = f"data:{mime};base64," + base64.b64encode(raw).decode()
            log(f"参考图已载入: {files[0].name} ({len(raw)} bytes)")

    def submit() -> dict | None:
        """提交一次；返回接单任务（PROCESSING）或 None（限流/失败，应继续重试）。"""
        payload = {
            "kind": kind or "video", "model": model, "prompt": prompt,
            "image": image or None, "size": size, "quality": quality,
            "session_id": session_id, "attempts": 1,
            "max_wait_seconds": 0,          # 交给脚本控制节奏，不设服务端超时
            "attachments": atts,
        }
        if not payload["image"]:
            payload.pop("image")
        r = http("POST", "/api/media/generations", payload)
        if r.get("__http_error"):
            log(f"  HTTP {r['__http_error']}: {r.get('body', '')[:120]}")
            return None
        status = r.get("status")
        if status == "PROCESSING":
            log(f"  ✅ 已接单 upstream={r.get('upstream_id')!r} task={r['id'][:8]}")
            return r
        if status == "FAIL":
            err = (r.get("error") or "")[:110]
            log(f"  提交失败: {err}")
            if "1305" not in err and "1302" not in err and "1234" not in err:
                log("  非限流/非瞬断错误，停止（可排查后手动重试）")
                raise SystemExit(1)
            return None
        log(f"  未知响应: {json.dumps(r, ensure_ascii=False)[:160]}")
        return None

    deadline = time.time() + TOTAL_MAX_HOURS * 3600
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        log(f"#{attempt} 提交…")
        task = submit()
        if task is None:
            time.sleep(SUBMIT_RETRY_SECONDS)
            continue

        # ── 接单后：限时等待终态，慢队列直接换发 ──────────────
        task_id = task["id"]
        started = time.time()
        while True:
            time.sleep(POLL_SECONDS)
            t = http("GET", f"/api/media/tasks/{task_id}?poll=1")
            status = t.get("status")
            waited = int(time.time() - started)
            log(f"  等待 {waited}s | status={status} | upstream={t.get('upstream_id')!r}")
            if status == "SUCCESS":
                blob = http("GET", f"/api/media/tasks/{task_id}/content")
                if isinstance(blob, dict):  # 错误信封
                    log(f"  产物下载失败: {json.dumps(blob, ensure_ascii=False)[:160]}")
                    return 1
                out = pathlib.Path("outputs") / f"i2v_infinite_{task_id[:8]}.mp4"
                out.parent.mkdir(exist_ok=True)
                out.write_bytes(blob)
                log(f"🎉 生成成功！{len(blob)} bytes -> {out} (magic={blob[:8].hex()})")
                return 0
            if status == "FAIL":
                log(f"  上游终止: {(t.get('error') or '')[:200]}")
                break
            if status not in ("PROCESSING", "SUBMITTING"):
                log(f"  未知状态 {status!r}，换发")
                break
            if waited > QUEUE_CUTOFF_SECONDS:
                log(f"  ⏱ 排队已超 {QUEUE_CUTOFF_SECONDS}s（慢队列，大概率 1234）→ 放弃这条，重新提交抢快窗口")
                break
        # 内层结束（FAIL / 超时 / 未知状态）→ 稍候回到提交循环

    log(f"{TOTAL_MAX_HOURS} 小时内未成功，放弃")
    return 1


if __name__ == "__main__":
    sys.exit(main())
