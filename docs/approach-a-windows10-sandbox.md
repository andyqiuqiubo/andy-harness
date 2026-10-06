# 方案 A 详细设计：Windows 10 宿主机 + 2 个云端沙箱（互不相干）

> 本文是 `docs/EPIC-multiuser-asset-permission.md` 的**轻量首发切片**：只做「桌面隔离」这一半（不建部门/角色/ACL），
> 目标 = 在 **Windows 10 宿主机**上跑 1 个网关 + 2 个 Linux 桌面沙箱，2 个用户各自操作自己的沙箱，互不干扰，且**绝不触碰 Windows 宿主机桌面**。
>
> 适用前提：用户操作的是**各自一台 Linux/X11 云桌面**（computer_use 原生支持）。若想让用户操作自己本地的 Windows 电脑，那是另一套「桌面桥接」方案，不在本文范围。

---

## 1. 总体架构

```
┌──────────────────── Windows 10 宿主机（Docker Desktop / WSL2 后端）────────────────────┐
│                                                                                       │
│   Windows 桌面（宿主，用户永不触碰）   ← 只跑 Docker Desktop + 网关进程                  │
│                                                                                       │
│   ┌──────────────── WSL2 Linux VM（Docker 引擎所在）────────────────┐                 │
│   │                                                                 │                 │
│   │   网关 (nginx 反向代理 + auth_request)   ← 127.0.0.1:80         │                 │
│   │        │                          │                           │                 │
│   │   ┌────┴─────┐               ┌────┴─────┐                ┌────┴────┐            │
│   │   │ 沙箱 A   │  Xvfb:99      │ 沙箱 B   │  Xvfb:99     │ frontend │            │
│   │   │ 完整     │ + openbox     │ 完整     │ + openbox     │ (静态)   │            │
│   │   │ harness  │ + x11vnc      │ harness  │ + x11vnc      │          │            │
│   │   │ + pyautogui│ + websockify │ + pyautogui│ + websockify │          │            │
│   │   │ 专属卷   │ → noVNC 6080  │ 专属卷   │ → noVNC 6080  │          │            │
│   │   └──────────┘               └──────────┘                └──────────┘            │
│   │   用户A ──路由──▶ 沙箱A    用户B ──路由──▶ 沙箱B                                 │
│   └───────────────────────────────────────────────────────────────────────────────────┘
└───────────────────────────────────────────────────────────────────────────────────────┘
        用户浏览器：http://<宿主IP>/desk/userA/  →  看自己的云桌面 + 发 computer use 指令
```

**核心结论**：每个用户的 computer use 操作的是**自己沙箱容器里的 Xvfb 虚拟桌面**，不是 Windows 宿主机，也不是对方的沙箱。隔离由 Docker 命名空间（PID/挂载/网络）+ 专属数据卷天然保证。

---

## 2. Windows 10 宿主机准备

### 2.1 系统要求
- Windows 10 64 位，**内部版本 ≥ 19041**（2020 年 5 月更新及以后），家庭版/专业版/企业版均可（走 **WSL2 后端**，无需 Hyper-V 开关）。
- 开启 WSL2：管理员 PowerShell 跑 `wsl --install`，重启后在 Microsoft Store 装一个 Ubuntu（如 Ubuntu 22.04）。
- 安装 **Docker Desktop for Windows**，安装时勾选 **Use WSL 2 instead of Hyper-V**（默认即是）。

### 2.2 给 WSL2 / Docker 限资源（关键，否则会吃光内存）
在 `C:\Users\<你的用户名>\.wslconfig` 写入：

```ini
[wsl2]
memory=8GB        # 给 WSL2 VM 的上限；2 沙箱+中央建议 8GB，最少 6GB
processors=4      # 建议 4 核；2 沙箱最少 2 核但会很挤
swap=2GB
localhostForwarding=true   # 允许宿主 127.0.0.1 转发到容器端口
```

改完执行 `wsl --shutdown` 生效。

### 2.3 宿主资源建议
| 组件 | 内存占用（约） |
|---|---|
| Windows 10 自身 | 2.5–3.5 GB |
| WSL2 基础 + Docker | 0.5–1 GB |
| 沙箱 A（Xfce+Xvfb+Python+uvicorn） | 0.6–1 GB |
| 沙箱 B（同上） | 0.6–1 GB |
| 网关 + 前端 | 0.3 GB |

