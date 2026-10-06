# EPIC：多用户资产权限治理（团队版 / 私有化方向）

- **状态**：草案（设计评审中）
- **作者**：架构组
- **日期**：2026-10-04
- **版本**：v0.1
- **关联材料**：微信文《光是"权限隔离"四个字，就难倒 90% 做 LLM 私有化的人》

---

## 0. 文档目的与范围

本文档定义将 andy-harness 从「单人本机智能体」演进为「**团队 / 企业私有化多用户平台**」所需的**资产权限治理**能力。

覆盖三件事：

1. **现状盘点**——诚实评估现有能力与缺口。
2. **改造量评估**——按模块量化工作量与优先级。
3. **部署架构**——回答「单机能否跑」「是否需要集群」「数据库如何选」等部署疑问。

> 注意：本文档仅做设计，**不涉及任何代码改动**。所有工作量为人日（PD）估算，假设为 1 名熟练后端 + 0.5 名前端 + 0.25 名 DevOps 的配比，未含联调与回归测试缓冲。

---

## 1. 背景与产品方向变更

### 1.1 为什么要做

企业 LLM 私有化落地的核心痛点不是「接模型」，而是**资产权限隔离**：

- 多级部门下，A 部门的模型 / 技能 / 知识库默认 B 部门**完全不可见**；
- 同一应用内，不同角色可调用的技能不同；
- 资源的「查看 / 编辑 / 运行调用」权限需拆分；
- 跨部门共享需**单资源单独授权**，而非开放全部；
- 权限校验必须落在 **Agent 运行时（function call / 知识库检索）**，而非仅前端隐藏。

市场上绝大多数开源 LLM 平台只做了「工作空间成员管理 + 前端按钮隐藏」，并非真正的资产隔离。本文档对标的是「部门隔离 + 三元 ACL + 运行时拦截 + 全链路审计」的完整体系。

### 1.2 产品方向变更声明（重要）

当前项目铁律是 **Computer Use——操控整个本机桌面**，默认单人本机。本 EPIC 意味着产品方向向 **「团队 / 企业私有化多用户平台」** 扩展。

这带来一个**必须决策的结构性冲突**（详见 §5.4）：Computer Use 本质绑定物理机 / 桌面环境，而多用户要求「每个用户各自隔离的桌面控制权」。本 EPIC 不试图在本文解决该冲突的全部细节，但必须在架构上预留隔离边界。

---

## 2. 现状盘点（Baseline）

### 2.1 现有两层权限

| 层     | 模块                                | 能力                                                                                | 性质                              |
| ----- | --------------------------------- | --------------------------------------------------------------------------------- | ------------------------------- |
| 认证层   | `auth_manager`（`HARNESS_AUTH` 开关） | 用户 / 密码 / Bearer token；`users` 表；`is_admin` 布尔                                    | 多用户**认证**，但无角色 / 部门             |
| 操作安全层 | `permission_manager`              | 工具按 `read/write/dangerous` 分级；`auto/confirm/deny` 策略；运行时已在 `agent_loop.py:518` 生效 | **AI 调用护栏**（output-side safety） |

**结论**：现有体系解决的是「AI 自主调用工具是否可信」，与文章讨论的「人 / 部门之间谁能看见 / 调用哪份资产」是**正交维度**。

### 2.2 数据层现状（已核实）

| 表           | 关键字段                                                      | 多用户就绪度                                        |
| ----------- | --------------------------------------------------------- | --------------------------------------------- |
| `users`     | `id, username, password_hash, salt, is_admin, created_at` | ❌ 扁平，无 `tenant_id` / `department_id` / `role` |
| `sessions`  | 含 `user_id`（默认 `''`）                                      | ✅ 已埋点，启用认证后按用户隔离                              |
| `memories`  | 含 `user_id`（默认 `''`）                                      | ✅ 已埋点                                         |
| `providers` | `id, name, base_url, api_key_encrypted, models_json…`     | ❌ **无 owner / department**，全局共享               |
| `skills`    | **文件系统**：`backend/skills/`、`~/.andy-harness/skills/`      | ❌ 无 DB 归属、无 ACL                               |
| `mcp`       | **JSON 配置**：`mcp.json`                                    | ❌ 无 DB 归属、无 ACL                               |
| `spans`     | 执行追踪                                                      | ⚠️ 非审计，无决策记录                                  |

**关键事实**：`sessions` / `memories` 已为多用户预留 `user_id`，说明项目**早已规划多用户会话/记忆隔离**；但**资产（providers/skills/mcp）完全没有归属字段**，是本次改造的主要数据负债。

### 2.3 运行时拦截现状

