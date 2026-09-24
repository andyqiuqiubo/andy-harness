# P4 阶段总结 — Agent 引擎

> 完成时间：2026-09-24
> 阶段目标：核心循环打通，支持工具调用与钩子拦截。

---

## 1. 完成内容

### 1.1 钩子点 data 类型（`harness/engine/hook_types.py`）

基于 P1 的 `HookContext` / `HookResult` 基础结构，定义各钩子点的具体 data 类型：

| 钩子点 | data 类型 | 关键字段 |
|---|---|---|
| pre/post_context_build | `BuildContext` | session_id, budget, model, messages |
| pre/post_model_call | `ModelRequest` | messages, model, params, response, tool_calls |
| pre/post_tool_call | `ToolCallContext` | tool_name, args, result, error |
| pre_message_persist | `MessageRecord` | session_id, role, content, tool_calls, tokens, latency_ms |

### 1.2 工具注册表（`harness/engine/tool_registry.py`）

- `ToolRegistry`：管理 tool_name → tool 映射
- register / unregister / unregister_all(owner) / get / has / list_tools / get_tool_definitions
- `get_tool_definitions()` 生成 function calling 格式的工具定义列表
- `ToolNotFoundError` 异常

### 1.3 AgentLoop（`harness/engine/agent_loop.py`）

核心 ReAct 风格循环：

```
用户输入 → persist_message(user)
  循环（最多 max_tool_iterations 次）:
    1. ContextService.build() → 装配上下文
    2. hooks.pre_model_call → Provider.chat(stream) → 解析 content + tool_calls → hooks.post_model_call
    3. 若无 tool_calls → 终答，跳出循环
    4. 若有 tool_calls:
       a. hooks.pre_tool_call → ToolRegistry.execute → hooks.post_tool_call
       b. 回填 tool 结果到会话消息
       c. 回到步骤 1
  persist_message(assistant)
```

| 能力 | 实现 |
|---|---|
| 钩子管线全量接入 | 7 个钩子点全部接入，支持改写数据与短路 |
| 单轮最大工具迭代数保护 | `AgentLoopConfig.max_tool_iterations`（默认 10），超限后停止 |
| 停止/中断 | `loop.stop()` 设置 `_stopped` 标志，循环中检查 |
| 模型失败重试与降级 | 通过 Provider 层指数退避重试，AgentLoop 层捕获异常不崩溃 |
| 工具异常捕获 | 工具未找到/执行异常被捕获，作为 tool 消息回填，不导致循环崩溃 |
| AgentLoopResult | content / tool_calls_made / iterations / latency_ms / error / short_circuited |

### 1.4 内置示例工具（`harness/engine/builtin_tools.py`）

| 工具 | tool_name | 功能 |
|---|---|---|
| `CalculatorTool` | `calculator` | 安全解析数学表达式（AST + 安全运算符白名单），支持加减乘除幂取模 |
| `CurrentTimeTool` | `current_time` | 获取当前日期时间 |
| `BuiltinToolsPlugin` | — | 插件形式注册上述两个工具到 ToolRegistry |

---

## 2. 测试覆盖（26 个新测试，累计 113 个）

| 测试文件 | 数量 | 覆盖场景 |
|---|---|---|
| `test_builtin_tools.py` | 10 | 计算器（加法/乘法 123*456=56088/复杂表达式/幂/除零/无效/空表达式）+ 当前时间 |
| `test_tool_registry.py` | 6 | 注册获取/未找到异常/注销/按 owner 批量注销/列表/工具定义列表 |
| `test_agent_loop.py` | 10 | 简单对话/工具调用计算/连续两工具/钩子改写请求/钩子短路/工具未找到/最大迭代保护/工具执行异常捕获 |

---

## 3. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `uv run pytest -v` | 113 passed |
| 代码检查 | `uv run ruff check .` | All checks passed |
| 类型检查 | `uv run mypy harness` | no issues found in 35 source files |

---

## 4. DoD 逐项对照

| 完成标准 | 状态 | 验证方式 |
|---|---|---|
| CLI 提问「现在几点？顺便算一下 123*456」→ agent 自动连续调用两个工具并给出终答 | 通过 | `test_two_tools_in_sequence`（MockProvider 模拟两轮工具调用 + 终答，验证 tool_calls_made 包含 current_time 和 calculator，计算结果 56088） |
| 编写一个 hook 插件改写模型请求（如追加系统提示），测试证明其生效 | 通过 | `test_pre_model_call_hook_modifies_request`（pre_model_call 钩子修改 model 名称，对话仍正常完成） |
| 工具异常被捕获并作为 tool 消息回填，不导致循环崩溃 | 通过 | `test_tool_not_found` + `test_tool_exception_captured`（工具未找到/工具执行异常均被捕获，循环继续执行） |
| hook 返回 `short_circuit=True` 时，Agent Loop 正确终止并返回错误信息 | 通过 | `test_hook_short_circuit`（pre_model_call 短路 → result.error 包含"被钩子拦截"） |

---

## 5. 遇到的问题与解决

| 问题 | 原因 | 解决 |
|---|---|---|
| ruff E501 测试中 JSON 长行超 120 字符 | tool_calls 嵌套 JSON 较长 | 添加 `# noqa: E501` |
| mypy `Cannot call function of unknown type` | `_SAFE_OPERATORS` 字典值类型为 Any | 类型标注为 `dict[type, Callable[..., Any]]` |
| mypy `Returning Any` | `context_service.build()` 返回 Any | 使用 `cast("list[dict[str, Any]]", ...)` 显式转换 |

---

## 6. 后续阶段衔接

- **P5**：REST API + WebSocket 暴露 AgentLoop 能力，WS `/ws/chat` 推流 token_delta / tool_event / context_snapshot / done
- **P6**：前端调用 WS 实现流式渲染 + 工具调用过程可视化
- **P8**：tool-code-runner 插件通过 ToolRegistry 注册，与 calculator/current_time 走相同代码路径
