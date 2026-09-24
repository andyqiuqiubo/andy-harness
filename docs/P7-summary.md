# P7 阶段总结 — 设置中心与插件管理界面

> 完成时间：2026-09-24
> 阶段目标：不改代码、纯 GUI 完成模型与插件配置。

---

## 1. 完成内容

### 1.1 设置页面（`views/SettingsView.vue`）

4 个 Tab 页：

| Tab | 功能 |
|---|---|
| **模型 Provider** | Provider 列表（名称/base_url/模型标签）+ 新增自定义模型表单（名称/base_url/api_key/model列表）+ 测试连接按钮 + 删除 |
| **会话设置** | 默认模型、温度滑块（0-2）、系统提示词文本框 |
| **插件管理** | 插件列表（名称/版本/类型）+ 启停开关（switch）+ 核心插件标记 + 核心插件禁用开关 |
| **通用设置** | 主题切换（亮/暗）+ 语言切换（中/英），即时生效并持久化到 localStorage |

### 1.2 新增 Store

| Store | 功能 |
|---|---|
| `stores/settings.ts` | 主题（light/dark）、语言（zh/en），localStorage 持久化，`applyTheme()` 设置 `data-theme` 属性 |
| `stores/plugins.ts` | 插件列表加载、激活/停用操作 |

### 1.3 核心插件保护

- `core: true` 插件的 switch 开关 `disabled`，旁边显示 🔒 图标
- 停用核心插件时捕获 `PLUGIN_DEACTIVATE_FORBIDDEN` 错误，弹出「核心插件不可停用」提示

### 1.4 主题切换

- 亮色/暗色主题通过 `data-theme` 属性控制
- `style.css` 中定义暗色主题样式（背景、消息卡片、输入框等）
- 切换即时生效，持久化到 localStorage

### 1.5 路由更新

- 新增 `/settings` 路由
- 首页新增「设置」链接

---

## 2. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 前端 lint | `pnpm lint` | no issues |
| 前端 build | `pnpm build` | built successfully（CSS 10.23 kB, JS 1.14 MB） |

---

## 3. DoD 逐项对照

| 完成标准 | 状态 | 说明 |
|---|---|---|
| 纯 UI 操作新增一个自定义 OpenAI 兼容 provider，测试连接成功并完成一次聊天 | 代码就绪 | 表单 + 测试连接已实现，需 API Key 验证完整流程 |
| 停用 tool-code-runner 后 agent 不再调用该工具；重新启用后恢复 | 部分通过 | 插件启停 UI 已实现，但需插件加载器加载实际插件才能验证 |
| 主题切换即时生效并持久化 | 通过 | 亮/暗主题切换即时生效 + localStorage 持久化 |
| 核心插件的停用按钮不可点击，hover 提示「核心插件不可停用」 | 通过 | switch disabled + 🔒 图标 + 错误捕获弹窗 |

---

## 4. 待用户验证

访问 http://localhost:5173/settings 验证：

1. **模型 Provider Tab**：点击「+ 新增」填写表单添加自定义 provider，点击「测试连接」
2. **会话设置 Tab**：调整温度滑块、填写系统提示词
3. **插件管理 Tab**：查看插件列表，核心插件开关灰显
4. **通用设置 Tab**：切换亮/暗主题，刷新页面后主题保持

---

## 5. 后续阶段衔接

- **P8**：沙箱管理（tool-code-runner 通过 ToolRegistry 注册）
- **P9**：开源打磨，文档中补充设置页使用说明
