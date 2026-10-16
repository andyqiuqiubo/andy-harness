"""知识库检索技能（Tool）—— 方案 5.5 的 function calling 暴露。

把 :meth:`KnowledgeBaseService.skill_search` 包装成 ``ToolPlugin``，
AgentLoop 通过 function calling 把本工具的描述注入模型上下文，
由大模型**自主判定**是否需要私有事实作答：

- 需要私有事实 / 实体 lookup → 模型输出工具调用 → 执行检索 → 引用来源作答；
- 闲聊 / 通用常识 / 纯推理 → 不调用。

安全边界（方案 5.5.5）：技能**仅读不写**（risk_level=read），
不触达外部网络，不直接生成最终答案——只返回分段与来源，由模型合成。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.contracts.tool import ToolPlugin
from harness.modules.knowledge_base.service import KnowledgeBaseService

logger = logging.getLogger("harness.kb.tool")

# 内部 warn 代码 → 面向模型/用户的人话（方案 5.4.4 的降级提示）
_WARN_TEXT = {
    "missing_vector_index": "该知识库尚未建立向量索引，当前为关键词检索",
}


class KnowledgeBaseSearchTool(ToolPlugin):
    """``knowledge_base_search`` —— 私有知识库检索工具。"""

    def __init__(self, service: KnowledgeBaseService) -> None:
        self._service = service

    @property
    def tool_name(self) -> str:
        return "knowledge_base_search"

    @property
    def risk_level(self) -> str:
        # 只读检索，无副作用（方案 1.2「默认安全」+ 5.5「技能仅读不写」）
        return "read"

    @property
    def description(self) -> str:
        return (
            "当用户的问题需要依据私有知识库中的事实性信息作答时调用；返回相关文档分段及来源。"
            "不适用于闲聊、通用常识、纯推理或代码生成。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        # 与方案 5.5.2 的 Skill Schema 一致（query 必填，其余可选带默认值）
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "经提问分析后的检索 query",
                },
                "knowledge_base_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "限定知识库 id 列表；缺省为全部（技能内按 KB 路由）",
                },
                "retrieval_mode": {
                    "type": "string",
                    "enum": ["keyword", "semantic", "hybrid"],
                    "description": "检索模式；缺省 hybrid",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回分段条数；默认 5",
                },
                "rerank": {
                    "type": "boolean",
                    "description": "是否启用重排（配置了重排模型时）；默认 true",
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """执行技能检索，返回带来源标注的文本（供模型引用作答）。"""
        query = str(args.get("query") or "").strip()
        if not query:
            return "错误: 未提供 query（知识库检索需要检索词）"
        kb_ids = args.get("knowledge_base_ids")
        mode = args.get("retrieval_mode")
        top_k_raw = args.get("top_k")
        rerank = args.get("rerank")
        try:
            top_k = max(1, min(int(top_k_raw or 5), 50))
        except (TypeError, ValueError):
            top_k = 5
        try:
            result = await self._service.skill_search(
                query,
                knowledge_base_ids=[str(k) for k in kb_ids] if isinstance(kb_ids, list) else None,
                retrieval_mode=mode if mode in ("keyword", "semantic", "hybrid") else None,
                top_k=top_k,
                rerank=bool(rerank) if rerank is not None else None,
            )
        except Exception as e:  # noqa: BLE001 —— 工具失败降级为文本，不中断问答
            logger.warning("knowledge_base_search 技能执行失败: %s", e)
            return f"错误: 知识库检索失败（{e}）"
        return _format_result(result)


def _format_result(result: dict[str, Any]) -> str:
    """把技能检索结果格式化为模型可引用的文本（含来源标注，方案 5.5.5）。"""
    if result.get("skipped"):
        return "（已按强制开关跳过知识库检索）"
    segments = result.get("segments") or []
    if not segments:
        if result.get("reason") == "below_threshold":
            return "知识库中未找到相关信息（所有结果均低于相关性阈值）。"
        # 方案 5.5.5：无相关时模型必须明确告知，禁止编造
        return "知识库中未找到相关信息。"
    lines: list[str] = []
    for i, seg in enumerate(segments, 1):
        src = seg.get("source") or {}
        doc_name = src.get("doc_name") or src.get("doc_id") or "未知文档"
        position = seg.get("position")
        pos_part = f"，位置 {position}" if isinstance(position, int) and position else ""
        lines.append(f"[{i}] 来源：{doc_name}{pos_part}\n{seg.get('content', '')}")
    header = f"共检索到 {len(segments)} 个相关分段"
    warn = result.get("warn")
    if warn:
        header += f"（提示：{_WARN_TEXT.get(str(warn), warn)}）"
    return header + "：\n\n" + "\n\n".join(lines)
