# BrowserSkill vendored skill — VENDOR.md

本目录 `backend/skills/browser-skill/` 是腾讯开源项目 **BrowserSkill** 官方
Agent Skill bundle 的源码方式集成，遵循项目「vendored 本地资源」约定（见根
`AGENTS.md` 与 `mcp_servers/README.md`）：离线可用、版本锁定、可审计、可复现拉取。

## 上游信息

| 项 | 值 |
|----|----|
| 上游仓库 | https://github.com/Tencent/BrowserSkill |
| 集成 tag | `cli-v0.3.3` |
| tag commit | `d9a78aed47874459ae1e44523a597e42dd9acf99` |
| release 发布 | 2026-10-15T02:33:41Z |
| 许可证 | MIT |
| 对应 CLI | bsk v0.3.3（windows-x64 SHA-256 `5ed6aa00e60c0538a637febac6bfbc6a0bd55a11083cd90a987968cff4bc0e02`） |
| 扩展协议版本 | 1.3（要求 CLI / 扩展 / skill 三者版本一致） |
| 上游 skill 源路径 | `crates/bsk-cli/skill/`（`bsk install-skill` 安装即此 bundle） |

## 组件三件套与官方扩展下载 URL

BrowserSkill 需要 **Agent + bsk CLI + 浏览器扩展** 三件套。CLI / 扩展 / 本 skill
共享同一发布版本号，**必须匹配**。扩展官方商店：

- Chrome Web Store：https://chromewebstore.google.com/detail/hhcmgoofomhgciiibhipgmgkgnoenaoi
- Microsoft Edge Add-ons：https://microsoftedge.microsoft.com/addons/detail/browserskill/emacgiaaaiojkkpkddmmdfhmokgmnikg

CLI 安装：
- Windows PowerShell：`irm https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1 | iex`
- macOS / Linux：`curl -fsSL https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.sh | sh`
- 项目发行页：https://github.com/Tencent/BrowserSkill/releases/tag/cli-v0.3.3
- Agent 安装指南：https://github.com/Tencent/BrowserSkill/blob/main/AGENT_INSTALL.md

> 依据官方约定，**skill 安装不包含 CLI 与扩展**（二者由用户/Agent 单独安装）。
> 本目录仅收录 skill 文本 bundle；17MB 的 `bsk.exe` 二进制不入库，
> 校验下载产物一致后已从工作区清理。

## 拉取方式（可复现）

v0.3.3 逐个文件从 raw.githubusercontent 按 commit tag 下载：

```
BASE=https://raw.githubusercontent.com/Tencent/BrowserSkill/cli-v0.3.3/crates/bsk-cli/skill
# SKILL.md + references/{debugging,environment,extraction,files,help-and-recovery,
# interaction-details,screenshots-and-canvas,tabs-and-profiles,video}.md
```

## 本地文件清单与 SHA-256（入库基准）

```
6a2bc09e9beb104ecf5988b75383411100191bf137ae836c80c0d3e4d6d5e416  SKILL.md
a73b2ccbc8e5042e8be1312b09a88d3e47f0923eb1d1a96ee4ee9e67a911e994  references/debugging.md
3dd6dce9fd7b4798d0b024cc19dc49b36670c8ac79a1f6c0e0f5e8966b7eb130  references/environment.md
dd45378a53fbfe8400d966572660ab8d14960bd46976cc03c226d2c0810683da  references/extraction.md
adc7e65fb1c8ee9415d091d48db20e68a2ab4836f45c6565b0e9755608195a14  references/files.md
6d1f4037b99a68e29a495fa47e718935b1b422e280ecede5e70e85d68f210fff  references/help-and-recovery.md
acdc880fa175e5307b8c43be9446389cd6588ad12856c5326a82c0fc1a6c9d97  references/interaction-details.md
d4dafd443606d55d55150a8cd7e2fd381ed8dcc7baa7ef1594b93a1cc9808e47  references/screenshots-and-canvas.md
bad13d9c4956d55576cf0e35e839007050536834b5925272450ba2c44b7d8cd6  references/tabs-and-profiles.md
133fd011edb741544022bda38b458df1d41aa69ce8df67fab5b0db569ab038a3  references/video.md
```

> `VENDOR.md` 本身不在上游 bundle 内，为本地新增（来源记录文件）。

## 与上游的差异

1. **SKILL.md frontmatter**：上游 `description` 为多行块标量；集成时补充了
   `version: 0.3.3`、`license: MIT`，并在 `description` 追加中文触发语（覆盖
   中英「网页浏览 / browser use / 自动化操作」等意图），供本项目 skill 目录命中。
