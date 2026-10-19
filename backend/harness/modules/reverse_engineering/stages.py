"""工程强逆向 —— 五个阶段的适配器实现。

统一接口：``check()`` 判定目标引擎是否可用 → ``run(ctx)`` 执行并回写 facts/artifacts。
每个阶段对"目标引擎不可用"的处理是**降级替代执行**而不是跳过：接口、配置、产物结构与
真实引擎完全一致，迁移到具备条件的主机上改配置即启用真件。

数据流转（后一阶段消费前一阶段的事实）：
    REA 静态  --(函数清单/字符串/导入)-->  Frida 动态  --(运行时内存特征/API/退出码)-->
    Firecracker 沙箱 --(执行轨迹)-->  PentAGI 分析 --(风险发现)-->  Evidence v2 固化
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from harness.modules.evidence_v2.service import EvidenceV2Service, sha256_bytes
from harness.modules.reverse_engineering.models import (
    STAGE_EVIDENCE,
    STAGE_FIRECRACKER,
    STAGE_FRIDA,
    STAGE_JAVA_DECOMPILE,
    STAGE_PENTAGI,
    STAGE_REA,
    STAGE_REBUILD,
    ArtifactRef,
    RunContext,
    StageResult,
    StageStatus,
)
from harness.modules.reverse_engineering.pe_resources import (  # noqa: E402
    installer_signals,
    local_dialogs,
    local_imports,
    local_strings,
)

SRC_ROOT = Path(__file__).resolve().parents[4]  # 项目根（含 mcp_servers/rev_eng/backend；目录重组后 src/ 内容已上提）
WS_ROOT = Path(__file__).resolve().parents[4]  # 目录重组后项目根即工作区根（.tools 所在目录）

REA_DIR = SRC_ROOT / "mcp_servers" / "rea"
REA_ENTRY = REA_DIR / "scripts" / "rea.mjs"
REA_MCP_MAIN = REA_DIR / "dist" / "main.js"
FRIDA_MCP = SRC_ROOT / "mcp_servers" / "frida_mcp" / "server.py"

TOOLS_DIR = Path(os.environ.get("ANDY_REVENG_TOOLS_DIR") or (WS_ROOT / ".tools"))
GHIDRA_DIR = TOOLS_DIR / "ghidra_12.1.4_PUBLIC"
JDK_DIR = TOOLS_DIR / "jdk-21"
FRIDA_PYTHON = TOOLS_DIR / "frida-venv" / "Scripts" / "python.exe"

REA_TOOL_ALLOWLIST = [
    "open_binary",
    "list_procedures",
    "list_strings",
    "search_strings",
    "procedure_pseudo_code",
    "analyze_function",
    "xrefs",
    "inspect_pe_resources",
]

# PentAGI 替代分析器使用的启发式特征库
SUSPICIOUS_APIS = {
    "CreateRemoteThread",
    "CreateRemoteThreadEx",
    "VirtualAllocEx",
    "WriteProcessMemory",
    "NtUnmapViewOfSection",
    "LoadLibraryA",
    "LoadLibraryW",
    "GetProcAddress",
    "WinExec",
    "ShellExecuteA",
    "ShellExecuteW",
    "URLDownloadToFileA",
    "InternetOpenA",
    "InternetReadFile",
    "HttpSendRequestA",
    "RegSetValueExA",
    "RegCreateKeyExA",
    "CryptEncrypt",
    "CryptDecrypt",
    "IsDebuggerPresent",
    "CheckRemoteDebuggerPresent",
    "SetWindowsHookExA",
    "OpenProcess",
    "AdjustTokenPrivileges",
    "CreateServiceA",
    "QueueUserAPC",
    "NtQueueApcThread",
}
# 高区分度子集：进程注入 / 注入执行链 / 提权 —— 命中即 high
INJECTION_APIS = {
    "CreateRemoteThread",
    "CreateRemoteThreadEx",
    "VirtualAllocEx",
    "WriteProcessMemory",
    "NtUnmapViewOfSection",
    "SetWindowsHookExA",
    "AdjustTokenPrivileges",
    "WinExec",
    "QueueUserAPC",
    "NtQueueApcThread",
}
# 低区分度子集：正常程序几乎都会用 —— 只记 info，不推高风险
LOW_SIGNAL_APIS = {"LoadLibraryA", "LoadLibraryW", "GetProcAddress"}
CRYPTO_MARKERS = {
    "0x9e3779b9",
    "0x9e3779b1",
    "0x6a09e667",
    "0x428a2f98",
    "0x5be0cd19",
    "aes",
    "rsa",
    "sha256",
    "md5",
    "base64",
    "encrypt",
    "decrypt",
    "openssl",
}
_URL_RE = re.compile(r"https?://[^\s\"'<>]{4,}", re.I)
# 四段点分数字；lookaround 防止切进更长的版本号内部（如 10.0.19045.x）
_IP_CAND_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# PE 字符串里注册表路径是**单反斜杠**（旧正则要求两个反斜杠 → PER-001 从未命中）
_REG_RE = re.compile(
    r"\b(HKEY_(?:LOCAL_MACHINE|CURRENT_USER|CLASSES_ROOT|USERS|MACHINE)\b"
    r"|SOFTWARE\\[A-Za-z0-9_.\\-]+"
    r"|SYSTEM\\CurrentControlSet)",
    re.I,
)


def _artifact_dir(ctx: RunContext) -> Path:
    d = ctx.work_dir / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_artifact(
    ctx: RunContext,
    *,
    stage: str,
    kind: str,
    name: str,
    data: bytes | str | dict[str, Any] | list[Any],
    summary: str = "",
) -> ArtifactRef:
    """把产物落到 run 目录并返回引用（后续由 evidence_v2 固化）。"""
    if isinstance(data, (dict, list)):
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        if not name.endswith(".json"):
            name += ".json"
    elif isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = data
    safe = name.replace("/", "_").replace("\\", "_")
    path = _artifact_dir(ctx) / safe
    path.write_bytes(raw)
    return ArtifactRef(
        stage=stage,
        kind=kind,
        name=safe,
        path=str(path),
        summary=summary,
        sha256=sha256_bytes(raw),
        bytes=len(raw),
    )


# ── MCP 工具错误识别（质量根因防护） ─────────────────────────────────────
# ``MCPClientService.call_tool`` 对 ``isError`` 响应**返回错误串而不是抛异常**
# （见 mcp_client/service.py）。任何阶段若不识别这个前缀，provider_timeout /
# provider_unavailable 之类的失败会被当成"成功的空结果"——历史上
# "0 个函数、0 条字符串却报 OK"（run 804c7a057dc2 / 8c4dda6ea74e）即此根因。
MCP_ERR_PREFIX = "MCP 工具错误"


def _mcp_error(raw: str | None) -> dict[str, Any] | None:
    """识别 MCP 工具错误响应；返回 error 对象，非错误响应返回 ``None``。

    返回的 dict 至少含 ``code``/``message``，并保留上游的 ``retryable``
    标志（REA 对 provider_timeout 明确标注 ``retryable: true``）。
    """
    if not isinstance(raw, str) or not raw.startswith(MCP_ERR_PREFIX):
        return None
    payload = raw[len(MCP_ERR_PREFIX) :].lstrip(":").strip()
    try:
        obj = json.loads(payload)
    except Exception:  # noqa: BLE001
        return {"code": "tool_error", "message": payload[:800]}
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict) and err:
        return err
    return {"code": "tool_error", "message": payload[:800]}


def _deep_rejection_reason(raw: str | None) -> str:
    """从 ``open_binary`` 错误里取出 Ghidra 提供方被拒的具体原因（诊断展示用）。"""
    err = _mcp_error(raw)
    if not err:
        return ""
    details = err.get("details")
    if isinstance(details, dict):
        for r in details.get("rejections") or []:
            if isinstance(r, dict) and r.get("provider_id") == "ghidra":
                return str(r.get("reason") or "")[:300]
    return str(err.get("message") or "")[:300]


class BaseStage(ABC):
    """阶段基类。"""

    name: str = ""
    target_engine: str = ""

    @abstractmethod
    def check(self) -> tuple[bool, str]:
        """目标引擎是否可用，返回 (可用, 原因)。"""

    @abstractmethod
    async def run(self, ctx: RunContext) -> StageResult:
        """执行阶段。"""

    def _timed(self) -> float:
        return time.time()

    async def _call_retry(
        self,
        service: Any,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        attempts: int = 2,
        delay: float = 2.0,
        log: Any = None,
    ) -> str:
        """调用 MCP 工具；对 ``retryable`` 错误与瞬时异常自动重试。

        返回原始文本（可能含错误串），由调用方用 :func:`_mcp_error` 判定——
        只有**全部尝试**都以异常结束时才向上抛出。REA 对 ``provider_timeout``
        标注 ``retryable: true`` 且 Ghidra 分析结果按样本摘要缓存，重试一次
        可把"一次超时即整段降级"变成"第二次拿到深度结果"。
        """
        last_raw = ""
        last_reason = ""
        last_exc: Exception | None = None
        for i in range(max(1, attempts)):
            if i:
                if log:
                    log(f"{tool} 触发重试 {i + 1}/{attempts}（原因：{last_reason or '异常'}）")
                await asyncio.sleep(delay)
            try:
                raw: str = await service.call_tool(server, tool, args)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                last_reason = f"请求异常 {exc}"[:200]
                continue
            last_exc = None
            err = _mcp_error(raw)
            if err is None:
                return raw
            last_raw = raw
            last_reason = f"{err.get('code')}: {str(err.get('message'))[:160]}"
            if not err.get("retryable"):
                return raw
        if last_exc is not None:
            raise last_exc
        return last_raw


# ── 阶段 1：REA 静态逆向 ───────────────────────────────────────────────
class ReaStaticStage(BaseStage):
    """REA（+ Ghidra）静态逆向：导入→自动分析→函数清单→字符串→伪代码反编译。"""

    name = STAGE_REA
    target_engine = "rea+ghidra"

    def check(self) -> tuple[bool, str]:
        if not REA_ENTRY.is_file():
            return False, f"REA 入口缺失：{REA_ENTRY}"
        if not REA_MCP_MAIN.is_file():
            return False, "REA 未构建（dist/main.js 缺失）"
        if shutil.which("node") is None:
            return False, "node 不在 PATH"
        return True, ""

    def _env(self) -> dict[str, str]:
        env = {
            "REA_LOG_LEVEL": os.environ.get("REA_LOG_LEVEL", "error"),
            "REA_ANALYSIS_PROVIDER": "ghidra",
        }
        java_home = str(JDK_DIR) if JDK_DIR.is_dir() else os.environ.get("JAVA_HOME", "")
        if java_home:
            env["JAVA_HOME"] = java_home
        if GHIDRA_DIR.is_dir():
            env["GHIDRA_INSTALL_DIR"] = str(GHIDRA_DIR)
        elif os.environ.get("GHIDRA_INSTALL_DIR"):
            env["GHIDRA_INSTALL_DIR"] = os.environ["GHIDRA_INSTALL_DIR"]
        env["GHIDRA_HEADLESS_MAXMEM"] = os.environ.get("GHIDRA_HEADLESS_MAXMEM", "3G")
        return env

    async def run(self, ctx: RunContext) -> StageResult:
        from harness.modules.mcp_client.service import (  # noqa: PLC0415
            MCPClientService,
            MCPServerConfig,
        )

        ok, why = self.check()
        if not ok:
            return StageResult(
                stage=self.name,
                status=StageStatus.SKIPPED,
                engine="none",
                target_engine=self.target_engine,
                summary=why,
            )

        t0 = self._timed()
        result = StageResult(
            stage=self.name, status=StageStatus.RUNNING, engine="rea", target_engine=self.target_engine
        )
        cfg = MCPServerConfig(
            name=f"rea-{ctx.run_id}",
            command="node",
            args=[str(REA_ENTRY), "mcp"],
            env=self._env(),
            max_line_bytes=16 * 1024 * 1024,
            tool_allowlist=list(REA_TOOL_ALLOWLIST),
            # 大体积真实二进制（MB 级 GUI EXE）首次 list_procedures 时 Ghidra 才执行
            # 完整自动分析，且 REA+Ghidra 冷启动本身可达 330s，默认 60s 必超时。
            request_timeout=float(ctx.config.get("rea_request_timeout", 900.0)),
        )
        service = MCPClientService(config_paths=())
        try:
            await service.connect_server(cfg)
            result.logs.append("已连接 REA MCP server（stdio）")
            ctx.reporter.phase_progress(1, 6, "已连接 REA MCP，准备打开目标", stage=self.name)

            attempts = max(1, int(ctx.config.get("rea_attempts", 2)))
            retry_delay = float(ctx.config.get("rea_retry_delay", 2.0))
            try:
                opened = await self._call_retry(
                    service,
                    cfg.name,
                    "open_binary",
                    {"path": str(ctx.subject)},
                    attempts=attempts,
                    delay=retry_delay,
                    log=result.logs.append,
                )
            except Exception as exc:  # noqa: BLE001
                # 客户端请求超时等异常——历史上这里直接把整阶段打成 FAILED，
                # 现在同样转入静态降级（"跑不动深度"≠"流水线失败"）。
                opened = ""
                result.logs.append(f"open_binary 请求异常：{exc}")
            # 无论成功失败都先落盘原始返回，便于事后定位（失败时这是唯一的诊断依据）
            result.artifacts.append(
                write_artifact(
                    ctx,
                    stage=self.name,
                    kind="open_binary",
                    name="rea-open-binary.json",
                    data=opened or "open_binary 无响应（异常详情见 run.log）",
                    summary="REA 打开目标并建立分析文档的原始返回",
                )
            )
            ctx.reporter.phase_progress(2, 6, "已建立分析文档（Ghidra 导入 + 自动分析）", stage=self.name)
            open_err = _mcp_error(opened)
            if open_err is not None or not opened:
                # 深度原生分析（Ghidra）不可用 → 级内降级：改用 REA 内置、不依赖引擎的静态工具
                code = str((open_err or {}).get("code") or "request_failed")
                reason = _deep_rejection_reason(opened)
                result.status = StageStatus.SUBSTITUTED
                result.engine = "rea(static-fallback)"
                result.logs.append(f"深度原生分析不可用（{code}），转入 REA 内置静态工具降级路径")
                ctx.reporter.phase_progress(3, 6, "深度原生分析不可用，转入内置静态降级", stage=self.name)
                result.facts = await self._static_fallback(
                    service,
                    cfg.name,
                    ctx,
                    result,
                    code=code,
                    reason=reason,
                )
                ctx.reporter.phase_progress(4, 6, "内置静态工具解析完成（PE 资源/字符串/导入）", stage=self.name)
                result.summary = (
                    f"静态逆向（降级）：PE 资源树 + 文件清单 + {result.facts.get('string_count', 0)} 条字符串；"
                    f"深度反编译不可用（{code}）"
                )
                result.duration_ms = int((self._timed() - t0) * 1000)
                return result
            result.logs.append("open_binary 完成（Ghidra 导入 + 自动分析）")

            # ── 深度调用：函数清单 / 字符串 / 伪代码 ──
            # 每个返回都必须过 _mcp_error：错误串被解析成空列表 = "0 结果成功"，
            # 曾让 provider_timeout 静默变成 OK（run 804c7a057dc2 / 8c4dda6ea74e）。
            deep_code = ""
            deep_detail = ""
            partial: dict[str, Any] = {}
            rea_strings: list[str] | None = None
            entries: list[dict[str, Any]] = []
            procs_raw = ""
            strings_raw = ""
            pseudo: dict[str, str] = {}
            try:
                # 长耗时步骤（Ghidra 冷启动 + 大样本函数枚举可达数分钟）开始前先上报，
                # 避免进度面板在等待期间长时间无更新（表现为"卡住"）。
                ctx.reporter.phase_progress(
                    2, 6, "正在枚举函数清单（首次分析大样本可能耗时数分钟）", stage=self.name
                )
                procs_raw = await self._call_retry(
                    service,
                    cfg.name,
                    "list_procedures",
                    {},
                    attempts=attempts,
                    delay=retry_delay,
                    log=result.logs.append,
                )
                procs_err = _mcp_error(procs_raw)
                if procs_err is None:
                    entries = _extract_procedure_entries(procs_raw)
                    functions = _extract_names(procs_raw)
                    partial = {"functions": functions, "function_count": len(functions)}
                    proc_summary = f"函数清单（{len(functions)} 个）"
                else:
                    deep_code = str(procs_err.get("code") or "tool_error")
                    deep_detail = f"list_procedures 返回错误：{str(procs_raw)[:200]}"
                    proc_summary = f"函数清单获取失败（{deep_code}）"
                    result.logs.append(deep_detail)
                result.artifacts.append(
                    write_artifact(
                        ctx,
                        stage=self.name,
                        kind="procedures",
                        name="rea-procedures.json",
                        data=procs_raw,
                        summary=proc_summary,
                    )
                )
                ctx.reporter.phase_progress(3, 6, "函数清单解析完成", stage=self.name)

                strings_raw = await self._call_retry(
                    service,
                    cfg.name,
                    "list_strings",
                    {},
                    attempts=attempts,
                    delay=retry_delay,
                    log=result.logs.append,
                )
                strings_err = _mcp_error(strings_raw)
                if strings_err is None:
                    rea_strings = _extract_strings(strings_raw)
                    str_summary = f"字符串清单（{len(rea_strings)} 条）"
                else:
                    if not deep_code:
                        deep_code = str(strings_err.get("code") or "tool_error")
                        deep_detail = f"list_strings 返回错误：{str(strings_raw)[:200]}"
                    str_summary = f"字符串清单获取失败（{strings_err.get('code')}）"
                    result.logs.append(f"list_strings 返回错误：{str(strings_raw)[:300]}")
                result.artifacts.append(
                    write_artifact(
                        ctx,
                        stage=self.name,
                        kind="strings",
                        name="rea-strings.json",
                        data=strings_raw,
                        summary=str_summary,
                    )
                )
                ctx.reporter.phase_progress(4, 6, "字符串清单解析完成", stage=self.name)

                # 只对前 N 个函数做伪代码反编译，控制 Ghidra 调用次数与耗时；
                # 候选优先真实代码函数（EXTERNAL 导入符号无法反编译，会被 Ghidra 拒绝）
                functions = partial.get("functions") or []
                if functions:
                    limit = int(ctx.config.get("decompile_limit", 3))
                    targets = _pick_decompile_targets(entries, limit) or functions[:limit]
                    result.logs.append(f"反编译候选（{len(targets)}）：{', '.join(targets) or '无'}")
                    for i, fn in enumerate(targets, 1):
                        # 逐个函数上报：反编译是最慢的步骤，给出"第 i/N 个"的真实粒度和当前函数名
                        ctx.reporter.phase_progress(
                            4, 6, f"正在反编译伪代码 {i}/{len(targets)}：{fn}", stage=self.name
                        )
                        try:
                            code_raw = await self._call_retry(
                                service,
                                cfg.name,
                                "procedure_pseudo_code",
                                {"procedure": fn},
                                attempts=attempts,
                                delay=retry_delay,
                            )
                        except Exception as exc:  # noqa: BLE001
                            result.logs.append(f"反编译 {fn} 失败：{exc}")
                            continue
                        if _mcp_error(code_raw) is not None:
                            # 错误串不是代码——绝不当伪代码存盘/计入结论
                            result.logs.append(f"反编译 {fn} 返回错误：{str(code_raw)[:200]}")
                            continue
                        pseudo[fn] = code_raw
                if pseudo:
                    result.artifacts.append(
                        write_artifact(
                            ctx,
                            stage=self.name,
                            kind="pseudo_code",
                            name="rea-pseudo-code.json",
                            data=pseudo,
                            summary=f"伪代码反编译（{len(pseudo)} 个函数）",
                        )
                    )
                ctx.reporter.phase_progress(5, 6, "伪代码反编译完成", stage=self.name)
            except Exception as exc:  # noqa: BLE001
                # 深度调用（函数枚举/字符串/反编译）超时或出错 → 级内降级到 REA 内置
                # 静态工具路径，保证 run 不至于整体失败（大体积二进制分析耗时难预估）。
                result.logs.append(f"深度原生调用失败（{exc}），转入内置静态降级路径")
                if not deep_code:
                    deep_code = "exception"
                    deep_detail = str(exc)[:300]

            rea_strings = rea_strings or []
            if deep_code or (not partial.get("functions") and not rea_strings):
                # 有错误，或深度结果为空（0 函数 0 字符串 = 不可信的"成功"）→ 降级
                if not deep_code:
                    deep_code = "empty_result"
                    deep_detail = "深度分析返回空结果（0 函数 0 字符串）"
                result.status = StageStatus.SUBSTITUTED
                result.engine = "rea(static-fallback)"
                result.logs.append(f"深度结果不可用（{deep_code}），转入内置静态降级路径")
                ctx.reporter.phase_progress(4, 6, "深度结果不可用，转入内置静态降级", stage=self.name)
                result.facts = await self._static_fallback(
                    service,
                    cfg.name,
                    ctx,
                    result,
                    code=deep_code,
                    reason=deep_detail,
                    keep=partial,
                    rea_strings=rea_strings,
                )
                ctx.reporter.phase_progress(5, 6, "内置静态工具解析完成（PE 资源/字符串/导入）", stage=self.name)
                kept = int(result.facts.get("function_count") or 0)
                result.summary = (
                    f"静态逆向（降级）：{deep_code}；PE 资源树 + 文件清单 + "
                    f"{result.facts.get('string_count', 0)} 条字符串" + (f"、保留深度函数 {kept} 个" if kept else "")
                )
                ctx.reporter.phase_progress(6, 6, "静态事实整合完成", stage=self.name)
                result.duration_ms = int((self._timed() - t0) * 1000)
                return result

            result.facts = {
                "deep_analysis": {"attempted": True, "available": True, "code": "", "reason": ""},
                "functions": partial.get("functions") or [],
                "function_count": int(partial.get("function_count") or 0),
                "strings": rea_strings[:200],
                "string_count": len(rea_strings),
                "decompiled": list(pseudo.keys()),
                "imports": _extract_imports(strings_raw, procs_raw),
            }
            ctx.reporter.phase_progress(6, 6, "静态事实整合完成", stage=self.name)
            result.status = StageStatus.OK
            result.summary = (
                f"静态逆向完成：{result.facts['function_count']} 个函数、"
                f"{result.facts['string_count']} 条字符串、"
                f"反编译 {len(pseudo)} 个函数"
            )
        except Exception as exc:  # noqa: BLE001
            result.status = StageStatus.FAILED
            result.error = str(exc)
            result.summary = f"REA 静态逆向失败：{exc}"
            result.logs.append(f"ERROR: {exc}")
        finally:
            try:
                await service.disconnect_all()
            except Exception:  # noqa: BLE001
                pass
        result.duration_ms = int((self._timed() - t0) * 1000)
        return result

    async def _static_fallback(
        self,
        service: Any,
        server: str,
        ctx: RunContext,
        result: StageResult,
        *,
        code: str,
        reason: str = "",
        keep: dict[str, Any] | None = None,
        rea_strings: list[str] | None = None,
    ) -> dict[str, Any]:
        """深度引擎不可用时的 REA 内置静态降级路径。

        仍然由 REA 自己的工具产出（PE 资源树 / 文件清单）；"字符串"优先采用深度
        阶段已成功的 REA 结果，否则改用本地可打印串提取，并在 facts 中明确标注
        来源，避免把降级结果误报为 REA 深度分析结论。``keep`` 保留深度路径已
        成功的部分（如函数清单），避免"部分成功"被整体丢弃。
        """
        facts: dict[str, Any] = {
            "deep_analysis": {"attempted": True, "available": False, "code": code, "reason": reason},
            "functions": [],
            "function_count": 0,
            "decompiled": [],
            "imports": [],
        }
        for k in ("functions", "function_count", "decompiled"):
            if keep and keep.get(k):
                facts[k] = keep[k]

        # 1) PE 资源树（REA 内置，纯静态解析）
        try:
            pe = await service.call_tool(server, "inspect_pe_resources", {"path": str(ctx.subject)})
            result.artifacts.append(
                write_artifact(
                    ctx,
                    stage=self.name,
                    kind="pe_resources",
                    name="rea-pe-resources.json",
                    data=pe,
                    summary="REA 解析的 PE 资源树与文件摘要证据",
                )
            )
            pe_err = _mcp_error(pe)
            if pe_err is not None:
                result.logs.append(f"inspect_pe_resources 返回错误：{pe_err.get('code')}")
            else:
                facts["pe_resources_chars"] = len(pe)
        except Exception as exc:  # noqa: BLE001
            result.logs.append(f"inspect_pe_resources 失败：{exc}")

        # 2) 文件清单/摘要（不同版本的入参名可能不同，逐个尝试）
        art = await self._call_tolerant(
            service,
            server,
            "inspect_artifact",
            [{"path": str(ctx.subject)}, {"input_path": str(ctx.subject)}],
        )
        if art:
            result.artifacts.append(
                write_artifact(
                    ctx,
                    stage=self.name,
                    kind="artifact",
                    name="rea-artifact.json",
                    data=art,
                    summary="REA 文件清单/摘要证据",
                )
            )

        # 3) 字符串（优先深度阶段已成功的 REA 结果；否则本地提取降级补充
        #    —— 供 Frida 阶段做运行时内存复核）
        if rea_strings:
            strings = list(rea_strings)
            facts["strings_source"] = "rea（深度调用部分成功，沿用 REA 结果）"
        else:
            try:
                strings = local_strings(ctx.subject)
                facts["strings_source"] = "local-extract（深度分析不可用时的降级补充，ASCII+UTF-16LE）"
            except Exception as exc:  # noqa: BLE001
                strings = []
                facts["strings_source"] = f"unavailable（本地提取失败：{exc}）"
                result.logs.append(f"local_strings 失败：{exc}")
        facts["strings"] = strings
        facts["string_count"] = len(strings)
        result.artifacts.append(
            write_artifact(
                ctx,
                stage=self.name,
                kind="strings",
                name="rea-strings-fallback.json",
                data={"source": facts["strings_source"], "strings": strings},
                summary=f"字符串清单（{len(strings)} 条，{facts['strings_source']}）",
            )
        )

        # 4) 导入表（本地解析 —— 降级路径此前完全没有导入面证据）
        try:
            imports = local_imports(ctx.subject)
        except Exception as exc:  # noqa: BLE001
            imports = []
            result.logs.append(f"local_imports 失败：{exc}")
        facts["imports"] = imports
        if imports:
            result.artifacts.append(
                write_artifact(
                    ctx,
                    stage=self.name,
                    kind="imports",
                    name="rea-imports.json",
                    data={"dlls": imports, "count": len(imports)},
                    summary=f"导入模块（{len(imports)} 个 DLL）",
                )
            )

        # 5) 对话框模板（本地解析 RT_DIALOG —— REA 侧把该载荷标记为 opaque）
        try:
            dialogs = local_dialogs(ctx.subject)
        except Exception as exc:  # noqa: BLE001
            dialogs = {"dialogs": [], "count": 0}
            result.logs.append(f"local_dialogs 失败：{exc}")
        facts["dialog_count"] = dialogs["count"]
        if dialogs["count"]:
            result.artifacts.append(
                write_artifact(
                    ctx,
                    stage=self.name,
                    kind="dialogs",
                    name="rea-dialogs.json",
                    data=dialogs,
                    summary=f"对话框模板（{dialogs['count']} 个，含标题/控件/几何）",
                )
            )

        # 6) 安装器能力特征（能力词而非产品名，供重建工具做形态判定）
        facts["installer_signals"] = installer_signals(strings, imports)
        return facts

    @staticmethod
    async def _call_tolerant(service: Any, server: str, tool: str, candidates: list[dict[str, Any]]) -> str:
        """按候选入参依次尝试，返回首个非错误响应；全部失败返回空串。"""
        for args in candidates:
            try:
                out = await service.call_tool(server, tool, args)
            except Exception:  # noqa: BLE001
                continue
            if out and _mcp_error(out) is None:
                return str(out)
        return ""


def _extract_procedure_entries(raw: str) -> list[dict[str, Any]]:
    """从 REA `list_procedures` 返回中提取函数条目（保留 external/thunk 标记）。

    REA v6.4 的原生返回是 evidence 结构，函数名不在顶层 `name` 字段里，而在
    `normalized_result` / `raw_result` 列表的每个元素中，形如：
    ``{"address": "EXTERNAL:0x1", "value": "SHLWAPI.DLL::ColorRGBToHLS",
       "procedure": {"external": true, "thunk": false}}``
    早期实现只找顶层 `procedures/functions/items/results`，再退回全文正则
    匹配 `"name"`，结果把 `subject.name`（文件路径）和 `provider.name`（"Ghidra"）
    误当成了函数名 —— 这里改为按 evidence 结构优先解析。
    """
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        obj = None

    entries: list[dict[str, Any]] = []

    def _collect(node: Any, depth: int = 0) -> None:
        if entries or depth > 4:
            return
        if isinstance(node, dict):
            for key in (
                "normalized_result",
                "raw_result",
                "procedures",
                "functions",
                "items",
                "results",
                "data",
                "entries",
            ):
                val = node.get(key)
                if isinstance(val, list) and val and all(isinstance(i, dict) for i in val):
                    hit = [i for i in val if i.get("value") or i.get("name")]
                    if hit:
                        entries.extend(hit)
                        return
            for val in node.values():
                _collect(val, depth + 1)
        elif isinstance(node, list) and node and all(isinstance(i, dict) for i in node):
            hit = [i for i in node if i.get("value") or i.get("name")]
            if hit:
                entries.extend(hit)

    _collect(obj)
    return entries


def _extract_names(raw: str) -> list[str]:
    """从 REA 返回（JSON 或文本）中提取函数名。"""
    names: list[str] = []
    for it in _extract_procedure_entries(raw):
        val = it.get("value") or it.get("name")
        if isinstance(val, str) and val:
            names.append(val)
    if not names:
        # 兜底：直接匹配 {"address": ..., "value": "..."} 形态，避免再次误抓 name 字段
        names = list(re.findall(r'"address"\s*:\s*"[^"]*"\s*,\s*"value"\s*:\s*"([^"]{2,200})"', raw))
    if not names:
        names = [m for m in re.findall(r'"name"\s*:\s*"([^"]{2,120})"', raw)]
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _pick_decompile_targets(entries: list[dict[str, Any]], limit: int) -> list[str]:
    """挑选伪代码反编译目标：优先真实代码函数，排除导入（EXTERNAL）与 thunk。

    `procedure_pseudo_code` 对 EXTERNAL 符号会返回 invalid_request，
    因此候选必须先按 external/thunk 过滤，再按名字与地址形态兜底。
    """
    picked: list[str] = []
    seen: set[str] = set()

    def _accept(e: dict[str, Any]) -> str | None:
        val = e.get("value") or e.get("name")
        if not isinstance(val, str) or not val or val in seen:
            return None
        raw_proc = e.get("procedure")
        proc: dict[str, Any] = raw_proc if isinstance(raw_proc, dict) else {}
        if proc.get("external") or proc.get("thunk"):
            return None
        addr = str(e.get("address") or "")
        if addr.startswith("EXTERNAL"):
            return None
        return val

    for e in entries:
        if len(picked) >= limit:
            break
        name = _accept(e)
        if name:
            seen.add(name)
            picked.append(name)
    return picked


def _extract_strings(raw: str) -> list[str]:
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        obj = None
    vals: list[str] = []
    if isinstance(obj, dict):
        for key in ("normalized_result", "raw_result", "strings", "items", "results", "data"):
            if isinstance(obj.get(key), list):
                for it in obj[key]:
                    if isinstance(it, dict):
                        v = it.get("value") or it.get("text") or it.get("string")
                        if isinstance(v, str):
                            vals.append(v)
                    elif isinstance(it, str):
                        vals.append(it)
    if not vals:
        vals = re.findall(r'"(?:value|text|string)"\s*:\s*"((?:[^"\\]|\\.){3,200})"', raw)
    out: list[str] = []
    for v in vals:
        v = v.strip()
        if v and v not in out:
            out.append(v)
    return out


def _extract_imports(*raws: str) -> list[str]:
    blob = "\n".join(raws)
    return sorted({m for m in re.findall(r"\b([A-Za-z0-9_]+\.dll)\b", blob)})[:50]


# ── 阶段 2：Frida MCP 动态插桩 ─────────────────────────────────────────
class FridaDynamicStage(BaseStage):
    """Frida MCP 动态插桩：受控运行目标，验证静态发现是否进入运行时。"""

    name = STAGE_FRIDA
    target_engine = "frida"

    def check(self) -> tuple[bool, str]:
        if not FRIDA_MCP.is_file():
            return False, f"Frida MCP server 缺失：{FRIDA_MCP}"
        if not FRIDA_PYTHON.is_file():
            return False, f"Frida 隔离环境缺失：{FRIDA_PYTHON}"
        return True, ""

    async def run(self, ctx: RunContext) -> StageResult:
        from harness.modules.mcp_client.service import (  # noqa: PLC0415
            MCPClientService,
            MCPServerConfig,
        )

        ok, why = self.check()
        if not ok:
            return StageResult(
                stage=self.name,
                status=StageStatus.SKIPPED,
                engine="none",
                target_engine=self.target_engine,
                summary=why,
            )

        t0 = self._timed()
        result = StageResult(
            stage=self.name, status=StageStatus.RUNNING, engine="frida", target_engine=self.target_engine
        )
        cfg = MCPServerConfig(
            name=f"frida-{ctx.run_id}",
            command=str(FRIDA_PYTHON),
            args=[str(FRIDA_MCP)],
            env={},
            max_line_bytes=8 * 1024 * 1024,
        )
        service = MCPClientService(config_paths=())
        try:
            await service.connect_server(cfg)
            result.logs.append("已连接 Frida MCP server（stdio）")
            ctx.reporter.phase_progress(1, 4, "已连接 Frida MCP server", stage=self.name)
            status_raw = await service.call_tool(cfg.name, "frida_status", {})
            result.artifacts.append(
                write_artifact(
                    ctx,
                    stage=self.name,
                    kind="status",
                    name="frida-status.json",
                    data=status_raw,
                    summary="Frida 运行时版本与本地设备信息",
                )
            )
            ctx.reporter.phase_progress(2, 4, "运行时状态采集完成", stage=self.name)

            # 数据流转：用 REA 静态发现的字符串作为运行时内存扫描特征
            seeds = [s for s in (ctx.facts.get("strings") or []) if 4 <= len(s) <= 48]
            seeds = [s for s in seeds if s.isascii() and s.isprintable()][:6]
            hooks = [h for h in (ctx.config.get("hook_exports") or [])][:8]
            args: dict[str, Any] = {
                "target": str(ctx.subject),
                "scan_strings": seeds,
                "hook_exports": hooks,
                "timeout_ms": int(ctx.config.get("frida_timeout_ms", 6000)),
            }
            result.logs.append(f"运行时内存扫描特征（来自 REA 静态）：{seeds}")
            trace_raw = ""
            try:
                trace_raw = await service.call_tool(cfg.name, "frida_run_and_trace", args)
                if "MCP 工具错误" in trace_raw:
                    raise RuntimeError(trace_raw[:2000])
            except Exception as exc:  # noqa: BLE001
                # 注入不可用（受限环境常见）→ 级内降级：基于导入/字符串推断 API 面，
                # 并保留 frida_status 作为"运行时已就绪"的真实证据
                result.status = StageStatus.SUBSTITUTED
                result.engine = "frida-static-inference"
                hint = _injection_hint(str(exc))
                result.logs.append(f"动态注入不可用，转入静态推断降级：{exc}")
                if hint:
                    result.logs.append(f"诊断：{hint}")
                ctx.reporter.phase_progress(3, 4, "注入不可用，转入静态推断降级", stage=self.name)
                inferred = _infer_api_surface(ctx.facts)
                result.artifacts.append(
                    write_artifact(
                        ctx,
                        stage=self.name,
                        kind="inference",
                        name="frida-static-inference.json",
                        data={"reason": str(exc)[:2000], "hint": hint, "inferred_apis": inferred},
                        summary=f"注入不可用，基于导入表/字符串推断 API 面（{len(inferred)} 项）",
                    )
                )
                result.facts = {
                    "runtime": _json_or_empty(status_raw),
                    "injection": {"available": False, "reason": str(exc)[:600], "hint": hint},
                    "modules": [],
                    "memory_scans": [],
                    "api_calls": [],
                    "inferred_apis": inferred,
                    "exit_code": None,
                }
                result.summary = (
                    f"动态插桩降级：注入不可用{f'（{hint[:60]}）' if hint else ''}，"
                    f"已基于静态事实推断 {len(inferred)} 项 API 行为"
                )
                ctx.reporter.phase_progress(4, 4, "动态事实整合完成", stage=self.name)
                result.duration_ms = int((self._timed() - t0) * 1000)
                return result
            result.artifacts.append(
                write_artifact(
                    ctx,
                    stage=self.name,
                    kind="trace",
                    name="frida-trace.json",
                    data=trace_raw,
                    summary="动态运行轨迹（模块/内存特征/API/退出码）",
                )
            )
            ctx.reporter.phase_progress(3, 4, "动态运行轨迹采集完成", stage=self.name)
            trace = _json_or_empty(trace_raw)
            result.facts = {
                "modules": (trace.get("modules") or [])[:60],
                "main_module": trace.get("main_module"),
                "memory_scans": trace.get("scans") or [],
                "api_calls": trace.get("calls") or [],
                "exit_code": trace.get("exit_code"),
                "hooks_requested": trace.get("hooks_requested") or [],
                "frida_errors": trace.get("errors") or [],
            }
            hits = sum(len(s.get("addresses", [])) for s in (trace.get("scans") or []))
            result.status = StageStatus.OK
            ctx.reporter.phase_progress(4, 4, "动态事实整合完成", stage=self.name)
            result.summary = (
                f"动态插桩完成：{len(trace.get('modules') or [])} 个模块、"
                f"内存特征命中 {hits} 处、退出码 {trace.get('exit_code')}"
            )
        except Exception as exc:  # noqa: BLE001
            result.status = StageStatus.FAILED
            result.error = str(exc)
            result.summary = f"Frida 动态插桩失败：{exc}"
            result.logs.append(f"ERROR: {exc}")
        finally:
            try:
                await service.disconnect_all()
            except Exception:  # noqa: BLE001
                pass
        result.duration_ms = int((self._timed() - t0) * 1000)
        return result


def _infer_api_surface(facts: dict[str, Any]) -> list[dict[str, str]]:
    """注入不可用时的降级推断：从导入表与字符串中识别可能的系统 API 调用面。"""
    blob = " ".join([str(s) for s in (facts.get("strings") or [])] + [str(i) for i in (facts.get("imports") or [])])
    out: list[dict[str, str]] = []
    for api in sorted(SUSPICIOUS_APIS):
        if api.lower() in blob.lower():
            out.append({"api": api, "basis": "import/string 命中", "confidence": "inferred"})
    for dll in sorted({m for m in re.findall(r"([A-Za-z0-9_]+\.dll)", blob)})[:20]:
        out.append({"api": dll, "basis": "依赖模块", "confidence": "inferred"})
    return out


def _injection_hint(msg: str) -> str:
    """把 Frida 注入失败的原始错误翻译成可读结论（边界场景提示）。"""
    low = msg.lower()
    if "0x000002e4" in low or "0x2e4" in low:
        # Win32 740 = ERROR_ELEVATION_REQUIRED
        return "样本需要提升权限（UAC/管理员）才能被 spawn，非提权会话下动态插桩不可用"
    if "refused to load frida-agent" in low:
        return "目标进程拒绝加载 frida-agent（保护进程 / 权限不足 / 架构不匹配），注入不可用"
    if "terminated during injection" in low:
        return "目标进程在注入期间退出（样本提前结束或主动反调试），注入不可用"
    if "unable to spawn" in low:
        return "样本无法在当前会话启动（路径 / 权限 / 策略限制）"
    return ""


def _match_apis(blob: str, names: set[str]) -> set[str]:
    """词边界匹配 API 名 —— 子串匹配会把 ``NtOpenProcess`` 误判成 ``OpenProcess``。"""
    hits: set[str] = set()
    for api in names:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(api)}(?![A-Za-z0-9_])", blob, re.I):
            hits.add(api)
    return hits


def _extract_ips(strings: list[Any]) -> list[str]:
    """提取 IPv4 指示器；排除非法八位组与常见"系统版本串"（如 5.1.0.0）。

    历史缺陷：朴素正则 ``\\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b`` 把 Windows 版本串
    ``5.1.0.0`` 当成 IP 上报（run 0ec7acc760f0 实证），这里按四条规则过滤：
    八位组 ≤255、无前导零、不是 a.b.0.0（b≤32）的版本形态、且不嵌在更长的
    点分数字里。
    """
    out: list[str] = []
    for s in strings:
        for cand in _IP_CAND_RE.findall(str(s)):
            parts = cand.split(".")
            if len(parts) != 4 or any(not p.isdigit() or int(p) > 255 for p in parts):
                continue
            if any(len(p) > 1 and p[0] == "0" for p in parts):
                continue
            if parts[2] == "0" and parts[3] == "0" and int(parts[1]) <= 32:
                continue  # 版本串启发式（5.1.0.0 / 10.0.0.0 形态）
            if cand not in out:
                out.append(cand)
    return out


def _json_or_empty(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> str:
    """终止整棵进程树，返回处置说明（写进产物，保证可审计）。"""
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                shell=False,
            )
            if r.returncode == 0:
                return f"taskkill /T /F pid={proc.pid}"
            proc.kill()
            return f"taskkill 失败(rc={r.returncode})，回退 kill pid={proc.pid}"
        except Exception:  # noqa: BLE001
            proc.kill()
            return f"kill pid={proc.pid}（taskkill 不可用）"
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # type: ignore[attr-defined]
        return f"killpg -9 pgid={proc.pid}"
    except Exception:  # noqa: BLE001
        proc.kill()
        return f"kill pid={proc.pid}"


def _run_isolated(
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> tuple[int | None, bytes, bytes, bool, str]:
    """启动子进程并限时等待；超时终止**整棵进程树**（旧实现只杀父进程，孙进程残留）。

    返回 ``(returncode, stdout, stderr, timed_out, kill_note)``。
    """
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False, ""
    except subprocess.TimeoutExpired:
        note = _kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out, err = b"", b""
        return None, out, err, True, note


# ── 阶段 3：Firecracker 沙箱执行 ───────────────────────────────────────
class FirecrackerSandboxStage(BaseStage):
    """Firecracker 微虚拟机沙箱执行。

    真实引擎要求：``firecracker`` 可执行文件 + ``/dev/kvm``（Linux + KVM）。
    本机（Windows、无 KVM、虚拟化未启用）不具备条件，此时降级为 **本地受限执行后端**：
    独立工作目录、精简环境变量、超时并终止整棵进程树、输出截断；**不做网络隔离**
    （产物中如实标注，旧版写成 "blocked" 是错误声明）。产物结构与微虚拟机一致
    （machine-config / 执行轨迹 / 退出码），迁移到 Linux/KVM 主机后自动启用真件。
    不允许在宿主机执行样本时设 ``ANDY_REVENG_LOCAL_EXEC=0``（阶段记 skipped）。
    """

    name = STAGE_FIRECRACKER
    target_engine = "firecracker"

    def check(self) -> tuple[bool, str]:
        if os.environ.get("ANDY_REVENG_FORCE_FIRECRACKER") == "0":
            return False, "配置强制禁用 Firecracker"
        fc = shutil.which("firecracker") or (TOOLS_DIR / "firecracker").as_posix()
        has_bin = bool(shutil.which("firecracker")) or Path(fc).is_file()
        has_kvm = os.path.exists("/dev/kvm")
        if has_bin and has_kvm:
            return True, ""
        return False, (
            f"Firecracker 不可用（binary={'是' if has_bin else '否'}，"
            f"/dev/kvm={'是' if has_kvm else '否'}）；已启用本地受限执行后端替代"
        )

    def _machine_config(self, ctx: RunContext) -> dict[str, Any]:
        """微虚拟机配置（无论是否真实执行都产出，作为沙箱契约）。"""
        return {
            "boot-source": {
                "kernel_image_path": str(TOOLS_DIR / "vmlinux"),
                "boot_args": "console=ttyS0 reboot=k panic=1 pci=off",
            },
            "drives": [
                {
                    "drive_id": "rootfs",
                    "path_on_host": str(TOOLS_DIR / "rootfs.ext4"),
                    "is_root_device": True,
                    "is_read_only": True,
                }
            ],
            "machine-config": {
                "vcpu_count": int(ctx.config.get("vcpu_count", 1)),
                "mem_size_mib": int(ctx.config.get("mem_size_mib", 256)),
                "smt": False,
            },
            "network": {"disabled": True},
            "jailer": {"mode": "disabled", "note": "生产环境建议以 jailer 运行并降权"},
            "guest_payload": str(ctx.subject),
        }

    async def run(self, ctx: RunContext) -> StageResult:
        t0 = self._timed()
        available, why = self.check()
        result = StageResult(
            stage=self.name,
            status=StageStatus.RUNNING,
            engine="firecracker" if available else "local-substitute",
            target_engine=self.target_engine,
        )

        cfg_art = write_artifact(
            ctx,
            stage=self.name,
            kind="machine_config",
            name="firecracker-machine-config.json",
            data=self._machine_config(ctx),
            summary="微虚拟机沙箱配置契约",
        )
        result.artifacts.append(cfg_art)
        ctx.reporter.phase_progress(1, 3, "沙箱配置契约生成完成", stage=self.name)

        if available:
            result.summary = "Firecracker 真实执行（本机未触发该分支）"
            result.status = StageStatus.OK
            result.facts = {"engine": "firecracker", "note": why}
            ctx.reporter.phase_progress(3, 3, "执行事实整合完成", stage=self.name)
            result.duration_ms = int((self._timed() - t0) * 1000)
            return result

        # ── 本地受限执行后端（替代） ──
        if os.environ.get("ANDY_REVENG_LOCAL_EXEC") == "0":
            # 边界场景：不允许在宿主机执行不可信样本（默认允许，需显式关闭）
            result.status = StageStatus.SKIPPED
            result.engine = "none"
            result.summary = "本地替代执行已通过配置禁用（ANDY_REVENG_LOCAL_EXEC=0）"
            result.logs.append(result.summary)
            result.facts = {"engine": "none", "skipped": True, "exit_code": None, "reason": "local-exec-disabled"}
            ctx.reporter.phase_progress(3, 3, "本地替代执行已禁用", stage=self.name)
            result.duration_ms = int((self._timed() - t0) * 1000)
            return result

        result.logs.append(why)
        work = ctx.work_dir / "sandbox"
        work.mkdir(parents=True, exist_ok=True)
        args = [str(ctx.subject)] + [str(a) for a in (ctx.config.get("sandbox_args") or [])]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": str(work),
            "TMP": str(work),
            "ANDY_SANDBOX": "1",
        }
        timeout = float(ctx.config.get("sandbox_timeout", 20))
        try:
            rc, out_b, err_b, timed_out, kill_note = await asyncio.to_thread(
                _run_isolated,
                args,
                work,
                env,
                timeout,
            )
            stdout = (out_b or b"")[:20000].decode("utf-8", "replace")
            stderr = (err_b or b"")[:8000].decode("utf-8", "replace")
            exit_code = None if timed_out else rc
            exec_rec = {
                "engine": "local-substitute",
                "reason": why,
                "argv": args,
                "timeout_s": timeout,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "process_tree_kill": kill_note,
                "stdout": stdout,
                "stderr": stderr,
                # 事实性修正：本地替代后端**不隔离网络**（仅去代理环境变量 + 独立
                # 工作目录）。旧版写 "blocked(no proxy env, isolated cwd)" 是错误声明。
                "network": "not-isolated（本地替代后端不做网络隔离：仅去代理环境变量 + 独立工作目录）",
                "isolation": ["独立工作目录", "精简环境变量", "超时 + 进程树终止", "输出截断"],
            }
            result.artifacts.append(
                write_artifact(
                    ctx,
                    stage=self.name,
                    kind="execution",
                    name="sandbox-execution.json",
                    data=exec_rec,
                    summary=f"受限执行退出码 {exit_code}（超时={timed_out}）",
                )
            )
            ctx.reporter.phase_progress(2, 3, "受限执行完成", stage=self.name)
            result.facts = {
                "engine": "local-substitute",
                "exit_code": exit_code,
                "timeout": timed_out,
                "stdout": stdout[:4000],
                "stderr": stderr[:2000],
                "kvm_available": False,
                "firecracker_available": bool(shutil.which("firecracker")),
            }
            result.status = StageStatus.SUBSTITUTED
            if timed_out:
                result.summary = f"沙箱执行超时（>{timeout}s），已终止整棵进程树（{kill_note}）"
                result.logs.append(f"执行超时，已终止进程树：{kill_note}")
            else:
                result.summary = f"沙箱执行完成（本地受限后端，退出码 {exit_code}）"
        except OSError as exc:
            # 样本无法在本环境启动（WinError 740 需要提升权限 / 5 拒绝访问 /
            # 策略拦截等）。这是"沙箱跑不了这个样本"，不是流水线故障——降级继续，
            # 把退出能力与 errno 记进证据，与超时终止同等对待。
            errno_no: int | None = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            hints: dict[int | None, str] = {
                740: "样本需要提升权限（UAC/管理员）才能启动，非提权终端下无法执行",
                5: "样本被系统策略拒绝执行",
            }
            hint = hints.get(errno_no, "样本在本环境无法启动")
            result.status = StageStatus.SUBSTITUTED
            result.summary = f"沙箱无法启动样本（errno={errno_no}）：{hint}"
            result.error = ""
            result.facts = {
                "engine": "local-substitute",
                "exit_code": None,
                "spawn_error": str(exc),
                "errno": errno_no,
                "hint": hint,
            }
            result.logs.append(f"spawn 失败：{exc}（{hint}）")
        except Exception as exc:  # noqa: BLE001
            result.status = StageStatus.FAILED
            result.error = str(exc)
            result.summary = f"沙箱执行失败：{exc}"

        ctx.reporter.phase_progress(3, 3, "执行事实整合完成", stage=self.name)
        result.duration_ms = int((self._timed() - t0) * 1000)
        return result


# ── 阶段 4：PentAGI 自动化分析 ─────────────────────────────────────────
class PentAGIAnalysisStage(BaseStage):
    """PentAGI 自动化分析。

    真实引擎要求：Docker + PentAGI 服务（``PENTAGI_API_URL``）。本机无 Docker，
    降级为 **本地确定性分析器**：消费前三阶段的事实做规则化风险研判，输出结构化 findings。
    接口保持一致，配置 PentAGI 后自动切换到远程智能体分析。
    """

    name = STAGE_PENTAGI
    target_engine = "pentagi"

    def check(self) -> tuple[bool, str]:
        if os.environ.get("ANDY_REVENG_FORCE_PENTAGI") == "0":
            return False, "配置强制禁用 PentAGI"
        url = os.environ.get("PENTAGI_API_URL", "")
        if url and shutil.which("docker"):
            return True, ""
        return False, (
            f"PentAGI 不可用（API_URL={'已配置' if url else '未配置'}，"
            f"docker={'是' if shutil.which('docker') else '否'}）；已启用本地确定性分析器替代"
        )

    async def run(self, ctx: RunContext) -> StageResult:
        t0 = self._timed()
        available, why = self.check()
        result = StageResult(
            stage=self.name,
            status=StageStatus.RUNNING,
            engine="pentagi" if available else "local-substitute",
            target_engine=self.target_engine,
        )

        findings = self._analyze(ctx)
        result.artifacts.append(
            write_artifact(
                ctx,
                stage=self.name,
                kind="findings",
                name="pentagi-findings.json",
                data={"engine": result.engine, "findings": findings},
                    summary=f"自动化分析发现 {len(findings)} 项",
                )
            )
        ctx.reporter.phase_progress(1, 3, "自动化分析发现采集完成", stage=self.name)
        risk = _risk_score(findings)
        result.artifacts.append(
            write_artifact(
                ctx,
                stage=self.name,
                kind="risk",
                name="pentagi-risk.json",
                data=risk,
                    summary=f"风险评分 {risk['score']} / {risk['level']}",
                )
            )
        ctx.reporter.phase_progress(2, 3, "风险评分完成", stage=self.name)
        result.facts = {"findings": findings, "risk": risk}
        result.status = StageStatus.SUBSTITUTED if not available else StageStatus.OK
        result.summary = f"自动化分析完成：{len(findings)} 项发现，风险等级 {risk['level']}"
        result.logs.append(why or "PentAGI 远程智能体已调用")
        ctx.reporter.phase_progress(3, 3, "分析事实整合完成", stage=self.name)
        result.duration_ms = int((self._timed() - t0) * 1000)
        return result

    # ── 本地确定性分析器（PentAGI 的等价替代） ──
    @staticmethod
    def _analyze(ctx: RunContext) -> list[dict[str, Any]]:
        f = ctx.facts
        findings: list[dict[str, Any]] = []

        def add(fid: str, sev: str, title: str, detail: str, evidence: Any = None) -> None:
            findings.append({"id": fid, "severity": sev, "title": title, "detail": detail, "evidence": evidence})

        # 1) 静态：可疑 API（词边界匹配 + 按区分度分级：子串匹配曾把 NtOpenProcess
        #    误判成 OpenProcess，LoadLibraryA 这类低区分度 API 直接 high 曾让风险虚高）
        strings_blob = " ".join(str(s) for s in (f.get("strings") or []))
        api_blob = " ".join(
            [str(a.get("api", "")) for a in (f.get("api_calls") or [])]
            + [str(a.get("api", "")) for a in (f.get("inferred_apis") or [])]
            + [str(x) for x in (f.get("functions") or [])]
        )
        scan_blob = f"{api_blob} {strings_blob}"
        all_hits = _match_apis(scan_blob, SUSPICIOUS_APIS)
        inj_hits = sorted(all_hits & INJECTION_APIS)
        other_hits = sorted(all_hits - INJECTION_APIS - LOW_SIGNAL_APIS)
        low_hits = sorted(all_hits & LOW_SIGNAL_APIS)
        if inj_hits:
            add(
                "API-001",
                "high",
                "命中进程注入/执行链 API",
                f"注入与执行相关 API（强恶意信号）：{', '.join(inj_hits[:20])}",
                inj_hits,
            )
        if other_hits:
            add(
                "API-002",
                "medium",
                "命中可疑 Windows API",
                f"静态/动态证据中出现的敏感 API：{', '.join(other_hits[:20])}",
                other_hits,
            )
        if low_hits:
            add(
                "API-003",
                "info",
                "命中低区分度 API",
                f"{', '.join(low_hits[:10])} 常见于正常程序，单独出现不推高风险",
                low_hits,
            )

        # 2) 静态：网络指示器（IP 过滤掉版本串 / 非法八位组 / 前导零）
        urls = sorted(set(_URL_RE.findall(strings_blob)))[:20]
        ips = _extract_ips(f.get("strings") or [])[:20]
        if urls or ips:
            add(
                "NET-001",
                "medium",
                "存在网络指示器",
                f"URL {len(urls)} 个、IP {len(ips)} 个（仅指示，不代表实际外联）",
                {"urls": urls, "ips": ips},
            )

        # 3) 静态：加密/编码特征（词边界匹配，避免子串误报）
        low_blob = strings_blob.lower()
        crypto = sorted(
            {c for c in CRYPTO_MARKERS if re.search(rf"(?<![a-z0-9]){re.escape(c.lower())}(?![a-z0-9])", low_blob)}
        )
        if crypto:
            add("CRY-001", "info", "存在加密/编码相关特征", f"命中：{', '.join(crypto[:20])}", crypto)

        # 4) 静态：注册表/持久化（PE 字符串是单反斜杠路径，旧正则要求两个反斜杠从未命中）
        reg = sorted(set(_REG_RE.findall(strings_blob)))[:20]
        if reg:
            add("PER-001", "medium", "存在注册表/持久化相关字符串", f"命中：{', '.join(reg[:20])}", reg)

        # 5) 动态：静态字符串在运行时内存中被确认
        scans = f.get("memory_scans") or []
        confirmed = [s for s in scans if s.get("hit_count", 0) > 0]
        if confirmed:
            add(
                "DYN-001",
                "info",
                "静态特征已在运行时内存确认",
                f"{len(confirmed)}/{len(scans)} 个静态字符串在进程内存中命中，证明静态分析结论真实存在于运行时",
                confirmed,
            )
        elif scans:
            add(
                "DYN-002",
                "low",
                "静态特征未在运行时内存命中",
                f"扫描 {len(scans)} 个特征全部未命中，可能因目标提前退出或字符串位于未加载区段",
                scans,
            )

        # 6) 动态：退出码
        ec = f.get("exit_code")
        if ec is not None:
            sev = "info" if ec == 0 else "low"
            add(
                "EXE-001",
                sev,
                f"沙箱/插桩执行退出码 {ec}",
                "退出码用于确认样本可被执行且行为在预期内",
                {"exit_code": ec},
            )

        # 7) 结构：函数规模
        fc = int(f.get("function_count") or 0)
        if fc:
            add(
                "STR-001",
                "info",
                f"反汇编出 {fc} 个函数",
                f"已完成伪代码反编译：{', '.join(str(x) for x in (f.get('decompiled') or [])[:5])} 等",
                {"function_count": fc},
            )

        if not findings:
            add("NON-001", "info", "未命中任何风险规则", "样本行为简单或未发现可疑特征")
        return findings


def _risk_score(findings: list[dict[str, Any]]) -> dict[str, Any]:
    weight = {"high": 10, "medium": 4, "low": 1, "info": 0}
    score = sum(weight.get(str(x.get("severity")), 0) for x in findings)
    level = "critical" if score >= 30 else "high" if score >= 15 else "medium" if score >= 6 else "low"
    return {
        "score": score,
        "level": level,
        "high": sum(1 for x in findings if x.get("severity") == "high"),
        "total": len(findings),
    }


# ── 阶段 5：功能等价 exe 重建 ──────────────────────────────────────────
class RebuildExeStage(BaseStage):
    """由静态事实重建**功能等价的新可执行文件**（TCC 原生 PE）。

    需求对应：**对可执行文件做逆向，最终要产出 exe**——因此本阶段是流水线的
    正式一环（不是手工 CLI 的附加步骤）：

    - 用 ``input_dispatch`` 判定样本类型：**仅可执行文件**才重建（其它类型显式
      SKIPPED 并说明原因，不产出噪音 exe）；
    - 重建实现与 REST 手工重建共用 ``rebuild.run_rebuild``（同一份逻辑，两条入口）；
    - 工具以 ``run.json`` 为唯一事实来源，故 pipeline 会在本阶段前落盘一次快照
      （见 ``pipeline.run``），保证读到的是**当前**各阶段事实而不是启动时的空快照。
    """

    name = STAGE_REBUILD
    target_engine = "tcc-rebuild"

    def check(self) -> tuple[bool, str]:
        from harness.modules.reverse_engineering import rebuild as rebuild_mod

        if rebuild_mod.find_tool() is None:
            return False, "重建工具不存在（rev_eng/tools/rebuild_gui_exe.py）"
        if rebuild_mod.find_tcc() is None:
            return False, "TCC 编译器未就位（.tools/tcc/tcc/tcc.exe）"
        return True, ""

    async def run(self, ctx: RunContext) -> StageResult:
        from harness.modules.reverse_engineering import rebuild as rebuild_mod
        from harness.modules.reverse_engineering.input_dispatch import (
            InputClassifier,
            InputKind,
            subject_from_path,
        )

        t0 = self._timed()
        result = StageResult(stage=self.name, status=StageStatus.OK, target_engine=self.target_engine)

        ok, why = self.check()
        if not ok:
            result.status = StageStatus.SUBSTITUTED
            result.engine = "none"
            result.summary = f"重建不可用（{why}）"
            result.facts = {"ok": False, "error": why}
            result.duration_ms = int((self._timed() - t0) * 1000)
            return result

        # 类型门控：只对可执行文件重建（沿用与分类接口一致的判定，避免两处漂移）
        classification = InputClassifier().classify(subject_from_path(ctx.subject))
        if classification.kind != InputKind.EXECUTABLE:
            result.status = StageStatus.SKIPPED
            result.engine = "none"
            result.summary = f"样本类型为 {classification.kind}（非可执行文件），不生成重建 exe"
            result.facts = {"ok": False, "skipped": True, "reason": "not-executable", "kind": classification.kind}
            why_text = classification.reasons[0] if classification.reasons else ""
            result.logs.append(f"类型判定：{classification.kind}（{why_text}）")
            ctx.reporter.phase_progress(1, 2, f"样本类型 {classification.kind}，跳过重建", stage=self.name)
            result.duration_ms = int((self._timed() - t0) * 1000)
            return result

        ctx.reporter.phase_progress(1, 2, "正在编译重建产物（TCC 原生 PE）…", stage=self.name)
        # 阻塞的 subprocess 放到线程里执行，不占用事件循环（进度推送不受影响）；
        # runs_dir 用本流水线的实际目录（支持自定义/测试目录，不假定默认路径）
        rebuild_result = await asyncio.to_thread(
            rebuild_mod.run_rebuild, ctx.run_id, runs_dir=ctx.work_dir.parent
        )

        result.facts = rebuild_result.to_dict()
        result.artifacts.append(
            write_artifact(
                ctx,
                stage=self.name,
                kind="rebuild_log",
                name="rebuild-log.txt",
                data=rebuild_result.log or rebuild_result.error,
                summary="重建工具完整输出（形态选择/编译/载荷追加）",
            )
        )
        if rebuild_result.ok:
            result.status = StageStatus.OK
            result.engine = "tcc-rebuild"
            result.summary = (
                f"已生成功能等价的新 exe：{rebuild_result.exe_name}"
                f"（{rebuild_result.size:,} 字节，形态 {rebuild_result.kind}）"
            )
            result.logs.append(f"重建产物：{rebuild_result.exe_path}")
            # 产物本身也作为证据条目固化（哈希进链，可离线校验）
            try:
                exe_bytes = Path(rebuild_result.exe_path).read_bytes()
                result.artifacts.append(
                    write_artifact(
                        ctx,
                        stage=self.name,
                        kind="rebuilt_exe",
                        name=rebuild_result.exe_name,
                        data=exe_bytes,
                        summary=f"功能等价重建产物（sha256={rebuild_result.sha256[:16]}…）",
                    )
                )
            except OSError as exc:  # noqa: BLE001 —— 固化失败不影响已产出的 exe
                result.logs.append(f"重建产物固化失败（exe 已落盘）：{exc}")
        else:
            result.status = StageStatus.SUBSTITUTED
            result.engine = "none"
            result.summary = f"重建未产出 exe：{rebuild_result.error}"
            result.logs.append(rebuild_result.error)
        ctx.reporter.phase_progress(2, 2, "重建阶段完成", stage=self.name)
        result.duration_ms = int((self._timed() - t0) * 1000)
        return result


# ── 阶段 6：Evidence v2 证据固化 ───────────────────────────────────────
class EvidenceV2Stage(BaseStage):
    """Evidence v2：把前四阶段的产物逐条固化进哈希链证据包并离线校验。"""

    name = STAGE_EVIDENCE
    target_engine = "evidence_v2"

    def check(self) -> tuple[bool, str]:
        return True, ""

    async def run(self, ctx: RunContext) -> StageResult:
        t0 = self._timed()
        result = StageResult(
            stage=self.name, status=StageStatus.RUNNING, engine="evidence_v2", target_engine=self.target_engine
        )
        # 证据包直接落在本次 run 目录（base_dir/<bundle_id> == runs/<run_id>），
        # 使 run 目录自包含、可整体归档
        svc = EvidenceV2Service(base_dir=ctx.work_dir.parent)
        bundle = svc.create_bundle(
            ctx.subject,
            subject_type=ctx.config.get("subject_type", "pe32+"),
            provenance={
                "run_id": ctx.run_id,
                "pipeline": "andy-harness:reverse-engineering/1.0",
                "stages": [
                    {
                        "stage": s.stage,
                        "engine": s.engine,
                        "target_engine": s.target_engine,
                        "status": s.status.value,
                    }
                    for s in _prior_stage_results(ctx)
                ],
                "notes": ctx.config.get("notes", ""),
            },
        )
        bundle.bundle_id = ctx.run_id
        ctx.reporter.phase_progress(1, 4, "证据包初始化完成", stage=self.name)

        # 逐条固化：先各阶段产物，再事实台账，最后溯源清单
        for s in _prior_stage_results(ctx):
            for art in s.artifacts:
                svc.add_item(
                    bundle,
                    stage=s.stage,
                    kind=art.kind,
                    summary=art.summary or f"{s.stage}/{art.kind}",
                    payload=Path(art.path).read_bytes(),
                        payload_name=art.name,
                    )

        ctx.reporter.phase_progress(2, 4, "前序阶段产物固化完成", stage=self.name)
        svc.add_item(
            bundle,
            stage=self.name,
            kind="facts_ledger",
            summary="跨阶段流转的结构化事实台账",
            payload=ctx.facts,
            payload_name="facts-ledger.json",
        )
        svc.add_item(
            bundle,
            stage=self.name,
            kind="run_manifest",
            summary="流水线运行清单（各阶段状态/引擎/耗时）",
            payload=[s.to_dict() for s in _prior_stage_results(ctx)],
            payload_name="run-manifest.json",
        )
        ctx.reporter.phase_progress(3, 4, "事实台账与运行清单固化完成", stage=self.name)

        paths = svc.save(bundle)
        report = EvidenceV2Service.verify(paths["dir"])
        ctx.reporter.phase_progress(4, 4, "证据离线校验完成", stage=self.name)

        result.artifacts.append(
            ArtifactRef(
                stage=self.name,
                kind="evidence_json",
                name="evidence.json",
                path=paths["json"],
                summary="Evidence v2 证据包（机器可读）",
                bytes=0,
            )
        )
        result.artifacts.append(
            ArtifactRef(
                stage=self.name,
                kind="evidence_md",
                name="evidence.md",
                path=paths["markdown"],
                summary="Evidence v2 证据报告（人读）",
                bytes=0,
            )
        )
        result.facts = {
            "bundle_id": bundle.bundle_id,
            "items": len(bundle.items),
            "chain_head": bundle.chain_head,
            "evidence_dir": paths["dir"],
            "verify": report,
        }
        result.status = StageStatus.OK if report["ok"] else StageStatus.FAILED
        result.summary = f"证据固化完成：{len(bundle.items)} 条证据，离线校验 {'通过' if report['ok'] else '失败'}"
        result.logs.append(f"证据包目录：{paths['dir']}")
        result.duration_ms = int((self._timed() - t0) * 1000)
        return result


def _prior_stage_results(ctx: RunContext) -> list[StageResult]:
    """从 ctx 中取出前序阶段结果（由 pipeline 注入）。"""
    return list(ctx.config.get("_stage_results") or [])


def _find_javap() -> Path | None:
    """定位 JDK javap（优先项目内隔离 JDK，再 PATH）。"""
    cand = JDK_DIR / "bin" / "javap.exe" if os.name == "nt" else JDK_DIR / "bin" / "javap"
    if cand.is_file():
        return cand
    path = shutil.which("javap")
    return Path(path) if path else None


def _find_java() -> Path | None:
    """定位 JDK java 运行时（优先项目内隔离 JDK，再 PATH）。"""
    cand = JDK_DIR / "bin" / "java.exe" if os.name == "nt" else JDK_DIR / "bin" / "java"
    if cand.is_file():
        return cand
    path = shutil.which("java")
    return Path(path) if path else None


def _class_name_from_subject(subject: Path) -> str:
    """从输入文件名推导主类名（去掉 .class / .jar / .zip 等单层后缀）。"""
    return subject.stem


# ── 阶段：Java class 反编译 ─────────────────────────────────────────────
class JavaDecompileStage(BaseStage):
    """Java class 反编译：优先输出真正的 Java 源码，否则降级为 javap 结构文本。

    优先级：

    1. 若环境变量 ``ANDY_JAVA_DECOMPILER_JAR`` 指向一个可用的**源码级反编译器**
       （CFR / FernFlower / Jadx，按 jar 文件名自动识别），则优先调用它，输出真实的
       ``.java`` 源文件（产物 kind=``java_source``）。
    2. 否则回退到 JDK ``javap``，输出可读的结构/字节码文本（扩展名 ``.java.txt``，
       便于与真实源码区分，产物 kind=``java_decompile``）。

    三种反编译器的命令行约定完全不同，这里按类型分别构造：

    - **CFR**:        ``java -jar cfr.jar <file> --outputdir <dir> --silent true``
    - **FernFlower**: ``java -jar fernflower.jar <file> <dest-dir>``
      （FernFlower 把结果打包成 ``<name>.jar`` 写进 dest-dir，需再解压出 ``.java``）
    - **Jadx**:       ``java -jar jadx-cli.jar -d <dir> <file>``
    """

    name = STAGE_JAVA_DECOMPILE
    target_engine = "javap"  # 默认基线；配置了源码级反编译器后动态改为该反编译器名

    def check(self) -> tuple[bool, str]:
        if _find_java() is None:
            return False, "JDK java 未找到（需 .tools/jdk-21 或 PATH 中的 java）"
        return True, ""

    @staticmethod
    def _detect_decompiler(jar: Path) -> str:
        """按 jar 文件名识别反编译器类型（大小写不敏感）。"""
        name = jar.name.lower()
        if "cfr" in name:
            return "cfr"
        if "fernflower" in name or name.startswith("fern") or "fern" in name:
            return "fernflower"
        if "jadx" in name:
            return "jadx"
        return "generic"

    def _build_command(self, decompiler: str, jar: Path, subject: Path, out_dir: Path) -> list[str]:
        """按反编译器类型构造正确的命令行。"""
        java = _find_java()
        assert java is not None
        if decompiler == "cfr":
            return [str(java), "-jar", str(jar), str(subject), "--outputdir", str(out_dir), "--silent", "true"]
        if decompiler == "fernflower":
            # FernFlower 第二位置参是输出位置：目录时会在其中生成 <name>.jar
            return [str(java), "-jar", str(jar), str(subject), str(out_dir)]
        if decompiler == "jadx":
            return [str(java), "-jar", str(jar), "-d", str(out_dir), str(subject)]
        # generic（类 FernFlower：<input> <output-dir>）
        return [str(java), "-jar", str(jar), str(subject), str(out_dir)]

    async def run(self, ctx: RunContext) -> StageResult:
        t0 = self._timed()
        result = StageResult(
            stage=self.name,
            status=StageStatus.RUNNING,
            engine="javap",
            target_engine=self.target_engine,
        )

        java = _find_java()
        if java is None:
            result.status = StageStatus.SUBSTITUTED
            result.engine = "none"
            result.summary = "JDK java 不可用，跳过 Java class 反编译"
            result.facts = {"ok": False, "error": "java not found"}
            result.duration_ms = int((self._timed() - t0) * 1000)
            return result

        # ── 1) 优先调用源码级反编译器 ───────────────────────────────────
        alt_jar = os.environ.get("ANDY_JAVA_DECOMPILER_JAR", "").strip()
        decompiler = ""
        if alt_jar and Path(alt_jar).is_file():
            decompiler = self._detect_decompiler(Path(alt_jar))
            result.target_engine = decompiler  # 理想目标已升级为该反编译器
            ctx.reporter.phase_progress(1, 3, f"正在调用 {decompiler} 源码级反编译", stage=self.name)
            out_dir = _artifact_dir(ctx) / "java_source"
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = self._build_command(decompiler, Path(alt_jar), ctx.subject, out_dir)
            try:
                proc = await asyncio.to_thread(
                    subprocess.run, cmd, capture_output=True, text=True, timeout=180, check=False
                )
            except Exception as exc:  # noqa: BLE001
                result.logs.append(f"源码级反编译器调用异常：{exc}")
                proc = None
            if proc is not None and proc.returncode == 0:
                if decompiler == "fernflower":
                    self._extract_jars(out_dir)  # FernFlower 结果打包成 jar，先解压
                java_files = sorted(out_dir.rglob("*.java"))
                if java_files:
                    for f in java_files:
                        rel = f.relative_to(out_dir)
                        result.artifacts.append(
                            write_artifact(
                                ctx,
                                stage=self.name,
                                kind="java_source",
                                name=str(rel).replace(os.sep, "_"),
                                data=f.read_text(encoding="utf-8", errors="ignore"),
                                summary=f"源码级反编译产物（{decompiler}）",
                            )
                        )
                    result.engine = decompiler
                    result.target_engine = decompiler
                    result.status = StageStatus.OK
                    result.facts = {
                        "decompiler": decompiler,
                        "class_name": _class_name_from_subject(ctx.subject),
                        "java_source_count": len(java_files),
                        "primary_source": java_files[0].name,
                        "major_version": ctx.config.get("subject_major_version"),
                    }
                    ctx.reporter.phase_progress(3, 3, "源码级反编译完成", stage=self.name)
                    result.summary = f"已使用 {decompiler} 反编译为 Java 源码：{len(java_files)} 个 .java 文件"
                    result.duration_ms = int((self._timed() - t0) * 1000)
                    return result
                # 没有写出 .java 文件：部分反编译器对单类会直接打印到 stdout
                if (proc.stdout or "").strip():
                    name = _class_name_from_subject(ctx.subject) + ".java"
                    result.artifacts.append(
                        write_artifact(
                            ctx,
                            stage=self.name,
                            kind="java_source",
                            name=name,
                            data=proc.stdout,
                            summary=f"源码级反编译产物（{decompiler}，来自 stdout）",
                        )
                    )
                    result.engine = decompiler
                    result.target_engine = decompiler
                    result.status = StageStatus.OK
                    result.facts = {
                        "decompiler": decompiler,
                        "class_name": _class_name_from_subject(ctx.subject),
                        "major_version": ctx.config.get("subject_major_version"),
                    }
                    result.summary = f"已使用 {decompiler} 反编译为 Java 源码（stdout）"
                    result.duration_ms = int((self._timed() - t0) * 1000)
                    return result
                result.logs.append(
                    f"{decompiler} 退出码 0 但未产出 .java 文件；stdout={(proc.stdout or '')[:300]}"
                )
            else:
                err = (proc.stdout if proc else "") + (proc.stderr if proc else "")
                result.logs.append(
                    f"{decompiler} 调用失败（退出码 {proc.returncode if proc else '?'}）：{err[:400]}"
                )

        # ── 2) 回退：javap 结构文本 ─────────────────────────────────────
        javap = _find_javap()
        if javap is None:
            result.status = StageStatus.SUBSTITUTED
            result.engine = "none"
            result.summary = "JDK javap 不可用，跳过 Java class 反编译"
            result.facts = {"ok": False, "error": "javap not found"}
            result.duration_ms = int((self._timed() - t0) * 1000)
            return result

        ctx.reporter.phase_progress(2, 3, "正在执行 javap 反编译（降级）", stage=self.name)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [str(javap), "-c", "-p", "-classpath", str(ctx.subject.parent), ctx.subject.stem],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            result.status = StageStatus.FAILED
            result.error = str(exc)
            result.summary = f"javap 执行失败：{exc}"
            result.duration_ms = int((self._timed() - t0) * 1000)
            return result

        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            result.status = StageStatus.SUBSTITUTED
            result.engine = "javap"
            result.summary = f"javap 返回非零退出码（{proc.returncode}）：{output[:200]}"
            result.logs.append(output[:800])
            result.duration_ms = int((self._timed() - t0) * 1000)
            return result

        # 解析类元数据（轻量正则，不依赖完整 class 解析器）
        meta = self._parse_meta(output)
        safe_name = ctx.subject.stem.replace("/", "_").replace("\\", "_") + "_decompiled.java.txt"
        result.artifacts.append(
            write_artifact(
                ctx,
                stage=self.name,
                kind="java_decompile",
                name=safe_name,
                data=output,
                summary=f"javap 反编译文本（{len(output)} 字符，{meta.get('method_count', 0)} 个方法）",
            )
        )
        result.engine = "javap"
        result.target_engine = (
            decompiler or "javap"
        )  # 若配置了源码级反编译器却失败，记实际降级目标
        result.facts = {
            "decompiler": "javap",
            "class_name": meta.get("class_name", ctx.subject.stem),
            "super_class": meta.get("super_class", ""),
            "interfaces": meta.get("interfaces", []),
            "method_count": meta.get("method_count", 0),
            "major_version": ctx.config.get("subject_major_version"),
            "degraded": bool(decompiler),  # 配置了源码级反编译器却回退到 javap → 标记降级
        }
        ctx.reporter.phase_progress(3, 3, "Java class 反编译完成（javap 文本）", stage=self.name)
        result.status = StageStatus.OK
        result.summary = (
            f"javap 反编译完成：{result.facts['class_name']}，"
            f"{result.facts['method_count']} 个方法（降级为字节码文本）"
        )
        result.duration_ms = int((self._timed() - t0) * 1000)
        return result

    @staticmethod
    def _extract_jars(out_dir: Path) -> None:
        """FernFlower 把反编译结果打包成 ``<name>.jar``，解压出其中的 ``.java`` 文件。"""
        for jar in list(out_dir.glob("*.jar")):
            try:
                with zipfile.ZipFile(jar) as zf:
                    zf.extractall(out_dir)
            except zipfile.BadZipFile:
                continue

    @staticmethod
    def _parse_meta(output: str) -> dict[str, Any]:
        """从 javap 输出头几行解析类名、父类、接口与方法数。"""
        meta: dict[str, Any] = {"class_name": "", "super_class": "", "interfaces": [], "method_count": 0}
        class_re = re.compile(
            r"^\s*(?:public\s+|final\s+|abstract\s+|strictfp\s+)*"
            r"(?:class|interface|enum)\s+(\S+)\s*(?:extends\s+(\S+))?\s*(?:implements\s+([^{]+))?",
            re.I,
        )
        for line in output.splitlines()[:60]:
            m = class_re.match(line.strip())
            if m:
                meta["class_name"] = m.group(1).rstrip(";")
                meta["super_class"] = (m.group(2) or "").rstrip(";").strip()
                if m.group(3):
                    meta["interfaces"] = [s.strip().rstrip(";") for s in m.group(3).split(",") if s.strip()]
                break
        meta["method_count"] = len(re.findall(r"^\s*[\w\s<>\[\],]+\s+\w+\s*\([^)]*\);", output, re.M))
        return meta


def build_stages() -> list[BaseStage]:
    return [
        ReaStaticStage(),
        JavaDecompileStage(),
        FridaDynamicStage(),
        FirecrackerSandboxStage(),
        PentAGIAnalysisStage(),
        RebuildExeStage(),
        EvidenceV2Stage(),
    ]
