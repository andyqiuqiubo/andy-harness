# mcp_servers — 本地 vendored MCP 服务

本目录存放**以源码方式集成进 andy-harness 的本地 MCP server**（stdio 传输，由后端
MCP 客户端插件按 `mcp.json` 配置拉起子进程）。与「MCP 市场」里一键接入的远程公开
服务（libgen / deepwiki / context7 等）不同，这里的服务需要一次性的本地构建，但
换来的是**离线可用、版本锁定、可审计**。

```
mcp_servers/
├── rea/                          # REA（Reverse Engineer Anything）v6.4.0 完整源码快照
│   ├── VENDOR.md                 #   上游锁定记录（commit / 版本 / 许可证 / 原生构件来源）
│   ├── package.json              #   依赖声明（npm ci 的依据）
│   ├── package-lock.json         #   锁 lockfile（可复现安装）
│   ├── native/windows/build/     #   官方发布的 Windows 原生构件（rea-windows-x64.node）
│   └── scripts/rea.mjs           #   CLI + MCP server 统一入口
└── samples/electron-demo-app/    # 验证样本：迷你 Electron 笔记应用（供 REA 分析）
```

---

## REA — 逆向工程 MCP 服务

REA 让 AI 助手在**不看源码**的前提下分析二进制与应用程序：反编译/反汇编需
Ghidra / IDA / Hopper（任选，另行安装），而**内置静态工具无需任何反编译器**即可
工作——Windows PE 资源树、JavaScript/Electron 应用模块图、文件清单与摘要等。分析
全程在本机执行，不上传目标文件；每条结论附带证据（文件 / 偏移 / 哈希）与局限说明。

### 1. 环境要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Node.js | **>= 22.19**（或 >= 24.11，或 26+） | 本机 `node -v` 为 22.22.2，满足；推荐 24 LTS |
| npm | 随 Node 附带 | 用于安装依赖与构建 |
| 反编译器 | 可选 | 仅深度原生二进制分析（伪代码/反汇编）才需要 Ghidra / IDA / Hopper |
| 磁盘 | ~1 GB | `node_modules` + `dist`，建议与项目同盘 |

> REA 自身 `engines` 与 `.npmrc` 的 `engine-strict=true` 会强制校验 Node 版本，
> 低于 22.19 会直接安装失败——这是有意为之，请升级 Node 而非跳过校验。

### 2. 构建（干净环境下执行一次）

```bash
cd mcp_servers/rea
npm ci                     # 按 package-lock.json 精确安装 375 个包（本机实测约 6 分钟）
npm run compile            # tsc 编译 → dist/（6.4.0 的运行时即 tsc 产物，无打包步骤）
npm run artifacts:skills   # 生成 skills/ 技能元数据
```

- 6.4.0 的 MCP 入口为 **`dist/main.js`**（`scripts/rea.mjs` 内 `mcp` 模式直接导入），
  CLI 入口为 `dist/cli.js`；无需 `bundle-runtime`（该脚本在 6.4.0 尚不存在）。
- 上游 `rea.mjs` 的报错信息会提示 `npm run build:cached`（turbo 路径）；本仓库
  验证过的路径即上面三条命令，二者产物一致。
- 构建产物（`dist/`、`node_modules/`、`skills/`、`.turbo/`、`.cache/`）已在仓库根
  `.gitignore` 忽略，不随仓库分发。

#### 2.1 Windows 原生构件（仓库内已包含）

`native/windows/build/rea-windows-x64.node`（848 KB）与 `manifest.json` 是 REA
Windows 下的**原生权限构件**（NTFS 对象身份校验、私有 DACL、作业对象进程控制，
JS/Electron 静态分析与进程抓取依赖它）。GitHub 源码快照不含该产物（上游由
Linux + MinGW 交叉编译在发布时生成，本机无该工具链），因此本仓库从**官方 npm
发布包**（`rea-agents@6.4.0`，与源码快照同一 commit `a56f7bb`）提取：

