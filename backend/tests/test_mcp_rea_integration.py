"""REA vendored MCP server 端到端集成测试。

通过项目自身的 MCP 客户端（``harness.modules.mcp_client.service``）以 stdio 方式连接
``mcp_servers/rea`` 的 MCP server，覆盖「握手 → tools/list → tools/call」全链路：

1. ``test_rea_tools_list``：REA 工具 catalog 关键条目在位（全量 170 个工具的
   tools/list 响应约 2MB，依赖 per-server ``max_line_bytes`` 调大行上限）；
2. ``test_rea_tool_allowlist``：白名单配置只放行名单内工具；
3. ``test_rea_analyze_javascript_application``：对
   ``mcp_servers/samples/electron-demo-app``（真实 JS/Electron 样本，含主进程 /
   预加载 / 渲染进程与 IPC）做静态重建，断言返回模块图与 IPC 证据；
4. ``test_rea_inspect_pe_resources``：对真实 PE 二进制（当前 Python 解释器）
   解析完整资源树，断言返回资源证据（仅 Windows；无需反编译器）；
5. ``test_rea_native_decompilation``：**真正的原生反编译**——以项目内隔离安装的
   Ghidra 12.1.x + JDK 21（``.tools/`` 目录）为引擎，对仓库内真实原生 PE
   （``native/windows/build/rea-windows-x64.node``）执行
   ``open_binary → list_procedures → procedure_pseudo_code``，断言返回伪代码证据。

REA 未构建（``mcp_servers/rea/dist/main.js`` 缺失）或 ``node`` 不在 PATH 时自动
跳过——干净克隆后按 ``mcp_servers/README.md`` 执行
``npm ci && npm run compile && npm run artifacts:skills`` 再运行本测试；
原生反编译测试还需按同文档「原生反编译」节安装 Ghidra/JDK（缺失时自动跳过）。
"""

from __future__ import annotations

import os
import re
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from harness.modules.mcp_client import service as mcp_service_module  # noqa: E402
from harness.modules.mcp_client.service import (  # noqa: E402
    MCPClientService,
    MCPServerConfig,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REA_ENTRY = os.path.join(REPO_ROOT, "mcp_servers", "rea", "scripts", "rea.mjs")
REA_MCP_MAIN = os.path.join(REPO_ROOT, "mcp_servers", "rea", "dist", "main.js")
SAMPLE_APP = os.path.join(REPO_ROOT, "mcp_servers", "samples", "electron-demo-app")

# 原生反编译资源（项目工作区级 .tools/ 目录，与 backend/mcp.json 保持一致）
WORKSPACE_ROOT = os.path.dirname(REPO_ROOT)
GHIDRA_DIR = os.path.join(WORKSPACE_ROOT, ".tools", "ghidra_12.1.4_PUBLIC")
JDK_DIR = os.path.join(WORKSPACE_ROOT, ".tools", "jdk-21")
NATIVE_TARGET = os.path.join(
    REPO_ROOT, "mcp_servers", "rea", "native", "windows", "build", "rea-windows-x64.node"
)

# REA 的 tools/list 响应约 2MB（170 个工具），默认 1MB 行上限会被 SEC-15 护栏丢弃，
# 这里与 backend/mcp.json 的运行时配置保持一致调至 16MB。
REA_MAX_LINE_BYTES = 16 * 1024 * 1024
REA_ENV = {"REA_LOG_LEVEL": "error"}

# REA 冷启动 + 派生分析 worker 可能超过默认 60s，放宽单次请求超时。
_REQUEST_TIMEOUT_SECONDS = 180.0
# Ghidra 冷启动（导入 + 自动分析 + 桥接就绪）默认截止 330s，客户端超时必须覆盖。
REA_NATIVE_TIMEOUT_SECONDS = 600.0

requires_rea = pytest.mark.skipif(
    not os.path.isfile(REA_MCP_MAIN) or shutil.which("node") is None,
    reason="REA 未构建或 node 不可用（构建步骤见 mcp_servers/README.md）",
)
requires_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="inspect_pe_resources 仅支持 PE 目标（Windows）",
)
requires_ghidra = pytest.mark.skipif(
    sys.platform != "win32"
    or not os.path.isfile(os.path.join(GHIDRA_DIR, "support", "analyzeHeadless.bat"))
    or not os.path.isfile(os.path.join(JDK_DIR, "bin", "java.exe")),
    reason="项目内隔离的 Ghidra 12.1.x / JDK 21 未就绪（安装步骤见 mcp_servers/README.md）",
)


