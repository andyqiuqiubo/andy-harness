"""Frida MCP server —— 把 Frida 动态插桩能力以 MCP（stdio）方式暴露给 AI。

定位：REA 负责"静态看结构"，本服务负责"动态看行为"。它以 stdio JSON-RPC 形式实现
MCP 最小可用子集（initialize / tools/list / tools/call / ping），与 andy-harness 的
MCP 客户端 ``harness.modules.mcp_client.service`` 直接对话，无需任何 MCP SDK。

设计约束：
- **仅本地进程**：不依赖 frida-server / 越狱设备，使用 ``frida.get_local_device()``；
- **进程必杀**：spawn 出来的目标一律在超时后 kill，杜绝残留；
- **stdout 纯净**：所有日志走 stderr，stdout 只承载 JSON-RPC 帧；
- **体积收敛**：只暴露 5 个工具，避免工具定义挤占模型上下文。

工具清单：
  1. frida_status             —— Frida 运行时与本地设备可用性
  2. frida_list_processes     —— 枚举本地进程
  3. frida_list_modules       —— 枚举指定进程的模块
  4. frida_spawn_instrument   —— 派生目标并注入自定义 JS 脚本
  5. frida_run_and_trace      —— 派生目标并采集行为轨迹（模块/内存特征/API 调用/退出码）

运行：``<frida-venv>/Scripts/python.exe server.py``
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

LOG = logging.getLogger("frida-mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "frida-mcp", "version": "1.0.0"}

# ── 工具契约 ────────────────────────────────────────────────────────────
TOOLS: list[dict[str, Any]] = [
    {
        "name": "frida_status",
        "description": "查询 Frida 运行时版本与本地设备可用性。无副作用，用于能力自检。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "frida_list_processes",
        "description": "枚举本地可附加的进程（pid / 名称）。用于选定动态分析目标。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "最多返回条数，默认 50"}},
        },
    },
    {
        "name": "frida_list_modules",
        "description": "枚举指定进程的已加载模块（名称 / 基址 / 大小 / 路径）。",
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer", "description": "目标进程 pid"}},
            "required": ["pid"],
        },
    },
    {
        "name": "frida_spawn_instrument",
        "description": (
            "派生（spawn）指定可执行文件并注入自定义 Frida JS 脚本，采集脚本 send() 出的消息，"
            "超时后强制结束目标进程。脚本中可用 send(obj) / console.log。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "可执行文件绝对路径"},
                "script": {"type": "string", "description": "Frida JS 脚本源码"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "命令行参数"},
                "timeout_ms": {"type": "integer", "description": "采集时长（毫秒），默认 5000"},
            },
            "required": ["target", "script"],
        },
    },
    {
        "name": "frida_run_and_trace",
        "description": (
            "对指定可执行文件做一次受控动态运行并采集行为轨迹：加载模块清单、主模块内存特征扫描"
            "（验证静态分析发现的字符串是否真的进入运行时内存）、指定 API 调用钩子、退出码。"
            "目标进程为一次性派生，结束后强制终止，不影响系统。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "可执行文件绝对路径"},
                "scan_strings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要在主模块内存中扫描的 ASCII 特征串（通常来自 REA 静态分析的字符串结果）",
                },
                "hook_exports": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要挂钩的导出 API 名称，留空则只挂 ExitProcess",
                },
                "timeout_ms": {"type": "integer", "description": "采集时长（毫秒），默认 6000"},
            },
            "required": ["target"],
        },
    },
]

TOOL_HANDLERS: dict[str, Any] = {}


def _ok(text: str, **extra: Any) -> dict[str, Any]:
    """构造 MCP 文本结果。"""
    return {"content": [{"type": "text", "text": text}], "isError": False, **extra}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _import_frida() -> Any:
    try:
        import frida  # noqa: PLC0415

        return frida
    except Exception as exc:  # pragma: no cover - 环境相关
        raise RuntimeError(
            f"frida 未安装或不可用：{exc}。请在项目隔离环境安装："
            "uv pip install frida frida-tools"
        ) from exc


# ── 工具实现 ────────────────────────────────────────────────────────────
def tool(name: str):  # noqa: ANN201
    def deco(fn):  # noqa: ANN001, ANN202
        TOOL_HANDLERS[name] = fn
        return fn

    return deco


@tool("frida_status")
def _status(_args: dict[str, Any]) -> dict[str, Any]:
    frida = _import_frida()
    dev = frida.get_local_device()
    info = {
        "frida_version": frida.__version__,
        "device": {"id": dev.id, "name": dev.name, "type": dev.type},
        "available": True,
    }
    return _ok(json.dumps(info, ensure_ascii=False, indent=2))


@tool("frida_list_processes")
def _list_processes(args: dict[str, Any]) -> dict[str, Any]:
    frida = _import_frida()
    limit = int(args.get("limit") or 50)
    dev = frida.get_local_device()
    procs = [
        {"pid": p.pid, "name": p.name}
        for p in dev.enumerate_processes()[:limit]
    ]
    return _ok(json.dumps({"count": len(procs), "processes": procs}, ensure_ascii=False, indent=2))


@tool("frida_list_modules")
def _list_modules(args: dict[str, Any]) -> dict[str, Any]:
    frida = _import_frida()
    pid = int(args["pid"])
    dev = frida.get_local_device()
    session = dev.attach(pid)
    try:
        script = session.create_script(
            "send(JSON.stringify(Process.enumerateModules().map(m=>({name:m.name,base:m.base.toString(),size:m.size,path:m.path}))));"
        )
        out: list[Any] = []
        script.on("message", lambda m, d: out.append(m.get("payload")))  # noqa: ARG005
        script.load()
        deadline = time.time() + 8
        while not out and time.time() < deadline:
            time.sleep(0.05)
        return _ok(json.dumps({"pid": pid, "modules": json.loads(out[0]) if out else []}, ensure_ascii=False))
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001
            pass


def _spawn_and_collect(
    target: str, script_src: str, timeout_ms: int, args: list[str] | None
) -> dict[str, Any]:
    """派生目标 → 注入脚本 → 采集消息 → 强制终止。"""
    frida = _import_frida()
    dev = frida.get_local_device()
    argv = [target] + list(args or [])
    pid = dev.spawn(argv)
    session = dev.attach(pid)
    messages: list[Any] = []
    errors: list[str] = []

    def on_message(message: dict[str, Any], _data: Any) -> None:
        if message.get("type") == "send":
            messages.append(message.get("payload"))
        elif message.get("type") == "error":
            errors.append(str(message.get("description") or message.get("stack") or message))

    script = session.create_script(script_src)
    script.on("message", on_message)
    script.load()
    dev.resume(pid)

    deadline = time.time() + max(0.5, timeout_ms / 1000.0)
    exited = False
    while time.time() < deadline:
        if any(isinstance(m, dict) and m.get("type") == "exit_code" for m in messages):
            exited = True
            break
        time.sleep(0.05)

    try:
        script.unload()
    except Exception:  # noqa: BLE001
        pass
    try:
        session.detach()
    except Exception:  # noqa: BLE001
        pass
    try:
        dev.kill(pid)
    except Exception:  # noqa: BLE001
        pass  # 进程可能已自然退出

    return {"pid": pid, "messages": messages, "errors": errors, "exited": exited}


@tool("frida_spawn_instrument")
def _spawn_instrument(args: dict[str, Any]) -> dict[str, Any]:
    target = args["target"]
    res = _spawn_and_collect(
        target,
        args["script"],
        int(args.get("timeout_ms") or 5000),
        args.get("args") or [],
    )
    return _ok(json.dumps(res, ensure_ascii=False, indent=2)[:60000])


TRACE_SCRIPT_TEMPLATE = """
'use strict';
var SCAN = %(scan)s;
var HOOKS = %(hooks)s;
var TARGET = %(target)s;