- 来源：`https://registry.npmjs.org/rea-agents/-/rea-agents-6.4.0.tgz` → `package/native/windows/build/`
- 完整性：`rea-windows-x64.node` SHA-256 =
  `c4fd9f377e567bcf15ba3fec3e0b0ed965d82b765a2adcdac49e46f14524b6d5`，与 `manifest.json`
  声明一致（REA 加载器每次启动都会复核该哈希与 PE 头）。

### 3. 启动入口与参数

MCP server 的启动入口就是 REA 的统一 CLI：

```bash
node mcp_servers/rea/scripts/rea.mjs mcp
```

- `mcp` 子命令：以 **stdio** 传输提供 MCP 服务（换行分隔的 JSON-RPC 2.0），
  与 andy-harness 后端 MCP 客户端的 stdio 传输对应；
- 不带参数或带其它子命令时是普通 CLI（如
  `node scripts/rea.mjs analyze-javascript-application <目录> --json`）。

### 4. 注册到 andy-harness（mcp.json）

后端 MCP 客户端按以下优先级读取配置：`MCP_CONFIG_PATH`（`;` 分隔多个）>
`backend/mcp.json` > `~/.andy-harness/mcp.json`。stdio 条目字段：
`command` / `args` / `env` / `description` / `disabled`，外加本项目扩展的两个
per-server 选项（见 4.1）。

**本机运行时配置**（`backend/mcp.json`，已注册、默认启用，绝对路径避免 cwd 歧义）：

```json
"rea": {
  "type": "stdio",
  "command": "node",
  "args": ["<仓库绝对路径>/src/mcp_servers/rea/scripts/rea.mjs", "mcp"],
  "env": { "REA_LOG_LEVEL": "error" },
  "max_line_bytes": 16777216,
  "tool_allowlist": ["analyze_javascript_application", "inspect_pe_resources", "..."],
  "description": "REA 逆向工程 MCP 服务 …",
  "disabled": false
}
```

**示例配置**（仓库根 `mcp.example.json`，默认 `disabled: true`，供干净克隆参考）：
`args` 写 `["../mcp_servers/rea/scripts/rea.mjs", "mcp"]`——后端两种启动方式
（`start-all.bat` 浏览器模式、Tauri 桌面壳）的 cwd 均为 `backend/`，故相对路径
`../mcp_servers/...` 可解析；从仓库根直接跑 uvicorn 的场景请改用绝对路径。

保存后**重启后端**（插件在 activate 时连接所有 enabled 的 server），或在
「设置 → MCP」标签页查看连接状态与工具清单。

#### 4.1 本项目为大型 MCP server 扩展的三个选项

REA 的 `tools/list` 广播 **170 个工具、约 2 MB**，且深度分析（原生反编译）冷启动
耗时较长，直接接入会撞上三个限制，故 MCP 客户端
`harness/modules/mcp_client/service.py` 增加了三个**默认关闭、不影响存量 server**
的 per-server 选项：

| 选项 | 作用 | REA 的取值与原因 |
| --- | --- | --- |
| `max_line_bytes` | stdio 单行读取上限（64 KB ~ 64 MB）。默认 1 MB（SEC-15 内存护栏），大响应 server 调大 | `16777216`（16 MB）：REA 的 tools/list 约 1.95 MB，默认上限会触发"超长单行丢弃 → 握手假死" |
| `tool_allowlist` | 工具白名单（空 = 不过滤）。只把名单内工具注册给 AI | 收敛为 **23 个**工具（7 个静态分析 + 16 个 Ghidra 原生反编译族）：170 个工具全量注入 ≈ 数十万 token，会撑爆模型上下文 |
| `request_timeout` | 单次 JSON-RPC 请求等待上限（秒，5 ~ 3600，默认 60） | `600`：Ghidra 冷启动（导入 + 自动分析 + 桥接就绪）提供方截止即 **330s**，默认 60s 会误杀 |

`REA_LOG_LEVEL=error`：REA 默认 info 级 stderr 日志较吵，压到 error 保持后端日志干净。