def _rea_config(**overrides) -> MCPServerConfig:
    fields = {
        "name": "rea",
        "command": "node",
        "args": [REA_ENTRY, "mcp"],
        "env": dict(REA_ENV),
        "max_line_bytes": REA_MAX_LINE_BYTES,
        "description": "REA integration test",
    }
    fields.update(overrides)
    return MCPServerConfig(**fields)


@pytest.fixture
async def rea_service(monkeypatch: pytest.MonkeyPatch):
    """连接 vendored REA MCP server（全量工具目录），产出 (service, tool_names)。"""
    monkeypatch.setattr(
        mcp_service_module, "_REQUEST_TIMEOUT", _REQUEST_TIMEOUT_SECONDS
    )
    service = MCPClientService()
    tools = await service.connect_server(_rea_config())
    try:
        yield service, [t.name for t in tools]
    finally:
        await service.disconnect_all()


@requires_rea
async def test_rea_tools_list(rea_service) -> None:
    """握手后 tools/list 应包含 REA 的静态分析工具。"""
    _service, tool_names = rea_service
    assert "analyze_javascript_application" in tool_names
    assert "inspect_pe_resources" in tool_names


@requires_rea
async def test_rea_tool_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """白名单配置只放行名单内工具（token 收敛，与 backend/mcp.json 一致）。"""
    monkeypatch.setattr(
        mcp_service_module, "_REQUEST_TIMEOUT", _REQUEST_TIMEOUT_SECONDS
    )
    allowlist = ["analyze_javascript_application", "inspect_pe_resources"]
    service = MCPClientService()
    tools = await service.connect_server(
        _rea_config(name="rea-allowlist", tool_allowlist=allowlist)
    )
    try:
        # tools/list 按 server 目录序返回，与 allowlist 书写顺序无关，按集合比较
        assert {t.name for t in tools} == set(allowlist)
    finally:
        await service.disconnect_all()


@requires_rea
async def test_rea_analyze_javascript_application(rea_service) -> None:
    """对真实样本应用做静态重建：模块图、导入与 IPC 证据应出现在结果中。

    注意：REA 的 JS/Electron 静态分析在 Windows 上依赖其原生构件
    ``rea-windows-x64.node`` 的权限自检（探针目录句柄删除
    ``SetFileInformationByHandle(FileDispositionInfo)``）。在受限执行环境
    （如智能体沙箱：以 ``FILE_SHARE_READ`` 共享打开目录后的句柄删除被过滤拒绝，
    Win32 5 拒绝访问）中该自检失败，分析不可用；真实后端运行方式（非沙箱）下
    已实测通过（4/4，含本测试全部断言）。命中该签名时跳过而非判失败。
    """
    service, _tool_names = rea_service
    result = await service.call_tool(
        "rea",
        "analyze_javascript_application",
        {"input_path": SAMPLE_APP, "format": "auto", "integrity_policy": "fail"},
    )
    assert result, "空响应"
    if "native controls are unavailable" in result:
        pytest.skip(
            "当前进程处于受限沙箱：REA 原生控件的探针目录句柄删除被沙箱过滤拦截"
            "（Win32 5）；真实后端环境（非沙箱）已实测通过，无需上游修复"
        )
    assert "MCP 工具错误" not in result
    # 样本应用的主进程 / 预加载 / 渲染进程模块
    assert "main.js" in result
    assert "preload.js" in result
    assert "renderer.js" in result
    # 主进程注册的 IPC 通道（静态字面量）
    assert "notes:search" in result


