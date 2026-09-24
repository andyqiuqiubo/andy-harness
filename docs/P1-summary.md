# P1 阶段总结 — 内核：事件总线 / 服务注册表 / 插件加载器

> 完成时间：2026-09-24
> 阶段目标：最小内核可发现、加载、启停插件，三种通信机制可用，定义全部跨阶段共享的契约接口。

---

## 1. 完成内容

### 1.1 契约接口层（`kernel/contracts/`）

| 文件 | 定义内容 |
|---|---|
| `base.py` | `PluginType` 枚举、`PluginManifest` 数据类（含 `from_dict`/`to_dict`）、`BasePlugin` 抽象基类 |
| `hook.py` | `HookContext`（hook_name/data/session_id/metadata）、`HookResult`（data/short_circuit/error）、`HookHandler` 类型、`HookPlugin` |
| `token_counter.py` | `TokenCounter` ABC —— `count(text, model) -> int`，供 P2 实现和 P3 调用 |
| `provider.py` | `ModelProviderPlugin` ABC —— `chat`/`list_models`/`count_tokens`/`health_check` |
| `tool.py` | `ToolPlugin` ABC —— `tool_name`/`description`/`parameters_schema`/`execute` |
| `service.py` | `ServicePlugin` —— 继承 BasePlugin |
| `channel.py` | `ChannelPlugin` —— 继承 BasePlugin |

### 1.2 内核组件（`kernel/`）

| 文件 | 组件 | 核心能力 |
|---|---|---|
| `eventbus.py` | `EventBus` | 异步 pub/sub、通配符主题（fnmatch）、按 owner 批量注销、sync+async 处理器兼容、异常隔离 |
| `services.py` | `ServiceRegistry` | 按接口注册/解析、**后注册覆盖策略**（栈式管理，deactivate 后自动回退）、按 owner 批量注销 |
| `hooks.py` | `HookManager` | 有序管线、前一个输出作为后一个输入、`short_circuit` 短路终止、按 owner 批量注销 |
| `context.py` | `PluginContext` | 受限门面（logger/config/secrets/storage/events/services/hooks）；`PluginConfig`、`SecretStore`、`PluginStorage` |
| `loader.py` | `PluginLoader` | discover→validate→load→activate/deactivate→unload；**核心插件保护**（`core: true` 不可停用）；core_api 语义化版本检查；一键 `load_and_activate_all` |
| `exceptions.py` | 异常体系 | `KernelError`/`PluginError`/`PluginValidationError`/`PluginLoadError`/`PluginNotLoadedError`/`PluginAlreadyLoadedError`/`PluginDeactivateError`/`ServiceUnavailable` |

### 1.3 示例插件（`plugins/hello_plugin/`）

- `plugin.json`：id=`hello_plugin`，type=`service`，entry=`plugins.hello_plugin.main:HelloPlugin`
- `main.py`：`HelloPlugin` —— activate 时注册 `HelloService` + 订阅 `message.created` 事件 + 注册 `pre_model_call` hook + 发布 `plugin.activated` 事件
- `HelloService`：提供 `greet(name)` 方法

### 1.4 测试覆盖（29 个测试用例）

| 测试文件 | 数量 | 覆盖场景 |
|---|---|---|
| `test_eventbus.py` | 5 | 精确订阅/通配符订阅/注销/按 owner 批量注销/异常隔离 |
| `test_service_registry.py` | 6 | 注册获取/ServiceUnavailable/后注册覆盖/注销回退/按 owner 批量注销/has 检查 |
| `test_hook_manager.py` | 5 | 单钩子执行/多钩子顺序/短路终止/按 owner 批量注销/无钩子返回原始数据 |
| `test_plugin_loader.py` | 12 | 清单校验（合法/缺字段/无效type/版本不兼容/core标记）+ 集成测试（加载激活/服务可用/停用后服务不可用/事件自动注销/核心插件不可停用）+ 服务覆盖策略 + TokenCounter mock |
| `test_health.py` | 1 | P0 健康检查（回归） |

