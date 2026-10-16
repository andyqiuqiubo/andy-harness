# VENDOR.md — REA vendored 记录

本目录是 [morluto/rea](https://github.com/morluto/rea)（REA: Reverse Engineer Anything）的
**完整源码快照**，以 MCP 服务形式集成到 andy-harness。

| 项 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/morluto/rea |
| 锁定版本 | `6.4.0`（`package.json` / `.release-please-manifest.json`） |
| 锁定提交 | `a56f7bb668ab23fec8d33f7af353ac7ed2fe1895`（npm `rea-agents@6.4.0` 的 `gitHead`，与发布版严格一致） |
| 许可证 | MIT（见 `LICENSE`；第三方组件清单见 `THIRD_PARTY_NOTICES.md`） |
| 拉取方式 | `https://codeload.github.com/morluto/rea/tar.gz/a56f7bb668ab23fec8d33f7af353ac7ed2fe1895` |
| 快照日期 | 2026-10-14 |
| 裁剪 | 无（完整仓库快照；git 子模块 `third_party/*` 为空挂载点，与 npm 发布包 `files` 白名单一致，均属运行时按需提供的外部工具） |

## 附带的官方发布物：Windows 原生构件

`native/windows/build/{rea-windows-x64.node, manifest.json}` 不在 GitHub 源码快照中
（上游由 Linux + MinGW 交叉编译、于发布时生成），本仓库从**同版本官方 npm 发布包**
补齐（版本必须与 `package.json` 一致，REA 加载器会强制校验）：

- 来源：`https://registry.npmjs.org/rea-agents/-/rea-agents-6.4.0.tgz` → `package/native/windows/build/`
- 完整性：`rea-windows-x64.node` SHA-256 `c4fd9f377e567bcf15ba3fec3e0b0ed965d82b765a2adcdac49e46f14524b6d5`
  与 `manifest.json` 声明一致（加载器每次启动复核该哈希与 PE32+ 头）

## 与上游的差异

1. **`.gitignore` 增加一条例外**（`!native/windows/build/`）：上游忽略 `build/`
   目录（原生构件由发布流水线生成、不入库）；本仓库为使官方原生构件随快照入库
   （离线可复现启动），对该目录加白名单例外。
2. 除构建产物与上述官方原生构件外，**未修改任何上游源文件**。构建产物
   （`node_modules/`、`dist/`、`.turbo/`、`.cache/`、`skills/`）已在仓库根
   `.gitignore` 中忽略，干净克隆后需按 `../README.md` 的步骤重新生成。