function hexPattern(s) {
  var out = [];
  for (var i = 0; i < s.length; i++) {
    var c = s.charCodeAt(i) & 0xff;
    out.push((c < 16 ? '0' : '') + c.toString(16));
  }
  return out.join(' ');
}

var result = { modules: [], scans: [], calls: [], exit_code: null, errors: [] };

try {
  var mods = Process.enumerateModules();
  for (var i = 0; i < mods.length; i++) {
    result.modules.push({ name: mods[i].name, base: mods[i].base.toString(),
                          size: mods[i].size, path: mods[i].path });
  }
  var main = null;
  for (var j = 0; j < mods.length; j++) {
    if (String(mods[j].path).toLowerCase().indexOf(String(TARGET).toLowerCase()) >= 0) { main = mods[j]; break; }
  }
  if (main === null && mods.length > 0) { main = mods[0]; }
  result.main_module = main ? { name: main.name, base: main.base.toString(), size: main.size, path: main.path } : null;

  if (main !== null) {
    for (var k = 0; k < SCAN.length; k++) {
      var needle = SCAN[k];
      try {
        var hits = Memory.scanSync(main.base, main.size, hexPattern(needle));
        result.scans.push({ needle: needle, hit_count: hits.length,
          addresses: hits.slice(0, 8).map(function (h) { return h.address.toString(); }) });
      } catch (e) { result.errors.push('scan ' + needle + ': ' + e); }
    }
  }
} catch (e) { result.errors.push('enumerate: ' + e); }

