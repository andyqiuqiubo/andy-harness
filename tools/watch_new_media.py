"""盯最新媒体任务：状态一变就打印（供联调时观察后端进程行为）。

用法：python tools/watch_new_media.py [db_path] [rounds]
每 10s 读一次 data/harness.db 的最新任务，仅在 (id, status) 变化时输出。
"""

import json
import sqlite3
import sys
import time

DB = sys.argv[1] if len(sys.argv) > 1 else "data/harness.db"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 120

last = None
for i in range(ROUNDS):
    try:
        con = sqlite3.connect(DB)
        row = con.execute(
            "SELECT id, kind, model, status, prompt, params_json, upstream_id, error "
            "FROM media_tasks ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        con.close()
    except Exception as e:  # noqa: BLE001
        print(f"[db error] {e}", flush=True)
        time.sleep(10)
        continue

    if row:
        tid, kind, model, status, prompt, pj, upstream, err = row
        params = json.loads(pj or "{}")
        sig = (tid, status)
        if sig != last:
            last = sig
            print(
                f"[{time.strftime('%H:%M:%S')}] {tid[:8]} | {kind}/{model} | {status} "
                f"| has_image={params.get('has_image')} | waited={params.get('deadline_ts', 0) and ''}"
                f"upstream={upstream!r} | prompt={prompt[:24]!r}"
                + (f" | err={err[:80]!r}" if err else ""),
                flush=True,
            )
    time.sleep(10)
print("[watch] 监控结束", flush=True)