→ **宿主机至少 8 GB 物理内存，推荐 16 GB**。CPU 4 核起步。
> ⚠️ 你目前的开发机（i7-8550U / 16GB）能跑通**演示**（甚至 2 个轻量沙箱），但 2 个用户同时跑重任务（开浏览器/大程序）会明显卡顿。生产建议用一台独立机器或云端 Windows VM。

---

## 3. 沙箱镜像（带桌面的 harness 镜像）

在现有 `backend/Dockerfile` 基础上派生一个 `Dockerfile.desktop`，**加上虚拟桌面三件套**：`Xvfb`（虚拟显示）+ `openbox`（轻量窗口管理器，pyautogui 需要 WM 才能稳定点窗口）+ `x11vnc`+`websockify/noVNC`（把桌面暴露成网页）。

```dockerfile
# backend/Dockerfile.desktop
FROM python:3.12-slim

# 1) 虚拟桌面依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11-utils openbox x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install uv
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev
# 桌面控制依赖（加到 pyproject/requirements-sandbox.txt 亦可）
RUN pip install pyautogui mss

COPY harness/ ./harness/
COPY plugins/ ./plugins/
COPY marketplace/ ./marketplace/
COPY skill_marketplace/ ./skill_marketplace/
COPY mcp_marketplace/ ./mcp_marketplace/
COPY skills/ ./skills/
COPY entrypoint-desktop.sh /app/entrypoint-desktop.sh
RUN chmod +x /app/entrypoint-desktop.sh

ENV DISPLAY=:99
EXPOSE 8000 6080
CMD ["/app/entrypoint-desktop.sh"]
```

`entrypoint-desktop.sh`（启动顺序：显示 → 窗口管理器 → VNC → noVNC → 后端）：

```bash
#!/bin/bash
set -e
Xvfb :99 -screen 0 1280x800x24 -ac +extension RANDR >/dev/null 2>&1 &
sleep 2
openbox >/dev/null 2>&1 &
sleep 1
# 把 :99 暴露成 VNC（无密码，仅容器内/网关可达）
x11vnc -display :99 -nopw -forever -rfbport 5900 >/dev/null 2>&1 &
# 把 VNC 转成网页 noVNC，用户浏览器直接看桌面
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
# 启动 harness 后端（computer_use 在此进程内控制 :99）
exec uv run uvicorn harness.main:app --host 0.0.0.0 --port 8000
```

> 要点：`RealDesktopController` 用 `pyautogui`+`mss`，它们读 `DISPLAY=:99` → 只认容器内的 Xvfb，**天然碰不到 Windows 宿主桌面**。

---

## 4. 网关 / 编排（两种粒度，二选一）

### 方案 A-1：静态 2 沙箱（推荐 MVP，零动态编排）
直接写死 2 个沙箱服务，配一张 `user → 沙箱` 映射表。最简单、最稳，适合"正好 2 个用户"。

### 方案 A-2：动态按需拉起（可扩到 N 用户）
网关在用户首次登录时，用 **Docker SDK** 现拉一个沙箱容器并绑定。更灵活但多一块编排代码 + 需处理 Docker socket（见 §6 红线）。

下面给 **A-1** 的 `docker-compose.sandbox.yml` 草案：

```yaml
services:
  gateway:
    image: nginx:alpine
    volumes:
      - ./nginx-sandbox.conf:/etc/nginx/conf.d/default.conf:ro
    ports:
      - "127.0.0.1:80:80"
    depends_on: [sandbox-a, sandbox-b, frontend]
    restart: unless-stopped

  frontend:
    build: { context: ./frontend, dockerfile: Dockerfile }
    # 构建产物由 nginx 反代；或单独 expose 由网关路由
    expose: ["5173"]
    restart: unless-stopped

  sandbox-a:
    build: { context: ./backend, dockerfile: Dockerfile.desktop }
    environment:
      - DISPLAY=:99
      - HARNESS_AUTH=1                 # 开启既有 auth_manager
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
    volumes:
      - sandbox-a-data:/app/data        # 专属卷，绝不挂 C:\
    deploy:
      resources:
        limits: { cpus: "2", memory: 2G }
    restart: unless-stopped

  sandbox-b:
    build: { context: ./backend, dockerfile: Dockerfile.desktop }
    environment:
      - DISPLAY=:99
      - HARNESS_AUTH=1
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
    volumes:
      - sandbox-b-data:/app/data
    deploy:
      resources:
        limits: { cpus: "2", memory: 2G }
    restart: unless-stopped

volumes:
  sandbox-a-data:
  sandbox-b-data:
```

