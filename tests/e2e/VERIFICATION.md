# andy-harness 会话功能修复 — 端到端验证报告

验证时间：2026-10-12 22:50 ~ 23:25（Asia/Shanghai）
验证环境：H:/jsd-andyharness 工作空间，后端 uvicorn 127.0.0.1:8000（含修复代码），
前端 Vite dev 5173（含修复代码），真实浏览器 Chrome 128（bsk 驱动），
Provider：OortCloud（custom_oortcloud，真实流式）+ 前端模拟模式。

## 一、API 级验证（e2e_chat.py，真实模型流式）—— 16/16 通过

| 场景 | 结果 |
|---|---|
| S1 停止生成：流式中途 stop → 部分内容持久化 + stopped 标记 + 提问保留 | PASS（34 字部分回答落库，stopped=1） |
| S1 done.stopped=true、done.session_id 归属正确 | PASS |
| S2 正常完成（对照）：stopped=false、完整回答落库 | PASS |
| S4 历史消息加载：3 轮问答按序加载、问答交替 | PASS |

数据库复核：`messages.stopped=1` 两行（API E2E 34 字 + UI 实测 8593 字）。

## 二、真实浏览器 UI 验证（bsk + Chrome）—— 全部通过

### 场景 A：会话更多菜单视口防截断（缺陷 1）
- 点击列表底部会话的 ⋮：菜单实测高 **392px**（旧实现写死 300px 夹取，底部约 92px 会被裁掉）。
- 修复后夹取到视口内：left=222, top=251, right=490, bottom=643 ≤ 视口高 651。
- `menuFullyInViewport: true`；5 个选项（重命名/导出/复制为新会话/归档/删除）全部 visible；
  **删除选项 dangerVisible: true**。
- 菜单项可交互：对空会话点击「归档」成功（会话被归档并自动切换）。
- 证据：shot_a_menu.png、e2e_a_eval2.txt。

### 场景 B：停止生成保留内容 + 停止标注（缺陷 2）
- 真实流式生成中（已输出 8140 字符）点击「停止生成」：
  - 停止前：isStreaming=true，streamingTextLen=8140（e2e_b_pre_stop3.txt）
  - 停止后：isStreaming=false；部分内容完整保留为一条 assistant 消息；
    `stoppedBadges: ["用户已停止回答"]`（e2e_b_post_stop3.txt）
  - 后端已把 8593 字部分回答落库（stopped=1），前端 reload 后由持久化消息接管，标注保留。
- 证据：shot_b_stop.png。

### 场景 C：生成途中「新建」再返回（缺陷 3）
- 会话「你好」生成途中点击「+ 新建」：
  - 新会话视图干净：assistantRows=0、无流式气泡、输入框/发送可用（e2e_c_afternew.txt）。
- 等待 6 秒后点击返回「你好」：
  - 流式状态完整恢复：isStreaming=true、streamingTextLen=361 且持续追加、停止按钮可用
    （e2e_c_afterback.txt）——已生成的回答文字完整可见，并非只剩提问。
- 生成完成：完整回答落库为消息（e2e_c_final.txt，assistantRows=8）。

### 场景 D：历史消息加载 + 会话切换
- 切换到「E2E-S4-历史加载」：3 轮问答 6 行消息按序完整加载（e2e_d_afterarchive.txt）。
- 会话列表搜索、归档切换在过程中均正常。

## 三、自动化测试

- 后端 pytest：**743 passed, 2 skipped**（含 stopped 列迁移 / 仓库层 / AgentLoop 改动）。
- 前端 vitest：**99 passed（16 文件）**，新增回归：
  - stores/chat.test.ts（5）：停止保留+stopped 标记、跨会话停止不串数据、
    新建后返回内容恢复、跨会话 done 不污染、模拟模式切走保留。
  - components/MessageItem.test.ts（3）：停止标注存在、位于内容下方、未停止不展示。
  - components/SessionSidebar.test.ts（+2）：菜单右下角/左上角点击均完整落入视口。
- 前端生产构建：`pnpm build` 通过（vue-tsc + vite）。

## 四、复现方式

```bash
# API 级（需后端运行、Provider 可用）
src/backend/.venv/Scripts/python.exe e2e/e2e_chat.py
# 前端单测
cd src/frontend && pnpm test
```