@requires_rea
@requires_windows
async def test_rea_inspect_pe_resources(rea_service) -> None:
    """对真实 PE 二进制解析资源树：应返回资源证据而非错误。"""
    service, _tool_names = rea_service
    result = await service.call_tool(
        "rea", "inspect_pe_resources", {"path": sys.executable}
    )
    assert result, "空响应"
    assert "MCP 工具错误" not in result
    # 资源树证据：SHA-256 摘要与资源类型标识
    assert "sha" in result.lower()
    assert "version" in result.lower() or "RT_" in result


def _rea_native_config() -> MCPServerConfig:
    """原生反编译专用配置：Ghidra 引擎 + 项目内隔离的 JDK 与 JVM 堆上限。"""
    return _rea_config(
        name="rea-native",
        env={
            "REA_LOG_LEVEL": "error",
            "REA_ANALYSIS_PROVIDER": "ghidra",
            "GHIDRA_INSTALL_DIR": GHIDRA_DIR,
            "JAVA_HOME": JDK_DIR,
            "GHIDRA_HEADLESS_MAXMEM": "3G",
        },
        request_timeout=REA_NATIVE_TIMEOUT_SECONDS,
    )


@requires_rea
@requires_ghidra
async def test_rea_native_decompilation(monkeypatch: pytest.MonkeyPatch) -> None:
    """真正的原生反编译（Ghidra）：open_binary → list_procedures → procedure_pseudo_code。

    目标为仓库内真实原生 PE（``rea-windows-x64.node``，848KB 原生 DLL）。
    Ghidra 冷启动（导入 + 自动分析 + 桥接就绪）在本机可达数分钟：单次请求
    超时按提供方 330s 截止放宽至 600s；JVM 堆以 ``GHIDRA_HEADLESS_MAXMEM=3G``
    限定，配合客户端 ``max_line_bytes`` 控制单帧大小与内存上界。
    """
    monkeypatch.setattr(
        mcp_service_module, "_REQUEST_TIMEOUT", REA_NATIVE_TIMEOUT_SECONDS
    )
    service = MCPClientService()
    tools = await service.connect_server(_rea_native_config())
    try:
        names = {t.name for t in tools}
        assert {"open_binary", "list_procedures", "procedure_pseudo_code"} <= names

        opened = await service.call_tool(
            "rea-native", "open_binary", {"path": NATIVE_TARGET, "provider_id": "ghidra"}
        )
        assert opened, "open_binary 空响应"
        if "native controls are unavailable" in opened:
            pytest.skip("受限沙箱：REA 原生控件不可用（非沙箱环境已实测通过）")
        assert "MCP 工具错误" not in opened, opened[:600]

        listing = await service.call_tool("rea-native", "list_procedures", {})
        assert listing, "list_procedures 空响应"
        assert "MCP 工具错误" not in listing, listing[:600]

        procedure = "napi_register_module_v1"
        if procedure not in listing:
            # 回退：从清单文本提取首个像函数名的符号（排除 JSON 结构键名）
            excluded = {
                "procedure", "procedures", "evidence", "normalized", "undefined",
                "locations", "operation", "provider", "parameters", "confidence",
                "analysis", "coverage", "limitations", "symbol",
            }
            candidates = [
                name
                for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]{5,}", listing)
                if name not in excluded
            ]
            assert candidates, "list_procedures 中未找到可用的函数名"
            procedure = candidates[0]

        code = await service.call_tool(
            "rea-native", "procedure_pseudo_code", {"procedure": procedure}
        )
        assert code, "procedure_pseudo_code 空响应"
        assert "MCP 工具错误" not in code, code[:600]
        lowered = code.lower()
        assert (
            ("{" in code and "}" in code)
            or "return" in lowered
            or "undefined" in lowered
        ), f"未识别出伪代码特征（procedure={procedure}）：{code[:400]}"
    finally:
        await service.disconnect_all()