`nginx-sandbox.conf`（登录后按用户路由；`auth_request` 可接一个极简校验端点，或先用静态口令演示）：

```nginx
# 用户 A 的桌面与 API
location /desk/userA/  { proxy_pass http://sandbox-a:6080/; }   # noVNC 网页桌面
location /api/userA/   { proxy_pass http://sandbox-a:8000/; }
# 用户 B
location /desk/userB/  { proxy_pass http://sandbox-b:6080/; }
location /api/userB/   { proxy_pass http://sandbox-b:8000/; }
# 前端静态
location /             { proxy_pass http://frontend:5173/; }
```

> 简化版（不追求隐藏端口）：也可以直接让 `sandbox-a` 把 `6080/8000` 各映射到一个宿主机端口，用户凭账号进对应端口。但那样端口裸露，**生产务必走上面的 nginx 统一入口**。

---

## 5. 用户 ↔ 沙箱绑定与隔离

- **绑定**：在网关/前端维护 `user_id → sandbox-a|b` 的小映射（可先写死 2 条，或落一张 `user_sandbox` 表）。登录后前端只渲染该用户的 `/desk/userX/` 与 `/api/userX/`。
- **文件系统隔离**：`sandbox-a-data` 与 `sandbox-b-data` 是两个独立 Docker 卷，用户 A 的 `file_read/file_write` 只落在 A 的卷里，物理上读不到 B。
- **桌面隔离**：各自 `:99` Xvfb，用户 A 的鼠标/截图只作用于 A 的虚拟屏。
- **网络隔离**：默认 bridge 网络，沙箱间不通，只有网关能访问。
- **共享面**：仅网关 + 中央用户表（auth）。**没有跨沙箱访问路径**。

---

## 6. 安全边界（Windows 宿主专属红线）

这些配置一旦踩中，用户就能摸到宿主机或彼此，**绝对不能做**：

| 红线 | 为什么危险 |
|---|---|
| ❌ 沙箱 `--privileged` / 给 `CAP_SYS_ADMIN` | 容器逃逸到宿主 |
| ❌ 把 `C:\` 或 `/mnt/c` 挂进沙箱 | 用户可读写整个 Windows 盘 |
| ❌ `network_mode: host` | 沙箱看到宿主网络、能扫宿主 |
| ❌ 把 Docker socket 挂进**沙箱**（仅编排器可用，且编排器不能给用户调） | 拿到 socket = 拿到宿主 root，能删所有容器 |
| ❌ 挂宿主 X socket（`/tmp/.X11-unix`） | 用户操作到宿主真实桌面 |

**编排器如何连 Docker（Windows 差异）**：Windows 上 Docker 守护进程走 named pipe，不是 unix socket。
- 若编排器跑在**容器里**：挂载 `-v //./pipe/docker_engine://./pipe/docker_engine`，Docker SDK 默认连 `npipe:////./pipe/docker_engine`。
- 若编排器跑在 **Windows 宿主原生 Python**：直接 `pip install docker`，SDK 默认就能连上（同 npipe）。
- 此 socket **只给编排器**，绝不进沙箱。

---

## 7. 网络与端口规划

- Docker Desktop 在 Windows 上用 NAT 网络；容器拿私有 IP。
- 用户从 Windows 宿主浏览器访问 `http://127.0.0.1/` 或 `<宿主局域网IP>/`（网关 80 端口）。
- 沙箱内部端口（`8000` 后端 / `6080` noVNC）**只暴露给网关**，不映射到宿主公网。
- 若要从局域网/公网访问，在 Windows 防火墙放行 80，并前置一层 HTTPS（如用 nginx 加证书或 Cloudflare 隧道）——本文不展开。

---