- `agent_loop.py:518-523`：`_check_permission()` 在每次工具调用前查询 `PermissionService.decide(tool_name)`，**运行时校验已具备**（文章批评的「只前端隐藏」本项目未犯）。
- `require_admin`（`api/deps.py`）：仅保护管理端点，只有 admin 一档。
- **缺口**：对「资源加载」（provider / skill / mcp 加载、知识库检索）**无鉴权点**；对「资产可见性」（部门隔离）**无概念**。

### 2.4 部署现状

- 单机 + SQLite（`Database` 单共享连接 + `threading.RLock`，WAL 模式）。
- 源码注释自述：「单共享连接上的并发保护……同一连接仍不可被多线程同时访问」。
- 本机桌面 agent（Computer Use）进程与后端同机。

---

## 3. 目标架构

### 3.1 组织模型（新增）

```
Tenant（租户）
  └─ Department（部门，多级树）
       └─ User（用户，可属多部门）
            └─ Role（角色，可多角色）
```

- 新增表：`tenants`、`departments`、`department_members`、`roles`、`user_roles`。
- `users` 表扩展：`tenant_id`、`department_id`（主部门）、`status`。
- Token 载荷扩展：携带 `tenant_id`、`department_id`、`roles`。

### 3.2 资源 / 资产模型

统一抽象为「资产（Asset）」，类型包括：

| 资产类型                 | 现状载体          | 改造                                                   |
| -------------------- | ------------- | ---------------------------------------------------- |
| `model` / `provider` | `providers` 表 | 加 `owner_tenant` / `owner_department` / `visibility` |
| `skill`              | 文件系统          | 纳入 `assets` 表，记录路径 + 归属                              |
| `mcp_server`         | `mcp.json`    | 纳入 `assets` 表                                        |
| `plugin`             | 文件系统          | 纳入 `assets` 表                                        |
| `knowledge_base`     | 待定            | 纳入 `assets` 表（团队版新增资产）                               |
| `cloud_disk`         | 待定            | 对象存储挂载（团队版新增资产）                                      |
| `session` / `memory` | 已有 `user_id`  | 加 `tenant_id` / `department_id`                      |

- 新增统一表 `assets`：`id, type, ref_id, name, owner_tenant, owner_department, owner_user, visibility(enumerated|department|tenant|public), created_at`。

### 3.3 权限模型（三元 ACL + 部门隔离）

- **部门隔离（默认 deny）**：非本部门资源，`visibility=department` 时默认不可见、不可用。
- **三元 ACL**：策略项 `(role, app, resource) → allow|deny`，叠加在部门隔离之上做**精细放行**。
- **权限动词拆分**：`view`（查看）/ `edit`（编辑）/ `execute`（运行调用）三种动作分别授权。
- 新增表：`acl_policies`、`asset_shares`（跨部门单资源单独授权）。

### 3.4 运行时拦截点（新增）

| 拦截点            | 位置                                 | 动作                            |
| -------------- | ---------------------------------- | ----------------------------- |
| 工具调用           | `agent_loop._check_permission`（已有） | 叠加 ACL：该角色在所属应用能否 execute 此资源 |
| Provider 加载    | `provider_registry.get_provider`   | 校验调用方可见且有权                    |
| Skill / MCP 加载 | `skill_service` / `mcp_client`     | 校验可见性 + 部门                    |
| 知识库检索          | 检索入口                               | 校验可见性 + 部门                    |
| 资源读取 API       | REST 路由                            | `require_visible(asset)` 依赖   |

### 3.5 审计

- 新增 `permission_audit` 表：`id, tenant_id, user_id, session_id, asset_type, asset_id, action(view/edit/execute), decision(allow|deny|confirm), reason, created_at`。
- 在全部上述拦截点落库（异步写入，不影响主链路延迟）。
- 提供查询 API + 前端审计页。

---

## 4. 改造量评估

> 估算假设：熟练后端 1 人为主，前端 / DevOps 部分投入。含单元 + 集成测试，不含 UAT。

