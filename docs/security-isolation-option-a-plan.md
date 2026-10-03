# 方案 A 详细实施方案：代码执行沙箱默认 Docker 化（本地兜底）

> 本文档是 `docs/security-isolation-analysis.md` 中"方案 A"的详细实施方案文档。
> **性质说明**：纯说明性 / 方案设计文档，**不执行任何代码改造，不提供可运行的改造脚本**。所有"改动点"均标注具体文件路径与行号，供后续实施阶段参考。

---

## 1. 目的与范围

**目标**：将 `tool_code_runner` 的代码执行后端**默认切换为 `DockerBackend`（一次性容器隔离）**，并以 `LocalSubprocessBackend` 作为**自动兜底**，在 Docker 不可用时透明降级到本机子进程，保证可用性不回退。

**范围边界（与方案 B / C 区分）**：
- ✅ 本方案只触及"代码执行沙箱这一层"的后端选择、镜像构建与配置默认值。
- ❌ 不涉及把整个后端进程打进容器（方案 B，部署级容器化）。
- ❌ 不涉及把插件 / 定时任务 / 其他能力做进程级强隔离（方案 C，需跨进程 IPC）。
- ❌ **不改动** `computer_use` 插件——它必须驱动真实本机桌面，与容器化天然冲突，本方案完全不触碰它。

**非目标**：本方案不追求"容器逃逸防护达到生产级"，而是用最低成本把"不可信代码执行"这一最主要威胁模型的隔离强度从"进程级"提升到"容器级"。

---

## 2. 现状盘点（已具备 / 仍缺失）

### 2.1 已实现（可直接复用，改动量低的根因）

| 组件 | 位置 | 现状 |
|------|------|------|
| `DockerBackend` 完整实现 | `backend/harness/modules/sandbox_manager/service.py:629-831` | 一次性容器 `docker run --rm`、bind mount 工作区到 `/workspace`、默认 `--network none` 禁网、`--memory`/`--cpus` 资源限制、镜像白名单、超时 `docker rm -f` 强删、输出截断、文件快照对比。 |
| Docker 可用性探测 | `service.py:672-678` `is_available()` | 调 `docker info` 探测守护进程，结果缓存。 |
| 容器外安全检查 | `service.py:151-174, 752-766` | 危险命令黑名单 + 路径遍历拦截，在进容器前先挡掉。 |
| 插件后端选择钩子 | `backend/plugins/tool_code_runner/main.py:131-141` | 支持 `HARNESS_SANDBOX_BACKEND` 环境变量或 `config.backend`（`local`/`docker`）选后端。 |
| 测试覆盖 | `backend/tests/test_docker_sandbox.py`（18 条逻辑 + 真实 docker 集成 skip） | 覆盖参数构造、白名单、超时强删、输出截断、不可用拦截等。 |
| 整体部署 Dockerfile | `backend/Dockerfile` | 仅用于"方案 B 整后端容器化"，**本方案不依赖它**，但镜像构建可借鉴其 `uv sync` 思路。 |

### 2.2 仍缺失（本方案必须补的 4 处）

1. **无自动兜底**。当前 `execute()` 在 `docker` 模式下若守护进程不可用，直接返回 `blocked`（"Docker 不可用：未安装 docker 或守护进程未运行"，`service.py:776-781`），**不会降级到 local**。这是本方案的核心新增逻辑。
2. **沙箱镜像未含 `requirements-sandbox.txt` 依赖**。`DockerBackend` 默认镜像 `python:3.12-slim`（`service.py:57`）不含 numpy/pandas/matplotlib/requests；而 `SandboxServiceImpl` 对 Docker 后端**跳过宿主 `pip install`**（`service.py:862` `self._prerequisites_checked = not isinstance(self._backend, LocalSubprocessBackend)`），且容器内 `--network none` 无法联网安装 → **科学计算类代码在 Docker 后端会直接失败**。必须构建预装依赖的专用镜像。
3. **默认值仍为 `local`**。`plugin.json:15` `backend` 默认 `local`；`main.py:132` 默认回退 `local`。需改为默认 `docker`。
4. **镜像白名单未固定**。默认允许任意 `python:3.12-slim`，未设 `allowed_images`，存在镜像漂移 / 拉取不可信镜像风险。应固定为自建镜像。

---

## 3. 总体设计

### 3.1 后端委托模型（推荐：复合后端）