for (var h = 0; h < HOOKS.length; h++) {
  (function (name) {
    try {
      var addr = Module.findExportByName(null, name);
      if (addr === null) { result.errors.push('export not found: ' + name); return; }
      Interceptor.attach(addr, {
        onEnter: function (a) { send({ type: 'call', api: name, args: a.slice(0, 4).map(function (x) { return x.toString(); }) }); }
      });
    } catch (e) { result.errors.push('hook ' + name + ': ' + e); }
  })(HOOKS[h]);
}

try {
  var ep = Module.findExportByName('kernel32.dll', 'ExitProcess');
  if (ep !== null) {
    Interceptor.attach(ep, {
      onEnter: function (a) { send({ type: 'exit_code', value: a[0].toInt32() & 0xffffffff }); }
    });
  }
} catch (e) { result.errors.push('ExitProcess hook: ' + e); }

send(result);
"""


@tool("frida_run_and_trace")
def _run_and_trace(args: dict[str, Any]) -> dict[str, Any]:
    target = args["target"]
    scan = args.get("scan_strings") or []
    hooks = args.get("hook_exports") or []
    src = TRACE_SCRIPT_TEMPLATE % {
        "scan": json.dumps([str(s) for s in scan]),
        "hooks": json.dumps([str(h) for h in hooks]),
        "target": json.dumps(str(target)),
    }
    res = _spawn_and_collect(target, src, int(args.get("timeout_ms") or 6000), [])
    # 合并：脚本一次性 send 的 result + 后续事件消息
    merged: dict[str, Any] = {}
    calls: list[Any] = []
    exit_code: Any = None
    for m in res["messages"]:
        if isinstance(m, dict) and m.get("type") == "exit_code":
            exit_code = m.get("value")
        elif isinstance(m, dict) and m.get("type") == "call":
            calls.append({"api": m.get("api"), "args": m.get("args")})
        elif isinstance(m, dict) and "modules" in m:
            merged = m
    merged["calls"] = calls
    merged["exit_code"] = exit_code
    merged["errors"] = merged.get("errors", []) + res["errors"]
    merged["pid"] = res["pid"]
    merged["exited_before_timeout"] = res["exited"]
    merged["hooks_requested"] = hooks
    return _ok(json.dumps(merged, ensure_ascii=False, indent=2)[:60000])


# ── MCP 协议（stdio） ───────────────────────────────────────────────────
def _handle(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "tools": [
                    {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                    for t in TOOLS
                ]
            },
        }
    if method == "tools/call":
        name = params.get("name")
        fn = TOOL_HANDLERS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未知工具: {name}"}}
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": fn(params.get("arguments") or {})}
        except Exception as exc:  # noqa: BLE001
            LOG.exception("tool %s failed", name)
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(exc)}}
    if rid is None:
        return None  # 通知类消息不回包
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"不支持的方法: {method}"}}


def main() -> int:
    LOG.info("frida-mcp server starting")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            LOG.warning("invalid json: %s", line[:200])
            continue
        resp = _handle(req)
        if resp is None:
            continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
