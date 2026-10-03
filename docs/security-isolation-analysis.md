# andy-harness 安全隔离技术分析（含 Docker 容器化评估）

> 文档性质：纯说明性技术分析。本文只描述现有机制、评估引入 Docker 容器隔离的改动量与影响，**不执行任何代码改造，也不提供可运行的改造脚本**。文中引用的路径与组件均来自 `F:/traeprojects/andy-harness-gy` 当前代码库。

---

## 0. 结论先行（TL;DR）

1. **现状**：本项目已采用"应用层 + 逻辑层"的多层防御式隔离，覆盖代码执行、工具权限、数据、会话/任务、插件、密钥与桌面进程边界；其中**代码执行沙箱已经内置可选的 `DockerBackend`**，并非完全空白。
2. **Docker 的真实定位**：当前 Docker 仅作为 `code_runner` 的**可选**执行后端（非默认）和一个整体后端的**部署镜像**存在。所谓"引入 Docker 隔离"在本项目中应区分为三种不同力度的方案（见 §2），其改动量从**低**到**高**差距巨大。
3. **核心约束**：本项目的核心使命是 **Computer Use（操控真实本机桌面）**，而该能力（鼠标/键盘/截图/命令执行）本质上必须运行在宿主桌面进程内。对后端做"整进程容器化"会与这一使命产生**结构性冲突**。
4. **建议**：把 Docker 作为**代码执行沙箱边界**的默认后端（低改动、高安全收益）是值得推进的方向；把 Docker 升级为"插件级/任务级/整后端"的强隔离，当前**投入高、兼容性风险大、且与 Computer Use 定位冲突**，不建议现阶段列为隔离目标。

---

## 1. 现有安全隔离技术：实现机制、架构与核心组件

整体架构遵循 `01-architecture.md` 的"一切皆插件 / 契约优先 / 内核无业务"原则，隔离能力分散在 L1–L5 各层，形成**纵深防御（defense-in-depth）**，而非依赖单一边界。

### 1.1 隔离分层总览

| 层 | 隔离域 | 核心组件 | 隔离强度 | 隔离边界 |
|---|---|---|---|---|
| 代码执行 | 任意代码/Shell 执行 | `sandbox_manager`（LocalSubprocessBackend / DockerBackend）+ `tool_code_runner` | 中（本地）/ 强（Docker 后端） | 工作区目录、进程树、资源上限、危险命令 |
| 工具权限 | 工具调用决策 | `permission_manager` | 逻辑层（deny-by-default） | 风险分级 + 人工确认/拒绝 |
| 数据 | 持久化数据（库/工件/附件） | `infra/database` + 环境变量覆盖 | 逻辑层 + 进程级（SQLite 文件） | 数据库文件、目录路径、用户归属列 |
| 会话/任务 | 无人值守自动运行 | `scheduler` + `scheduler/runner` | 逻辑层（最小工具集） | 隔离会话、受限 ToolRegistry、Skill 白名单 |
| 插件 | 第三方/市场插件 | `kernel/loader` + `PluginContext` + `plugin.json` | 逻辑层（受限门面） | 权限声明、core 保护、版本兼容 |
| 密钥 | API Key 等敏感数据 | `infra/crypto` | 数据静态加密 | 机器密钥派生的 Fernet 密钥 |
| 桌面进程 | GUI/WebView 能力 | Tauri `capabilities/default.json` | 进程级（能力声明） | webview 可调用的系统能力 |
| 模型调用 | 跨会话状态泄漏 | MCP/WS 每连接代理 | 逻辑层（每连接代理） | provider 实例不可变、代理隔离 |

### 1.2 代码执行沙箱（最核心的"不可信输入"边界）

文件：`backend/harness/modules/sandbox_manager/service.py`、`backend/plugins/tool_code_runner/main.py`。

**架构**：`tool_code_runner` 只面向 `SandboxService` 抽象编程，不感知后端实现；后端通过 `SandboxBackend` 抽象切换，当前有两个实现：

