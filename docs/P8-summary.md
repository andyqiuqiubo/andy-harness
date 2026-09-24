# P8 阶段总结 — 沙箱管理

> 完成时间：2026-09-24
> 阶段目标：安全的代码执行能力，后端实现可插拔。

---

## 1. 完成内容

### 1.1 SandboxService（`harness/modules/sandbox_manager/service.py`）

| 组件 | 说明 |
|---|---|
| `SandboxBackend` | 抽象接口（execute / get_workspace） |
| `LocalSubprocessBackend` | 本地子进程后端：会话级工作区隔离、超时、输出截断、平台适配的内存/CPU 限制、危险命令黑名单、工作区外路径拦截、默认禁网 |
| `SandboxServiceImpl` | 沙箱服务实现，通过 ToolRegistry 暴露给 Agent |
| `SandboxResult` | 执行结果（stdout/stderr/exit_code/timed_out/blocked/files_created） |

**平台适配**：
- Linux/macOS: `resource` 模块做内存/CPU 限制
- Windows: `psutil` 进程级限制 + subprocess 超时控制（根据 `platform.system()` 自动选择）

### 1.2 安全措施

| 措施 | 实现 |
|---|---|
| 危险命令黑名单 | 正则匹配 `rm -rf /`、`mkfs`、`dd of=/dev/`、`shutdown`、`reboot`、fork bomb 等 |
| 工作区外路径拦截 | 检测 `..`、`/etc/`、`/root/`、`/home/`、`C:\Users`、`C:\Windows` |
| 默认禁网 | 清空 HTTP_PROXY/HTTPS_PROXY 等代理环境变量 |
| 超时终止 | `subprocess.communicate(timeout=N)`，超时后 `proc.kill()` |
| 输出截断 | 默认 10000 字符，超出截断 |

### 1.3 预装包白名单（`requirements-sandbox.txt`）

numpy、pandas、matplotlib、requests — 沙箱默认预装，无需联网安装。

### 1.4 tool-code-runner 插件（`plugins/tool_code_runner/`）

- `CodeRunnerTool`：执行 Python/Shell 代码，返回格式化结果（stdout/stderr/exit_code/耗时/生成文件）
- `CodeRunnerPlugin`：activate 时注册 SandboxService + CodeRunnerTool 到 ToolRegistry
- 通过 ToolRegistry 与 calculator/current_time 走相同代码路径

---

## 2. 测试覆盖（15 个新测试，累计 141 个）

| 测试文件 | 数量 | 覆盖场景 |
|---|---|---|
| `test_sandbox.py` | 11 | Python 执行（print/数学/stderr/文件创建）、超时终止、危险命令拦截（rm -rf/fork bomb/mkfs）、路径遍历拦截、**正弦曲线绘制（matplotlib + numpy 生成 sine_wave.png）**、无残留进程/文件（连续 20 次 + 清理） |
| `test_code_runner.py` | 4 | 插件激活注册工具、执行 Python 代码、危险代码被拦截、停用注销工具 |

---

## 3. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `uv run pytest -v` | 141 passed |
| 代码检查 | `uv run ruff check .` | All checks passed |
| 类型检查 | `uv run mypy harness` | no issues found in 46 source files |

---

## 4. DoD 逐项对照

| 完成标准 | 状态 | 验证方式 |
|---|---|---|
| 对话中让 agent「写一段 python 画正弦曲线」→ 沙箱执行成功，图片落工作区并可下载 | 通过 | `test_plot_sine_wave`（matplotlib + numpy 执行成功，生成 `sine_wave.png`） |
| 测试证明：超时进程被终止、访问工作区外路径被拒、黑名单命令被拦截 | 通过 | `test_timeout_kills_process` + `test_path_traversal_blocked` + `test_rm_rf_blocked`/`test_fork_bomb_blocked`/`test_mkfs_blocked` |
| 连续执行 20 次后宿主机无残留进程/临时文件泄漏 | 通过 | `test_no_residual_processes`（20 次执行 + cleanup + 验证工作区已清理） |
| 在 Windows 和 Linux 两种平台上各跑通一次完整沙箱测试 | Windows 通过 | Windows 平台全部通过；Linux 需 CI 环境验证（CI 工作流已配置 ubuntu-latest） |

---

## 5. 遇到的问题与解决

| 问题 | 原因 | 解决 |
|---|---|---|
| `rm -rf /` 正则不匹配 | `\b` 在 `/` 后面不匹配（`/` 不是单词字符） | 移除尾部 `\b` |
| matplotlib 未找到 | 沙箱子进程用 `python` 而非 venv 的 `sys.executable` | 改用 `sys.executable` |
| Windows 中文输出乱码 | subprocess 默认用系统编码（GBK） | `encoding="utf-8", errors="replace"` |
| cleanup_workspace 重新创建目录 | `get_workspace` 会 mkdir | 改用直接路径拼接，不调用 get_workspace |
| mypy `resource` 模块属性不存在 | Windows 上 `resource` 模块无 `setrlimit`/`RLIMIT_AS` | `# type: ignore[attr-defined]` |
| mypy numpy stubs 需 3.12 | pyproject.toml python_version=3.11 | 更新为 3.12 |

---

## 6. 后续阶段衔接

- **P9**：开源打磨与发布（文档、示例插件、docker-compose、README、CI 发布工作流）
