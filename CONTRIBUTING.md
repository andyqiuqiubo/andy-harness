# 贡献指南

感谢你对 andy-harness 项目的兴趣！欢迎提交 Issue、PR 和建议。

---

## 开发环境搭建

```bash
# 克隆仓库
git clone https://github.com/andyqiuqiubo/andy-harness.git
cd andy-harness

# 安装依赖
make install

# 启动开发服务器
make backend  # 终端 1
make frontend # 终端 2
```

详细步骤请参考 [README.md](README.md)。

---

## 代码规范

### 后端（Python）

- **格式化**：`ruff format .`
- **检查**：`ruff check . && mypy harness`
- **行宽**：120 字符
- **Python 版本**：3.11+
- **类型注解**：必须添加类型注解（mypy strict 模式）

```bash
make lint     # 检查
make format   # 格式化
```

### 前端（TypeScript）

- **格式化**：`prettier --write src/`
- **检查**：`eslint .`
- **测试**：`vitest run`（当前 9 个测试文件 / 43 个用例）
- **框架**：Vue 3 Composition API + `<script setup>`
- **状态管理**：Pinia
- **类型**：必须添加 TypeScript 类型

```bash
cd frontend
pnpm lint
pnpm test
pnpm format
```

### 提交前

```bash
make lint   # 后端 ruff + mypy，前端 eslint
make test   # 后端 pytest
cd frontend && pnpm test   # 前端 vitest（make test 不含）
```

> CI（`.github/workflows/ci.yml`）是唯一权威门禁：`ruff check` / `ruff format --check` / `mypy harness` / `pytest` 与 `eslint` / `vitest` / `vue-tsc` / `vite build` 全部必须通过。仓库里的 `.pre-commit-config.yaml` 工具版本较旧，**结果与 CI 可能不一致，请以 CI 为准**。

---

## 安全红线（提交前必读）

本项目会把用户上传的文件、模型输出、插件代码与 API Key 放在同一台机器上执行/落盘，因此以下几条是硬性要求：

1. **绝不提交密钥与运行时数据**。以下路径已在 `.gitignore` 中，请不要用 `git add -f` 绕过：
   - `backend/data/`（含 `harness.db`、`auth_secret.txt`（**认证签名密钥**）、`permission.json`、`settings.json`、附件）
   - `backend/workspace/`（制品落盘）、`backend/mcp.json`（可能含鉴权 header）、`*.db`、`*.log`
   - API Key 一律通过**设置页 → Providers** 配置（入库时用 Fernet 加密，密钥由本机 machine key 派生），或用环境变量 `DEEPSEEK_API_KEY` / `JEV_API_KEY`；**不要**把 Key 写进任何会被提交的文件。
2. **不要绕过 `require_admin`**。插件 / 技能 / MCP 的安装卸载、权限策略与系统设置的写操作必须保持管理员校验（`backend/harness/api/deps.py`）。认证关闭时该依赖放行是**为了保持单用户开箱即用**，不是可以随意删除的理由。
3. **不要在仓库里留调试产物**：Vite 的 `.dev_*.txt` 转储、临时脚本、日志、`.trash_*` 目录等，提交前用 `git status` 过一遍。
4. **改动沙箱、权限、认证相关代码时必须补测试**：`backend/tests/test_sandbox.py`、`test_permission_manager.py`、`test_auth.py`、`test_audit_fixes.py` 是这几条红线的回归网。
5. **示例配置里不要写真实路径与真实密钥**：`mcp.example.json` 这类示例文件用 `.` 或占位符。

---

## 版本号约定（改版本前必读）

仓库里有**三套互不相同**的版本号，改之前先分清：

| 对象 | 位置 | 当前值 | 是否随产品版本变化 |
|---|---|---|---|
| 产品版本 | `backend/pyproject.toml`、`harness/__init__.py` 的 `__version__`、`frontend/package.json`、`desktop/package.json`、`desktop/src-tauri/tauri.conf.json` 与 `Cargo.toml` | `1.0.0` | ✅ 是 |
| 内核 API 版本 | `backend/harness/kernel/loader.py` 的 `CORE_API_VERSION` | `0.1.0` | ❌ 否（契约代次） |
| 插件版本 | 41 个 `plugin.json` / `ui-plugin.json` 的 `"version"` 与 `core_api` | `0.1.0` | ❌ 否 |

⚠️ **不要用全局替换把 `0.1.0` 改成 `1.0.0`**：会同时改到 7 个测试文件里的 18 处硬编码断言。若确实要升级 `CORE_API_VERSION`，必须同步更新全部 41 个清单的 `core_api` 区间，否则所有插件会加载失败。

---

## 提交 PR

1. Fork 仓库并创建分支：
   ```bash
   git checkout -b feature/my-feature
   ```

2. 编写代码，确保：
   - 代码通过 lint 和测试
   - 新功能有对应测试
   - 如有新 API，更新文档

3. 提交 commit（使用清晰的 commit message）：
   ```bash
   git commit -m "feat: 添加 XXX 功能"
   git commit -m "fix: 修复 XXX 问题"
   git commit -m "docs: 更新 XXX 文档"
   ```

4. Push 并创建 PR：
   ```bash
   git push origin feature/my-feature
   ```

5. 在 PR 描述中说明：
   - 变更内容
   - 变更原因
   - 测试方式

---

## 开发插件

请参考 [插件开发指南](docs/plugin-dev-guide.md)。

---

## 项目架构

请阅读 [架构设计文档](01-architecture.md) 了解项目分层和模块设计。

---

## 行为准则

- 保持友善和尊重
- 欢迎新手提问
- 代码审查时关注建设性反馈
- 尊重不同观点和背景
