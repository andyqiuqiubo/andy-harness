"""输入分类与处理流程路由（input_dispatch）—— 可扩展的「分类 → 流程」门面。

| 输入类型 | 判定依据（优先级降序） | 处理流程 | 预期输出 |
| --- | --- | --- | --- |
| ``executable`` | 魔数（PE/ELF/Mach-O）→ 可执行扩展名软信号 | 全链路逆向 + 功能等价重建 | 功能等价的新 exe + 证据包 |
| ``source_code`` | 源码扩展名 + 内容特征（shebang/语法） | 代码理解 / 重构 | 重构后源码 + 理解报告 |
| ``text`` | 可解码文本（低控制字符比） | 需求实现 | 实现产物 + 使用说明 |
| ``binary`` | 兜底：无法识别 | 通用二进制分诊 | 分诊报告与处置建议 |

扩展方式（无需改动核心分类逻辑）：

```python
from harness.modules.reverse_engineering.input_dispatch import (
    ClassificationRule, InputHandler, register_rule, register_handler,
)


@register_rule
class ArchiveMagicRule(ClassificationRule):
    name = "archive_magic"
    priority = 95  # 高于源码、低于可执行魔数

    def match(self, subject):
        if subject.data[:4] == b"PK\x03\x04":
            return RuleMatch(kind="archive", confidence=0.95, reason="ZIP 魔数命中", evidence={})
        return None


@register_handler
class ArchiveHandler(InputHandler):
    kind = "archive"

    def build_plan(self, subject, classification):
        return ProcessingPlan(kind="archive", flow="archive_extract", goal="解包并清点内容")
```

内置规则/处理器在导入本包时自动注册；也可向 :class:`InputClassifier` 构造参数注入
自定义规则/处理器（单测与定制场景）。
"""

from harness.modules.reverse_engineering.input_dispatch.handlers import (
    BinaryHandler,
    ExecutableHandler,
    InputHandler,
    SourceCodeHandler,
    TextHandler,
    default_handlers,
    register_handler,
    unregister_handler,
)
from harness.modules.reverse_engineering.input_dispatch.models import (
    Classification,
    ClassificationSubject,
    FlowStep,
    InputKind,
    ProcessingPlan,
    RouteResult,
    RuleMatch,
)
from harness.modules.reverse_engineering.input_dispatch.rules import (
    BinaryFallbackRule,
    BinaryMagicRule,
    ClassificationRule,
    ExecutableExtensionRule,
    SourceCodeRule,
    TextRule,
    default_rules,
    register_rule,
    unregister_rule,
)
from harness.modules.reverse_engineering.input_dispatch.service import (
    InputClassifier,
    classify_any,
    classify_input,
    route_input,
    route_text,
    subject_from_path,
    subject_from_text,
)

__all__ = [
    "BinaryFallbackRule",
    "BinaryHandler",
    "BinaryMagicRule",
    "Classification",
    "ClassificationRule",
    "ClassificationSubject",
    "ExecutableExtensionRule",
    "ExecutableHandler",
    "FlowStep",
    "InputClassifier",
    "InputHandler",
    "InputKind",
    "ProcessingPlan",
    "RouteResult",
    "RuleMatch",
    "SourceCodeHandler",
    "SourceCodeRule",
    "TextHandler",
    "TextRule",
    "classify_any",
    "classify_input",
    "default_handlers",
    "default_rules",
    "register_handler",
    "register_rule",
    "route_input",
    "route_text",
    "subject_from_path",
    "subject_from_text",
    "unregister_handler",
    "unregister_rule",
]