> **上下文成本实测**：23 个工具的完整定义（名称 + 描述 + 参数 schema）合计约
> **269 KB**（按 JSON 折算约 6~7 万 token）——这是每次模型调用的固定成本。
> 若上下文吃紧，可从 `tool_allowlist` 中裁掉低频工具（例如
> `compare_managed_members` 21 KB、`inspect_evm_interface` 9 KB、
> `analyze_function` 22 KB 三项即省约 52 KB），或设
> `REA_MCP_INPUT_SCHEMA_PROFILE=compact`（实测目录整体再省约 9%）。

### 5. 环境变量（按需）

| 变量 | 用途 |
| --- | --- |
| `REA_ANALYSIS_PROVIDER` | 选择深度分析引擎：`ghidra` / `ida` / `hopper` / `objdump` / `rizin`（本项目已设 `ghidra`） |
| `GHIDRA_INSTALL_DIR` | Ghidra 安装目录（本项目指向 `.tools/ghidra_12.1.4_PUBLIC`；REA 要求 12.1.x） |
| `JAVA_HOME` | Ghidra 使用的 JDK（本项目指向 `.tools/jdk-21`，Temurin 21；**不改动系统已有的 JDK 8**） |
| `GHIDRA_HEADLESS_MAXMEM` | Ghidra headless JVM 堆上限（Ghidra 默认 2G；本项目设 `3G` 限定内存占用） |
| `REA_GHIDRA_STARTUP_TIMEOUT_MS` | Ghidra 启动截止（默认 330000ms；慢机器/大目标可调大） |
| `REA_HOPPER_STARTUP_TIMEOUT_MS` | Hopper 启动超时 |
| `REA_IDA_MCP_CONFIG` | IDA 原生 MCP bridge 配置 |
| `REA_MCP_MAX_RESPONSE_BYTES` | 单次工具响应上限（≥10 MB，默认取接收缓冲上限；分析大应用被截断时调大） |
| `REA_MCP_INPUT_SCHEMA_PROFILE` / `REA_MCP_OUTPUT_SCHEMA_PROFILE` | 工具 schema 档位（`compact` 可省部分 token；实测 REA 6.4.0 目录 1.95 MB → 1.77 MB，收益有限） |
| `REA_LOG_LEVEL` | 日志级别：`trace`/`debug`/`info`（默认）/`warn`/`error`/`fatal`/`silent` |
| `REA_JEB_MCP_URL` / `REA_JADX_MCP_JAR` / `REA_ADB_PATH` / `REA_APKTOOL_COMMAND` / `REA_WABT_BIN_DIRECTORY` / `REA_PWNTOOLS_PYTHON` / `REA_MITMDUMP_COMMAND` / `REA_TSHARK_COMMAND` / `REA_BROWSER_EXECUTABLE` / `REA_WAKARU_COMMAND` / `REA_ILSPY_CMD_PATH` | 各专项分析的外部工具路径（均"调用方自备"，REA 不安装） |

在 `mcp.json` 的 `env` 字段里配置即可随 server 启动注入。

### 6. 工具清单（MCP 工具名前缀 `mcp__rea__`）

默认白名单共 **23 个**，分两组：

**A. 引擎无关的静态分析（7 个，无需反编译器）**：

| 工具 | 能力 |
| --- | --- |
| `analyze_javascript_application` | 静态重建本地 JS/Electron/ASAR 应用：模块、导入、source map、路由、IPC 与 native add-on 关系，附证据 |
| `inspect_pe_resources` | 解析 PE32/PE32+ 完整资源树（type/name/language、RVA、SHA-256、图标候选），不执行、不开反汇编器 |
| `inspect_artifact` | 文件清单、摘要、plist、Apple bundle 解剖等 |
| `inspect_managed_artifact` | .NET 程序集元数据 / CIL / 本地依赖（纯静态） |
| `inspect_evm_interface` | EVM 字节码 dispatch selectors 等 |
| `inspect_analysis_view` | 查看已保留分析证据的选定视图（模块 / 页面 / 节 / 符号等，分析后的追问入口） |
| `compare_managed_members` | 按声明类型/名称/签名对比两个 .NET 程序集成员清单（纯静态） |