- **`LocalSubprocessBackend`（默认）**
  - **会话级工作区隔离**：每个 `session_id` 映射到独立临时目录（`get_workspace`），执行前后做文件快照对比，仅报告真正新增文件。
  - **进程级资源限制**：
    - Linux/macOS：`preexec_fn` 内用 `resource.setrlimit(RLIMIT_AS/RLIMIT_CPU)` 限制内存（512MB）与 CPU 时间（10s）。
    - Windows：通过 `ctypes` 调用 kernel32 创建 **Job Object** 施加进程内存上限（512MB），并对子进程整树 `taskkill /F /T` 终止。
  - **超时与进程树清理**：`proc.communicate(timeout)` 超时后强制杀掉整棵进程树，避免孙进程残留。
  - **输出截断**：stdout/stderr 超 `DEFAULT_MAX_OUTPUT`（10000 字符）截断，防日志吞噬。
  - **危险命令黑名单**：正则匹配 `rm -rf /`、`mkfs`、`dd of=/dev/`、`shutdown`、fork bomb 等，在容器/进程**外**先拦截。
  - **路径穿越拦截**：检测 `../`、`..\` 及指向 `/etc/`、`/root/`、`C:\Users`、`C:\Windows` 等敏感绝对路径的访问。
  - **网络隔离（软）**：通过清空 `HTTP(S)_PROXY` 等代理变量 + 注入 `HARNESS_NO_NETWORK=1` 标记变量实现"软禁网"，**非 OS 级强制**——代码本身仍可在子进程内绕过标记直接建连。
  - **风险分级**：`tool_code_runner` 声明 `risk_level="dangerous"`，默认策略下需人工确认（`permission_manager`）。

- **`DockerBackend`（已实现，可选，非默认）**
  - 每次执行以**一次性容器** `docker run --rm` 运行，结束即销毁，天然无残留。
  - 会话工作区以 **bind mount** 挂到容器内 `/workspace`，多次执行间文件持久。
  - 默认 `--network none` 强制禁网（可配置 `network=True` 切到 bridge）。
  - **镜像白名单**（`allowed_images`）：不在白名单的镜像直接拦截。
  - 资源限制：`--memory 512m` / `--cpus 1.0`。
  - 超时/异常时 `docker rm -f` 强制删除，防容器泄漏。
  - 复用与本地后端一致的"容器外先拦截"（危险命令、路径穿越）逻辑。
  - 后端选择：环境变量 `HARNESS_SANDBOX_BACKEND=docker` 或插件配置 `backend: docker`；`is_available()` 探测 docker 守护进程，不可用时明确报错（本地后端作兜底）。

> **边界说明**：本地后端把"隔离"落在**宿主进程树 + 逻辑规则**上，强依赖于宿主 OS 的资源管控能力与黑名单规则的完备性；Docker 后端把"隔离"落在**内核级命名空间/控制组**上，是真正的强边界。二者共用同一 `SandboxService` 接口，对上层透明。

### 1.3 工具权限控制（控制面隔离）

文件：`backend/harness/modules/permission_manager/service.py`。

- **风险三级**：`read` / `write` / `dangerous`；未注册工具默认按 `dangerous`（最保守）。
- **策略模式**：`auto`（全放行）/ `confirm_dangerous`（默认，仅危险需确认）/ `confirm_write`（写+危险需确认）/ `confirm_all`（全确认）。
- **单工具覆盖**：`overrides` 可把任意工具单独设为 `auto`/`confirm`/`deny`，持久化于 `data/permission.json`。
- **决策优先级**：单工具覆盖 > 全局策略；默认 deny-by-default 之外的一切可审计、可确认。
- **边界**：这是**逻辑层**的调用决策闸，不阻止代码本身的能力，只控制"模型能否触发该工具"。它必须与沙箱、数据隔离等其他层配合才构成完整防线。

### 1.4 数据隔离（持久化边界）

文件：`backend/harness/infra/database.py`、`backend/tests/conftest.py`。

- **数据库路径可覆盖**：`Database` 优先取 `db_path` 入参，其次环境变量 `HARNESS_DB_PATH`，最后回退默认 `data/harness.db`。这天然支持"测试/冒烟/调试使用独立临时库，绝不污染真实库"。
- **并发保护**：单共享连接 + `threading.RLock` 包裹所有 DB 操作；启用 WAL（`journal_mode=WAL`）+ `busy_timeout=5000` 缓解写冲突。
- **会话域划分**：`sessions`/`messages`/`artifacts`/`memories` 等均以 `session_id` 外键关联，按会话域隔离数据。
- **多用户归属（E12）**：`users` 表 + `sessions.user_id` / `memories.user_id` 列，认证启用后按用户隔离会话与记忆。
- **工件/附件目录隔离**：`HARNESS_ARTIFACTS_DIR`、`HARNESS_ATTACHMENTS_DIR` 环境变量可重定向落盘位置。
- **测试隔离范式**（`conftest.py`）：会话级 fixture 将 `HARNESS_DB_PATH` / `HARNESS_ARTIFACTS_DIR` / `HARNESS_ATTACHMENTS_DIR` / `MCP_CONFIG_PATH` 全部指向 `tmp_path_factory` 临时目录，确保集成测试不触碰真实数据，也不去连真实远端 MCP。
- **边界**：隔离发生在"文件路径/会话归属"层面，是单库多租户式隔离，**非独立实例/独立进程**隔离；同一进程的并发写入依赖锁与 WAL。

### 1.5 会话/任务隔离（无人值守边界）

文件：`backend/plugins/scheduler/main.py`、`backend/harness/modules/scheduler/runner.py`。

- **隔离新会话**：每个定时任务运行新建独立会话（标题含任务名+时间），过程可追溯。
- **最小工具集**：`TaskRunner.build_registry` 仅把任务**勾选**的内置工具、指定 MCP 服务器的工具、指定 Skill 装入一个**全新的 `ToolRegistry`**；未勾选工具根本不在注册表中。
- **Skill 限定**：通过 `AgentLoopConfig.skill_allowlist` 只注入勾选的 Skill，并用 `ScopedUseSkillTool` 包装器在调用前二次校验，挡住未勾选 Skill。
- **预授权但最小**：运行期对"已勾选工具"自动放行确认（`_auto_approve` 仅当 `tool_name in allowed`），但工具集本身已被裁剪到最小，避免无人值守时误用危险工具。
- **资源护栏**：`max_tool_iterations`（1–20）、`timeout_seconds`（≥30，默认 300）限制运行时长。
- **边界**：隔离在**逻辑层**完成（独立注册表 + 白名单 + 新会话），不引入新进程/容器；任务间仍共享同一后端进程与数据库。

### 1.6 插件隔离（扩展边界）

文件：`backend/harness/kernel/loader.py`、`backend/harness/kernel/context.py`、`backend/harness/kernel/contracts/*`。

- **受限门面 `PluginContext`**：插件拿到的不是整个内核，而是受限上下文（logger/config/events/services），无法越权访问内部实现。
- **声明式权限**：`plugin.json` 的 `permissions` 字段声明 `network`/`storage`/`secret` 等能力，供加载期校验。
- **核心插件保护**：`core: true` 的内置插件（如 `session-manager`、`scheduler`）不可停用/卸载，防系统自锁。
- **版本与依赖校验**：`core_api` 语义化兼容校验、依赖插件存在性校验。
- **优雅降级**：插件异常激活/调用时返回 `ServiceUnavailable` 标准错误，事件订阅自动注销，ToolRegistry 自动清理其工具，不拖垮系统。
- **边界**：这是**调用面/生命周期**层面的隔离，保证"坏插件不拖垮系统"，但不限制插件代码本身的宿主资源消耗（除非该插件经由沙箱执行）。

### 1.7 密钥与桌面进程边界

- **密钥静态加密**（`infra/crypto.py`）：Fernet 对称加密，密钥由**机器标识**派生（Windows `winreg` MachineGuid / Linux `/etc/machine-id` / macOS `IOPlatformUUID`），API Key 不落明文；密钥丢失时提示重新输入。隔离粒度是"**绑定到本机**"，换机即失效。
- **桌面能力边界**（Tauri `desktop/src-tauri/capabilities/default.json`）：以 capability 声明 webview 可调用能力（`core:app`/`core:event`/`core:window`/`core:webview`），是 GUI 进程对系统能力的**最小授权**模型。
- **Computer Use 护栏**（`plugins/computer_use`）：工具层仅暴露**通用 OS 原语**（鼠标/键盘/截图/文件读写/命令执行），**不绑定具体 App/网站**；强制护栏包括"键盘输入只能是输入框内容"、"导航须完整 http(s) URL"、"执行后 wait+screenshot 校验"、"优先键盘、尽量不靠坐标"。这是**行为层**隔离/护栏，保证通用 Agent 不会因写死某 App 而污染其它桌面任务。

### 1.8 各层隔离的"原理—边界"小结

- **原理分两类**：
  - *内核级/OS 级*：仅 Docker 后端（`--network none`、cgroup 资源、命名空间）属此类；本地沙箱的 Job Object / `setrlimit` 属"宿主内核资源管控"，但**非完整命名空间隔离**。
  - *逻辑层/应用层*：权限、数据路径、会话/任务、插件门面、密钥加密、行为护栏——均依赖"代码自己守规矩"，可被恶意/有缺陷代码绕过。
- **边界**：当前纵深防御在"应用层"非常厚实，在"OS 内核层"仅在代码执行这一条路径上（且需显式开启 Docker 后端）才落地。这正是 Docker 议题的价值所在。

---

## 2. 引入 Docker 容器技术进行隔离：改动量与影响评估

### 2.1 当前代码库中的 Docker 足迹（务必先认清）

1. **代码执行沙箱 `DockerBackend`**：已完整实现（见 §1.2），支持一次性容器、禁网、镜像白名单、资源限制、超时强删，并经 `test_docker_sandbox.py` 覆盖（含真实 docker 可用时的端到端用例）。
2. **整体后端 `Dockerfile`**：`backend/Dockerfile` 基于 `python:3.12-slim`，`uv sync` 装配依赖，暴露 8000，运行 `uvicorn`——即"**把整个后端打成一个容器镜像**"，属于**部署级容器化**，但**内部各组件仍同处一进程、无相互隔离**。

因此"引入 Docker 隔离"在本项目中并非从零开始，而应理解为三种不同力度的方案，需分别评估。

### 2.2 三种方案的定义与定性改动量

| 方案 | 含义 | 代码改动 | 配置改动 | 架构改动 |
|---|---|---|---|---|
| **A. 沙箱默认 Docker 化** | 把 `code_runner` 的执行后端默认切到 `DockerBackend`（已有实现），本地子进程仅作兜底 | **低** | **低**（设 `HARNESS_SANDBOX_BACKEND=docker`、定镜像白名单、预构建含 `requirements-sandbox` 的镜像） | **低**（接口不变，仍是 `SandboxService→Backend`） |
| **B. 后端整体容器化部署** | 沿用/完善现有 `Dockerfile`，加编排文件，使后端以标准容器运行（含卷挂载、健康检查、env 注入） | **低** | **中**（docker-compose / k8s、挂载 `data/` 卷、env、网络、与桌面壳通信边界） | **中**（进程模型从"裸机/桌面壳拉起"变为"容器内运行"，需处理状态卷与端口） |
| **C. 插件级/任务级/租户级强隔离** | 每个插件或每次任务在**独立容器**中运行，内核级隔离 + 独立资源账期 | **高** | **高**（插件运行时镜像化、动态编排、跨容器服务发现、MCP/WS 网络打通、共享卷权限、镜像仓库与 CI） | **高**（破坏"内核极小 + 进程内 ServiceRegistry/EventBus"假设，需引入跨进程 IPC） |

> 以下影响分析以**最贴近"隔离"语义的 A 与 C**为主，B 作为部署视图附带说明。

### 2.3 对系统性能的影响

- **方案 A（沙箱 Docker 化）**
  - *负面影响*：每次代码执行需 `docker run` 启动容器，冷启动延迟（镜像拉取/容器创建）通常比本地子进程高数十到数百毫秒；频繁小代码段执行会更明显。
  - *正面影响*：资源限制（cgroup）更精准、更稳定，避免本地后端在 Windows 上 Job Object 偶发失效导致的资源泄漏；超时后容器即销毁，无残留进程树。
  - *结论*：对"长时/heavy 代码"利大于弊，对"高频短代码"需配合镜像预热/常驻容器优化。
- **方案 C（插件/任务级隔离）**
  - 每个插件/任务独立容器的调度与网络开销显著，尤其 `computer_use` 类需频繁与宿主交互的任务，跨容器 IPC 序列化会成为瓶颈；整体吞吐与延迟下降明显，运维复杂度陡增。

### 2.4 对部署运维的影响

- **方案 A**：部署形态基本不变；新增依赖是"目标机需安装并运行 Docker 守护进程"。在**本项目目标场景（用户 Windows 10 桌面 + Tauri 桌面壳）**下，Docker 需 WSL2 后端，增加了桌面用户的环境门槛与体积（Docker Desktop 数 GB）。运维收益主要体现在"代码执行这一高危路径"的安全闭环。
- **方案 B**：标准化部署，利于 CI/CD 与多机分发；但需维护镜像构建、数据卷、与桌面壳的通信边界（容器 ↔ 宿主 ↔ Tauri）。
- **方案 C**：需自建容器编排（或 k8s）、镜像仓库、构建流水线、资源记账与回收、日志聚合。对一个以"桌面端单机 Agent"为定位的项目，运维成本与定位严重不匹配。

### 2.5 对兼容性的影响

- **致命冲突——Computer Use 使命**：本项目核心定位是操控**真实本机桌面**（`computer_use` 的鼠标/键盘/截图/命令执行必须作用于宿主 GUI）。把后端整体塞进容器后，这些 OS 级动作要么无法触达宿主屏幕/输入设备，要么需复杂的"容器内指令 → 宿主代理执行"桥接，**本质上削弱甚至破坏产品核心能力**。这是方案 B/C 的硬兼容性障碍。
- **MCP 与桌面壳**：大量 MCP 服务器以 `stdio` 本地子进程方式运行，容器内外进程模型差异需逐一适配；Tauri 桌面壳与后端若跨容器通信，需重新设计本地 socket/端口边界。
- **平台差异**：本地沙箱对 Windows（Job Object）与 Linux（setrlimit）做了平台适配；Docker 后端在不同宿主上行为更一致，但引入了对 Docker 守护进程本身的平台依赖（Windows 需 WSL2）。
- **方案 A 兼容性最佳**：仅替换代码执行后端，不影响 Computer Use、插件机制、桌面壳，且保留本地后端兜底。

### 2.6 对安全性的影响

| 维度 | 现状（逻辑层为主） | 方案 A（沙箱 Docker 默认） | 方案 C（插件/任务级隔离） |
|---|---|---|---|
| 代码执行逃逸 | 本地后端软禁网，黑名单可被绕过；Job Object/setrlimit 非命名空间隔离 | **显著提升**：`--network none` + cgroup 为内核级强边界 | 同 A 且扩展至插件/任务 |
| 工具/权限误用 | `permission_manager` deny-by-default 有效 | 不变（仍生效） | 不变（仍生效） |
| 数据越权 | 单库多租户 + 用户归属列 | 不变 | 需额外处理跨容器卷权限 |
| 插件污染 | 受限门面 + 优雅降级 | 不变 | **显著提升**：坏插件无法影响宿主/其它插件 |
| 密钥泄露面 | 机器密钥绑定加密 | 若容器化密钥管理不当，反而增加暴露面 | 需引入 secrets 管理，否则风险升高 |
| 新增攻击面 | — | Docker 守护进程本身的漏洞面 | 编排/镜像仓库/网络策略错误配置面 |

> **关键判断**：Docker 对"不可信代码执行"这一威胁模型收益最大（方案 A）；但对"宿主桌面控制"威胁模型（Computer Use）不仅无收益，反而制造结构冲突。安全性提升必须落在正确的边界上。

---

## 3. 对比结论与建议

### 3.1 对比结论

| 维度 | 现状 | 方案 A（推荐推进） | 方案 C（不建议现阶段） |
|---|---|---|---|
| 隔离强度（代码执行） | 中（本地）/ 已有强（Docker 可选） | **强（默认）** | 强（全覆盖） |
| 改动量 | — | 低 | 高 |
| 与 Computer Use 兼容性 | 完全兼容 | 完全兼容 | **冲突** |
| 运维成本 | 低 | 低（需 Docker 守护进程） | 高 |
| 安全收益/复杂度比 | 基线 | **高** | 低 |

### 3.2 建议

1. **将方案 A 列为优先**：把 `code_runner` 的 `DockerBackend` 作为**可用时的默认**执行后端（代码已具备 `is_available()` 自动探测与本地兜底），补齐"含 `requirements-sandbox` 预装包的镜像构建与分发"。这是当前**改动最小、安全收益最高**的一步，且完全不触碰 Computer Use 与插件内核模型。
2. **不把 Docker 用作"整后端/插件级"隔离目标（方案 C）**：与项目"桌面端单机 Agent + Computer Use 操控真实桌面"的核心定位存在结构性冲突，且会破坏"进程内 ServiceRegistry/EventBus"的解耦假设，投入与收益不匹配。
3. **方案 B 仅作为部署演进**：若需要标准化分发/CI，可完善现有 `Dockerfile` 并加编排，但要显式处理"数据卷 + 桌面壳通信边界 + Computer Use 仍须跑在宿主"的架构约束（容器只承载无界面后端逻辑，桌面交互/输入设备控制在宿主侧）。
4. **继续加固逻辑层隔离**：现有纵深防御（权限、数据路径、会话/任务最小工具集、插件门面、密钥加密、行为护栏）已是合理基线，应在推进方案 A 的同时持续完善（如本地后端的"软禁网"升级为更可靠的规则，黑名单→更严格的系统调用约束），而非寄希望于单一容器边界。

### 3.3 风险提示

- 在 Windows 桌面目标场景下，**Docker 守护进程依赖 WSL2**，会带来额外安装体积与稳定性考量；方案 A 必须保留本地子进程兜底，避免无 Docker 环境不可用。
- 任何容器化方案都**新增 Docker 守护进程攻击面**；镜像来源、镜像白名单、容器运行时权限（禁止 `--privileged`、限制 capabilities）需配套治理，否则"隔离"反成"提权跳板"。
- 切勿为追求隔离而牺牲 Computer Use 的宿主控制能力——那是本项目区别于"纯浏览器 Agent"的根本所在。

> 本文为说明性分析，未改动任何代码、未生成可运行改造脚本。如需在方案 A 上落地，应在新的任务中单独评估镜像构建、权限治理与回退策略。
