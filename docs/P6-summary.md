# P6 阶段总结 — 前端 MVP：对话界面与前端插件内核

> 完成时间：2026-09-24
> 阶段目标：浏览器内完成完整聊天体验；前端插件机制可用。

---

## 1. 完成内容

### 1.1 前端插件内核（`src/core/`）

| 文件 | 内容 |
|---|---|
| `plugin-types.ts` | `UIPluginManifest`、`PluginView`、`PluginMenuItem`、`UIPluginContext`、`EventBusBridge`、`APIClient`、`PluginRouter`、`PluginStoreRegistry` 接口定义 |
| `event-bus-bridge.ts` | `EventBusBridgeImpl` 事件总线桥接实现，导出单例 `eventBus`，支持 on/off/emit/clear |

### 1.2 API 客户端（`src/api/`）

| 文件 | 内容 |
|---|---|
| `client.ts` | `APIClientImpl`：REST（GET/POST/PATCH/DELETE）+ WebSocket（connect/disconnect/send/onMessage + 3 秒断线重连） |
| `types.ts` | `Session`、`Message`、`ToolCall`、`Provider`、`Model`、`WSFrame` 类型定义 |

### 1.3 Pinia Stores（`src/stores/`）

| Store | 状态 | 方法 |
|---|---|---|
| `chat.ts` | sessions / currentSessionId / messages / streamingContent / toolEvents / isStreaming / contextSnapshot / error | loadSessions / createSession / selectSession / renameSession / archiveSession / handleWSFrame / sendMessage / stopStreaming |
| `providers.ts` | providers / models | loadProviders / loadModels |

### 1.4 Chat 页面组件

| 组件 | 文件 | 功能 |
|---|---|---|
| `SessionSidebar.vue` | `components/` | 会话侧栏：新建/切换/重命名/归档 |
| `MessageItem.vue` | `components/` | 消息项：按角色渲染 + Markdown + 代码高亮 + 工具调用折叠卡片 |
| `MarkdownRenderer.vue` | `components/` | Markdown 渲染（markdown-it + highlight.js 代码高亮） |
| `ModelSelector.vue` | `components/` | 模型选择器（按 provider 分组的 optgroup） |
| `ChatView.vue` | `views/` | 主聊天视图：侧栏 + 消息列表 + 流式渲染 + 工具调用可视化 + 上下文快照 + 错误提示 + 停止生成 |
| `HomeView.vue` | `views/` | 首页：项目名 + "开始对话" 链接 |

### 1.5 流式渲染与交互

- **流式渲染**：WS `token_delta` 帧实时追加到 `streamingContent`，带光标 ▌
- **停止生成**：`stopStreaming()` 按钮，将已生成内容标记 `[已中断]` 并存入消息列表
- **工具调用可视化**：`tool_event` 帧以折叠卡片展示（工具名/参数/结果/错误）
- **上下文快照**：`context_snapshot` 帧展示 token 用量
- **错误提示**：`error` 帧展示错误信息横幅
- **WS 断线重连**：3 秒自动重连，连接状态控制台日志

### 1.6 示例前端插件（`src/plugins/hello/`）

- `ui-plugin.json`：id=`hello-ui-plugin`，contributes 一个 view（`/hello`）和一个 menu_item
- `HelloView.vue`：验证前端插件机制的视图组件
- 已注册到路由，访问 `http://localhost:5173/hello` 可验证

---

## 2. 新增依赖

| 依赖 | 版本 | 用途 |
|---|---|---|
| markdown-it | 15.0.2 | Markdown 解析渲染 |
| highlight.js | 11.12.0 | 代码语法高亮 |
| @types/markdown-it | 14.2.0 | markdown-it 类型定义 |

---

## 3. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 前端 lint | `pnpm lint` | no issues |
| 前端 build | `pnpm build` | built successfully（270 modules, 1.1MB） |
| 后端测试（回归） | `uv run pytest -v` | 126 passed |

---

## 4. DoD 逐项对照

| 完成标准 | 状态 | 说明 |
|---|---|---|
| 浏览器完成：新建会话 → 提问 → 流式渲染 → 触发工具调用可见 → 切换会话历史正确 → 刷新页面后数据仍在 | 代码就绪 | 需启动前后端 + API Key 验证完整流程；UI 组件和 WS 连接已就绪 |
| 中断生成、WS 断线重连均有明确 UI 反馈 | 通过 | stopStreaming 按钮 + WS 3 秒自动重连 + 控制台日志 |
| 示例前端插件的视图出现在侧栏，停用后消失、重新启用后恢复 | 部分通过 | Hello 插件视图已注册到路由可访问；动态启停需前端插件加载器实现（当前为静态路由注册） |

---

## 5. 待用户验证

启动前后端后可访问：

```bash
# 终端 1：后端
cd f:\traeprojects\andy-harness\backend
$env:UV_CACHE_DIR="f:\traeprojects\andy-harness\.uv-cache"
uv run uvicorn harness.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2：前端
cd f:\traeprojects\andy-harness\frontend
pnpm dev
```

- http://localhost:5173/ — 首页（项目名 + "开始对话"链接）
- http://localhost:5173/chat — 聊天界面（会话侧栏 + 消息列表 + 输入框 + 模型选择器）
- http://localhost:5173/hello — Hello 插件示例视图
- http://localhost:8000/docs — Swagger UI（REST API 调试）

---

## 6. 后续阶段衔接

- **P7**：设置页（provider CRUD + 测试连接 + 插件管理 UI + 主题/语言切换）
- **P8**：沙箱管理（tool-code-runner 通过 ToolRegistry 注册，前端展示执行过程）
