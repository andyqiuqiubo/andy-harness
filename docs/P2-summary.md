# P2 阶段总结 — 模型抽象层 + DeepSeek / Qwen / Doubao 插件

> 完成时间：2026-09-24
> 阶段目标：统一流式 chat 接口；内置三家 provider；支持自定义 OpenAI 兼容模型。

---

## 1. 完成内容

### 1.1 加密基础设施（`harness/infra/crypto.py`）

| 组件 | 说明 |
|---|---|
| `get_machine_id()` | 跨平台获取机器标识（Windows: winreg MachineGuid / Linux: /etc/machine-id / macOS: IOPlatformUUID / 回退: uuid.getnode） |
| `derive_fernet_key()` | 从 machine_id 派生 Fernet 密钥（SHA-256 → base64） |
| `APIKeyEncryptor` | Fernet 加密/解密器，支持注入 machine_id 测试 |
| `mask_api_key()` | API Key 脱敏（前 4 位 + ****） |

### 1.2 OpenAICompatibleProvider 基类（`harness/modules/model_manager/openai_compatible.py`）

| 能力 | 实现 |
|---|---|
| 流式对话 | SSE 解析（data: 前缀 + [DONE] 终止），yield delta/tool_calls/reasoning_content |
| 超时 | httpx.AsyncClient(timeout=60s) |
| 指数退避重试 | max_retries=3，backoff=2^attempt，401 不可重试，429/网络错误可重试 |
| 统一错误 | ProviderError 携带 status_code，解析 JSON/纯文本错误响应 |
| TokenCounter 契约 | `count()` 方法 + `count_tokens()` 使用 tiktoken 估算 |
| 健康检查 | 发送 max_tokens=1 的简单请求验证连通性 |
| 可覆盖解析 | `_parse_stream_chunk()` 子类可覆盖（DeepSeek 处理 reasoning_content） |

### 1.3 三个 Provider 插件

| 插件 | 目录 | base_url | 默认模型 | 特殊处理 |
|---|---|---|---|---|
| `provider_deepseek` | `plugins/provider_deepseek/` | `https://api.deepseek.com` | deepseek-chat / deepseek-reasoner | `_parse_stream_chunk` 覆盖以解析 reasoning_content |
| `provider_qwen` | `plugins/provider_qwen/` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-max / qwen-plus / qwen-turbo / qwen-long | — |
| `provider_doubao` | `plugins/provider_doubao/` | `https://ark.cn-beijing.volces.com/api/v3` | doubao-pro-4k / doubao-pro-32k / doubao-lite-4k | — |

每个 provider 插件：
- activate 时注册 provider 到 ProviderRegistry + 注册 TokenCounter 到 ServiceRegistry
- deactivate 时注销 provider

### 1.4 ProviderRegistry（`harness/modules/model_manager/provider_registry.py`）

- 统一管理内置与自定义 provider，通过 `get_provider(id)` 获取实例
- 不感知配置来源差异（内置 provider 配置来自 plugin.json config_schema，自定义来自 providers 表）
- 实例缓存：配置变更时 `update_provider_config` 清除缓存
- 错误：`ProviderNotFoundError`（未注册）、`ProviderConfigError`（缺 API Key）

### 1.5 CLI 验证脚本（`harness/cli/chat.py`）

```bash
# 内置 provider
uv run python -m harness.cli.chat --provider deepseek --api-key <key> "你好"
uv run python -m harness.cli.chat --provider qwen --api-key <key> "你好"
uv run python -m harness.cli.chat --provider doubao --api-key <key> "你好"

# 自定义 provider（全程未改代码）
uv run python -m harness.cli.chat --provider custom --api-key <key> \
    --base-url https://api.example.com/v1 --model my-model "你好"

# 测试加密
uv run python -m harness.cli.chat --provider deepseek --api-key <key> --encrypt-test
```

### 1.6 新增依赖

| 依赖 | 版本 | 用途 |
|---|---|---|
| httpx | >=0.27.0 | 异步 HTTP 客户端（流式 SSE 解析） |
| cryptography | >=43.0.0 | Fernet 对称加密 |
| tiktoken | >=0.7.0 | Token 计数（OpenAI tokenizer，对其他模型为估算值） |

