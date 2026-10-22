# andy-gateway 代码体检报告

| 项 | 内容 |
|----|------|
| 报告日期 | 2026-10-22 |
| 体检对象 | `backend/harness/modules/gateway/`（14 个文件，约 2,500 行）+ `main.py` 装配 + `backend/tests/test_gateway_core.py` + `backend/gateway.example.json` |
| 体检方法 | 全量人工代码走查；`ruff check`、`mypy`（均通过）；`pytest tests` 全量（1427 passed / 5 skipped）；真实上游端到端调用（Cline / WorkBuddy / Zen 各一次） |
| 对应修改报告 | [docs/reports/andy-gateway-修改报告-2026-10-22.md](andy-gateway-修改报告-2026-10-22.md) |
| 结论 | **功能主链路可用，但存在 6 个 P1（生产可用性）问题与 9 个 P2 问题，不建议直接对外发布**。P1 集中在事件循环阻塞、异常类型泄漏、流式运维缺失三处。 |

---

## 1. 问题总览

| # | 级别 | 位置 | 问题 | 影响 |
|---|------|------|------|------|
| G-01 | P1 | `credentials.py:83,110-120` + `service.py:271` | 凭据解析在事件循环内**同步**执行：Cline token 刷新走 `httpx.post`（阻塞网络 I/O），写回重试用 `time.sleep(0.2)` | 刷新期间（可达数秒）**整个后端事件循环被阻塞**，所有会话/其他 API 全部卡住 |
| G-02 | P1 | `credentials.py:63,75,98,131` | 登录态文件读取未捕获 `FileNotFoundError`/`JSONDecodeError`/`KeyError` | Cline/Magpie 文件缺失或损坏时抛非 `GatewayError` 异常 → 数据面返回 **500 而非 502/503**，且可能带出堆栈 |
| G-03 | P1 | `service.py:432-444` | 流式透传**无 SSE 心跳** | Cline/Zen 首 token 30–120s 时客户端/反向代理判超时断连（设计 §4.3.6 明确要求心跳） |
| G-04 | P1 | `service.py:448-450,456-457,467` | 流式中断统一记 `CLIENT_ABORTED`，且**不降健康度** | 502 与 499 无法区分；上游连续中断也不会熔断，故障被淹没 |
| G-05 | P1 | `service.py:182-190` + `config.py:190-202` | `apply_config` **先写盘后校验**；`save()` 未做 `load()` 的全量校验（重复 id/私网地址/空 models） | 非法配置可落盘污染 `gateway.json`，下次启动/热更新才失败；审计记录"成功"但现网未变 |
| G-06 | P1 | `service.py:76-96` | `start()` **不幂等**（重复调用会再起一个用量写任务与配置监视器）；`stop()` 不等待 span 任务 | 测试/重载场景下任务泄漏，日志出现多条 writer |
| G-07 | P2 | `auth.py:101-132` | `issue_key` 不校验租户存在/启用 | 可给不存在租户签 Key，`verify` 时静默失效（排障困难） |
| G-08 | P2 | `api/admin.py:182-198` | `create_tenant` 重复 id 未捕获 | 返回 500 而非 409 |
| G-09 | P2 | `api/openai_compat.py:62-84` | 未校验 `messages`；无请求体大小上限；`ConfigError` 未转换；429/503 无 `Retry-After` | 畸形请求直接打到上游；大 body 可打爆内存；客户端无法感知退避 |
| G-10 | P2 | `service.py:134-150,551` | `models()` 别名不按租户过滤；`_emit_span` 用 `asyncio.create_task` **无强引用** | 越权可见性泄露（信息面）；span 任务可能被 GC 丢弃 |
| G-11 | P2 | `service.py:243-250` | `channel.strategy` 配置**未生效**（只用 defaults.strategy） | 单通道多模型场景无法按通道调策略，配置项失真 |
| G-12 | P2 | `router.py:130-144` → `service.py:136,140` | 跨模块调用私有方法 `Router._tenant_allows` | 封装破坏，重构易漏改 |
| G-13 | P2 | `store.py` 全文 | 无用量保留清理 | `gw_usage` 无界增长（10 万行/天量级） |
| G-14 | P2 | 全模块 | **无主动健康探测**（设计 §4.3.4 要求） | 上游整体挂掉时只能等真实请求失败，首请求必超时 |
| G-15 | P2 | `service.py:80-84` | `httpx.AsyncClient` 的 read timeout 在启动时固定，配置热更新后不重建 | 改 `read_timeout_seconds` 不生效 |
| G-16 | P2 | `credentials.py:130-139` | 多凭据仅取 `credentials[0]`，未按权重/策略排序 | 设计 §4.4 凭据池未落地 |
| G-17 | P3 | `adapters/base.py:35-49` | `register_adapter`/`global_registry` 死代码 | 无行为影响，增加认知面 |
| G-18 | P3 | `test_gateway_core.py:44` | 鉴权测试依赖真实 `backend/data/gateway.json` | 测试隔离性差，CI/他人环境可能失败 |
| G-19 | P3 | `tools/andy_gateway_lite.py` + `tools/gateway.providers.json` | 诊断工具与 `backend/gateway.example.json` **双份配置** | 模型清单漂移风险 |
| G-20 | P3 | 测试覆盖 | 缺流式透传/心跳、`apply_config`/rollback、`messages` 校验、租户校验、别名过滤用例 | 回归保护不足 |