在 `SandboxServiceImpl` 与其调用方之间引入一个 **`FallbackSandboxBackend`（复合后端）**，而非在插件激活时一次性二选一。理由：守护进程可能在运行时崩溃（启动可用 → 中途挂掉），激活时检查无法覆盖运行时故障，复合后端可在每次 `execute` 时按可用性委托，实现**真正的透明降级**。

```
Agent / CodeRunnerTool
        │
        ▼
   SandboxServiceImpl  (接口不变)
        │
        ▼
   FallbackSandboxBackend  (新增，薄包装)
     ├─ primary : DockerBackend     (默认优先)
     └─ secondary: LocalSubprocessBackend  (兜底)
        │
        执行时先 is_available(docker)，可用则走 docker；
        若 docker 不可用 / 返回 "Docker 不可用" blocked，则委托 local 重跑同份代码。
```

**替代方案（简版）**：仅在 `tool_code_runner.activate` 中做一次 `await docker.is_available()`，不可用就用 local。
- 优点：改动极小（仅插件层）。
- 缺点：**无运行时恢复**——守护进程中途崩溃后，所有执行将持续 `blocked` 直到进程重启。
- 结论：推荐复合后端，简版仅作"时间紧迫时的降级实现"。

### 3.2 关键约束（实施必须遵守）

- **两个后端必须共用同一 `base_workspaces_dir`**，否则 fallback 时工作区文件（用户上一步 `open('a.csv')` 产出的文件）在 local 下找不到。在构造复合后端时，把同一目录传给 docker 与 local。
- **`cleanup()` 必须穿透复合层**：`service.py:969` 的 `isinstance(self._backend, (LocalSubprocessBackend, DockerBackend))` 对复合后端为 `False`，会导致工作区永不清理。需改为：复合后端暴露 `cleanup_workspace()`，内部调用两个内层后端的清理；或在 `cleanup()` 中增加 `hasattr(self._backend, "cleanup_workspace")` 分支。
- **`_prerequisites_checked` 标志**：复合后端传给 `SandboxServiceImpl` 后，`service.py:862` 的判定只看 `LocalSubprocessBackend`。若 primary 是 docker，`_prerequisites_checked` 为 `True`（跳过宿主 pip install），符合预期；但 fallback 到 local 时，宿主 Python 可能缺包——而 local 模式本就依赖宿主预装，因此**宿主侧 `requirements-sandbox.txt` 也应保持可安装**（保留现有 `_check_prerequisites` 逻辑对 local 生效即可，复合后端判定时需注意）。

### 3.3 镜像策略（安全 + 离线双重要求）

- 新建 `backend/Dockerfile.sandbox`，基于 `python:3.12-slim`，`COPY requirements-sandbox.txt` 并 `pip install --no-cache-dir`。
- 镜像**固定 Tag**，例如 `harness/sandbox:1.0`，并在 `DockerBackend` 中设 `allowed_images=["harness/sandbox:1.0"]`，同时把 `DEFAULT_DOCKER_IMAGE`（`service.py:57`）改为该镜像。
- **离线分发**：构建机联网一次 `docker build`（需拉 `python:3.12-slim` 基础镜像），产物通过 `docker save/load` 或私有仓库下发到**运行机**；运行机执行 `docker run` 时镜像已存在，**不依赖 Docker Hub 或运行时联网**，与 `--network none` 一致。
- 运行机**必须预拉取 / 预加载镜像**，否则 `docker run` 会尝试联网拉取 → 在禁网下直接失败（而非降级，因为失败发生在 docker 层）。运维 runbook 必须包含"镜像就位校验"。

---

## 4. 分阶段实施步骤

### 阶段 1 — 自动兜底后端（核心逻辑，代码量：低）

**改动文件**：`backend/harness/modules/sandbox_manager/service.py`
**新增内容**：`class FallbackSandboxBackend(SandboxBackend)`，实现：
- `__init__(self, primary, secondary, shared_base_dir)`：持有两个内层后端，`get_workspace` 用 `shared_base_dir` 统一派发。
- `async execute(...)`：
  1. 若 `await primary.is_available()`：先试 `primary.execute(...)`；若返回 `blocked` 且 `blocked_reason` 含 "Docker 不可用"，则委托 `secondary` 重跑，并在 `SandboxResult` 上附加 `fallback=True` 标记（便于可观测）。
  2. 否则直接委托 `secondary`。
