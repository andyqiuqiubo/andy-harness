# andy-harness · 桌面壳（Tauri）

把插件化 Agent Harness 打包成**桌面应用**：一个窗口内同时运行前端与后端，
双击即用，不依赖浏览器。

## 目录结构

```
desktop/
├── package.json                # Tauri CLI 脚本（dev / build / icons）
├── src-tauri/
│   ├── Cargo.toml              # Rust 依赖（Tauri 2）
│   ├── tauri.conf.json         # 窗口、前端产物、打包配置
│   ├── capabilities/           # 权限声明（Tauri 2）
│   ├── icons/                  # 应用图标（PNG / ICO / ICNS）
│   └── src/
│       ├── main.rs             # 入口
│       └── lib.rs              # 后端进程生命周期 + get_backend_url 命令
└── launcher/
    └── launch-backend.py       # 后端启动器（解析 venv / 数据目录 / 进程树）
```

## 设计要点

- **职责很薄**：Tauri 侧只负责窗口、选端口、拉起与回收后端；对话逻辑全部
  在既有 `harness` 后端中，没有在 Rust 里重写。
- **回环监听**：后端默认只监听 `127.0.0.1`，不暴露到局域网；端口由壳
  动态分配（可用 `HARNESS_PORT` 固定），前端通过 `get_backend_url` 查询。
- **数据目录**：默认使用每用户目录
  - Windows：`%APPDATA%/andy-harness`
  - macOS：`~/Library/Application Support/andy-harness`
  - Linux：`~/.local/share/andy-harness`
- **进程树回收**：退出时 Windows 用 `taskkill /F /T`、其它平台杀进程组，
  确保不残留 uvicorn。

## 前置依赖

| 依赖 | 说明 |
|---|---|
| Rust 工具链 | 安装 [rustup](https://rustup.rs/)（稳定版即可，`rustc` ≥ 1.77.2） |
| **MSVC 工具集 + Windows SDK** | **必需**：Windows 上 Tauri 用 MSVC 的 `link.exe` 链接，光有 rustup 编不过。装 [VS Build Tools](https://aka.ms/vs/17/release/vs_buildtools.exe) 并执行 `vs_buildtools.exe --quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended`；可用 `--installPath` / `--cache` 与 `CachePath`、`SharedInstallationPath` 策略改到其它盘 |
| Tauri CLI | 已随本目录 `pnpm install` 安装 |
| 系统 WebView | Windows 10/11 一般自带 WebView2；缺失时打包安装包会引导下载 |
| 后端 venv | 先在 `backend/` 执行 `uv sync --extra dev --extra desktop` |
| 前端依赖 | 先在 `frontend/` 执行 `pnpm install` |

## 开发运行

```bash
cd desktop
pnpm install          # 安装 Tauri CLI
pnpm dev              # 启动 Vite 前端 + 编译 Rust + 拉起后端
```

`tauri dev` 会自动执行 `beforeDevCommand`（`pnpm --dir ../frontend dev`）。
开发模式下后端使用 `backend/.venv` 中的 Python；也可用 `HARNESS_PYTHON`
指定其它解释器，`HARNESS_PORT` 固定端口。

## 打包

```bash
cd desktop
pnpm build
```

产物在 `src-tauri/target/release/bundle/` 下：Windows 为 MSI/NSIS 安装包，
macOS 为 `.app` / `.dmg`，Linux 为 AppImage / deb。

> 说明：生产包默认从 PATH 解析 `python`（或 `HARNESS_PYTHON`）。若要把
> Python 运行时与后端完全随包分发（无外部依赖），使用
> [Tauri sidecar](https://v2.tauri.app/develop/sidecar/)：将
> `python -m PyInstaller` 产物放入 `binaries` 并在 `tauri.conf.json` 中
> 声明，把 `lib.rs` 中的命令换成 sidecar 路径即可。

## 更新代码后如何保证跑的是最新构建

**不需要任何手动重编动作**，两条机制已经保证：

1. **Rust 侧**：`pnpm dev` = `tauri dev`，每次启动都会先执行 `cargo build`；
   cargo 用**内容指纹**（而非文件时间戳）决定是否重编，因此
   `src-tauri/src/*.rs` / `Cargo.toml` / `Cargo.lock` / `tauri.conf.json` /
   `capabilities/` 内容变了立刻重编，没变则秒级 no-op。`tauri dev` 还会
   watch `src-tauri`，运行中改 Rust 代码会重编并重启应用。
2. **前端侧**：dev 模式窗口加载的是 Vite 的 `devUrl`（`http://localhost:5173`），
   前端代码**不在 exe 里**，改前端由 HMR 即时生效；只有 `pnpm build` 打包时
   才会通过 `beforeBuildCommand` 把 `frontend/dist` 烘进安装包。

为了让你**能自己核对**，仓库根目录的 `start-desktop.bat` 会打印：

- `shell: FRESH|STALE|MISSING ~ <时间>` —— exe 与全部受跟踪源文件的新旧关系，
  并点名最新的那个源文件；
- `What is running` 区块 —— exe 路径、构建时间、git HEAD 与工作区是否干净、
  前端地址、后端形态。

壳自身也会在 `andy-harness-desktop` 窗口打印 `[andy-harness] 壳已启动；后端基地址 …`。

兜底：

```bash
# 仓库根目录（推荐，交互确认后删除 target 并从零重编）
start-desktop.bat --clean
start-desktop.bat --help

# 手工等价操作
cd desktop/src-tauri && cargo clean
```

`--clean` 会删除 `desktop/src-tauri/target`（含增量缓存），随后全量重编；
一般**不需要**用它，`cargo` 的指纹机制已经足够。

## 图标

主源图 `src-tauri/icons/icon.png`（1024×1024，由 `icons/_make_icon.py`
生成）。重新生成全套图标：

```bash
pnpm icons
```

## 验证

后端测试套件包含 `tests/test_desktop_shell.py`：启动器 `--check` 解析、
缺失解释器报错、Tauri 配置 / 能力 / Cargo 一致性，以及**端到端**通过
启动器拉起后端、`/api/health` 实测后整树终止。