---

## 2. 关键问题详述

### G-01 事件循环阻塞（最严重）

`service.chat()` 在异步路径上直接调用 `self.credentials.resolve(channel)`（service.py:271）。该调用链上：

- `ClineTokenManager._refresh()` 使用同步 `httpx.post`（credentials.py:83），token 到期刷新时网络 RTT 完全阻塞事件循环；
- 写回重试 `time.sleep(0.2)`（credentials.py:114-120）同样阻塞。

复现路径：Cline access token 过期后第一个请求触发刷新 → 期间所有 WebSocket、其他会话、前端请求全部挂起数百毫秒到数秒。

### G-02 异常类型泄漏

`_load_json()` 对文件不存在/JSON 损坏直接抛 `OSError`/`JSONDecodeError`；字典取值 `["providers"]["cline"]` 对结构变化抛 `KeyError`。这些都不是 `GatewayError`，数据面只捕获 `GatewayError`，最终以 500 + 内部错误返回。

### G-03/G-04 流式运维缺失

`_stream()` 用 `async for line in response.aiter_lines()` 逐行透传，等待期间不发任何字节。设计文档要求“等待首字节 >15s 时每 10s 发 `: keepalive`”。同时，`except httpx.HTTPError: status = 502` 后 finally 里 `error_code` 恒为 `CLIENT_ABORTED`（service.py:467），且 `status != 200` 时只跳过 `mark_success`，从不 `mark_failure`，导致上游持续 502 也不熔断。

### G-05 配置先写后校验

`GatewayService.apply_config()` → `ConfigService.save()` 只做 `GatewayConfig(**payload)` 的 pydantic 校验就 `os.replace` 落盘，随后 `load()` 才做重复 id / 私网地址 / 空模型检查。一旦 `load()` 失败，磁盘上已是坏配置，而版本表未记录、审计已写“config.apply”。下次启动同样失败（虽有 best-effort 降级，但用户看到的是“配置改了但网关起不来”）。

---

## 3. 测试缺口清单

| 缺口 | 应补用例 |
|------|----------|
| 流式透传 | MockTransport 返回 SSE，断言逐字节透传 + 客户端断开后 usage 落库 + 心跳 |
| 配置写入口 | 非法 payload（重复 id / 私网地址）→ 400 且**文件未被修改**；合法 payload → 版本 +1 |
| 回滚 | 写入 v2 → rollback 到 v1 → 快照内容一致 |
| 输入校验 | 缺 `messages` → 400；`messages` 非列表 → 400 |
| 租户隔离 | 给不存在租户签 Key → 400；别名按租户过滤 |
| 凭据健壮性 | Cline 文件缺失 → `GatewayError`（不抛 FileNotFoundError） |
| 测试隔离 | 鉴权测试改用临时配置，不依赖 `backend/data/gateway.json` |

---

## 4. 修复优先级建议

1. **第一批（必须）**：G-01、G-02、G-05、G-09（阻塞/泄漏/落盘/输入）；
2. **第二批（应做）**：G-03、G-04、G-06、G-07、G-08、G-10、G-12、G-13、G-14；
3. **第三批（改进）**：G-11、G-15、G-16、G-17、G-18、G-19、G-20 与文档同步。

---

## 5. 体检结论

模块在“功能正确性”上达到可演示水平（三家真实上游均已跑通，全量测试通过），但在**事件循环卫生、异常边界、流式运维、配置写入安全**四个方面距离“可长期运行”有明确差距。按 §4 优先级修复后，可满足个人/小团队自用的稳定性要求；G-14（主动探活）与 G-13（保留清理）建议在公开部署前完成。