- `cleanup_workspace(session_id)`：依次清理两个内层后端的工作区。
- 可选：`failed_over` 计数，用于事件埋点。

**改动文件**：`backend/plugins/tool_code_runner/main.py`（`activate`）
- 构造顺序：先建 `docker = DockerBackend(...)` 与 `local = LocalSubprocessBackend(...)`，再 `backend = FallbackSandboxBackend(docker, local, shared_dir)`，传入 `SandboxServiceImpl(backend=backend, ...)`。
- 删除原先 `if backend_name == "docker"` 的硬分支，改为"docker 优先 + local 兜底"始终生效；`HARNESS_SANDBOX_BACKEND=local` 时直接构造纯 `LocalSubprocessBackend`（保留手动强制 local 的逃生通道）。

### 阶段 2 — 沙箱镜像构建（配置量：中）

**新增文件**：`backend/Dockerfile.sandbox`
```
FROM python:3.12-slim
WORKDIR /workspace
COPY backend/requirements-sandbox.txt /tmp/requirements-sandbox.txt
RUN pip install --no-cache-dir -r /tmp/requirements-sandbox.txt
# 维持与 DockerBackend 一致的工作目录约定
CMD ["python"]
```
**注意**：`requirements-sandbox.txt` 当前含 numpy/pandas/matplotlib/requests（见 `backend/requirements-sandbox.txt`）。若后续新增包，镜像须同步重建并升 Tag，避免"镜像与白名单脱节"。

**构建 / 分发**：
- 构建机：`docker build -f backend/Dockerfile.sandbox -t harness/sandbox:1.0 .`
- 运行机：`docker save harness/sandbox:1.0 | ...` 或私有仓库 `docker pull`。

### 阶段 3 — 配置默认值切换（配置量：低）

| 配置点 | 文件:行 | 现状 | 目标 |
|--------|---------|------|------|
| 插件后端默认值 | `backend/plugins/tool_code_runner/plugin.json:15` | `"default": "local"` | `"default": "docker"` |
| 插件镜像默认值 | `plugin.json:22` | `python:3.12-slim` | `harness/sandbox:1.0` |
| 代码内镜像常量 | `service.py:57` `DEFAULT_DOCKER_IMAGE` | `python:3.12-slim` | `harness/sandbox:1.0` |
| 镜像白名单 | `service.py` `DockerBackend.__init__` 新增 `allowed_images` 默认 `["harness/sandbox:1.0"]`；`main.py` 透传 `ctx.config.get("docker_allowed_images", ["harness/sandbox:1.0"])` | 默认空（不限） | 固定白名单 |
| 环境变量说明 | `docs/andy-harness-v1.0.0用户使用手册.md:422` | "默认 本地" | 改为 "默认 docker（不可用自动降级 local）" |

> 注：`HARNESS_SANDBOX_BACKEND` 仍保留作为**强制覆盖**（`local` 强制纯本地、`docker` 强制纯容器且不做兜底）。默认（不设）走"docker 优先 + 自动兜底"。

### 阶段 4 — 网络与资源策略确认（配置量：低）

- 默认保持 `docker_network=False` → `--network none`（与安全目标一致）。
- 若某些业务确实需要联网（如 `requests` 下载），**不靠运行时联网**，而应在镜像内预装或在 fallback 的 local 端执行；如确需容器联网，显式 `docker_network=true` 并评估暴露面。
- 资源默认值复核：`--memory 512m` / `--cpus 1.0`（`service.py:58-59`）。若真实负载出现 OOM（如 pandas 大表），上调需在 `plugin.json:24-25` 与运行时配置同步，并记录调整理由。

### 阶段 5 — 工作区持久化与权限（验证项，代码量：低）

- Docker 后端 bind mount 宿主目录到 `/workspace`（`service.py:707-710`），同一 `session_id` 跨多次执行持久。
- **Windows 风险点**：Docker Desktop 对 Windows 路径有共享目录限制与 8.3 短路径（`ADMINI~1`）问题（参见项目 memory 中 pytest safe-delete 同类坑）。需在 Windows 运行机上验证 `base_workspaces_dir` 落在 Docker 已共享的目录（如用户目录内），且容器写入文件在宿主可被 `shutil.rmtree` 清理。
- 验证项（非代码）：在目标 Windows 运行机执行一次 `code_runner` 连续两次写/读同一文件，确认持久化与清理均正常。

### 阶段 6 — 可观测性与运维埋点（代码量：低）

