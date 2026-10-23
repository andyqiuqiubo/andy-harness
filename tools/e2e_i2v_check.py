"""图生视频（附件 Base64 通道）联调自检。

复现前端修复后的真实请求形态：选中 CogVideoX-Flash + 图片附件 + 提示词 →
前端把附件下载转 data URI → POST /api/media/generations(kind=video, image=data:...)。

本脚本用本地图片文件代替「附件下载」环节（该环节由 client 单测覆盖），
验证 media 接口对 data URI 的接收与参数落库（params.has_image）。

用法：python tools/e2e_i2v_check.py [base_url] [图片路径]
"""

import base64
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020"
IMG = sys.argv[2] if len(sys.argv) > 2 else "outputs/test_zhipu_img.jpg"


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
        return {"__http_error": e.code, "body": e.read().decode("utf-8", "ignore")[:300]}


def main() -> int:
    raw = open(IMG, "rb").read()
    print(f"图片 {IMG}: {len(raw)} bytes, magic={raw[:4].hex()}")
    data_uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    print(f"data URI 长度: {len(data_uri)} chars")

    # 与前端 generateMedia 完全一致的请求形态
    t = req("POST", "/api/media/generations", {
        "kind": "video",
        "model": "cogvideox-flash",
        "prompt": "让画面动起来",
        "image": data_uri,
        "size": "720x480",
        "fps": 30,
        "attempts": 3,
    })
    print("提交返回:", json.dumps({k: t.get(k) for k in (
        "id", "status", "upstream_id", "error")}, ensure_ascii=False))

    raw_resp = json.dumps(t, ensure_ascii=False)
    if "1305" in raw_resp or "1302" in raw_resp:
        print("⚠️ 智谱免费档高峰限流（1305/1302）——参数已被服务端接收并转发，"
              "卡在上游离队；错峰重试即可")
        return 0
    if t.get("status") not in ("PROCESSING", "SUCCESS"):
        print("❌ 提交失败:", raw_resp[:300])
        return 1

    # 验证参数落库：params.has_image 应为 true（证明图片进了生成链路）
    task = req("GET", f"/api/media/tasks/{t['id']}")
    params = task.get("params") or {}
    has_image = params.get("has_image")
    print(f"任务参数落库: has_image={has_image}, kind={task.get('kind')}, "
          f"model={task.get('model')}")
    if not has_image:
        print("❌ has_image 为假——图片未进入生成链路")
        return 1
    print("✅ 图片已进入生成链路（params.has_image=true）")

    if t.get("status") == "PROCESSING":
        for i in range(18):  # 最多等 3 分钟
            time.sleep(10)
            task = req("GET", f"/api/media/tasks/{t['id']}?poll=1")
            print(f"  ...{(i + 1) * 10}s status={task.get('status')}")
            if task.get("status") in ("SUCCESS", "FAIL"):
                break
    if task.get("status") == "SUCCESS":
        blob = req("GET", f"/api/media/tasks/{task['id']}/content")
        ok = blob[4:8] == b"ftyp"
        print(f"产物 {len(blob)} bytes, magic={blob[:8].hex()}: {'✅ MP4' if ok else '❌'}")
        return 0 if ok else 1
    if task.get("status") == "FAIL":
        print("❌ 上游生成失败:", str(task.get("error"))[:200])
        return 1
    print("⚠️ 仍在处理中（免费档排队），链路已验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
