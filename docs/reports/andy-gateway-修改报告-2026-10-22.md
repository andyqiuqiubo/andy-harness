# andy-gateway 修改报告

| 项 | 内容 |
|----|------|
| 报告日期 | 2026-10-22 |
| 依据 | [docs/reports/andy-gateway-代码体检报告-2026-10-22.md](andy-gateway-代码体检报告-2026-10-22.md)（G-01 ~ G-20） |
| 结果 | **修复 18 项**（P1 全部 6 项 + P2 全部 9 项 + P3 中 3 项），另在修复过程中新发现并修复 1 项致命缺陷（G-21）；剩余 2 项列入后续（DNS 级 SSRF、前端面板） |

---

## 1. 修复明细（按体检编号）

| # | 级别 | 修复内容 | 主要文件 |
|---|------|----------|----------|
| G-01 | P1 | 凭据解析（含 Cline token 刷新的网络 I/O）改由 `asyncio.to_thread` 执行；删除写回重试里的阻塞 `sleep` | `service.py`、`credentials.py` |
| G-02 | P1 | 登录态读取统一走 `_load_json`/`_node`：文件缺失、无权限、JSON 损坏、结构异常一律转 `GatewayError`（502），不再泄漏 500/KeyError | `credentials.py` |
| G-03 | P1 | 流式透传新增 **SSE keepalive**：等待上游首字节超 `KEEPALIVE_SECONDS`（默认 15s）发 `: keepalive`，不再让客户端判超时 | `service.py` |
| G-04 | P1 | 流式中断区分 `CLIENT_ABORTED`(499) / `UPSTREAM_STREAM_ERROR`(502)，且**中断即降健康度**（冷却 60s），持续故障可熔断 | `service.py` |
| G-05 | P1 | 配置写入口改为「先全量校验、后落盘」：`ConfigService.validate_payload()` 不落盘预检；`save()` 补齐重复 id / 私网地址 / 空模型 / weight 范围校验并捕获 `OSError`；非法配置**磁盘保持不变** | `config.py`、`service.py` |
| G-06 | P1 | `start()` 幂等（重复调用直接返回）；`stop()` 取消并等待配置监视器、探活器，并等待未完成的 span 任务 | `service.py` |
| G-07 | P2 | `issue_key` 增加租户存在性与启用校验（不存在/停用抛 `ValueError`） | `auth.py`、`api/admin.py` |
| G-08 | P2 | 重复租户创建返回 **409 TENANT_EXISTS** | `api/admin.py` |
| G-09 | P2 | 数据面新增 `messages` 非空校验（400）、8MB 请求体上限（413）、`ConfigError` → 503、429/503 响应带 `Retry-After` | `api/openai_compat.py` |
| G-10 | P2 | `/v1/models` 别名按租户成员过滤（任一成员可见即列出，路由阶段仍按成员收敛）；span 发布任务改为**强引用集合**持有，避免被 GC | `service.py` |
| G-11 | P2 | `channel.strategy` 生效：候选全部属于同一通道时使用该通道策略，否则用默认策略 | `service.py` |
| G-12 | P2 | `Router._expand/_tenant_allows` 改为公开 `expand/tenant_allows`，消除跨模块私有调用 | `router.py`、`service.py` |
| G-13 | P2 | 新增 `gw_usage` 保留清理：`usage_retention_days`（默认 90 天）启动时 `prune_usage` | `store.py`、`config.py`、`service.py` |
| G-14 | P2 | 新增**主动健康探测**：Channel 可配 `probe_url`/`probe_interval_seconds`，后台任务周期性探测，失败熔断、成功恢复；三个真实通道已配置探测地址 | `config.py`、`adapters/openai_like.py`、`service.py` |
| G-15 | P2 | 非流式聚合按配置 `read_timeout_seconds` 加 `asyncio.wait_for` 超时（504 可转移），不再只依赖客户端级超时 | `service.py` |
| G-16 | P2 | 凭据池按权重 SWRR 排序（多 `credentials` 时首个可用者被选中），并向上抛出真实失败原因 | `credentials.py` |
| G-17 | P3 | 删除 `adapters/base.py` 死代码（全局注册表） | `adapters/base.py`、`adapters/__init__.py` |
| G-18 | P3 | 鉴权测试改用 `tmp_path` 临时配置，不再依赖真实 `backend/data/gateway.json`；同时把 `main.py` 的引导 Key 落盘条件加严（`PYTEST_CURRENT_TEST` **或** `HARNESS_DB_PATH` 时跳过），测试不再覆写真实 Key 文件；真实库已重置并重新生成引导 Key（前缀 `agw-TT18CiQm`，明文在 `backend/data/gateway.bootstrap-key`） | `tests/test_gateway_core.py`、`harness/main.py` |
| G-19 | P3 | 通道模板统一为 `backend/gateway.example.json`（已含探测地址与保留策略），`tools/gateway.providers.json` 双份配置废止 | `backend/gateway.example.json` |
| G-20 | P3 | 测试由 7 项增至 **13 项**：流式透传+心跳+usage、非法配置不落盘+回滚、`messages` 校验、租户校验、别名按租户过滤、凭据文件缺失、多模型/别名租户过滤 | `tests/test_gateway_core.py` |