## 8. 性能注意（Windows 专属坑）

1. **别把沙箱工作目录 bind 挂到 Windows 盘**（`C:\...` 在 WSL2 里是 `/mnt/c/...`，跨 OS 文件访问极慢，agent 频繁读写会卡死）。**用 Docker 命名卷**（存在 WSL2 的 ext4 里）替代。
2. **WSL2 内存会膨胀**：务必设 §2.2 的 `.wslconfig` 上限，否则跑几天把宿主内存吃满。
3. **Windows 更新 / 重启**会把 WSL2 VM 关掉，容器随之停。`restart: unless-stopped` 能让 Docker 起来后自启，但首次需手动开 Docker Desktop（或设开机自启）。
4. **杀软扫描 WSL2 文件系统**会拖慢 I/O，必要时把 WSL 发行版加入白名单。

---

## 9. 端到端部署步骤（MVP）

1. Windows 10 装 Docker Desktop（WSL2 后端）+ Ubuntu 22.04；写 `.wslconfig` 限资源。
2. 克隆项目；新增 `backend/Dockerfile.desktop` 与 `entrypoint-desktop.sh`（见 §3）。
3. 在 `pyproject.toml` 加 `pyautogui`、`mss`（或 `requirements-sandbox.txt`）。
4. 写 `docker-compose.sandbox.yml`（§4）与 `nginx-sandbox.conf`（§4）。
5. 在 `auth_manager` 开 `HARNESS_AUTH=1`，建 2 个用户（userA/userB），配 `user→沙箱` 映射。
6. `docker compose -f docker-compose.sandbox.yml up -d --build`。
7. 浏览器开 `http://127.0.0.1/`，用 userA 登录 → 进 `/desk/userA/` 看到 Xfce 桌面；发一条 computer use 指令验证它操作的是该桌面。
8. 用 userB 登录，确认是**另一个**独立桌面。

---

## 10. 验证清单（交付前必过）

- [ ] 用户 A 在 noVNC 里看到 Xfce 桌面；发 computer use（如"打开浏览器"）只在该桌面发生。
- [ ] 用户 A 执行 `file_write` 后，`ls /app/data` 只见自己的文件；切到用户 B 的桌面/卷看不到 A 的内容。
- [ ] 关掉沙箱 A 容器，沙箱 B 与 Windows 宿主桌面**完全不受影响**。
- [ ] 在 Windows 宿主上观察：Task Manager 里只有 Docker/WSL2 进程，**宿主自己的 Windows 桌面始终未被任何用户改动**。
- [ ] 压测：2 用户同时跑中等任务，WSL2 内存不超 `.wslconfig` 上限、不 OOM。

---

## 11. 风险与兜底

| 风险 | 兜底 |
|---|---|
| Xvfb + pyautogui 偶尔挑显示环境（窗口焦点/坐标） | 用 openbox + 充分冒烟；坐标基于 `screenshot` 返回的分辨率推算 |
| WSL2 内存膨胀 | `.wslconfig` 硬上限 + 容器 `memory` 限制 |
| Windows 更新重启打断服务 | Docker Desktop 开机自启 + `restart: unless-stopped` |
| 跨 OS 文件慢 | 只用 Docker 卷，不 bind Windows 盘 |
| 沙箱资源争抢 | compose 里给每个沙箱 `cpus/memory` 上限 |

---

## 12. 工作量估算（方案 A-1 静态 2 沙箱）

| 任务 | 人日 |
|---|---|
| 桌面沙箱镜像（Dockerfile.desktop + entrypoint + 依赖） | 1 |
| nginx 网关路由 + 用户↔沙箱映射 | 2 |
| auth_manager 启用 + 2 用户/映射落库 + 前端登录入口 | 2–3 |
| 数据卷隔离 + 安全红线核查 | 0.5 |
| Windows/WSL2 部署踩坑 + 性能调优 | 2 |
| 验证清单 + 文档 | 1 |
| **合计** | **≈ 8.5–10.5 PD** |

比完整 EPIC（65–84 PD）小一个数量级，是先把"多用户桌面隔离"跑通的最佳首发切片。后续若要扩到 N 用户/团队版，再切方案 A-2 动态编排，并叠加 EPIC 里的部门/ACL/审计。
