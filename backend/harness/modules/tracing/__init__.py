"""tracing —— 运行轨迹（span）服务。

对齐 OpenAI Agents SDK tracing / Pydantic AI(OTel) / LangSmith：把每次
模型调用与工具调用记为一个 span（含耗时、token、输入输出摘要），按 trace
分组成链，供前端泳道图回放。数据落在 `spans` 表，零新增依赖。
"""

from .service import (
    Span,
    SpanService,
    SpanServiceImpl,
    TraceSummary,
)

__all__ = ["Span", "SpanService", "SpanServiceImpl", "TraceSummary"]
