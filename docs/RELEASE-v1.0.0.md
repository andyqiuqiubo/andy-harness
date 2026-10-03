# 发布 v1.0.0 开发者预览版 · 注意事项与操作清单

> 背景：仓库 `github.com/andyqiuqiubo/andy-harness`，现有最新 tag 为 `v0.0.9`，而 CHANGELOG 已记录到 `0.0.48`（0.0.10 之后的版本**没有打 tag**）。本次要从 `v0.0.9` 直接跳到 `v1.0.0` 发布开发者预览版。

---

## 一、版本号语义（Semver）——先想清楚"1.0.0 承诺了什么"

从 `0.x` 升到 `1.0.0` 不只是数字变化，它向社会发出两个信号：

1. **API 稳定承诺**：从 1.0.0 起，破坏性变更必须升 minor（1.x.0）并写明迁移指引。所以发版前要明确"哪些是承诺稳定的公共接口"，建议在 README 或 docs 里写明：
   - REST API（`/api/**` 路径与响应结构）
   - 插件契约（`plugin.json` 字段、`BasePlugin/ToolPlugin` 接口）
   - 配置文件（`mcp.json`、环境变量、`settings.json`）
   - 数据库文件（`harness.db` 的兼容性承诺）
2. **"开发者预览版"要显式标注**：1.0.0 预览意味着"接口基本稳定但可能微调"。**代码版本号与 git tag 统一用 `v1.0.0`**（7 处版本号字段已定稿，不再回退为 `1.0.0-rc.x`）；"Developer Preview" 只出现在 Release 标题与 Notes 里（如 `andy-harness v1.0.0 (Developer Preview)`），避免用户当成完全稳定版。

## 二、发布前的硬性检查清单

### 2.1 版本号一致性（✅ 已于 2026-10-01 统一为 1.0.0）

**产品版本 = `1.0.0`**，全部 7 处已核对通过：

| 位置 | 原值 | 现值 | 状态 |
|---|---|---|---|
| `backend/pyproject.toml:3` | `0.1.0` | `1.0.0` | ✅ |
| `backend/harness/__init__.py` | （无 `__version__`） | 新增 `__version__ = "1.0.0"` 作为唯一版本源 | ✅ |
| `backend/harness/main.py`（FastAPI `version=`） | `0.1.0` | `1.0.0`（当前仍为字面量，改版本时需与 `__version__` 同步） | ✅ |
| `backend/harness/modules/mcp_client/service.py`（MCP `clientInfo`） | `0.1.0` | `1.0.0` | ✅ |
| `frontend/package.json:4` | `0.0.0` | `1.0.0` | ✅ |
| `desktop/package.json:4` | `0.1.0` | `1.0.0` | ✅ |
| `desktop/src-tauri/tauri.conf.json:4` / `Cargo.toml:3` | `0.1.0` | `1.0.0` | ✅ |
| `CHANGELOG.md` | 止于 `0.0.48` | 已补 `## [1.0.0]` 汇总节 | ✅ |

> ⚠️ **刻意保持 `0.1.0` 不动的两处**（不是漏改）：
> - `backend/harness/kernel/loader.py` 的 `CORE_API_VERSION = "0.1.0"` —— **内核 API 版本**，只表示 `contracts/` 契约代次，与产品版本无关；
> - 41 个插件清单（`backend/plugins` 33 + `backend/marketplace` 6 + `frontend/src/plugins` 2）的 `"version": "0.1.0"` 与 `core_api: ">=0.1.0 <1.0.0"` —— **插件自身版本**。
>
> 改内核版本前务必同步全部 `core_api` 区间，否则所有插件会校验失败。详见 `01-architecture.md` §3.2 的版本号说明。

### 2.2 提交与工作区
- [ ] 当前工作区有大量未提交改动（审计修复、本文档等）——**先分主题提交**，不要一个大杂烩 commit。
- [ ] CI 全绿（`.github/workflows/ci.yml`）。
- [ ] 确认 `git status` 干净后再打 tag。

### 2.3 安全与敏感信息（发到公网前必查）

> ✅ **已于 2026-10-03 完成审计（结论：干净）**，命令与结果如下：

