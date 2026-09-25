# Changelog

本项目的所有重要变更都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

### 新增

#### Jev 结构化决策
- Jev Manager 服务插件（jev_manager）：封装 TypeSafe AI Jev API，支持三种原语
  - Choice（选择型）：从固定选项中选一个，返回概率分布和置信度
  - Score（评分型）：在自定义等级上打分，返回概率分布和置信度
  - Noul（是非型）：判断命题是否成立，返回 0~1 概率值
- 三种原语可混合并行提问，独立计算、同时返回
- 插件配置 schema（api_key + base_url），支持设置页面配置
- 环境变量 JEV_API_KEY 回退支持
- 未配置 API Key 时插件仍可激活，调用工具时返回友好错误提示

#### Provider 状态管理增强
- Provider 列表返回 `enabled` 字段，前端可实时显示启用/停用状态
- ProviderUpdate 接口新增 `enabled` 字段，支持通过 API 启用/停用 Provider
- Provider 启用前自动测试连接，连接失败则不启用并显示错误原因
- 停用 Provider 后：模型从模型选择器隐藏、Token 计数跳过、聊天不可调用
- 内置 Provider 修改的密钥和启用状态持久化到数据库，重启后保留
- 内置 Provider 不可删除（仅可停用），自定义 Provider 可删除
- 测试连接接口返回具体错误信息，便于前端展示失败原因
- ProviderRegistry 新增 `_config_overrides` 机制：内置 Provider 插件激活时自动合并数据库中的密钥和启用状态覆盖项

### 待完成
- 插件从 zip/git 安装
- 会话分支与导出（Markdown/JSON）
- Docker 沙箱后端
- 桌面壳（Tauri）
- 多 Agent 协作
- 远程 channel（飞书/Telegram）

---

## [0.1.0] - 2026-09-24

### 新增

#### 核心（P0-P1）
- monorepo 骨架（backend + frontend + docs）
- FastAPI 后端 + Vue 3 前端最小应用
- 内核：EventBus（async pub/sub + AMQP 风格通配符）
- 内核：ServiceRegistry（stack 回退策略 + 线程安全）
- 内核：HookManager（有序管线 + 改写 + 短路 + metadata 传播）
- 内核：PluginLoader（发现/校验/加载/激活/停用/核心插件保护）
- 内核：PluginContext 受限门面
- 内核：TokenCounter 契约接口
- 示例插件 hello-plugin

#### 模型层（P2）
- OpenAICompatibleProvider 基类（流式解析/超时/指数退避重试/统一错误）
- DeepSeek Provider 插件（v4-flash/v4-pro/chat/reasoner，含 reasoning_content 思维链）
- Qwen Provider 插件
- Doubao Provider 插件
- ProviderRegistry 统一管理（实例缓存 + 配置更新清除缓存）
- API Key Fernet 加密存储（machine key 派生）
- 环境变量 DEEPSEEK_API_KEY 回退
- health_check 使用 GET /models 端点（不消耗 token）
- TokenCounter tiktoken 估算（类级缓存编码器）

#### 会话/上下文（P3）
- SQLite 数据模型（sessions/messages/context_snapshots/providers/settings/plugins）
- session-manager 插件（CRUD/重命名/归档/消息追加）
- context-manager 插件（滑动窗口/摘要压缩/消息钉住/系统提示词模板）
- tool_call_id 全链路传递
- 孤立 tool 消息清理（压缩前后两次过滤）
- content=null 处理（API 规范）
- 上下文快照持久化

#### Agent 引擎（P4）
- AgentLoop 核心循环（装配上下文 → 调模型 → 解析 tool_calls → 执行 → 回填）
- ToolPlugin 契约 + 工具注册表
- 钩子管线全量接入（7 个钩子点）
- 流式 tool_calls 分片按 index 累积合并
- 带工具调用的 assistant 消息持久化（API 规范）
- 失败轮次自动清理（防止上下文错乱）
- 系统时间注入
- 用户自定义 system_prompt 注入
- 模型调用指数退避重试（默认 3 次）
- 停止/中断生成
- 内置工具：计算器、当前时间

#### API 层（P5）
- REST API（sessions/messages/providers/models/settings/plugins）
- 插件安装接口 POST /api/plugins/install
- 插件卸载接口 DELETE /api/plugins/{id}
- WebSocket /ws/chat（流式/工具事件/停止/自动标题）
- 统一错误格式 {code, message, detail, trace_id}
- 核心插件保护（API 层拒绝停用核心插件）

#### 前端（P6-P7）
- Chat 页面（会话侧栏/消息列表/流式渲染/停止生成）
- Markdown 渲染（markdown-it + highlight.js，流式内容也支持）
- 思维链展示（逐步流式展示，完成后折叠）
- WS 客户端（断线重连/单连接管理/避免 token 重复）
- 模型选择器
- 设置页面（Provider 管理/会话级设置/插件管理/通用设置）
- 多主题系统（matrix/ocean/sunset/dark/light，CSS 变量切换）
- AI 主题切换（tool-theme-switcher 工具 + 前端指令监听）
- 插件在线安装表单
- 插件卸载按钮
- 插件列表滚动条
- 会话标题自动生成（AI 总结，不超过 20 字）
- 会话标题截断显示（hover 显示完整标题）
- 设置按钮（header 齿轮图标跳转）
- Matrix 数字雨首页（Canvas 0/1 瀑布雨 + 鼠标弯曲）
- 微交互（涟漪效果/右键菜单/长按）
- 国际化（中文/英文）
- 前端插件内核（UIPluginContext/PluginLoader/EventBusBridge）

#### 沙箱（P8）
- LocalSubprocessBackend（子进程/超时/输出截断/资源限制/黑名单/进程树终止）
- 平台适配（Linux/macOS resource 模块 + Windows psutil）
- 预装包白名单（requirements-sandbox.txt）
- tool-code-runner 插件

#### 工具插件
- tool-web-search（百度搜索引擎）
- tool-web-fetch（网页正文抓取）
- tool-theme-switcher（AI 主题切换）
- tool-ip-lookup（IP 地址地理位置查询）

#### 文档
- 架构设计文档（01-architecture.md）
- 分阶段开发计划（02-development-plan.md）
- 插件开发指南（docs/plugin-dev-guide.md）
- README 打磨版（徽章/快速开始/使用指南/FAQ）
- Docker 部署（docker-compose.yml + Dockerfile）
- 贡献指南（CONTRIBUTING.md）
- Issue/PR 模板

### 修复
- DeepSeek API 422 "missing field tool_call_id" — tool_call_id 全链路传递
- DeepSeek API 400 "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'" — 孤立 tool 消息清理
- 流式 tool_calls 参数不完整 — 分片按 index 累积合并
- Web 搜索 "未提供搜索关键词" — 分片累积修复后的连带修复
- 思维链显示为每行一个词 — streamingReasoning 从数组改为拼接字符串
- 回答内容字符重复 — WS 单连接管理（connect 前关闭旧连接 + onMessage 返回取消注册函数）
- Markdown 未渲染 — 流式内容改用 MarkdownRenderer 组件
- 失败轮次上下文错乱 — 异常时清理残留的未完成消息
- content 为 None 导致 len() 报错 — 所有 count_tokens 调用使用 `or ""` 回退
- system_prompt 未传递到模型 — WSMessage/AgentLoopConfig 添加 system_prompt 字段
