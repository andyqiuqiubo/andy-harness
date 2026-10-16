# 真实 GUI 样本清单（本地化）

> 目录：`src/rev_eng/samples/`。所有文件均落在项目目录内（H: 盘），不依赖外部链接与全局安装。

## 1. Process Explorer（主推 · 免安装 · 已运行出界面）

| 项 | 值 |
| --- | --- |
| 名称 | Sysinternals Process Explorer v17.x（2026-09-10 构建） |
| 来源 | `https://download.sysinternals.com/files/ProcessExplorer.zip` |
| 压缩包 | `ProcessExplorer.zip` · 3,633,642 B |
| 主程序 | `procexp/procexp64.exe` · 2,419,992 B · 单文件绿色 |
| SHA-256（zip） | `746770d3f54326dd7da16c2b2815803ab131f348cba24a91cb6a38cfd2f7073f` |
| SHA-256（exe） | `22bb3c30cc79252284066fff934b62d35508d240d3fa844451adc99981012835` |
| 数字签名 | **Valid**，CN=Microsoft Corporation，有效期 2026-04-17 → 2027-04-16 |
| 界面 | 进程树 + 进程列表 + CPU/内存实时曲线 + DLL/句柄视图 |
| 运行方式 | 双击 `procexp\procexp64.exe`；静默接受协议加参数 `/accepteula` |
| 实测 | 2026-10-15 00:19 启动，PID 2136，窗口标题 `Process Explorer - Sysinternals: www.sysinternals.com [...]`，SessionId=1（与桌面 explorer 同会话，窗口可见），常驻内存 97.6 MB / 23 线程 |

包内另有 `procexp.exe`（32 位，4,619,544 B）、`procexp64a.exe`（2,463,512 B）、`Eula.txt`。

## 2. 7-Zip 26.04 安装程序（备选 · 带安装向导）

| 项 | 值 |
| --- | --- |
| 名称 | 7-Zip 26.04（2026-10-05，Igor Pavlov） |
| 来源 | `https://github.com/ip7z/7zip/releases/download/26.04/7z2604.exe` |
| 文件 | `7z2604.exe` · 1,361,691 B · 32 位 x86 安装程序 |
| SHA-256 | `39bf65045153fc26c42fa9fe47afd8cd1b11671c7f11d031e738c9a13960c009` |
| 数字签名 | 官方**不签名**（7-Zip 一贯做法），以 SHA-256 校验 |
| 界面 | 安装向导 → 装完即 7-Zip 文件管理器（双栏 GUI） |
| 说明 | 体积 1.3 MB 却有完整安装流程 + GUI，是"能安装 + 能看界面"的最小样本 |

## 下载通道实测（本机沙箱代理）

- `download.sysinternals.com`：可达，**约 1.2 MB/s**（3.6 MB / 3.5 s）。
- GitHub release 附件：本次可达，**约 0.26~0.65 MB/s**（1.36 MB / 5.7 s）；`www.7-zip.org/a/...` 会 302 到 GitHub，需加 `-L`。
- 不可达：`www.nirsoft.net`（502）、`files.sumatrapdfreader.org` 旧版直链（404）、flos-freeware Notepad2 旧版路径（404）。

## 后续可用作逆向样本

`procexp64.exe` 是真实世界 GUI 二进制（2.4 MB、大量 Win32 导入、对话框/菜单/图标资源、签名表），
适合作为工程强逆向流水线（REA 静态 → Frida 动态 → 沙箱 → 分析 → Evidence v2）的第三轮真实样本；
`7z2604.exe` 可作为"安装包（自解压 SFX）"类样本。

---

## 外部解压器（`.tools/7zip/`，重建安装器的运行时依赖）

重建出的安装器在 Install 时需要一个 7z 解压器来真实释放内嵌载荷。按"隔离在项目目录"原则
下载并校验（官方页面不发布 SHA-256，故采用**多镜像交叉校验**）：

| 文件 | 大小 | SHA-256 | 来源 |
| --- | --- | --- | --- |
| `7zr.exe` | 602,624 | `256feca8e274e5da655e2a284fabafd9f554365eb164862089dacd4e8276d282` | SourceForge 三个镜像（master/netix/dl）哈希一致 |
| `7za.exe` | 1,335,808 | `15d4c788c148e3677e2fc1c4a01f191fb6669eba4f6acf8a465f0f1a6f1e1260` | 由 `7zr.exe` 从 `7z2604-extra.7z` 解出 |
| `7za.dll` | 416,256 | `e70d353e9a0f2d1755ab0a7a7100060ab1e70e47141c248e968364579a329a94` | 同上 |
| `7z2604-extra.7z` | 1,764,616 | `dc4b11d3399db18b063630137145f5585d8f7ac847bf3639bd1185d7d1f7cee0` | 双镜像一致（master + netix） |

- `7zr.exe` 是官方独立单文件版（仅支持 7z），用于引导解出 `7z2604-extra.7z`；
  `7za.exe` 是完整命令行版（支持 7z/zip/cab/gzip 等），作为重建安装器的默认解压器。
- 查找顺序：构建期嵌入的绝对路径 → exe 同目录 → PATH；都找不到时重建产物降级为
  "仅把载荷原样释放到目标目录"并在界面明示，不假装安装成功。
