"""盯一个 media 任务直到出结果（轮询后端，最长 20 分钟）。

用法：python tools/watch_media_task.py <task_id> [base_url]
"""

import json
import sys
import time
import urllib.request

TASK = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8020"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode())


for i in range(120):
    # poll=1 让后端起一个向上游的查询并回写（不依赖前端是否还在轮询）
    t = get(f"/api/media/tasks/{TASK}?poll=1")
    print(f"[{(i + 1) * 10}s] status={t.get('status')} "
          f"upstream={t.get('upstream_id')} file={t.get('has_file')} "
          f"size={t.get('size_bytes')} err={str(t.get('error'))[:60]}", flush=True)
    if t.get("status") in ("SUCCESS", "FAIL"):
        if t.get("has_file"):
            with urllib.request.urlopen(f"{BASE}/api/media/tasks/{TASK}/content", timeout=120) as r:
                blob = r.read()
                cd = r.headers.get("content-disposition", "")
            print(f"产物 {len(blob)} bytes magic={blob[:8].hex()} disposition={cd}", flush=True)
        break
    time.sleep(10)
else:
    print("20 分钟未出结果（智谱免费档排队中）", flush=True)