- [x] 确认 `backend/data/`（真实库、`permission.json`、`skill_state.json`、`auth_secret.txt`、`memory_state.json`）已在 `.gitignore` 中且从未被提交——`auth_secret.txt` 是签名密钥，**绝不能进仓库**。`.gitignore` 已含 `backend/data/`、`*.db`；`git log --all -- `backend/data/*`` 无任何记录。
- [x] 检查 git 历史中是否曾经提交过 `*.db`、API key、mcp.json 里的真实密钥。`git log --all --diff-filter=A -- '*.db'` 与 `-- '*auth_secret*'` 均为空；`git log --all -p | Select-String "sk-[A-Za-z0-9]{20,}"` 无命中。
- [x] **`mcp.example.json` 专项核查（本次补充）**：该文件只在提交 `33af6a8` 引入过一次，其**引入时即为公开 libgen 配置**（真实私有 MCP 配置从未落库）；`backend/mcp.json`／`mcp.json` 在全部历史中**零提交**（已在 `.gitignore` 第 82-83 行忽略）。
- [x] README 里的示例配置不含真实 key。

> 待提交杂物清单（按约定**排除**，不入库）：`smoke.bat`、`backend/scripts/`（smoke / e2e 脚本）、`backend/mcp.smoke.json`、`frontend/.dev_cv3.txt`、`frontend/.dev_pt.txt`；`.trae/`、`.workbuddy/`、`node_modules/`、`__pycache__/` 等已由 `.gitignore` 覆盖。

### 2.4 从 v0.0.9 升级的兼容性说明（老用户关心的）
自 v0.0.9 以来变化很大，Release Notes 必须包含"升级指引"：
- 数据库：新表全部 `CREATE TABLE IF NOT EXISTS` 自动升级，**老库直接启动即可**；仍建议升级前备份 `backend/data/harness.db`。
- 新增文件：`backend/data/auth_secret.txt`（认证密钥，首次启用认证自动生成）、`settings.json`（前端偏好持久化）、`mcp.json`（MCP 服务器配置）。
- 新增环境变量：`HARNESS_AUTH`、`HARNESS_AUTH_ADMIN_PASSWORD`、`HARNESS_SCHEDULER`、`HARNESS_MEMORY_SUMMARY` 等——列一张表。
- 行为变化：启用认证后 REST 全部要求 Bearer（图片/WS 已适配）；默认不启用、行为零变化，要写明。

### 2.5 文档完备性
- [ ] README：安装步骤、启动命令、截图、功能列表与实际一致（本次已订正失实描述）。
- [ ] 新增《用户使用手册》（本次已产出：`docs/andy-harness-v1.0.0用户使用手册.md`）。
- [ ] CHANGELOG 新增 `1.0.0` 汇总节（本次已产出）。
- [ ] LICENSE 存在（已有）。

## 三、Git 操作步骤（建议顺序）

```bash
# 1. 分主题提交当前改动
git add backend/... && git commit -m "fix(security): 附件/工件/轨迹归属校验 + 认证密钥持久化 (audit P0-1/2, P2-2)"
git add backend/harness/api/ws/chat.py ... && git commit -m "fix(ws): 单 reader 架构消除控制帧竞争 (audit P0-4)"
git add frontend/... && git commit -m "fix(ui): 认证模式图片 fetch+blob 渲染; 设置持久化接通后端 (audit P0-3, D-1)"
git add docs/ CHANGELOG.md README.md && git commit -m "docs: v1.0.0 用户手册 / 面试解说 / 发布清单 / CHANGELOG"

# 2. 统一版本号后提交
git commit -m "release: bump version to 1.0.0"

# 3. 打 tag（ annotated tag，含说明）
git tag -a v1.0.0 -m "andy-harness v1.0.0 developer preview"

# 4. 推送
git push origin main --follow-tags
```

> 注意：tag 一定要打在**版本号统一之后的 commit** 上；用 annotated tag（`-a`）而不是轻量 tag，GitHub Release 关联它更规范。

## 四、GitHub Release 发布

1. **用 Release 页面而非仅 push tag**：`Releases → Draft a new release → 选择 v1.0.0 tag`。
2. **Release Notes 结构建议**：
   ```markdown
   # andy-harness v1.0.0 (Developer Preview)
   ## Highlights（5 条以内：插件化 / MCP / 记忆与制品 / 定时任务 / 多用户认证）
   ## What's Changed since v0.0.9
   - 汇总 0.0.10 ~ 0.0.48 的 CHANGELOG（按 特性/修复/安全 分组，不要 49 条平铺）
   ## Security Fixes
   - 附件/工件/轨迹跨用户归属校验、权限 fail-open 日志、签名密钥持久化
   ## Upgrade Notes（v0.0.9 用户）
   - 数据库自动升级，先备份；新增环境变量表；默认行为零变化
   ## Known Limitations（预览版声明）
   - 渠道 / Computer Use / Jev / 断点续跑 / 用户管理暂无界面入口（仅 REST）
   ```
3. **附件（可选）**：可以附上 `Source code (zip/tar.gz)`（GitHub 自动生成），如提供桌面安装包（Tauri 产物）请附 SHA256。
4. **讨论区**：Release 可关联 Discussions，方便预览版反馈。

## 五、发布后的运营建议

- [ ] **分支保护**：main 开启 required reviews / CI 必过再合并。
- [ ] **Issue 模板**：Bug report（附版本号 + 启动命令）+ Feature request。
- [ ] **下一个版本的语义约定**：预览期的破坏性变更发 `1.0.0-rc.x` 或直接升 `1.1.0` 并注明，保持 Semver 纪律。
- [ ] **0.0.x 历史 tag**：无需处理，Release 页只展示 v1.0.0 即可；可在 v1.0.0 Notes 里链接 CHANGELOG 完整历史。

## 六、本次（发布前）已为 1.0.0 准备好的内容

- ✅ 审计修复全部落地并通过测试（安全 4 项、并发 1 项、前端 2 项、文档订正）。
- ✅ `docs/andy-harness-v1.0.0用户使用手册.md`（16 张系统截图）。
- ✅ `docs/面试解说.md`（面试话术底稿）。
- ✅ CHANGELOG 新增 1.0.0 汇总节。
- ✅ README 失实描述订正 + 无界面功能标注。
- ✅ 三个版本号字段已统一为 1.0.0（见 2.1，已改动并随本次发布提交；另：`CORE_API_VERSION` 与 41 个 `plugin.json` 的 `version` 按约定保持 `0.1.0` 不动）。
- ✅ 敏感信息历史审计已完成且结论干净（见 2.3），`mcp.example.json` 专项核查通过。

## 七、1.0.0 发布后补充修订（2026-10-01 安全与工程收口）

在 1.0.0 发布后落地的 hardening（单用户默认部署行为不变，详见 CHANGELOG 对应条目）：

- **状态变更端点管理员校验**：插件 / 技能 / MCP 的安装卸载、权限模式与系统设置写入统一接入 `require_admin`，仅 `HARNESS_AUTH=1` 时校验管理员身份。
- **后端仅本机监听**：`Makefile` 与 `start-all.bat` 由 `0.0.0.0` 改为 `127.0.0.1`；`docker-compose.yml` 端口绑定 `127.0.0.1`、去除过时 `version` 字段。
- **设置落盘路径修正**：`backend/harness/data/settings.json` → `backend/data/settings.json`，已迁移既有配置并清理错误目录。
- **Docker 镜像补齐**：`Dockerfile` 现 COPY `marketplace/` `skills/` `mcp_marketplace/`，容器内市场不再空白。
- **CI 全绿**：后端 ruff / ruff-format / mypy / pytest 通过；前端 eslint / vitest(43) / vue-tsc / vite build 通过，ci.yml 前端 job 新增 `pnpm test`。
- **说明文档同步**：README（模型名、Provider Key 读取方式、项目结构、文档索引、Docker 限制说明）、用户手册第 12 节（环境变量全表 + 数据文件全表）、`01-architecture.md`（版本号语义、测试规模）、`02-development-plan.md`（测试数）、`frontend/README.md`（由 Vite 模板改为正式前端开发说明）已全部对齐当前代码。

---

## 八、发布前门禁实测（2026-10-01，可复现）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 后端 Lint | `cd backend && uv run ruff check .` | ✅ All checks passed |
| 后端格式 | `cd backend && uv run ruff format --check .` | ✅ 260 files already formatted |
| 后端类型 | `cd backend && uv run mypy harness` | ✅ no issues found in 104 source files |
| 后端测试 | `cd backend && uv run pytest -q` | ✅ 593 passed, 2 skipped（88.9s） |
| 前端 Lint | `cd frontend && pnpm lint` | ✅ 无 error |
| 前端测试 | `cd frontend && pnpm test` | ✅ 9 files / 43 tests passed |
| 前端构建 | `cd frontend && pnpm build` | ✅ 通过（主 chunk 1.30 MB / gzip 441 KB，有 >500 kB 提示） |
| 工作区 | `git status` | ✅ 已按主题提交（除外测试脚手架与临时产物，见 §2.3），tag 打在版本号统一后的 commit 上 |

## 九、Release Notes 必须披露的已知问题（预览版）

这些不是"待修复的 bug 清单"，而是**已知的、有意保留的限制**，写进 Known Issues 比不提更专业：

1. **5 个能力无界面入口**：渠道接入、Computer Use、Jev 看板、断点续跑、用户管理，后端 REST 完整可用，UI 未接线。
2. **Docker 前端未配反向代理**：`frontend/Dockerfile` 仍用 `serve -s dist` 托管静态文件，没有 `/api` 与 `/ws` 代理，容器模式下页面能打开但连不上后端。请使用 `make install` + `start-all.bat` / `make backend`。
3. **桌面壳数据目录只覆盖两项**：`harness.db` 与 `attachments/` 会落到每用户目录；`auth_secret.txt`、`permission.json`、`skill_state.json`、`settings.json`、制品目录仍在仓库 `backend/` 下。装机到只读目录时需留意。
4. **前端包体未分包**：主 chunk 约 1.3 MB（gzip 约 440 KB），构建时有 chunk 体积告警。
5. **平台与模型覆盖**：仅在 Windows 10 验证过（CI 会在 ubuntu 上跑后端与前端测试，但未做真机联调）；仅 DeepSeek `deepseek-v4-flash` 做过深度联调，Qwen / Doubao 未充分验证。
6. **`/api/models` 只按启用状态过滤**：未配置 API Key 的 Provider 其模型仍会出现在模型选择器中，选中后调用才失败（已知的交互缺陷，1.0.x 修复）。
7. **登录接口无速率限制**：`POST /api/auth/login` 每次尝试执行 20 万次 PBKDF2，未开启认证时不影响默认部署；开启认证并对公网暴露时需自行加反向限流。
8. **pre-commit 配置滞后**（ruff v0.6.0 / eslint v9 / prettier v4）：与项目实际使用的工具版本不一致，本地钩子结果可能与 CI 不同，**以 CI 为准**。
