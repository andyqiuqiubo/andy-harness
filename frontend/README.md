# andy-harness 前端（Vue 3 + TypeScript + Vite）

对话界面、设置中心与前端插件内核的实现。**后端不在本目录**，所有数据都通过 `/api/**`（REST）与 `/ws/chat`（WebSocket）与 Python 后端通信。

---

## 快速开始

```bash
pnpm install          # 必须用 pnpm（仓库有 pnpm-lock.yaml，用 npm 会导致锁文件不一致）
pnpm dev              # 开发服务器 http://localhost:5173，/api 与 /ws 自动代理到 127.0.0.1:8000
pnpm build            # vue-tsc -b && vite build，产物在 dist/（桌面壳与生产部署都读这里）
pnpm preview          # 本地预览 dist（无后端代理，仅用于看静态效果）
```

> 后端没起来时前端不会白屏：启动后会轮询 `/api/health`（最长 120 秒）等待就绪，各 store 的错误态会在界面上给出提示。

## 质量门禁

```bash
pnpm lint             # eslint .（与 CI 一致）
pnpm test             # vitest run（9 个测试文件 / 43 个用例，与 CI 一致）
pnpm test:watch       # 监听模式
pnpm test:coverage    # 覆盖率报告（text + html）
```

提交前请确保这三条都通过。当前技术栈版本见 `package.json`：Vue 3.5 / Vite 8 / TypeScript 6 / Vitest 5 / Pinia 4 / Vue Router 4 / markdown-it 15 / highlight.js 11。

---

## 目录结构

```
src/
├── main.ts            # 应用引导：解析后端地址 → 初始化认证 → 实例化设置 store → 挂载 → 加载 UI 插件
├── App.vue            # 登录门 + 路由出口 + 顶栏
├── router/index.ts    # 路由表（UI 插件可动态 addRoute / removeRoute）
├── api/
│   ├── runtime.ts     # 运行环境识别：浏览器走相对路径，Tauri 走 get_backend_url 绝对地址
│   ├── client.ts      # 统一请求封装：Bearer 注入、401 回调、ApiError（区分网络错误 / HTTP 错误）
│   ├── token.ts       # token 本地持久化
│   └── types.ts       # 与后端 DTO 对齐的类型
├── stores/            # Pinia：chat / providers / plugins / plugin-loader / skills / todos / permissions / settings / auth
├── views/             # ChatView（主对话）/ SettingsView（设置中心）/ HomeView / LoginView
├── components/        # SessionSidebar / MessageItem / MarkdownRenderer / ProcessTrace / MatrixRain / AttachmentImage / ModelSelector / PluginOverlayHost
├── core/              # 前端插件内核：plugin-loader / plugin-context / plugin-types / event-bus-bridge
├── plugins/           # 内置 UI 插件：hello（示例）、pet（元气宠物）
├── composables/       # useLanguage / useRipple
├── i18n/              # zh.ts / en.ts（扁平 key，两边必须同步增删）
├── utils/             # datetime / format（各自带单测）
└── style.css
```

---

## 约定与注意事项

1. **统一走 `api/client.ts`**：不要直接用 `fetch` / `XMLHttpRequest`，否则会丢掉 Bearer 注入、401 统一处理与错误类型。所有业务错误都是 `ApiError`（`status` 为 `0` 表示网络层失败）。
2. **后端地址是动态的**：`apiBase()` 依赖 `runtime.ts` 的 `currentOrigin()`。在 Tauri 里必须先 `await initBackendRuntime()`，否则会请求到错误的主机。
3. **WebSocket 客户端只有一个**：`apiClient`（`APIClientImpl`）内部维护单连接 + 指数退避重连 + 握手期消息排队；`send()` 返回 `false` 表示连接不可用，调用方需要回滚乐观 UI 状态。认证 token 通过 `?token=` 传参，服务端返回关闭码 `1008` 表示登录失效（会自动清理 token 并跳回登录页）。
4. **状态放 Pinia，不放组件**：跨视图共享的会话、模型、设置、权限、待办、认证状态一律进 `stores/`。
5. **i18n 同步**：`i18n/zh.ts` 与 `i18n/en.ts` 是扁平结构，新增文案必须两边同时添加，否则英文界面会直接显示 key。
6. **插件清单 `ui-plugin.json`**：`type: "ui"`，通过 `contributes.views` 注入路由与侧栏菜单，`backend_plugin_id` 用于与后端插件启停状态联动；`core_api` 需落在后端 `CORE_API_VERSION`（当前 `0.1.0`）声明的兼容区间内。
7. **样式**：`style.css` 用 CSS 变量承载 5 套主题（`data-theme` 属性切换），新增颜色请用变量而不是硬编码。
8. **超大组件是已知债务**：`SettingsView.vue`（约 4300 行）与 `ChatView.vue`（约 1900 行）目前按标签聚合，改动前建议先确认影响面，1.0.x 内会拆分。

## 已知限制（预览版）

- 主包体约 1.3 MB（gzip 约 440 KB），尚未做路由级代码分割，构建时 Vite 会提示 chunk 超过 500 kB。
- 渠道管理、Computer Use 开关、Jev 看板、断点续跑、用户管理**尚无界面入口**，只能通过 REST 使用（后端与文档均已说明）。
- 桌面壳（`desktop/`）加载的是 `pnpm build` 的产物，dev 模式走 Tauri 的 `devUrl` 指向本仓库的 5173 端口。

