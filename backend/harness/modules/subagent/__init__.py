"""subagent —— 子代理委派服务。

对齐 Claude Code 的 Sub-agent delegation / Deep Agents 的 subagent
spawning：主代理派生一个**上下文干净**的子代理处理隔离子任务，
子代理在独立会话中运行，**只把最终结论摘要回传**给主代理，
从而避免探索过程污染主上下文。
"""

from .service import (
    SubagentResult,
    SubagentService,
    SubagentServiceImpl,
)

__all__ = ["SubagentResult", "SubagentService", "SubagentServiceImpl"]