| #      | 模块           | 现状                            | 改造内容                                             | 工作量(PD)       | 优先级 |
| ------ | ------------ | ----------------------------- | ------------------------------------------------ | ------------- | --- |
| A      | 组织模型 + 数据层   | 仅 `users`+`is_admin`          | 新增 5 张表 + `users` 扩展 + 迁移脚本（单用户→默认租户/部门）         | 8–10          | P0  |
| B      | 资源归属模型       | providers 无归属；skills/mcp 文件系统 | `assets` 表 + provider 加字段 + skill/mcp 入库索引       | 6–8           | P0  |
| C      | ACL 引擎       | 无                             | 新模块 `asset_acl_engine`：部门隔离求值 + 三元 ACL 求值 + 动词拆分 | 10–12         | P0  |
| D      | 运行时拦截集成      | 仅工具调用                         | 在 provider/skill/mcp/知识库/资源 API 插入检查点            | 8–10          | P1  |
| E      | 审计服务         | 仅 `spans`                     | `permission_audit` 表 + 落库 + 查询 API               | 4–5           | P1  |
| F      | 认证层增强        | token 无 tenant/role           | `auth_manager` 注入 tenant/department/roles；登录引导租户 | 3–4           | P0  |
| G      | 管理面 API      | 无                             | 部门 / 角色 / 资源授权 CRUD（admin 专属）                    | 6–8           | P1  |
| H      | 前端（Settings） | 无                             | 部门管理 / 角色管理 / 资源授权 / 审计页                         | 10–14         | P2  |
| I      | 部署改造         | SQLite 单连接                    | 数据库抽象层（支持 PostgreSQL）+ 配置 + 文档                   | 5–7           | P1  |
| J      | 回归与性能        | —                             | ACL 求值缓存、批量校验、压测                                 | 5–6           | P1  |
| **合计** |              |                               |                                                  | **~65–84 PD** |     |

**结论**：这是一个**独立产品线级别**的 EPIC，而非「加个模块」。最低可行切片（P0：A+B+C+F）约 **25–34 PD**，可先交付「部门隔离 + 基础 ACL」骨架。

---

## 5. 部署架构（核心回答）

### 5.1 单机能否跑？

**可以，但有前提。**

- **开发 / 小团队（≤20 人、低并发）**：**单台服务器**即可，但**必须弃用 SQLite、改用单实例 PostgreSQL**。
- 理由见 §5.2：SQLite 单共享连接 + RLock 的设计不支持真正的多用户并发写入，多用户下会出现锁竞争与写入串行化瓶颈。

### 5.2 数据库选型（硬性理由）

| 维度         | SQLite（现状）      | PostgreSQL（目标）                  |
| ---------- | --------------- | ------------------------------- |
| 并发写        | 单连接 + RLock，串行化 | 多连接，行级锁                         |
| 多用户写入      | 不适合             | 原生支持                            |
| 行级权限 / RLS | 无               | 内置 Row-Level Security（可做部门隔离兜底） |
| 备份 / 主从    | 文件拷贝            | 流复制、高可用                         |
| 运维复杂度      | 零               | 中（可用托管版）                        |

**决策**：团队版**强制 PostgreSQL**。建议通过**数据库抽象层**（`Database` 接口化，当前已是 `Database` 类，需抽象方言差异）兼容两种后端，使单人版仍可 SQLite、团队版切 PG。

### 5.3 集群化条件与方案

| 规模             | 是否需要集群 | 方案                                             |
| -------------- | ------ | ---------------------------------------------- |
| ≤20 人 / 低并发    | 否      | 单机 + 单 PG 实例 + 单应用进程                           |
| 20–100 人       | 可选     | 单机多应用实例 + 反向代理（sticky session）+ 单 PG           |
| >100 人 / HA 要求 | **是**  | 多应用实例（无状态）+ 负载均衡 + PG 主从 + 对象存储 + Redis（会话/缓存） |

- **应用进程需无状态化**：当前 agent 会话、调度任务等有状态逻辑需外置（会话存 PG / Redis，桌面控制见 §5.4）。
- **WebSocket（chat）有状态**：需 sticky session 或集中会话存储，否则跨实例断连。

### 5.4 Computer Use 在团队版的边界（结构性约束）

这是本方向**最关键的架构决策点**，必须在立项时拍板：

- Computer Use 操控的是**物理机 / 具体桌面环境**，天然是「单人 + 单机」语义。
- 多用户下不能让所有用户共享操控同一台物理机。可选落地形态：
  1. **每席位一台受控机 / 虚拟桌面**：用户登录后，agent 实例绑定到「该用户的桌面环境」（本地或云桌面），控制权随用户会话隔离。
  2. **云端 Agent + 各用户自带桌面桥接**：Agent 跑在服务端，桌面操控经用户侧桥接客户端下发。
- 无论哪种，都需要**会话级 agent 实例隔离**与**桌面控制权归属校验**，否则会出现「A 用户操控了 B 用户的桌面」的越权事故。

> 建议：团队版初期将 Computer Use 限定为「用户自有桌面桥接」模式，避免服务端托管物理机的合规与安全风险。

### 5.5 对象存储

知识库 / 云盘类资产需要对象存储：

- 小团队：本地 MinIO（单机可跑）。
- 中大型：S3 / 兼容云存储。
- 资产元数据入 `assets` 表，实体存对象存储，ACL 同时约束元数据与访问签名。

### 5.6 容器化与网关

