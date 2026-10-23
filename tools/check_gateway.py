#!/usr/bin/env python3
"""Andy Gateway 免费通道自检。

检查三件事：
1. 本机登录态文件是否存在且结构完整（Cline CLI / Magpie 的 WorkBuddy、Qoder CN）；
2. 运行中的后端是否把四个免费通道注册成了 Provider；
3. 可用性快照里每个通道有多少模型可用。

只输出「是否存在/是否可用」，**不打印任何 token、邮箱、机器码**。

用法：
    python tools/check_gateway.py
    python tools/check_gateway.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HOME = Path.home()
CLINE_STATE = HOME / ".cline" / "data" / "settings" / "providers.json"
MAGPIE_STATE = HOME / ".config" / "magpie" / "plugin-auth.json"


def _load(path: Path) -> tuple[dict[str, Any] | None, str]:
    """返回 (data, 错误说明)；文件不存在时 data=None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "文件不存在"
    except json.JSONDecodeError:
        return None, "不是合法 JSON"
    except OSError as exc:
        return None, f"读取失败: {exc}"


def _expiry_note(raw: Any) -> str:
    """不打印 token，只把过期时间转成可读说明（兼容 ISO 字符串与秒/毫秒时间戳）。"""
    if raw is None:
        return ""
    when: datetime | None = None
    if isinstance(raw, str):
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    else:
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            return ""
        if ts > 10_000_000_000:  # 毫秒
            ts /= 1000.0
        try:
            when = datetime.fromtimestamp(ts, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = datetime.now(tz=UTC)
    if when < now:
        return "已过期"
    left = when - now
    return f"有效（剩余 {left.days}天{left.seconds // 3600}小时）"


def check_official_keys() -> list[tuple[str, bool, str]]:
    """官方 API Key（主线）：只看环境变量是否设置，不输出任何内容。"""
    rows: list[tuple[str, bool, str]] = []
    for label, env in (("Qwen 百炼", "DASHSCOPE_API_KEY"), ("豆包方舟", "ARK_API_KEY")):
        value = os.environ.get(env, "")
        if value:
            rows.append((label, True, f"环境变量 {env} 已设置（长度 {len(value)}）"))
        else:
            rows.append((label, False, f"未设置环境变量 {env}"))
    return rows


def check_credentials() -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []

    cline, cline_err = _load(CLINE_STATE)
    if cline is None:
        rows.append(("Cline", False, f"{CLINE_STATE} —— {cline_err}（需先安装并登录 Cline CLI）"))
    else:
        try:
            auth = cline["providers"]["cline"]["settings"]["auth"]
            ok = bool(auth.get("accessToken") or auth.get("refreshToken"))
            note = _expiry_note(auth.get("expiresAt"))
            rows.append(("Cline", ok, f"{CLINE_STATE} —— " + ("已登录 " + note if ok else "缺少 token，请重新登录")))
        except (KeyError, TypeError):
            rows.append(("Cline", False, f"{CLINE_STATE} —— 结构异常（缺少 providers.cline.settings.auth）"))

    rows.append(("OpenCode Zen Free", True, "公开匿名通道（Bearer public），无需登录"))

    magpie, magpie_err = _load(MAGPIE_STATE)
    if magpie is None:
        rows.append(("WorkBuddy", False, f"{MAGPIE_STATE} —— {magpie_err}（需通过 Magpie 登录 WorkBuddy）"))
        rows.append(("Qoder CN", False, f"{MAGPIE_STATE} —— {magpie_err}（需通过 Magpie 登录 Qoder CN）"))
    else:
        wb = magpie.get("workbuddy") if isinstance(magpie, dict) else None
        wb_ok = isinstance(wb, dict) and bool(wb.get("access"))
        wb_note = "已登录" if wb_ok else "缺少 access，请重新登录"
        rows.append(("WorkBuddy", wb_ok, f"{MAGPIE_STATE} → workbuddy —— {wb_note}"))

        qoder = magpie.get("qoder-cn") if isinstance(magpie, dict) else None
        q_ok = isinstance(qoder, dict) and bool(qoder.get("access") or qoder.get("deviceToken"))
        q_machine = isinstance(qoder, dict) and bool(qoder.get("machineId"))
        if q_ok and q_machine:
            rows.append(("Qoder CN", True, f"{MAGPIE_STATE} → qoder-cn —— 已登录（含 machineId）"))
        elif q_ok:
            rows.append(("Qoder CN", False, "qoder-cn 缺少 machineId，请重新登录"))
        else:
            rows.append(("Qoder CN", False, f"{MAGPIE_STATE} → qoder-cn —— 缺少 access/deviceToken，请重新登录"))
    return rows


def _get(url: str, timeout: float = 5.0) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # 本机固定地址
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return 0, None


def check_backend(base: str) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    status, _ = _get(f"{base}/api/health")
    if status != 200:
        return False, {}, {}
    _, providers = _get(f"{base}/api/providers")
    _, availability = _get(f"{base}/api/gateway/model-availability")
    prov_list = providers if isinstance(providers, list) else []
    avail = availability if isinstance(availability, dict) else {}
    return True, prov_list, avail


def main() -> int:
    parser = argparse.ArgumentParser(description="Andy Gateway 免费通道自检")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    args = parser.parse_args()

    print("=" * 72)
    print("① 官方 API Key（主线：一个 Key 即用）")
    print("=" * 72)
    for name, ok, detail in check_official_keys():
        mark = "✅" if ok else "❌"
        print(f"{mark} {name:<18} {detail}")
    print()
    print("=" * 72)
    print("② 客户端免费通道登录态（可选增强，默认关闭）")
    print("=" * 72)
    cred_rows = check_credentials()
    for name, ok, detail in cred_rows:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name:<18} {detail}")

    print()
    print("=" * 72)
    print(f"③ 后端 {args.base}")
    print("=" * 72)
    alive, providers, availability = check_backend(args.base)
    if not alive:
        print("❌ 无法访问 /api/health —— 后端没启动？先运行 start-all.bat 或 uvicorn")
    else:
        print("✅ /api/health = ok")
        gw = [p for p in providers if str(p.get("id", "")).startswith("gw-")]
        if not gw:
            print("❌ /api/providers 里没有 gw-* Provider —— 未配 Key / 未登录，或网关未启动")
        for p in gw:
            print(f"   • {p.get('id')} / {p.get('name')} —— {len(p.get('models') or [])} 个模型")

        channels = (availability or {}).get("channels") or {}
        print()
        print("④ 可用性快照")
        for cid in ("qwen-bailian", "doubao-ark", "zen", "cline", "workbuddy", "qoder-cn"):
            ch = channels.get(cid)
            if not ch:
                print(f"   • {cid:<12} 未出现在快照中")
                continue
            models = ch.get("models") or {}
            usable = sum(1 for m in models.values() if m.get("available"))
            print(
                f"   • {cid:<12} enabled={ch.get('enabled')} "
                f"available={ch.get('available')} 模型 {usable}/{len(models)} 可用"
            )

    print()
    print("=" * 72)
    print("诊断建议")
    print("=" * 72)
    key_rows = check_official_keys()
    key_ok = any(ok for _, ok, _ in key_rows)
    missing = [name for name, ok, _ in cred_rows if not ok]
    if key_ok:
        print("• 官方 API Key 已配置：会话页应出现「Qwen 百炼」或「豆包方舟」，这是主线用法。")
    else:
        print("• 未配置官方 API Key：`set DASHSCOPE_API_KEY=sk-...` 或 `set ARK_API_KEY=...` 后重启后端。")
    if missing:
        print(f"• 未就绪的客户端通道：{', '.join(missing)}（可选增强，默认关闭，需要时再登录）。")
    else:
        print("• 客户端通道登录态齐全；若仍无回复，请查看后端控制台是否有 401/403/CRED_INVALID。")
    print("• 注意：客户端免费额度带客户端身份门禁，属服务条款灰区、接口易变，建议仅作补充。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
