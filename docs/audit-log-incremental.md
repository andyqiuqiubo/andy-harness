# 审计日志增量方案（Audit Log Incremental）

> 本文是 `docs/EPIC-multiuser-asset-permission.md` 的**首期落地件**之一。
> 对应微信文章强调的「全链路审计」——谁、何时、调了什么工具、策略如何裁决、最终是否执行。
> **不依赖任何组织模型（无 tenant/department/role）**，纯增量、低风险、可直接评审与实现。

---

## 1. 背景与目标

### 1.1 现状缺口
- `permission_manager` 的 `decide()` 已能在**每次工具调用前**做运行时裁决（`agent_loop.py` 调用），本项目**没有犯**文章批评的"只前端隐藏按钮"。
- 但**决策本身没有被记录**：现有 `spans` 表只追踪"工具执行了多久/什么参数"，没有"策略裁决了 allow/confirm/deny、原因是什么、最终人是否放行"这条审计链。
- `auth_manager` 已提供 `users.id` / `sessions.user_id`，**多用户主体已经存在**，可直接挂在审计记录上。

### 1.2 目标
新增一张 `permission_audit` 表 + 两个落库点（决策时 / 人类裁决后）+ 一组只读查询 API，使每一次权限决策可回溯。

### 1.3 设计原则
1. **仅追加（additive）**：不动 `decide()` 的纯函数语义，不动既有策略逻辑。
2. **零组织模型依赖**：直接用现有 `user_id` / `session_id`，不引入部门/角色。
3. **沿用既有约定**：表用 `CREATE TABLE IF NOT EXISTS`（旧库自动升级，无需迁移脚本）；时间存带时区偏移的 UTC；复用 `infra.database` 的 `Database` 单例（已配 WAL + `busy_timeout=5000`）。
4. **可平滑迁移 PG**：表结构用最朴素的列类型，后续团队版换 PostgreSQL 时原样搬。

---

## 2. 表结构

在 `backend/harness/infra/database.py` 的初始化段（现有 `CREATE TABLE IF NOT EXISTS sessions/messages/...` 同区）追加：

```sql
CREATE TABLE IF NOT EXISTS permission_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,   -- UTC ISO 带偏移，如 2026-10-04T09:47:38+00:00
    user_id       TEXT,              -- 关联 users.id；未登录/系统调用记 'system'
    session_id    TEXT,              -- 关联 sessions.id
    agent_run_id  TEXT,              -- 关联 agent_runs.id（可选，便于与 spans 联动）
    tool_name     TEXT    NOT NULL,
    risk          TEXT    NOT NULL,  -- read / write / dangerous
    action        TEXT    NOT NULL,  -- 策略裁决：allow / confirm / deny
    stage         TEXT    NOT NULL,  -- decision（策略判定）/ resolved（人类最终裁决）
    outcome       TEXT,              -- decision 阶段=action；resolved 阶段=executed/cancelled/rejected
    reason        TEXT,              -- 策略原因原文（来自 Decision.reason）
    policy_mode   TEXT,              -- 决策时刻的全局策略快照（forensics 用）
    decided_by    TEXT,              -- policy / override / human
    trace_id      TEXT               -- 可选，关联 spans.trace_id
);

CREATE INDEX IF NOT EXISTS idx_audit_created   ON permission_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_user      ON permission_audit(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_session   ON permission_audit(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_tool      ON permission_audit(tool_name, created_at);
```

### 2.1 字段说明
| 字段 | 含义 |
|---|---|
| `stage` | `decision` = 策略引擎给出的裁决（每次工具调用必写一条）；`resolved` = 人类对 `confirm` 的最终裁决（仅 confirm 类产生） |
| `outcome` | decision 阶段等于 `action`；resolved 阶段为 `executed`（人放行后真正执行）/ `cancelled`（人拒绝或超时取消）/ `rejected`（拒绝） |
| `decided_by` | 区分"策略自动裁决"还是"单工具 override 覆盖"还是"人类点击"——文章要求的可回溯核心 |
| `policy_mode` | 决策瞬间的全局模式快照，避免事后改策略导致审计对不上 |

### 2.2 为何不建外键
沿用本项目 SQLite 务实风格（现有表也未强依赖 FK），`user_id/session_id` 仅作逻辑关联，避免跨表约束拖慢写入与未来迁移。

---

## 3. 落库点（核心）