- 复合后端在 fallback 发生时，通过 `EventBus` 发布 `sandbox.fallback` 事件（复用 `service.py:921-954` 已有的事件总线），携带 `session_id` / `reason`，便于监控"docker 长期不可用"。
- `sandbox.exec.end` 事件补充 `backend_used` 字段（`docker` / `local`），便于统计实际隔离强度。

### 阶段 7 — 测试与验证（测试量：中）

**新增单元测试**（`backend/tests/test_sandbox_fallback.py`）：
- docker 可用时走 docker；docker 不可用（`_make_runner(available=False)` 注入）时透明 fallback 到 local 且 `fallback=True`。
- fallback 后工作区文件在 local 端可见（共用 base_dir 验证）。
- `cleanup_workspace` 对复合后端正确清理两侧目录。

**镜像验证**（手动 / CI）：
- `docker build -f backend/Dockerfile.sandbox -t harness/sandbox:1.0 .` 成功。
- 起一个真实容器执行 `import numpy, pandas, matplotlib, requests` 全通过。

**回归套件**：
- `cd backend && CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest -q --basetemp="C:/Users/Administrator/AppData/Local/Temp/harness_pytest_bt"`（全量，建议后台跑，>5 分钟前台会 SIGTERM）。
- `ruff check .` + `mypy harness`（基线 13/14 错，新增代码不得升高）。
- `make smoke`（一次性临时库起服务 + `smoke_http.py`，自清理）。

---

## 5. 改动文件清单与工作量（细化）

| 文件 | 改动类型 | 内容 | 工作量 |
|------|----------|------|--------|
| `backend/harness/modules/sandbox_manager/service.py` | 新增类 + 微调 | `FallbackSandboxBackend`；`cleanup()` 穿透；`DEFAULT_DOCKER_IMAGE` 改固定镜像；`DockerBackend` 默认 `allowed_images` | 低 |
| `backend/plugins/tool_code_runner/main.py` | 重构激活逻辑 | 永远构造"docker+local 复合后端"；`HARNESS_SANDBOX_BACKEND` 作为强制覆盖 | 低 |
| `backend/plugins/tool_code_runner/plugin.json` | 改默认值 | `backend` 默认 `docker`；`docker_image` 默认固定镜像 | 低 |
| `backend/Dockerfile.sandbox` | 新增 | 预装 `requirements-sandbox.txt` 的专用沙箱镜像 | 低 |
| `docs/andy-harness-v1.0.0用户使用手册.md` | 改说明 | `HARNESS_SANDBOX_BACKEND` 默认值与降级语义 | 低 |
| `backend/tests/test_sandbox_fallback.py` | 新增 | 兜底逻辑 + 共用工作区 + 清理测试 | 中 |
| 运维：镜像构建 / 分发 / 预拉取 | 流程 | `docker build/save/load` 或私有仓库 | 中（一次性，非代码） |

**合计定性**：代码改动 **低**、配置改动 **低–中**、架构改动 **低**（仅在既有 `SandboxService→Backend` 接口内插入一个薄复合层，接口契约不变，不触碰进程模型）。

---

## 6. 风险登记与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 运行机未预加载镜像，`docker run` 联网拉取失败 | 执行直接报错（非降级） | 运维 runbook 增加"镜像就位校验"；CI 构建并分发；运行机 `docker images` 校验 |
| Windows bind mount 路径 / 8.3 短路径 / 文件权限 | 工作区读写或清理失败 | 阶段 5 验证；工作区置于 Docker 共享目录；清理用 `shutil.rmtree(ignore_errors=True)` |
| 守护进程抖动导致频繁 fallback | 隔离强度降级、性能抖动 | `is_available()` 缓存 + `sandbox.fallback` 事件告警；监控连续 fallback 频率 |
| `requirements-sandbox.txt` 增包后镜像未重建 | 容器端 ImportError | 镜像 Tag 随依赖升版；文档约定"改依赖必重建镜像" |
| 复合后端清理遗漏 | 磁盘泄漏 | `cleanup_workspace` 显式清理两侧；`cleanup()` 穿透 |
| 高频短代码冷启动开销 | 延迟上升（容器创建 ~百毫秒~秒级） | 评估是否引入 warm 容器池（**超出本方案范围**，列为后续优化）；或对该类场景保留 local |
| `HARNESS_SANDBOX_BACKEND=docker` 强制模式无兜底 | docker 挂则全 block | 强调默认走复合后端；强制模式仅用于调试 |