**B. Ghidra 原生反编译族（16 个，依赖 6.5 节的项目内 Ghidra）**：

| 工具 | 能力 |
| --- | --- |
| `open_binary` | 入口：导入目标二进制并建立分析会话（自动分析 + 桥接启动，冷启动本机实测约 1~2 分钟） |
| `inspect_native_load_image` | 载入镜像总览（节区 / 链接地址 / 符号等） |
| `list_procedures` / `search_procedures` | 函数清单 / 按名检索（正则或字面量） |
| `list_strings` / `search_strings` | 字符串清单 / 检索 |
| `procedure_pseudo_code` | **反编译伪代码（核心能力）** |
| `procedure_assembly` | 指定函数的完整汇编清单 |
| `read_function_instructions` | 指令级读取（含操作数） |
| `analyze_function` | 单函数综合画像（分类、引用、调用等） |
| `procedure_callees` / `procedure_callers` | 被调函数 / 调用方（调用图双向导航） |
| `xrefs` | 指定地址的交叉引用 |
| `resolve_native_call_targets` | 解析（间接）调用目标 |
| `inspect_native_instruction` | 精确指令语义（对象级证据） |
| `inspect_native_data_type` | 精确数据类型布局（对象级证据） |

完整 170 个工具以 `tools/list` 为准（REA 侧文档：`rea/docs/mcp-contracts.md`）；其余未启用的代表：
`inspect_binary_layout`（ELF，需 pwntools）、`inspect_wasm_artifact`（需 WABT）、
`capture_browser_scenario` / `inspect_web_page`（需 Chrome 系浏览器）、
`set_address_name` / `annotate_native_function`（会话内注解，属写操作，默认不开）等——如需启用，
加进 `tool_allowlist` 并配好对应环境变量即可。

### 6.5 原生反编译（Ghidra 12.1.4，项目内隔离安装）

**已启用**：对原生 PE（x86 / x64 的 exe / dll）可输出**反编译伪代码**、汇编、
函数清单、字符串、调用关系与交叉引用——超越表层解析，直达代码语义。

**隔离安装位置**（工作区级 `.tools/` 目录，**不依赖系统全局安装**；项目专用
JDK 也不影响系统里已有的 JDK 8）：

| 组件 | 路径 | 来源与完整性 |
| --- | --- | --- |
| Ghidra 12.1.4 | `<工作区>/.tools/ghidra_12.1.4_PUBLIC/` | 官方发布 `ghidra_12.1.4_PUBLIC_20260921.zip`（569,649,598 字节），SHA-256 `ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db` **已校验一致**；GitHub 直连不可达，经 SourceForge 精确镜像三路并行下载（脚本 `.tools/dl_ghidra_parts.sh`，约 12 分钟） |
| Temurin JDK 21.0.12.1 | `<工作区>/.tools/jdk-21/` | 清华 TUNA 镜像（Adoptium）；`unzip -t` 通过 + `java -version` 实测可用 |
| 原始压缩包 | `.tools/downloads/` | 保留以便复核（确认无误后可自行删除，释放约 775 MB） |

**接入方式**（`backend/mcp.json` 的 `rea` 条目 `env`，已配置并实测）：

```json
"env": {
  "REA_ANALYSIS_PROVIDER": "ghidra",
  "GHIDRA_INSTALL_DIR": "H:\\jsd-andyharness\\.tools\\ghidra_12.1.4_PUBLIC",
  "JAVA_HOME": "H:\\jsd-andyharness\\.tools\\jdk-21",
  "GHIDRA_HEADLESS_MAXMEM": "3G",
  "REA_LOG_LEVEL": "error"
}
```

**使用流程**（AI 侧自动完成）：`open_binary`（导入 + 自动分析 + 桥接启动）→
`list_procedures` / `search_procedures` / `list_strings` / `search_strings`（锁定目标）
→ `procedure_pseudo_code` / `procedure_assembly` / `read_function_instructions`
（伪代码与汇编）→ `analyze_function` / `procedure_callees` / `procedure_callers` / `xrefs`
（函数画像与调用图）→ `resolve_native_call_targets` / `inspect_native_instruction` /
`inspect_native_data_type`（精确对象级证据）。