## 2. 修复过程中新发现的致命缺陷（G-21）

**现象**：按初版方案给 `_stream()` 加心跳时，用 `asyncio.wait_for(lines.__anext__(), timeout=15)` 实现；新增的流式回归测试立刻暴露：**整个流只输出 `: keepalive`，一条数据都没有**。

**根因**：`asyncio.wait_for` 超时会 **cancel** 被包裹的 `__anext__()`；对 async generator 而言，这次取消会把生成器关掉，后续再无数据——即"心跳把流掐死"。

**修复**：改为「取下一行任务 + 心跳定时任务」竞争模式（`asyncio.wait(..., FIRST_COMPLETED)`），超时只发注释、**不取消取行任务**，并在 `finally` 中清理未完成的 pending 任务。修复后测试同时断言 `keepalive` 与 `data: [DONE]` 均出现。

> 该缺陷若进入生产，表现为"流式请求全部只收到心跳、最终超时"，属 P0 级。这也是本次补流式回归测试的直接收益。

## 3. 验证结果

| 验证项 | 结果 |
|--------|------|
| `ruff check`（gateway 模块 + 测试 + `main.py`） | 全部通过 |
| `mypy`（gateway 模块） | 通过（14 个源文件） |
| 网关单测 `tests/test_gateway_core.py` | **13 passed**（原 7 项，新增 6 项） |
| 真实上游端到端（非流式） | `workbuddy/hy3` → `OK.`；`cline/cline-free/minimax-m3.1-flash` → `OK`；`zen/big-pickle` → `OK` |
| 真实上游端到端（流式） | `workbuddy/hy3` 93 个 SSE 分片，usage 正常落库 |
| 用量落库 | 4 行记录，status 全为 200 |
| 优雅降级 | 配置文件损坏时 `/api/health` 200、`/v1` 与 `/api/gateway/*` 返回 503（此前会拖垮启动） |
| 全量测试 `pytest tests` | **1433 passed, 5 skipped**（8 分 49 秒，日志 `logs/pytest-final3.log`；此后仅追加 `main.py` 的一处测试隔离条件，经 19 项应用级测试复验不影响） |

## 4. 变更文件清单

```
backend/harness/modules/gateway/
├── service.py        # 重写：心跳、错误码、健康降级、探活、幂等、配置校验前移、线程卸载
├── credentials.py    # 安全读取、无阻塞重试、凭据权重排序
├── config.py         # validate_payload、save 前全量校验、probe_url/保留天数字段
├── auth.py           # 租户校验、限流器清理
├── router.py         # expand/tenant_allows 公开
├── store.py          # prune_usage
├── adapters/base.py  # 去死代码
├── adapters/openai_like.py  # probe_url 支持
└── api/{openai_compat,admin}.py  # 输入校验、Retry-After、409/400、ConfigError→503
backend/gateway.example.json   # 增加探测地址与保留策略
backend/tests/test_gateway_core.py  # 7 → 13 项
```

## 5. 未修复与后续

| 项 | 原因 | 计划 |
|----|------|------|
| DNS 级 SSRF 防护（`net_guard` 接入） | 需要在解析期做 DNS 检查并处理 rebinding，涉及每次请求的开销权衡；当前已拦截字面量私网/localhost | P2，与 `net_guard` 统一方案一起做 |
| 前端 Gateway 面板 | 属于 P3 体验项 | 随 Settings 页面排期 |
| Qoder CN COSY 适配器 | 需移植 AES+RSA+MD5 信封 | P4 首个适配器任务 |
| `gw_usage` 异步写仍走事件循环线程 | 单节点本地场景可接受；高并发需 `asyncio.to_thread` 批量落库 | 视压测结果再定 |

## 6. 结论

体检发现的 6 个 P1 问题全部修复并经测试/真实调用验证；修复过程中暴露的 G-21（心跳掐断流）已被回归测试永久看守。模块当前达到"可长期自托管运行"的最低稳定性要求；对外公开发布前建议完成 DNS 级 SSRF 与前端面板两项。
