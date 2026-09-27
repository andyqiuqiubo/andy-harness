# andy-harness

> 插件化 Agent Harness（智能体底座）开源项目：提供对话 GUI、多模型接入（DeepSeek / Qwen / Doubao / 自定义）、会话管理、上下文管理、沙箱管理与系统设置。所有功能以插件形式构建，模块间完全解耦，面向开发者学习与二次开发。敬请下载使用！

[![CI](https://github.com/andyqiuqiubo/andy-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/andyqiuqiubo/andy-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Vue 3](https://img.shields.io/badge/Vue-3.5-green.svg)](https://vuejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)

---

## 项目状态

- 本仓库由作者在 **Windows 10** 个人 PC 下创建与开发，**尚未在 macOS / Linux 等其他操作系统上验证**，跨平台运行存在未知风险。
- 目前仅对 **DeepSeek `deepseek-v4.1-flash`** 模型做了深度调试与联调；Qwen / Doubao 等其它模型尚未充分验证，实际使用时可能遇到预期外的问题。

---

## 功能特性

- **多模型接入**：DeepSeek（含思维链）、Qwen、Doubao，支持自定义 OpenAI 兼容模型
- **Jev 结构化决策**：集成 TypeSafe AI Jev 模型，支持 Choice（选择型）/ Score（评分型）/ Noul（是非型）三种原语，AI 可自动调用进行结构化判断
- **插件化架构**：一切皆插件，内核零业务逻辑，工具/Provider/服务/UI 均可插件化
- **插件市场**：内置插件市场，一键安装与说明查看，卸载后可随时重装，系统插件不可删除
- **元气宠物**：全局悬浮的养成系宠物，自由走动、悬停卖萌、喂食玩耍，作为市场插件安装后立即可用
- **前后端插件联动**：UI 插件通过 backend_plugin_id 与后端插件联动，启用/停用实时同步
- **会话管理**：多轮对话持久化、自动生成标题、归档/删除；存在空会话时点击"新建"自动跳转并提示
- **Token 用量展示**：基于 DeepSeek 官方 usage API，每条 AI 回答下方显示「本次用量」（输入 / 缓存 / 未命中 / 输出 / 合计）
- **聊天滚动控制**：流式回复中上滑即停止自动滚动，固定「向下」按钮一键回到最新回复或下一条提问
- **工具调用可视化**：工具卡片完整展示工具名、参数与执行结果，历史会话回看同样完整
- **上下文管理**：滑动窗口 / 摘要压缩 / 消息钉住，token 预算可控
- **沙箱执行**：Python/Shell 代码执行，资源限制，进程树终止
- **思维链展示**：DeepSeek reasoning_content 逐步展示，完成后折叠
- **Provider 状态管理**：密钥配置、连接测试、启用/停用状态联动，停用后模型和工具自动隔离
- **多主题系统**：黑客帝国 / 深海蓝 / 日落紫红 / 中性深灰 / 亮色，AI 也可切换主题
- **Matrix 数字雨**：首页 Canvas 0/1 瀑布雨，鼠标弯曲效果
- **Markdown 渲染**：代码高亮、表格、GFM，流式内容实时渲染
- **前端插件机制**：UI 插件可注入视图、菜单项、路由
- **国际化**：中文 / 英文

---

## 演示

![andy-harness 演示](gif/screenshot_2026-09-27_16-08-22.gif)

> 演示插件市场安装元气宠物、全局悬浮宠物互动、插件管理与 Provider 状态管理等特性（GIF）。[观看完整 MP4 视频](https://github.com/andyqiuqiubo/andy-harness/blob/main/videos/20260925_132012.mp4)（更清晰，GitHub 在线播放）

---

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 22+
- pnpm 10+
- uv（Python 包管理器）
- **GNU Make**（Windows 上 Git for Windows 不自带 `make`；缺了它 `make install/backend/frontend` 会报 `command not found`。Windows 可用 Chocolatey `choco install make`，或改用下面的「不使用 make」方式）

### 安装

```bash
# 克隆仓库
git clone https://github.com/andyqiuqiubo/andy-harness.git
cd andy-harness

# 一键安装前后端依赖
make install
```

以上命令等价于：

```bash
cd backend && uv sync --extra dev
cd frontend && pnpm install
```

> Windows 下若不想装 `make`，可先按上面两条命令装好依赖，再双击 `start-all.bat` 一键拉起前后端（`start-all.bat` 不会自动安装依赖，它只会复用已存在的 `backend/.venv` 与 `frontend/node_modules`）。

### 配置 API Key

在 `docs/key.txt` 中写入 DeepSeek API Key（或通过环境变量配置）：

```
ds:sk-your-api-key-here
```

> 也可以启动后在设置页面配置：http://localhost:5173/settings → Providers → 编辑 DeepSeek → 填入 API Key

### 启动开发服务器

打开两个终端分别执行：

```bash
# 终端 1：启动后端（端口 8000）
make backend

# 终端 2：启动前端（端口 5173）
make frontend
```

访问：
- 前端：http://localhost:5173
- 后端健康检查：http://localhost:8000/api/health
- API 文档：http://localhost:8000/docs

### Docker 部署

```bash
docker-compose up -d
```

访问 http://localhost:5173 即可使用。

---

## ⚠️ 下载后常见问题（排错）

> 下面是「照着 README 走却仍然踩坑」的高频问题，建议先读一遍再动手。

### 1. 没装依赖就启动 → 报「系统找不到指定的路径」/ 模块缺失
仓库**不包含** `backend/.venv` 和 `frontend/node_modules`（都被 `.gitignore` 排除）。无论用 `make install` 还是双击 `start-all.bat`，都必须先装依赖：

```bash
cd backend && uv sync --extra dev      # 生成 .venv 并安装后端依赖
cd frontend && pnpm install            # 生成 node_modules
```

直接双击 `start-all.bat` 而没装依赖，Windows 会弹出一个黑色窗口并瞬间报「系统找不到指定的路径」——这不是代码 bug，就是依赖没装。

### 2. Windows 上 `make install` 报 `command not found: make`
Git for Windows 自带的 bash **不带 make**。两种解法：

- 安装 make：Chocolatey 执行 `choco install make`（或用 MinGW 的 `mingw-get install make`）；
- 不装 make，直接用上面第 1 条的等价命令（见「快速开始 → 安装」）。

### 3. 必须用 pnpm，不要 `npm install`
仓库用 `pnpm-lock.yaml` 锁定依赖版本。用 `npm install` 会重新解析依赖，可能与锁文件不一致甚至失败。请先安装 pnpm 10+：

```bash
npm i -g pnpm
```

之后用 `pnpm install`。国内网络可加镜像加速：`pnpm install --registry=https://registry.npmmirror.com`。

### 4. 前端打不开？只能用 `http://localhost:5173`
Vite 默认把开发服务器绑定到 `localhost`（解析为 IPv6 `[::1]`）。直接访问 `http://127.0.0.1:5173` 会**连不上**。浏览器地址栏请使用 **`http://localhost:5173`**。`start-all.bat` 末尾自动打开的也是这个地址。

### 5. 能启动但没法对话？需要先配 Provider 的 API Key
不配 Key 也能启动（后端日志只会有 `Jev Manager 已激活，但未配置 API Key` 的警告），但发消息会失败。配置方式二选一：

- 启动后在页面设置：http://localhost:5173/settings → Providers → 编辑对应 Provider → 填入 Key 并启用；
- 或把 Key 写进 `docs/key.txt`，格式为 `ds:sk-xxxx`（DeepSeek）；Qwen / Doubao 同理用 `qwen:` / `doubao:` 前缀。

> **Jev 结构化决策**需要**单独的** `jev_manager` API Key（设置 → 插件管理 → Jev Manager），与 Provider Key 不是同一个。未配置时调用 Jev 工具只会返回友好错误提示，不影响普通对话。

### 6. 端口被占用（8000 / 5173）
若目标端口已被占用，`start-all.bat` 会提示 `Port 8000 is already in use` / `Port 5173 is already in use` 并退出。先双击 `stop-all.bat` 关闭旧实例，或手动结束占用进程后再启动。

### 7. Python 用哪个版本？
后端要求 Python 3.11+。`uv sync` 会自动下载并管理合适的 Python（无需你手动安装），只要本机有可用的 3.11+ 或允许 uv 联网下载即可。

### 8. 国内网络下载慢 / 超时
- **Python 依赖**：`uv sync` 可指定国内镜像提速：

  ```bash
  UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --extra dev
  ```

- **前端依赖**：用 npmmirror 镜像 `pnpm install --registry=https://registry.npmmirror.com`。

---

## 使用指南

### 基本对话

1. 访问 http://localhost:5173/chat
2. 点击左侧"新建会话"（若已存在无任何消息的空会话，会自动跳转到该会话并提示"已有新会话，请提问"，不会重复创建）
3. 在输入框输入问题，按 Enter 发送
4. AI 回复实时流式渲染，思维链逐步展示后折叠；回复完成后下方显示「本次用量」胶囊（输入 / 缓存 / 未命中 / 输出 / 合计）

### 查看历史对话

- 流式回复中向上滚动即停止自动跟随，停留在当前位置查看历史
- 聊天区右下角固定显示「向下」按钮：流式中点击直接回到最新文字；非流式中每点击一次跳到下一条提问的位置
- 切换会话时自动回到消息底部

### 切换模型

在聊天页面右上角选择 Provider 和 Model。仅已启用的 Provider 的模型会出现在列表中。

### 使用 Jev 结构化决策

Jev 是 TypeSafe AI 的结构化决策模型，支持三种原语：

- **Choice（选择型）**：从固定选项中选一个，返回概率分布和置信度
- **Score（评分型）**：在自定义等级上打分，返回概率分布和置信度
- **Noul（是非型）**：判断命题是否成立，返回 0~1 概率值

使用前需在设置 → 插件管理 → Jev Manager 配置 API Key。配置后，AI 会根据对话内容自动判断是否调用 Jev 工具。

### 管理 Provider

1. 进入设置页面 → 模型 Provider 标签
2. **未配置密钥**：显示"未配置密钥"标签，测试连接和启用按钮置灰，编辑和删除可点击
3. **点击编辑**：填写配置信息后点击"启用"按钮，系统自动测试连接
   - 连接成功：自动启用并关闭编辑表单，列表显示"已启用"
   - 连接失败：在编辑表单内显示失败原因（含具体错误信息），Provider 保持停用状态
4. **已启用状态**：测试连接、停用、编辑按钮可点击，删除按钮置灰（需先停用才能删除）
5. **已停用状态**：测试连接、启用、编辑、删除按钮均可点击
6. 停用 Provider 后，其模型从模型选择器中自动隐藏，聊天时不可调用

### 从插件市场安装插件

1. 进入设置页面（聊天页面右上角齿轮图标）
2. 切换到"插件管理"
3. 点击"从插件市场安装插件"
4. 在市场卡片上点击「说明」查看详细介绍，点击「安装」一键安装（服务端直装）
5. 安装后插件出现在插件管理列表，可启用/停用/配置；卸载后可从市场随时重装
6. 系统内置插件默认为系统插件，不显示删除按钮；仅市场安装的插件可卸载

> 想要自己开发插件发布到市场，参考[插件开发指南](docs/plugin-dev-guide.md)。

### 切换主题

1. 设置 → 通用设置
2. 选择主题（黑客帝国 / 深海蓝 / 日落紫红 / 中性深灰 / 亮色）
3. 或在对话中让 AI "换个蓝色主题"

---

## 项目结构

```
andy-harness/
├── backend/           # Python 后端（FastAPI + SQLite + uv）
│   ├── harness/       # 核心代码
│   │   ├── kernel/    # L4 内核（插件加载/事件总线/服务注册/钩子）
│   │   ├── engine/    # L3 Agent 引擎（循环/工具/钩子）
│   │   ├── modules/   # L2 内置插件（会话/上下文/模型/沙箱）
│   │   ├── api/       # L1 REST + WebSocket
│   │   └── infra/     # L5 SQLite/加密/Repository
│   ├── plugins/       # 扩展插件（自动扫描）
│   └── tests/         # pytest 测试
├── frontend/          # Vue 3 前端（Vite + TypeScript + Pinia）
│   └── src/
│       ├── views/     # ChatView / SettingsView / HomeView
│       ├── stores/    # chat / providers / plugins / settings
│       ├── components/# Markdown / MatrixRain / SessionSidebar
│       └── core/      # 前端插件内核
├── docs/              # 文档（架构/开发计划/插件指南）
└── .github/           # CI 工作流
```

---

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / SQLite / uv |
| 前端 | Vue 3 / Vite / TypeScript / Pinia / Vue Router / pnpm |
| Markdown | markdown-it / highlight.js |
| 规范 | ruff + mypy（后端）、eslint + prettier（前端）、pre-commit |

---

## 开发文档

- [架构设计文档](01-architecture.md) — 总体分层、插件机制、核心模块详设
- [分阶段开发计划](02-development-plan.md) — P0–P9 任务清单与验收标准
- [插件开发指南](docs/plugin-dev-guide.md) — 从零开发可运行插件

---

## 开发命令

```bash
make install    # 安装依赖
make backend    # 启动后端
make frontend   # 启动前端
make test       # 运行测试
make lint       # 代码检查
make format     # 代码格式化
make clean      # 清理缓存
```

---

## FAQ

**Q: 如何接入新的模型厂商？**

A: 创建一个 Provider 插件，继承 `OpenAICompatibleProvider`，设置 `base_url` 和 `default_models`。详见[插件开发指南](docs/plugin-dev-guide.md#2-后端-provider-插件开发)。

**Q: 如何让 AI 调用自定义工具？**

A: 创建一个 Tool 插件，实现 `ToolPlugin` 契约（tool_name / description / parameters_schema / execute）。安装后 AI 会自动根据 description 判断何时调用。详见[插件开发指南](docs/plugin-dev-guide.md#1-后端工具插件开发)。

**Q: 插件需要重启服务才能生效吗？**

A: 不需要。通过 UI 安装的插件会自动加载激活。手动创建的插件目录需要重启后端。

**Q: 支持哪些模型？**

A: DeepSeek（v4-flash / v4-pro / chat / reasoner）、Qwen（max / plus / turbo / long）、Doubao（pro / lite）。任何 OpenAI 兼容端点都可接入。停用的 Provider 不会出现在模型列表中。

**Q: Jev 是什么？如何使用？**

A: Jev 是 TypeSafe AI 的结构化决策模型，支持 Choice（选择型）、Score（评分型）、Noul（是非型）三种原语。在设置 → 插件管理 → Jev Manager 中配置 API Key 后，AI 会根据对话内容自动判断是否调用 Jev 进行结构化判断。

**Q: Provider 停用后会有什么影响？**

A: 停用后该 Provider 的模型从模型选择器中隐藏，聊天时不可调用，Token 计数也会跳过该 Provider。重新启用时需先通过连接测试。

**Q: API Key 安全吗？**

A: API Key 使用 Fernet 对称加密存储在 SQLite 中，密钥派生自本地 machine key，日志中自动脱敏。

---

## 贡献

欢迎提交 Issue 和 PR！请阅读 [贡献指南](CONTRIBUTING.md)。

---

## License

MIT