- **小团队**：`docker-compose`（app + postgres + minio）。
- **中大型**：Kubernetes（Deployment + Service + Ingress + 可选 HPA）。
- 网关：nginx / 云 LB，终止 TLS，做认证中间件与限流。
- 现有 `docker-compose.yml` 可作为起点扩展。

### 5.7 推荐部署档位

| 档位  | 适用          | 组件                                           |
| --- | ----------- | -------------------------------------------- |
| 单机档 | POC / ≤20 人 | 1×app + 1×PostgreSQL（容器）+ 1×MinIO            |
| 标准档 | 20–100 人    | 2×app（sticky）+ PG 主从 + MinIO + Redis + nginx |
| 企业档 | >100 人 / HA | K8s 多副本 + PG 流复制 + 对象存储 + Redis 集群 + 监控      |

---

## 6. 分阶段路线（Roadmap）

| Phase | 内容               | 依赖 | 产出                          |
| ----- | ---------------- | -- | --------------------------- |
| P0    | 数据模型 + 迁移（A、B、F） | —  | 组织/资源表、单用户迁移、token 带 tenant |
| P1    | ACL 引擎（C）        | P0 | `asset_acl_engine` 可单测      |
| P2    | 运行时拦截（D）         | C  | provider/skill/mcp/检索 鉴权点   |
| P3    | 审计（E）            | D  | `permission_audit` + API    |
| P4    | 管理面 API（G）       | C  | 部门/角色/授权 CRUD               |
| P5    | 前端（H）            | G  | Settings 管理页                |
| P6    | 部署改造（I、J）        | 全程 | PG 抽象层、集群文档、压测              |

---

## 7. 风险与权衡

| 风险              | 说明                         | 缓解                           |
| --------------- | -------------------------- | ---------------------------- |
| 过度设计            | 单人场景下引入部门/租户属过度复杂          | P0 切片最小交付；单人版保持 SQLite 路径    |
| Computer Use 冲突 | 多用户共享桌面越权                  | §5.4 会话级隔离 + 控制权归属校验         |
| 性能              | 每次 function call 查 ACL 增延迟 | ACL 求值缓存 + 批量校验（J）           |
| 数据迁移            | 现有单用户数据归属                  | 默认租户/部门引导迁移（A）               |
| 复杂度扩散           | ACL 渗透所有资源加载点              | 集中 `require_visible` 依赖，避免散落 |

---

## 8. 验收标准

1. 启用团队模式后，非本部门资产在列表与 API 均**默认不可见**（验证部门隔离默认 deny）。
2. 三元 ACL 可精确控制「某角色在某应用对某资源」的 view/edit/execute。
3. 所有资源加载点（provider/skill/mcp/知识库/资源 API）**运行时拦截**生效，单测覆盖 deny 路径。
4. `permission_audit` 完整记录 allow/deny/confirm 决策，可前端查询回溯。
5. 部署：支持单实例 PostgreSQL 单机运行；文档覆盖标准档 / 企业档集群方案。
6. 单人本机模式行为**完全不变**（回归测试通过）。

---

## 9. 附录：目标表结构草案（关键表）

```sql
-- 租户
CREATE TABLE tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 部门（多级：parent_id 自引用）
CREATE TABLE departments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    parent_id TEXT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

-- 角色
CREATE TABLE roles (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 用户-部门 / 用户-角色 关联
CREATE TABLE user_departments (user_id TEXT, department_id TEXT, PRIMARY KEY(user_id, department_id));
CREATE TABLE user_roles (user_id TEXT, role_id TEXT, PRIMARY KEY(user_id, role_id));

-- 统一资产索引
CREATE TABLE assets (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,            -- model|skill|mcp|plugin|kb|disk|session
    ref_id TEXT NOT NULL,          -- 指向原载体（providers.id / 路径哈希等）
    name TEXT NOT NULL,
    owner_tenant TEXT NOT NULL,
    owner_department TEXT NOT NULL,
    owner_user TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'department', -- department|tenant|public|enumerated
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 三元 ACL 策略
CREATE TABLE acl_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    app TEXT NOT NULL,             -- 应用/会话上下文标识
    asset_type TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    action TEXT NOT NULL,          -- view|edit|execute
    effect TEXT NOT NULL           -- allow|deny
);

-- 跨部门单资源授权
CREATE TABLE asset_shares (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    target_department TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 权限审计
CREATE TABLE permission_audit (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    asset_type TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    action TEXT NOT NULL,
    decision TEXT NOT NULL,        -- allow|deny|confirm
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```


```

---

*本文档为设计草案，落地前需经架构评审确认 §1.2 产品方向变更与 §5.4 Computer Use 边界决策。*
```