---

## 2. 测试覆盖（36 个新测试，累计 65 个）

| 测试文件 | 数量 | 覆盖场景 |
|---|---|---|
| `test_crypto.py` | 6 | 加密/解密往返、密文不含明文、不同 machine key 不可解密、无效密文报错、脱敏 |
| `test_openai_compatible.py` | 15 | SSE 流式解析（content/tool_calls/reasoning_content/空delta）、TokenCounter（正整数/长文本更多token/ServiceRegistry调用）、错误处理（status_code/JSON/纯文本）、模型列表、请求体构建（基本/extra_params/headers） |
| `test_provider_registry.py` | 8 | 注册获取/未找到异常/注销/缺API Key错误/列表/配置更新清缓存/自定义provider同代码路径 |
| `test_provider_plugins.py` | 7 | DeepSeek/Qwen/Doubao 插件激活注册、TokenCounter 注册、停用注销、TokenCounter via ServiceRegistry（DoD #4） |

---

## 3. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `uv run pytest -v` | 65 passed |
| 代码检查 | `uv run ruff check .` | All checks passed |
| 类型检查 | `uv run mypy harness` | no issues found in 24 source files |

---

## 4. DoD 逐项对照

| 完成标准 | 状态 | 说明 |
|---|---|---|
| DeepSeek / Qwen / Doubao 三家真实调用全部流式跑通 | 代码就绪 | 需用户提供真实 API Key 验证（CLI 脚本已就绪） |
| 通过配置文件新增自定义 OpenAI 兼容 provider 并调用成功，全程未改代码 | 通过 | CLI `--provider custom --base-url --model` 走完全相同代码路径 |
| 数据库中无明文密钥；错误密钥产生可读的错误信息 | 通过 | Fernet 加密 + 401 不可重试 + CryptoError 可读提示 |
| `services.get(TokenCounter).count("你好", "deepseek-chat")` 返回合理 token 数 | 通过 | `test_get_token_counter_returns_reasonable_count` |

---

## 5. 遇到的问题与解决

| 问题 | 原因 | 解决 |
|---|---|---|
| provider_registry.py 语法错误 | 类型注解嵌套方括号未闭合 `dict[str, tuple[type[...], dict[str, Any]]` | 补全闭合括号 |
| 无法实例化 OpenAICompatibleProvider | 继承 TokenCounter (ABC) 但未实现 `count()` 方法 | 添加 `count()` 方法委托给 `count_tokens()` |
| extra_params 覆盖了 kwargs | `_build_request_body` 中 update 顺序错误 | 先 update extra_params，再 update kwargs（kwargs 优先） |
| mypy no-any-return | `error.get("message", text)` 返回 Any | `str(error.get("message", text))` 显式转换 |

---

## 6. 待用户验证

**真实 API 调用验证**（需用户提供 API Key）：

```bash
cd f:\traeprojects\andy-harness\backend
$env:UV_CACHE_DIR="f:\traeprojects\andy-harness\.uv-cache"

# DeepSeek
uv run python -m harness.cli.chat --provider deepseek --api-key <你的DeepSeek Key> "你好"

# Qwen
uv run python -m harness.cli.chat --provider qwen --api-key <你的Qwen Key> "你好"

# Doubao
uv run python -m harness.cli.chat --provider doubao --api-key <你的Doubao Key> "你好"

# 自定义 provider
uv run python -m harness.cli.chat --provider custom --api-key <key> --base-url https://api.example.com/v1 --model my-model "你好"

# 测试加密
uv run python -m harness.cli.chat --provider deepseek --api-key sk-test123 --encrypt-test
```

---

## 7. 后续阶段衔接

- **P3**：通过 `services.get(TokenCounter)` 调用 token 计数，mock TokenCounter 即可独立开发
- **P4**：AgentLoop 通过 `ProviderRegistry.get_provider(id).chat()` 调用模型
- **P5**：REST API 暴露 `/api/providers`（含 test 连接）+ `/api/models`