> ⚠️ **关键陷阱**：`decide()` 在 `PermissionServiceImpl.list_tool_risks()`（service.py:213）里被**对每个工具各调一次**，只是为了给前端渲染"每个工具的等级与生效动作"。若把审计写进 `decide()` 内部，会把这些"展示用"假决策也灌进库。**因此审计绝不能放在 `decide()` 体内**，必须在"真实执行的调用路径"上落库。

### 3.1 落库点 1 —— 策略决策（decision 阶段）
位置：`agent_loop.py` 调用 `permission_service.decide(tool_name)` 的**真实执行前**那一处（约 518 行附近，即现有运行时拦截点）。在拿到 `Decision` 后、执行工具前，写一条 `stage='decision'` 记录。

```python
# agent_loop.py（伪代码，落在现有 decide() 调用之后）
decision = permission_service.decide(tool_name)
audit_repo.record_decision(
    user_id=session.user_id or "system",
    session_id=session.id,
    agent_run_id=run_id,
    tool_name=tool_name,
    risk=decision.risk,
    action=decision.action,            # allow / confirm / deny
    stage="decision",
    outcome=decision.action,
    reason=decision.reason,
    policy_mode=permission_service.get_mode(),
    decided_by="override" if was_overridden else "policy",
)
```

> 如何判断 `was_overridden`：在 `decide()` 返回后由其 `reason` 是否含"被单独设置"判定，或在 `decide()` 内顺手把来源写进 `Decision`（推荐给 `Decision` 加一个 `source: str = "policy"|"override"` 字段，零破坏性）。

### 3.2 落库点 2 —— 人类裁决（resolved 阶段）
位置：现有"人工确认"流程里，用户在前端点了**允许/拒绝**的那一刻（即 `confirm` 动作的收口处）。根据结果补一条 `stage='resolved'`：

```python
if decision.needs_confirm:
    result = await wait_for_human_confirm(...)   # approved / rejected / timeout
    if result == "approved":
        audit_repo.record_decision(..., stage="resolved", outcome="executed", decided_by="human")
        # 继续真正执行工具
    else:  # rejected 或 timeout
        audit_repo.record_decision(..., stage="resolved", outcome="rejected", decided_by="human")
        return  # 不执行
```

### 3.3 落库实现（复用 Database 单例）
在 `permission_manager/service.py` 或新建 `audit_repo.py` 提供写入方法，直接走既有 `Database()`：

```python
def record_decision(self, *, user_id, session_id, tool_name, risk, action,
                    stage, outcome, reason, policy_mode, decided_by,
                    agent_run_id=None, trace_id=None) -> None:
    from harness.infra.database import Database
    Database().execute(
        """INSERT INTO permission_audit
           (created_at, user_id, session_id, agent_run_id, tool_name, risk,
            action, stage, outcome, reason, policy_mode, decided_by, trace_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(UTC).isoformat(), user_id, session_id, agent_run_id,
         tool_name, risk, action, stage, outcome, reason, policy_mode,
         decided_by, trace_id),
    )
```

> 写入走与全项目相同的 SQLite 单连接 + RLock + WAL，`busy_timeout=5000` 已就位；单次 `INSERT` 足够轻，不会成为瓶颈。若日后量极大，可改为内存缓冲批量落盘（本方案先不做）。

---

## 4. API 设计

新增路由 `backend/harness/api/rest/permission_audit.py`，风格与现有 `permissions.py` 一致（`APIRouter` + `require_admin` + `ServiceRegistry` 取服务）。**审计查看是管理员权限**，与权限配置的同套门禁对齐。

```python
router = APIRouter(prefix="/api/permissions/audit", tags=["permission-audit"])
```

### 4.1 列表查询（带过滤 + 分页）
`GET /api/permissions/audit`

| Query 参数 | 类型 | 说明 |
|---|---|---|
| `user_id` | str | 按用户过滤 |
| `session_id` | str | 按会话过滤 |
| `tool_name` | str | 按工具名 |
| `risk` | str | read/write/dangerous |
| `action` | str | allow/confirm/deny |
| `stage` | str | decision/resolved |
| `outcome` | str | executed/cancelled/rejected |
| `decided_by` | str | policy/override/human |
| `start` / `end` | str | created_at 区间（ISO） |
| `limit` | int | 默认 50，最大 500 |
| `offset` | int | 分页偏移 |

