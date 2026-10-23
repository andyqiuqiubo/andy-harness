"""前后端联调自检：直接打后端 HTTP 接口，验证图像/视频生成全链路。

用法：python tools/e2e_media_check.py [base_url]
默认 http://127.0.0.1:8020
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020"


def req(method, path, payload=None, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
        if path.endswith("/content"):
            return raw
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"__http_error": e.code, "body": e.read().decode("utf-8", "ignore")[:400]}


def main() -> int:
    print("== 1) /api/providers 是否含 zhipu ==")
    providers = req("GET", "/api/providers")
    zp = [p for p in providers if p.get("id") == "zhipu"]
    print("   zhipu:", zp[0]["models"] if zp else "❌ 未注册")
    if not zp:
        return 1

    print("== 2) 文生图 cogview-3-flash ==")
    t = req("POST", "/api/media/generations", {
        "kind": "image", "model": "cogview-3-flash",
        "prompt": "水墨风格远山如黛，云雾缭绕", "size": "1024x1024", "attempts": 6,
    })
    print("   ", json.dumps({k: t.get(k) for k in ("id", "status", "kind", "model", "error")}, ensure_ascii=False))
    if t.get("status") == "PROCESSING":
        t = poll(t["id"])
    ok_img = t.get("status") == "SUCCESS"
    if ok_img:
        blob = req("GET", f"/api/media/tasks/{t['id']}/content")
        print(f"    产物 {len(blob)} bytes, magic={blob[:4].hex()}, mime={t.get('mime')}")
        ok_img = blob[:2] == b"\xff\xd8" or blob[:4] == b"\x89PNG"
    print("   文生图:", "✅" if ok_img else "❌ " + str(t.get("error")))

    print("== 3) 文生视频 cogvideox-flash（异步 + 轮询）==")
    # 注意：免费档高峰会返回 1305「访问量过大」，attempts 太大时提交请求本身的
    # 退避时间会远超 req 超时——这里用 3 次短退避，撞限流则明确报告而非假超时。
    t = req("POST", "/api/media/generations", {
        "kind": "video", "model": "cogvideox-flash",
        "prompt": "一只橘猫在花园里追逐蝴蝶，阳光明媚",
        "size": "720x480", "fps": 30, "attempts": 3,
    })
    print("   ", json.dumps({k: t.get(k) for k in ("id", "status", "upstream_id", "error")}, ensure_ascii=False))
    raw = json.dumps(t, ensure_ascii=False)
    # 免费档高峰限流码出现在错误信封的 message 里（如 "上游错误: {'code': '1305'...}"）
    if "1305" in raw or "1302" in raw:
        print("   文生视频: ⚠️ 智谱免费档高峰限流（1305/1302），非代码问题——稍后重试或调大 attempts")
        ok_vid = None  # 未验证，不算通过也不算失败
    elif t.get("status") not in ("PROCESSING", "SUCCESS"):
        print("   文生视频: ❌ " + raw[:200])
        ok_vid = False
    else:
        started = time.time()
        if t.get("status") == "PROCESSING":
            t = poll(t["id"])
        waited = int(time.time() - started)
        ok_vid = t.get("status") == "SUCCESS"
        if ok_vid:
            blob = req("GET", f"/api/media/tasks/{t['id']}/content")
            print(f"    产物 {len(blob)} bytes, magic={blob[:8].hex()}, 等待 {waited}s")
            ok_vid = blob[4:8] == b"ftyp"
        print("   文生视频:", "✅" if ok_vid else "❌ " + str(t.get("error")))

    print("== 4) 参数校验（video 用非法 size 应 400）==")
    bad = req("POST", "/api/media/generations", {
        "kind": "video", "model": "cogvideox-flash", "prompt": "x", "size": "1280x720",
    })
    print("   ", bad.get("__http_error"), bad.get("body", "")[:120] if bad.get("__http_error") else bad)

    if ok_vid is None:
        print("\n结论: 生图通过 ✅；视频撞免费档 1305 限流（稍后重试）")
        return 0
    print("\n结论:", "全部通过 ✅" if (ok_img and ok_vid) else "存在失败 ❌")
    return 0 if (ok_img and ok_vid) else 1


def poll(task_id, timeout=900):
    waited = 0
    while waited < timeout:
        time.sleep(10)
        waited += 10
        t = req("GET", f"/api/media/tasks/{task_id}?poll=1")
        print(f"    ...{waited}s status={t.get('status')}")
        if t.get("status") in ("SUCCESS", "FAIL"):
            return t
    return {"status": "TIMEOUT", "error": "轮询超时"}


if __name__ == "__main__":
    sys.exit(main())
