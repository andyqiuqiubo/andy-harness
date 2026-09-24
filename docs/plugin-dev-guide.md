# andy-harness 插件开发指南

> 本指南教你从零开发一个可运行的插件。照做即可，无需修改项目核心代码。

---

## 目录

1. [后端工具插件开发](#1-后端工具插件开发)
2. [后端 Provider 插件开发](#2-后端-provider-插件开发)
3. [前端 UI 插件开发](#3-前端-ui-插件开发)
4. [在线安装插件](#4-在线安装插件无需重启)
5. [插件调试技巧](#5-插件调试技巧)

---

## 1. 后端工具插件开发

工具插件让 AI 能调用外部能力（搜索、计算、查询等）。

### 1.1 文件结构

在 `backend/plugins/` 下创建插件目录：

```
backend/plugins/tool_my_tool/
├── __init__.py      # 空文件
├── plugin.json      # 插件清单
└── main.py          # 插件代码
```

### 1.2 plugin.json 清单

```json
{
  "id": "tool_my_tool",
  "name": "My Tool",
  "version": "0.1.0",
  "type": "tool",
  "entry": "plugins.tool_my_tool.main:MyToolPlugin",
  "core_api": ">=0.1.0 <1.0.0",
  "permissions": ["network"],
  "description": "我的工具插件描述"
}
```

| 字段 | 说明 |
|---|---|
| `id` | 插件唯一标识，与目录名一致 |
| `entry` | `模块路径:类名`，PluginLoader 据此动态 import |
| `type` | `tool` / `provider` / `service` / `hook` / `channel` |
| `permissions` | `network`（网络访问）/ `filesystem`（文件访问）等 |
| `core_api` | 语义化版本约束 |

### 1.3 main.py 代码模板

```python
"""My Tool 工具插件。"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.my_tool")


class MyTool(ToolPlugin):
    """工具实现。"""

    @property
    def tool_name(self) -> str:
        return "my_tool"

    @property
    def description(self) -> str:
        return (
            "工具描述（AI 会根据这段描述判断何时调用此工具）。"
            "当用户询问 XXX 时使用此工具。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """JSON Schema 格式的参数定义。"""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询参数说明",
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """执行工具，返回结果字符串。"""
        query = args.get("query", "").strip()
        if not query:
            return "错误: 未提供查询参数"

        # 你的业务逻辑
        result = f"查询 '{query}' 的结果：..."

        return result


class MyToolPlugin(BasePlugin):
    """插件入口类，负责工具的注册和注销。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册工具到 ToolRegistry。"""
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry

        # 获取或创建 ToolRegistry
        if not ctx.services.has(ToolRegistry):
            ctx.services.register(
                ToolRegistry, ToolRegistry(), owner=self.plugin_id
            )
        tool_registry = ctx.services.get(ToolRegistry)

        # 注册工具
        tool_registry.register(MyTool(), owner=self.plugin_id)
        ctx.logger.info("My Tool 已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("My Tool 已注销")
```

### 1.4 启动验证

插件目录会被 PluginLoader 自动扫描，启动时自动加载激活。启动后端后在对话中测试：

> 用户：帮我查询一下 XXX

AI 会自动调用 `my_tool` 工具并返回结果。

---

## 2. 后端 Provider 插件开发

Provider 插件接入新的大模型厂商。

### 2.1 main.py 模板

```python
"""MyProvider Provider 插件。"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.token_counter import TokenCounter
from harness.modules.model_manager.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)
from harness.modules.model_manager.provider_registry import ProviderRegistry

logger = logging.getLogger("harness.provider.my_provider")


class MyProvider(OpenAICompatibleProvider):
    """继承 OpenAI 兼容基类，只需设置厂商专属配置。"""

    base_url = "https://api.my-provider.com/v1"
    default_models = ["my-model-pro", "my-model-lite"]
    provider_name = "my_provider"

    def _parse_stream_chunk(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """如有特殊字段（如思维链），重写此方法。"""
        return super()._parse_stream_chunk(data)


class MyProviderPlugin(BasePlugin):
    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._provider_registry: ProviderRegistry | None = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx

        if not ctx.services.has(ProviderRegistry):
            ctx.services.register(
                ProviderRegistry, ProviderRegistry(), owner=self.plugin_id
            )
        self._provider_registry = ctx.services.get(ProviderRegistry)

        # 从配置获取 API Key，回退到环境变量
        import os
        api_key = ctx.config.get("api_key", "") or os.getenv("MY_PROVIDER_API_KEY", "")
        base_url = ctx.config.get("base_url", MyProvider.base_url)

        config = {
            "name": "MyProvider",
            "api_key": api_key,
            "base_url": base_url,
            "models": MyProvider.default_models,
        }

        self._provider_registry.register_provider("my_provider", MyProvider, config)
        ctx.logger.info("MyProvider 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        if self._provider_registry:
            self._provider_registry.unregister_provider("my_provider")
        ctx.logger.info("MyProvider 已停用")
```

### 2.2 plugin.json

```json
{
  "id": "provider_my_provider",
  "name": "MyProvider Provider",
  "version": "0.1.0",
  "type": "provider",
  "entry": "plugins.provider_my_provider.main:MyProviderPlugin",
  "core_api": ">=0.1.0 <1.0.0",
  "permissions": ["network"],
  "description": "MyProvider 大模型接入插件"
}
```

---

## 3. 前端 UI 插件开发

前端插件可以注入新视图、菜单项到界面中。

### 3.1 文件结构

```
frontend/src/plugins/my_panel/
├── ui-plugin.json          # 前端插件清单
├── main.ts                 # 插件入口
└── components/
    └── MyPanelView.vue     # 视图组件
```

### 3.2 ui-plugin.json 清单

```json
{
  "id": "my_panel",
  "name": "My Panel",
  "version": "0.1.0",
  "type": "ui",
  "entry": "main:MyPanelPlugin",
  "contributes": {
    "views": [
      {
        "id": "my_panel",
        "route": "/my-panel",
        "title": "My Panel",
        "component": "components/MyPanelView.vue"
      }
    ],
    "menu_items": [
      {
        "id": "my_panel_menu",
        "parent": "sidebar",
        "label": "My Panel",
        "icon": "chart-bar",
        "view_id": "my_panel"
      }
    ]
  }
}
```

### 3.3 main.ts 模板

```typescript
import type { UIPlugin, UIPluginContext } from '../../core/plugin-types'

export class MyPanelPlugin implements UIPlugin {
  id = 'my_panel'
  ctx: UIPluginContext | null = null

  async activate(ctx: UIPluginContext) {
    this.ctx = ctx
    ctx.logger.info('My Panel 已激活')
  }

  async deactivate(ctx: UIPluginContext) {
    ctx.logger.info('My Panel 已停用')
  }
}

export default MyPanelPlugin
```

### 3.4 MyPanelView.vue 模板

```vue
<script setup lang="ts">
// 你的视图逻辑
</script>

<template>
  <div class="my-panel-view">
    <h1>My Panel</h1>
    <p>这是我的自定义面板。</p>
  </div>
</template>

<style scoped>
.my-panel-view {
  padding: var(--space-lg);
}
</style>
```

---

## 4. 在线安装插件（无需重启）

除了手动创建文件外，还可以通过 UI 在线安装插件：

1. 打开 http://localhost:5173/settings
2. 切换到"插件管理"标签
3. 点击"安装插件"
4. 填写表单：
   - **插件 ID**: `tool_my_tool`
   - **插件名称**: `My Tool`
   - **入口**: `plugins.tool_my_tool.main:MyToolPlugin`
   - **描述**: 工具描述
   - **插件 Python 源码**: 粘贴上面的 main.py 内容
5. 点击"安装"按钮

后端会自动创建插件文件、加载并激活插件，无需重启服务。

### 在线安装示例：随机数生成工具

**插件 ID**: `tool_dice`

**插件名称**: `Dice Roller`

**入口**: `plugins.tool_dice.main:DicePlugin`

**描述**: `掷骰子、生成随机数`

**插件 Python 源码**:

```python
"""Dice 工具插件。"""
from __future__ import annotations
import logging
import random
from typing import Any
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.dice")

class DiceTool(ToolPlugin):
    @property
    def tool_name(self) -> str:
        return "dice"
    @property
    def description(self) -> str:
        return "掷骰子或生成随机数。可指定骰子面数和数量，如 1d6、2d20。"
    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "骰子数量(默认1)", "default": 1},
                "sides": {"type": "integer", "description": "骰子面数(默认6)", "default": 6},
            },
        }
    async def execute(self, args: dict[str, Any]) -> str:
        count = max(1, min(args.get("count", 1), 100))
        sides = max(2, min(args.get("sides", 6), 10000))
        results = [random.randint(1, sides) for _ in range(count)]
        total = sum(results)
        detail = " + ".join(str(r) for r in results)
        return f"掷 {count}d{sides}: [{detail}] = {total}"

class DicePlugin(BasePlugin):
    manifest: PluginManifest
    _ctx: PluginContext | None = None
    def __init__(self) -> None:
        self._ctx = None
    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry
        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(DiceTool(), owner=self.plugin_id)
        ctx.logger.info("Dice 工具已注册")
    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry
        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:
            pass
```

安装后在对话中说"掷3个6面骰子"，AI 会自动调用工具。

---

## 5. 插件调试技巧

### 5.1 查看日志

后端日志会输出插件加载和激活信息：

```
INFO:     Plugin loaded: tool_my_tool
INFO:     My Tool 已注册
```

### 5.2 查看已加载插件

```bash
curl http://localhost:8000/api/plugins
```

### 5.3 手动启停插件

```bash
# 激活
curl -X POST http://localhost:8000/api/plugins/tool_my_tool/activate

# 停用
curl -X POST http://localhost:8000/api/plugins/tool_my_tool/deactivate
```

### 5.4 常见问题

| 问题 | 原因 | 解决方案 |
|---|---|---|
| 插件不出现 | plugin.json 格式错误 | 检查 JSON 语法和必填字段 |
| 工具不被 AI 调用 | description 写得不够清晰 | 确保 description 明确说明何时使用此工具 |
| 参数为空 | parameters_schema 定义有误 | 确保用 JSON Schema 格式，required 字段正确 |
| activate 报错 | import 路径错误 | 检查 entry 格式 `模块:类名` |
| 工具执行报错 | execute 方法异常 | 查看 backend 日志中的错误堆栈 |

### 5.5 最佳实践

1. **description 要写清楚**：AI 根据它判断是否调用此工具，写清"当用户 XXX 时使用"
2. **parameters_schema 要精确**：用 enum 限定取值范围，description 说明每个参数含义
3. **execute 返回字符串**：返回值会作为 tool 消息回填给模型，内容要清晰
4. **异常处理**：execute 内部 try/except，返回错误信息而非抛异常
5. **权限声明**：在 plugin.json 的 permissions 中声明需要的权限
6. **日志记录**：用 `ctx.logger.info()` 记录关键操作
