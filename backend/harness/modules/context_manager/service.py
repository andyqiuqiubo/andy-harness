"""ContextService —— 上下文管理服务。

策略可插拔：滑动窗口 / 超额摘要压缩 / 关键消息钉住 / 系统提示词模板。
token 预算分配：system > pinned > 近期消息 > 摘要。
快照持久化到数据库。
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from string import Template
from typing import Any, Protocol

from harness.kernel.contracts.token_counter import TokenCounter
from harness.kernel.services import ServiceRegistry

logger = logging.getLogger("harness.context_manager")


class ContextStrategy(ABC):
    """上下文压缩策略抽象基类。"""

    @abstractmethod
    def apply(
        self,
        messages: list[dict[str, Any]],
        token_counter: TokenCounter | None,
        model: str,
        budget: int,
        pinned_indices: set[int],
    ) -> list[dict[str, Any]]:
        """应用压缩策略。

        Args:
            messages: 原始消息列表
            token_counter: token 计数器（可能为 None，使用估算）
            model: 模型名称
            budget: token 预算
            pinned_indices: 需要钉住的消息索引

        Returns:
            压缩后的消息列表
        """
        ...


class SlidingWindowStrategy(ContextStrategy):
    """滑动窗口策略：保留最近的 N 条消息。"""

    def __init__(self, max_messages: int = 20) -> None:
        self.max_messages = max_messages

    def apply(
        self,
        messages: list[dict[str, Any]],
        token_counter: TokenCounter | None,
        model: str,
        budget: int,
        pinned_indices: set[int],
    ) -> list[dict[str, Any]]:
        """保留最近的消息 + 钉住的消息。"""
        if len(messages) <= self.max_messages:
            return messages

        # 保留最近的 N 条
        recent = messages[-self.max_messages:]

        # 钉住的消息（不在 recent 中的）
        pinned = []
        recent_ids = {id(m) for m in recent}
        for idx in sorted(pinned_indices):
            if idx < len(messages):
                msg = messages[idx]
                if id(msg) not in recent_ids:
                    pinned.append(msg)

        return pinned + recent


class SummaryCompressionStrategy(ContextStrategy):
    """摘要压缩策略：超限时压缩较旧的消息为摘要。"""

    def apply(
        self,
        messages: list[dict[str, Any]],
        token_counter: TokenCounter | None,
        model: str,
        budget: int,
        pinned_indices: set[int],
    ) -> list[dict[str, Any]]:
        """计算 token 数，超限时将旧消息替换为摘要。"""
        if token_counter is None:
            return SlidingWindowStrategy().apply(
                messages, token_counter, model, budget, pinned_indices
            )

        total_tokens = sum(
            token_counter.count_tokens(m.get("content") or "", model) for m in messages
        )

        if total_tokens <= budget:
            return messages

        # 分离 pinned / 其他
        pinned = []
        remaining: list[dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            if idx in pinned_indices:
                pinned.append(msg)
            else:
                remaining.append(msg)

        # 从最近的开始保留，直到达到预算
        result = list(pinned)
        current_tokens = sum(
            token_counter.count_tokens(m.get("content") or "", model) for m in result
        )

        kept: list[dict[str, Any]] = []
        for msg in reversed(remaining):
            msg_tokens = token_counter.count_tokens(msg.get("content") or "", model)
            if current_tokens + msg_tokens > budget:
                break
            kept.insert(0, msg)
            current_tokens += msg_tokens

        # 被丢弃的消息生成摘要占位
        dropped_count = len(remaining) - len(kept)
        if dropped_count > 0:
            # 收集被丢弃消息的摘要信息
            dropped_contents = [
                (m.get("content") or "")[:100] for m in remaining[:dropped_count]
            ]
            summary_text = f"[上下文摘要] 之前有 {dropped_count} 条消息被压缩。关键内容: {' | '.join(dropped_contents[:3])}"
            summary_msg = {
                "role": "system",
                "content": summary_text,
            }
            result.append(summary_msg)

        return result + kept


class ContextService(Protocol):
    """上下文服务接口。"""

    def build(
        self, session_id: str, budget: int = 4096, model: str = "gpt-4o"
    ) -> list[dict[str, Any]]: ...

    def get_snapshot(self, session_id: str) -> Any: ...

    def list_snapshots(self, session_id: str) -> list[dict[str, Any]]: ...


class ContextServiceImpl:
    """上下文服务实现。"""

    @staticmethod
    def _drop_orphan_tool_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """清理孤立的 tool 消息。

        压缩策略可能把带 tool_calls 的 assistant 消息丢弃，但保留了后面的
        tool 消息，导致 API 报错。此方法重建有效 tool_call_id 集合并过滤。
        """
        valid_ids: set[str] = set()
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                    if tc_id:
                        valid_ids.add(tc_id)
        result: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "tool":
                tcid = m.get("tool_call_id", "")
                if not tcid or tcid not in valid_ids:
                    continue
            result.append(m)
        return result

    def __init__(
        self,
        services: ServiceRegistry,
        strategy: ContextStrategy | None = None,
        system_prompt_template: str = "",
    ) -> None:
        self._services = services
        self._strategy: ContextStrategy = strategy or SlidingWindowStrategy()
        self._system_prompt_template: str = system_prompt_template
        self._pinned_messages: dict[str, set[int]] = {}  # session_id -> pinned indices

    def set_strategy(self, strategy: ContextStrategy) -> None:
        """设置压缩策略（运行时可切换）。"""
        self._strategy = strategy
        logger.info("上下文策略已切换: %s", strategy.__class__.__name__)

    def set_system_prompt_template(self, template: str) -> None:
        """设置系统提示词模板。"""
        self._system_prompt_template = template

    def pin_message(self, session_id: str, message_index: int) -> None:
        """钉住指定消息。"""
        if session_id not in self._pinned_messages:
            self._pinned_messages[session_id] = set()
        self._pinned_messages[session_id].add(message_index)

    def unpin_message(self, session_id: str, message_index: int) -> None:
        """取消钉住指定消息。"""
        if session_id in self._pinned_messages:
            self._pinned_messages[session_id].discard(message_index)

    def get_pinned_indices(self, session_id: str) -> set[int]:
        """获取钉住的消息索引。"""
        return self._pinned_messages.get(session_id, set())

    def _get_token_counter(self) -> TokenCounter | None:
        """获取 TokenCounter（可能未注册）。"""
        try:
            counter: TokenCounter | None = self._services.get(TokenCounter)
            return counter
        except Exception:
            return None

    def _get_snapshot_repo(self) -> Any:
        """获取 ContextSnapshotRepository（惰性创建）。"""
        try:
            from harness.infra.database import Database
            from harness.infra.repository import ContextSnapshotRepository

            db = self._services.get(Database)
            return ContextSnapshotRepository(db)
        except Exception:
            return None

    def build(
        self, session_id: str, budget: int = 4096, model: str = "gpt-4o"
    ) -> list[dict[str, Any]]:
        """构建上下文消息列表。

        Args:
            session_id: 会话 ID
            budget: token 预算
            model: 模型名称

        Returns:
            装配好的消息列表（含系统提示词 + 上下文消息）
        """
        from harness.modules.session_manager.service import SessionService

        session_service = self._services.get(SessionService)
        messages = session_service.list_messages(session_id)

        # 转换为 {role, content} 格式，保留 tool_call_id 和 tool_calls
        # 过滤掉缺少 tool_call_id 的 tool 消息（旧数据兼容）
        raw_messages: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool" and not m.tool_call_id:
                # 旧数据没有 tool_call_id，跳过避免 API 422 错误
                continue
            msg: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                msg["tool_calls"] = m.tool_calls
                # OpenAI/DeepSeek API 规范：assistant 消息带 tool_calls 时，
                # content 应为 null（空字符串可能导致 422）
                if m.role == "assistant" and not m.content:
                    msg["content"] = None
            if m.tool_call_id:
                msg["tool_call_id"] = m.tool_call_id
            raw_messages.append(msg)

        # 验证 tool 消息的完整性：每条 tool 消息前必须有对应的带 tool_calls
        # 的 assistant 消息，且 tool_call_id 在 assistant.tool_calls 中存在。
        # 否则 API 返回 400（Messages with role 'tool' must be a response to a
        # preceding message with 'tool_calls'）。压缩策略可能把 assistant
        # 的 tool_calls 消息丢弃但保留了 tool 消息，需在此清理孤立 tool 消息。
        valid_tool_call_ids: set[str] = set()
        for m in raw_messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                    if tc_id:
                        valid_tool_call_ids.add(tc_id)

        cleaned_messages: list[dict[str, Any]] = []
        for m in raw_messages:
            if m["role"] == "tool":
                tcid = m.get("tool_call_id", "")
                if not tcid or tcid not in valid_tool_call_ids:
                    # 孤立的 tool 消息，跳过
                    continue
            cleaned_messages.append(m)
        raw_messages = cleaned_messages

        # 插入系统提示词模板
        system_messages: list[dict[str, Any]] = []
        if self._system_prompt_template:
            system_content = self._render_template(
                self._system_prompt_template, session_id, model=model, budget=budget
            )
            system_messages.append({"role": "system", "content": system_content})

        non_system = [m for m in raw_messages if m["role"] != "system"]

        # 获取 token 计数器
        token_counter = self._get_token_counter()

        # 获取钉住的消息索引
        pinned_indices = self.get_pinned_indices(session_id)

        # 应用压缩策略
        compressed = self._strategy.apply(
            non_system, token_counter, model, budget, pinned_indices=pinned_indices
        )

        # 如果仍超预算且有 token 计数器，回退到摘要压缩策略
        if token_counter:
            total = sum(
                token_counter.count_tokens(m.get("content") or "", model)
                for m in compressed
            )
            if total > budget:
                logger.info(
                    "首次压缩后仍超预算 (%d > %d)，回退到摘要压缩策略",
                    total,
                    budget,
                )
                compressed = SummaryCompressionStrategy().apply(
                    non_system, token_counter, model, budget, pinned_indices=pinned_indices
                )

        # 压缩后再次清理孤立的 tool 消息（压缩可能把 assistant 的
        # tool_calls 消息丢弃，但保留了后面的 tool 消息）
        compressed = self._drop_orphan_tool_messages(compressed)

        # 合并系统消息
        result = system_messages + compressed

        # 计算实际 token 数
        token_count = 0
        if token_counter:
            token_count = sum(
                token_counter.count_tokens(m.get("content") or "", model) for m in result
            )
        else:
            token_count = sum(len(m.get("content") or "") // 4 for m in result)

        # 保存快照到数据库
        snapshot = {
            "session_id": session_id,
            "messages": result,
            "token_count": token_count,
            "budget": budget,
            "message_count": len(result),
        }

        # 持久化快照到数据库
        self._persist_snapshot(snapshot)

        logger.info(
            "上下文构建完成: session=%s, messages=%d, tokens=%d/%d",
            session_id,
            len(result),
            token_count,
            budget,
        )

        return result

    def _persist_snapshot(self, snapshot: dict[str, Any]) -> None:
        """持久化快照到数据库。"""
        repo = self._get_snapshot_repo()
        if repo is None:
            return
        try:
            from harness.infra.repository import ContextSnapshot

            cs = ContextSnapshot(
                id=str(uuid.uuid4()),
                session_id=snapshot["session_id"],
                message_id=None,
                messages=snapshot["messages"],
                token_count=snapshot["token_count"],
                budget=snapshot["budget"],
                created_at="",
            )
            repo.create(cs)
        except Exception as e:
            logger.warning("快照持久化失败: %s", e)

    def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        """获取最新的上下文快照。"""
        repo = self._get_snapshot_repo()
        if repo:
            try:
                snap = repo.get_latest(session_id)
                if snap:
                    return {
                        "session_id": snap.session_id,
                        "messages": snap.messages,
                        "token_count": snap.token_count,
                        "budget": snap.budget,
                        "message_count": len(snap.messages),
                        "created_at": snap.created_at,
                    }
            except Exception:
                pass
        return None

    def list_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        """列出会话的所有快照。"""
        repo = self._get_snapshot_repo()
        if repo:
            try:
                snaps = repo.list_by_session(session_id)
                return [
                    {
                        "session_id": s.session_id,
                        "messages": s.messages,
                        "token_count": s.token_count,
                        "budget": s.budget,
                        "message_count": len(s.messages),
                        "created_at": s.created_at,
                    }
                    for s in snaps
                ]
            except Exception:
                pass
        return []

    @staticmethod
    def _render_template(
        template_str: str,
        session_id: str = "",
        model: str = "",
        budget: int = 0,
        **kwargs: Any,
    ) -> str:
        """渲染系统提示词模板（变量插值）。

        支持变量: $session_id, $model, $budget, 以及 kwargs 中的自定义变量。
        """
        try:
            tpl = Template(template_str)
            return tpl.safe_substitute(
                session_id=session_id,
                model=model,
                budget=budget,
                **kwargs,
            )
        except (KeyError, ValueError):
            return template_str