返回：
```json
{
  "total": 1234,
  "items": [
    {
      "id": 1, "created_at": "2026-10-04T09:47:38+00:00",
      "user_id": "u_abc", "session_id": "s_123", "tool_name": "computer_use.mouse_click",
      "risk": "write", "action": "confirm", "stage": "decision",
      "outcome": "confirm", "reason": "当前策略：写操作与危险操作需确认",
      "policy_mode": "confirm_write", "decided_by": "policy"
    }
  ]
}
```

### 4.2 聚合统计
`GET /api/permissions/audit/stats`

```json
{
  "by_action":   { "allow": 980, "confirm": 210, "deny": 44 },
  "by_risk":     { "read": 700, "write": 480, "dangerous": 54 },
  "by_decided_by": { "policy": 1180, "override": 30, "human": 24 },
  "top_tools":   [ {"tool_name": "file_write", "count": 320}, {"tool_name": "shell_exec", "count": 150} ],
  "timeline_24h":[ {"bucket": "2026-10-04T09:00", "count": 18}, ... ]
}
```

### 4.3 单条详情
`GET /api/permissions/audit/{id}` → 返回该记录完整字段。

> 读取同样走 `Database()` 单例；列表查询用参数化 SQL 拼 `WHERE`，避免注入（所有过滤值走 `?` 占位）。

---

## 5. 与既有约定的对齐清单

| 约定 | 本文做法 |
|---|---|
| 新表用 `CREATE TABLE IF NOT EXISTS`，旧库自动升级 | ✅ 表与索引均 `IF NOT EXISTS`，无迁移脚本 |
| 时间存带时区偏移的 UTC | ✅ `datetime.now(UTC).isoformat()`；前端用 `formatLocalTime()` 展示 |
| SQLite 单连接 + RLock + WAL + busy_timeout | ✅ 直接复用 `Database()`，不另起连接 |
| REST 用 `APIRouter` + `require_admin` + `ServiceRegistry` | ✅ 与 `permissions.py` 同构 |
| 管理员门禁（权限类接口） | ✅ 审计查看同样 `Depends(require_admin)` |

---

## 6. 性能与风险

- **热路径写入**：每次工具调用多 1 条 `INSERT`。单条写入 + WAL，开销可忽略；`busy_timeout=5000` 吸收偶发锁竞争。
- **表膨胀**：审计表会持续增长。建议（不在本方案强制实现，列为准入条件）：保留策略 `audit_retention_days`（默认 90 天），由定时任务或启动清理过期行。
- **list_tool_risks 陷阱**（已处理）：审计只落在真实执行路径（agent_loop）与人工确认收口，绝不进 `decide()` 体内。
- **PG 迁移**：列类型朴素、无 SQLite 专属语法，团队版换 PostgreSQL 时表定义原样可用（仅 `AUTOINCREMENT` → `GENERATED ALWAYS AS IDENTITY`）。

---

## 7. 验收标准

- [ ] 跑一次含 write/dangerous 工具的会话，数据库 `permission_audit` 出现对应 `stage='decision'` 记录，且 `decision.reason/policy_mode/decided_by` 正确。
- [ ] 触发一次 `confirm`，出现 `stage='resolved'` 且 `outcome` 为 `executed`（放行）或 `rejected`（拒绝）。
- [ ] `list_tool_risks()` 调用**不**产生审计行（验证陷阱已规避）。
- [ ] `GET /api/permissions/audit` 按 `user_id/tool_name/risk/action` 过滤与分页正确；`/stats` 聚合正确；均仅管理员可访问。
- [ ] 旧库（无该表）启动后自动建表，无报错。

---

## 8. 工作量估算

| 任务 | 人日 |
|---|---|
| `permission_audit` 表 + 索引（database.py） | 0.5 |
| `record_decision` 写入 + `Decision.source` 字段 | 0.5 |
| agent_loop 落库点 1 + 人工确认落库点 2 | 1 |
| 审计 REST（列表/统计/详情，`require_admin`） | 1.5 |
| 前端审计页（管理员视图，复用既有表格样式） | 1.5 |
| 测试（含 list_tool_risks 不写库的回归） + 验收 | 1.5 |
| **合计** | **≈ 6.5 PD** |

---

## 9. 与 EPIC 的关系

本文是 EPIC 文档「§3 文章能力对照 → 审计日志」条目的**首期实现切片**，也是团队版治理的基石：
- 现在就能上（单人/已开 auth 即可用）；
- 团队版做"部门隔离 + 三元 ACL"时，`permission_audit` 表只需加 `tenant_id/department_id` 两列即可复用，无需重建。
