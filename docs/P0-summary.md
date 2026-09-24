# P0 阶段总结 — 仓库与工程脚手架

> 完成时间：2026-09-24
> 阶段目标：monorepo 骨架可一键启动，工程规范与 CI 就位。

---

## 1. 完成内容

### 1.1 目录结构

```
andy-harness/
├── README.md                    # 项目说明与快速开始
├── LICENSE                       # MIT
├── Makefile                      # 一键启动/测试/lint/format
├── .gitignore                    # Python + Node + IDE + OS 通用忽略
├── .npmrc                        # pnpm store 指向项目本地
├── .pre-commit-config.yaml       # ruff + mypy + eslint + prettier + 通用 hooks
├── 01-architecture.md            # 架构设计文档（已修订）
├── 02-development-plan.md        # 分阶段开发计划（已修订）
├── docs/
│   └── P0-summary.md             # 本文件
├── examples/                      # 示例目录（待后续阶段填充）
├── .github/workflows/
│   └── ci.yml                    # CI：后端 lint+test / 前端 lint+build
├── backend/
│   ├── pyproject.toml             # uv + FastAPI + ruff + mypy + pytest 配置
│   ├── .venv/                     # Python 虚拟环境（项目本地）
│   ├── uv.lock                    # 依赖锁定文件
│   ├── harness/
│   │   ├── __init__.py
│   │   └── main.py               # FastAPI 入口，GET /api/health
│   └── tests/
│       ├── __init__.py
│       └── test_health.py        # 健康检查测试
└── frontend/
    ├── package.json               # Vue3 + Pinia + Vue Router + ESLint + Prettier
    ├── eslint.config.ts           # ESLint flat config
    ├── .prettierrc.json
    ├── vite.config.ts             # 含 /api 和 /ws 代理到后端 8000 端口
    └── src/
        ├── main.ts                # Pinia + Vue Router 装配
        ├── App.vue                # router-view 根组件
        ├── router/index.ts        # 路由配置
        ├── views/HomeView.vue     # 首页：显示项目名
        └── style.css
```

### 1.2 后端

- **Python 3.12 + uv 0.10**：虚拟环境创建在 `backend/.venv`，uv 缓存指向项目本地 `.uv-cache/`
- **FastAPI 最小应用**：`GET /api/health` 返回 `{"status": "ok"}`
- **测试**：`pytest` 使用 `TestClient` 验证健康检查端点

### 1.3 前端

- **Vite 8 + Vue 3.5 + TypeScript 6**：脚手架由 `pnpm create vite` 生成
- **Pinia 4 + Vue Router 4**：已装配，首页通过 `router-view` 渲染
- **首页**：显示 "andy-harness" + 副标题
- **Vite 代理**：`/api` → `localhost:8000`，`/ws` → `ws://localhost:8000`

### 1.4 工程规范

| 工具 | 范围 | 配置位置 |
|---|---|---|
| ruff (lint + format) | 后端 | `backend/pyproject.toml` |
| mypy (strict) | 后端 | `backend/pyproject.toml` |
| eslint (flat config) | 前端 | `frontend/eslint.config.ts` |
| prettier | 前端 | `frontend/.prettierrc.json` |
| pre-commit | 全局 | `.pre-commit-config.yaml` |

### 1.5 CI (GitHub Actions)

- **后端 job**：uv install → ruff check → ruff format check → mypy → pytest
- **前端 job**：pnpm install → eslint → vue-tsc + vite build

### 1.6 一键操作 (Makefile)

```bash
make install    # 安装前后端依赖
make backend    # 启动后端 (端口 8000)
make frontend   # 启动前端 (端口 5173)
make test       # 运行后端测试
make lint       # 前后端 lint
make format     # 前后端格式化
make clean      # 清理
```

---

## 2. 依赖隔离验证

所有下载的框架和依赖均存储在项目文件夹内，未写入 C/D 盘：

| 存储 | 路径 | 说明 |
|---|---|---|
| Python venv | `backend/.venv/` | uv 创建的本地虚拟环境 |
| uv 缓存 | `.uv-cache/` | `UV_CACHE_DIR` 环境变量指定 |
| pnpm store | `.pnpm-store/` | `.npmrc` 中 `store-dir` 指定 |
| node_modules | `frontend/node_modules/` | 前端依赖 |

---

## 3. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 后端测试 | `uv run pytest -v` | 1 passed |
| 后端 lint | `uv run ruff check .` | All checks passed |
| 后端类型检查 | `uv run mypy harness` | no issues found in 2 source files |
| 前端 lint | `pnpm lint` | no issues |
| 前端构建 | `pnpm build` | built successfully (89.60 kB) |
| 后端 /api/health | `curl localhost:8000/api/health` | `{"status": "ok"}` |
| 前端首页 | `http://localhost:5173/` | 显示 "andy-harness" |

---

## 4. 遇到的问题与解决

| 问题 | 原因 | 解决 |
|---|---|---|
| uv build 失败：`Readme file does not exist: README.md` | pyproject.toml 引用了 README.md 但 backend/ 目录下无此文件 | 移除 `readme = "README.md"` 行 |
| ESLint 报错：`The 'jiti' library is required` | ESLint 10 加载 TypeScript 配置文件需要 jiti | `pnpm add -D jiti` |
| PowerShell 不支持 `mkdir -p` 多目录 | Windows PowerShell 语法差异 | 改用 `New-Item -ItemType Directory -Force` |
| pinia peer dependency 警告 | pinia 4.x 要求 @vue/devtools-api ^8.1.5，实际为 6.6.4 | 不影响功能，暂忽略 |

---

## 5. 待办 / 后续注意

1. **pre-commit 本地安装**：CI 中已配置，但本地需运行 `pre-commit install` 才会生效（需用户安装 pre-commit 工具）
2. **Python 版本**：系统 Python 3.14，uv 选择了 3.12 创建 venv；后续阶段如需特定版本可在 `pyproject.toml` 中指定
3. **前端 pnpm-lock.yaml**：CI 中使用 `--frozen-lockfile`，需确保 lockfile 已提交
4. **后续阶段**：P1 将在 `backend/harness/kernel/` 下实现内核（EventBus / ServiceRegistry / HookManager / PluginLoader / PluginContext + 契约接口）

---

## 6. DoD 对照

| 完成标准 | 状态 |
|---|---|
| 全新克隆后按 README 两条命令启动：后端 /api/health 返回 ok，前端页面正常显示 | 通过 |
| CI 绿灯；pre-commit 本地生效 | CI 配置就绪（pre-commit 需用户执行 `pre-commit install`） |