---

## 2. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `uv run pytest -v` | 29 passed |
| 代码检查 | `uv run ruff check .` | All checks passed |
| 类型检查 | `uv run mypy harness` | no issues found in 17 source files |

---

## 3. DoD 逐项对照

| 完成标准 | 状态 | 验证方式 |
|---|---|---|
| 加载 hello_plugin → 另一插件经 ServiceRegistry 调用其服务 | 通过 | `test_service_available_after_activate` |
| 事件跨插件送达 | 通过 | `test_load_and_activate_hello_plugin`（activate 时发布 plugin.activated 事件） |
| 停用后服务调用报 ServiceUnavailable 且事件订阅自动注销 | 通过 | `test_service_unavailable_after_deactivate` + `test_event_unsubscribed_after_deactivate` |
| 两个插件注册同一服务接口，后注册者覆盖前者；停用后者后自动回退 | 通过 | `test_two_plugins_same_service` |
| Hook 管线：多个 hook 依次执行，前一个输出作为后一个输入；short_circuit 终止管线 | 通过 | `test_multiple_hooks_in_order` + `test_short_circuit` |
| core: true 插件调用 deactivate 被拒绝并返回明确错误 | 通过 | `test_core_plugin_cannot_deactivate` |
| manifest 缺字段/版本不兼容时加载被拒绝并给出明确错误 | 通过 | `test_missing_required_field` + `test_incompatible_core_api` |
| TokenCounter 契约接口可被 mock 实现并通过 ServiceRegistry 调用 | 通过 | `test_mock_token_counter` |

---

## 4. 遇到的问题与解决

| 问题 | 原因 | 解决 |
|---|---|---|
| HookPlugin 导入失败 | `__init__.py` 导入了 hook.py 中未定义的 HookPlugin | 在 hook.py 中 import BasePlugin 并定义 HookPlugin |
| 插件加载失败：No module named 'main' | manifest id 用连字符 `hello-plugin`，目录用下划线 `hello_plugin`，路径不匹配 | 统一使用下划线 `hello_plugin` |
| EventBus unsubscribe 无效 | `_sub_topics` 仅在有 owner 时写入，无 owner 的订阅无法注销 | 所有订阅都写入 `_sub_topics` |
| Python 双重导入导致 HelloService 类不匹配 | loader 用 sys.path + `import main` 加载插件，测试用 `from plugins.hello_plugin.main import` 导致同一类被加载两次 | 改用全路径 entry `plugins.hello_plugin.main:HelloPlugin`，loader 直接 importlib 全路径 |
| ruff N818: ServiceUnavailable 命名 | 架构文档定义的名称无 Error 后缀 | 保留原名，加 `# noqa: N818` |
| mypy no-any-return | `getattr(module, class_name)()` 返回 Any | 显式类型标注 `plugin_instance: BasePlugin` |

---

## 5. 架构关键决策

1. **EventBus 支持 sync+async 处理器**：使用 `inspect.isawaitable` 统一处理，降低使用门槛
2. **ServiceRegistry 栈式覆盖**：每个接口维护实现栈，后注册压栈顶，注销时 pop 自动回退，天然支持「替换内置插件」场景
3. **插件 entry 使用全路径**：`plugins.hello_plugin.main:HelloPlugin` 避免双重导入问题
4. **HookContext.data 类型延迟到 P4 定义**：P1 定义基础结构（HookContext/HookResult），P4 定义各钩子点的具体 data 类型

---

## 6. 后续阶段衔接

- **P2**：实现 `ModelProviderPlugin` 契约 + `TokenCounter` 契约，注册到 ServiceRegistry
- **P3**：通过 `services.get(TokenCounter)` 调用计数，mock 即可独立开发
- **P4**：基于 P1 的 `HookContext`/`HookResult` 定义各钩子点的具体 data 类型