2. **SKILL.md 正文头部**：新增一段「andy-harness 集成补充」，写明三件套版本
   匹配要求、`bsk --version` / `bsk doctor` 前置检查，以及 Chrome / Edge 官方
   扩展下载 URL；其中「宿主自动启动」一节规定：daemon 未运行时由 Agent 用受管后台任务
   （PowerShell `run_in_background` + `Start-Process` + `Wait-Process` 保活）自行拉起
   `bsk daemon start --foreground`，**禁止把该步骤推给用户开终端**；仅扩展本体安装、
   chrome:// 页面操作与借标签页确认留给用户（2026-10-18 实测：受管后台任务启动 daemon
   → `session start` → `navigate` → `observe` 全链路零用户操作可通）。上游正文其余内容
   （工作流、护栏、命令表、references 索引）**原样保留**。
3. **会话窗口默认置顶可见（产品级行为变更）**：上游 Task workflow 第 1 步原文为
   "Sessions start in the background; use `--focus` only on explicit user request"，
   示例命令 `bsk session start --json`（后台）。本项目按用户明确要求改为默认置顶：
   示例命令改为 `bsk session start --focus --json`，并加中文说明"Agent Window 直接
   显示在屏幕最上层、全程可见，仅用户明确要求后台运行或批量并行任务时才 `--no-focus`"；
   `agent_loop._render_browser_hint`（两分支）同步注入"session start 必须带 --focus"。
   `--focus` 是 BrowserSkill 官方支持的置顶方式（2026-10-18 实测 session start --focus
   → navigate → stop 全链路正常），非焦点抢占滥用，用户可随时切走该窗口。
4. **references/** 全部 9 个文件与上游逐字节一致，未做任何修改。
5. **解析器配套改动**：`harness/modules/skill_manager/service.py` 的
   `_parse_simple_yaml` 增强为支持块标量（`key: |` / `key: >`），否则官方多行
   frontmatter 的 description 会被解析为空、整个 skill 被 `_load_skill` 跳过。
   该改动向后兼容既有简单 `key: value` skill。
6. **配套 daemon 托管插件（本地新增，非上游）**：`backend/plugins/browser_daemon/`
   提供 `browser_daemon_status` 工具，由后端长活进程以独立生命周期确定性
   探测/拉起/复核 `bsk daemon`，绕开对话宿主沙箱对常驻进程的回收限制
   （实测宿主 shell 起 daemon 报 `os error 5`）。SKILL.md「宿主自动启动」一节据此改写为
   「首选 browser_daemon_status 工具、次选受管后台任务、任何情况都不得让用户开终端」。
   注意：daemon 存活期随其后端父进程；智能体沙箱里派生的 daemon 会被回收属测试环境
   差异，真实部署（start-all.bat / Tauri 壳的 uvicorn 长活）下跨会话存活。`ensure` 幂等，
   每次浏览器任务前调用一次即可保证就绪。
7. **调用预算纪律（本项目补充章节）**：上游无此节。针对真实会话「搜索类任务走
   fill 路径 + 反复 tabs/snapshot 折腾导致 25 轮用满」的复盘（2026-10-18），新增
   「调用预算纪律」一节：搜索类任务用结果页 URL 直跳（B 站
   `https://search.bilibili.com/all?keyword=`、百度 `https://www.baidu.com/s?wd=`，
   均实测有效），navigate 后禁止 tabs/tab select 找标签，禁止无关 calculator，
   GBK 乱码时改用 observe 而非手写 subprocess 解码，达成即收口。
8. **沙箱 shell 引号修复（跨模块，非上游）**：`harness/modules/sandbox_manager/service.py`
   的 `_execute_shell` Windows 分支原用 `Popen([cmd, "/c", code])`，list2cmdline 会把
   整段代码当单参数二次转义，导致 `--value "dify"` 里的引号变成字面量（即搜索框出现
   `"dify"` 的根因）；改为传原始字符串 `f"{cmd} /c {code}"`，引号按 cmd/MSVCRT 规则正常
   剥除。`tests/test_sandbox.py::TestWindowsShellQuoting` 加 2 条回归（75 passed 验证
   不破坏既有行为）。

## 升级方式

1. 取上游最新 release：`https://api.github.com/repos/Tencent/BrowserSkill/releases/latest`
2. 对齐 `version.json` 中的 windows-x64 / darwin / linux 各平台 SHA-256；
3. 按上面「拉取方式」重下 `crates/bsk-cli/skill/` 覆盖本目录（保留 VENDOR.md）；
4. 重算本地 SHA-256 更新本文件的清单、commit 与 CLI/扩展版本；
5. 提醒用户同步更新 bsk CLI 与浏览器扩展到同一版本（协议版本需匹配）。