---

## 7. 运维 Runbook（部署与故障）

**部署前置（每台运行机）**：
1. 安装 Docker 引擎并启动守护进程（`docker info` 返回版本）。
   - **Windows 宿主机注意（WSL 2 前置）**：本沙箱镜像 `harness/sandbox:1.0` 基于 `python:3.12-slim`，是 **Linux 容器**，在 Windows 上必须走 Docker Desktop 的 **WSL 2 后端**（Linux 容器在 Windows 内核上需一个轻量 Linux 虚拟机承载）。安装 Docker Desktop 时会**自动启用 WSL 功能并部署专用的 `docker-desktop` WSL 发行版**，**无需手动安装/配置 WSL 或自建 Linux 发行版**；Windows 10 需 Build 19041+ 且 BIOS 开启虚拟化（VT-x）。老的"Windows 容器"（Hyper-V）模式只能跑 Windows 镜像，与本 Linux 镜像不匹配，不可用。
   - **Linux 宿主机**：Docker 原生运行，完全不需要 WSL。
   - **无 Docker / 无 WSL 的 Windows 机器**：方案 A 仍可用——复合后端会自动 fallback 到本地子进程（见阶段 1），仅失去容器隔离收益，不影响可用性。
2. 加载沙箱镜像：`docker load < harness-sandbox-1.0.tar`（或 `docker pull` 私有仓库）。
3. 校验：`docker run --rm --network none harness/sandbox:1.0 python -c "import numpy,pandas,matplotlib,requests;print('ok')"`。
4. 配置 `tool_code_runner` 插件 `backend=docker`（或保持默认）。

**日常监控**：
- `docker ps -a` 检查是否有残留容器（超时强删失效时）。
- 监听 `sandbox.fallback` 事件，持续触发说明 docker 守护进程异常。
- 磁盘：工作区目录（`harness_docker_workspaces` / 共享 base_dir）定期清理。

**故障降级验证**：
- 停掉守护进程 `sudo systemctl stop docker` → 执行 `code_runner` 应自动走 local 并成功，且返回含 `fallback=True` 标记。
- 恢复守护进程 → 后续执行应回到 docker。

---

## 8. 验收标准（Definition of Done）

1. 不设 `HARNESS_SANDBOX_BACKEND` 时，默认走 Docker 后端，且 docker 不可用时**自动** fallback 到 local 并成功。
2. 真实容器中可成功执行 `import numpy/pandas/matplotlib/requests` 的代码（镜像预装生效）。
3. `FallbackSandboxBackend` 单元测试全绿；`test_docker_sandbox.py` 仍全绿；全量 `pytest` + `ruff` + `mypy` 不劣化；`make smoke` 通过。
4. `sandbox.exec.end` 事件带 `backend_used`；fallback 时发 `sandbox.fallback`。
5. 运行机文档化"镜像就位校验"步骤，且能复现"停守护进程→自动降级→恢复→回归 docker"全流程。

---

## 9. 回滚方案

- **配置回滚**：将 `plugin.json` `backend` 改回 `local`、或设 `HARNESS_SANDBOX_BACKEND=local`，立即恢复纯本地模式，无需改代码。
- **代码回滚**：`FallbackSandboxBackend` 为新增类，若出问题，将 `tool_code_runner.activate` 改回原 `if backend_name == "docker"` 二选一（git revert 单文件）即可。
- **镜像回滚**：保留上一版 `harness/sandbox:<旧 Tag>`，白名单与 `DEFAULT_DOCKER_IMAGE` 同步回退。

---

## 10. 与方案 B / C 的边界重申

- 本方案 A **只改代码执行这一层**，不引入跨进程隔离，不动 `ServiceRegistry`/`EventBus` 的进程内假设，因此不触碰 `computer_use`、插件内核、定时任务内核。
- 方案 B（整后端容器化部署）与方案 C（插件/任务级强隔离）的改动量与风险显著更高，且 C 与 Computer Use 的"驱动真实桌面"使命结构冲突——**不在本方案讨论范围**，仅在 `security-isolation-analysis.md` 中对比。
- 若未来要推进 B，可复用本方案的沙箱镜像构建经验（预装 `requirements-sandbox.txt`），但需额外处理"数据卷 + 桌面壳通信 + Computer Use 仍跑宿主"的边界，属于独立立项。