**资源与上下文约束（按约束设计）**：

| 约束 | 措施 | 取值 |
| --- | --- | --- |
| 内存 | Ghidra headless JVM 堆上限（Ghidra 默认 2G） | `GHIDRA_HEADLESS_MAXMEM=3G`（16 GB 机器留有余量；大目标可再调，或降回 2G） |
| 请求时长 | per-server `request_timeout`（默认 60s；Ghidra 冷启动提供方截止 330s） | `600s` |
| 单帧大小 | `max_line_bytes`（防 2 MB 工具目录与大型证据帧被护栏丢弃） | `16MB` |
| 上下文长度 | `tool_allowlist` 收敛（23 个工具定义合计约 269 KB）；大结果配合 `detail:"summary"` + `inspect_analysis_view` 按需回看 | 见 4.1 |
| 磁盘 | 全部组件限定在项目工作区 `.tools/`（约 1.9 GB，含压缩包） | 不写 C 盘、不动系统目录 |

**更新 Ghidra**：下载新版发布 zip → 校验官方 SHA-256 → 解压到 `.tools/ghidra_<新版本>/`
→ 更新 `backend/mcp.json` 的 `GHIDRA_INSTALL_DIR`（REA 要求 12.1.x 系列；升级前
以 `node scripts/rea.mjs doctor --json` 确认新版本被识别）。

### 7. 验证记录（端到端，2026-10-14 实测）

项目自带回归测试 `backend/tests/test_mcp_rea_integration.py`（5 例）。**真实后端
运行方式（非沙箱）下逐项实测**：

```
test_rea_tools_list PASSED                      # 握手 + 170 工具目录（1.95 MB 单帧）
test_rea_tool_allowlist PASSED                  # 白名单只放行名单内工具
test_rea_analyze_javascript_application PASSED  # 样本应用模块图 + IPC 通道均命中
test_rea_inspect_pe_resources PASSED            # 真实 PE 二进制资源树 + SHA-256 证据
test_rea_native_decompilation PASSED（112.8s）  # 原生反编译：open_binary → list_procedures
                                                #   → procedure_pseudo_code(napi_register_module_v1)
                                                #   返回含 C 结构特征的伪代码证据
```

```bash
cd backend && uv run pytest tests/test_mcp_rea_integration.py -v -s
```

> **受限沙箱环境的已知现象**：智能体沙箱会拦截 REA 原生权限自检所需的句柄删除
> （`SetFileInformationByHandle(FileDispositionInfo)`，Win32 5），因此
> `test_rea_analyze_javascript_application` 与 `test_rea_native_decompilation`
> 两个依赖原生控件的用例在沙箱内以明确原因跳过；其余三项不受影响。该现象与
> REA 及本集成无关（非沙箱环境逐项实测通过），真实后端（`start-all.bat` /
> 桌面壳 / 手动 uvicorn）均不受影响。

### 8. 更新到上游新版本

```bash
cd mcp_servers/rea
git fetch origin && git checkout <新 tag 或 commit>     # 首次需先 git init + remote add
npm ci && npm run compile && npm run artifacts:skills
```

注意同步四件事：① 从同版本官方 npm 包重新提取 `native/windows/build/` 原生构件
（见 2.1，版本必须与 `package.json` 一致，否则加载器会拒绝）；② 更新
`rea/VENDOR.md` 的版本/提交记录；③ 复跑第 7 节验证并核对 `tools/list` 工具名
（白名单里的工具名如有变化需同步调整）；④ **Ghidra 引擎独立维护**——REA 要求
12.1.x 系列，升级 Ghidra 时按 6.5 节末尾步骤操作（下载 → 校验官方 SHA-256 →
解压到 `.tools/` 新目录 → 更新 `GHIDRA_INSTALL_DIR` → `rea doctor` 复核）。
