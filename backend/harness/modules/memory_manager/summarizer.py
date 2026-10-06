"""长期记忆的自主总结 —— 定期把会话问答压缩成记忆。

对齐 Letta 的「自编辑记忆」/ OpenAI Agents SDK 的 built-in memory 思路：
后台任务按固定间隔扫描最近若干个活跃会话，把「上一次总结之后新增的消息」
交给 LLM 提炼成**跨会话可复用**的记忆条目，写入 `memories`。

设计要点：
- **增量**：每个会话记录「已总结到的消息条数」游标（存 JSON 状态文件），
  只处理新增消息，避免重复消耗 token。
- **稳定优先**：提示词明确要求只记「长期有效」的偏好/事实/约定，忽略寒暄与一次性内容。
- **优雅降级**：没有可用 provider / 没有 API Key 时直接跳过并给出原因，不影响主流程。
- **幂等**：记忆按 key upsert，重复总结同一事实不会产生重复条目。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.memory.summary")

# 单次总结的输入上限，避免 token 爆炸
_MAX_MESSAGES_PER_SESSION = 60
_MAX_CHARS_PER_MESSAGE = 1200
_MAX_TRANSCRIPT_CHARS = 12000

_SUMMARY_SYSTEM = (
    "你是「长期记忆整理助手」。请从给定的对话片段中，提炼出**值得跨会话长期记住**的信息，"
    "例如用户偏好、稳定的项目约定、反复会用到的背景事实。\n"
    "不要记录：寒暄、一次性问题、临时数据、AI 的自我介绍。\n"
    "只输出一个 JSON 数组（不要多余文字）。每个元素形如：\n"
    '  {"key": "简短稳定的标识，如 user.language / project.stack", '
    '"value": "要记住的内容", "scope": "global"}\n'
    "scope 用 global 表示对所有会话可见（默认）；仅当前会话相关时用 session。\n"
    "最多 6 条；若没有值得记住的内容，输出 []。"
)


def summary_enabled() -> bool:
    return os.environ.get("HARNESS_MEMORY_SUMMARY", "1") not in ("0", "false", "False")


def summary_interval() -> int:
    """后台总结间隔（秒）。"""
    try:
        return max(60, int(os.environ.get("HARNESS_MEMORY_SUMMARY_INTERVAL", "1800")))
    except ValueError:
        return 1800


def summary_max_sessions() -> int:
    try:
        return max(1, int(os.environ.get("HARNESS_MEMORY_SUMMARY_MAX_SESSIONS", "5")))
    except ValueError:
        return 5


@dataclass
class SummaryOutcome:
    """一次总结的结果。"""

    scanned: int = 0
    summarized: int = 0
    saved: int = 0
    skipped_reason: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "summarized": self.summarized,
            "saved": self.saved,
            "skipped_reason": self.skipped_reason,
            "details": self.details,
        }


class MemorySummarizer:
    """把会话问答总结压缩成长期记忆。

    依赖通过 ServiceRegistry **延迟解析**：memory_manager 插件可能先于
    session_manager 激活，构造时拿不到 SessionService。
    """

    def __init__(
        self,
        services: Any,
        state_path: str | Path,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> None:
        self._services = services
        self._state_path = Path(state_path)
        self._provider_id = provider_id
        self._model = model

    # ── 依赖解析（延迟） ──────────────────────────────

    @property
    def _memory(self) -> Any:
        from harness.modules.memory_manager.service import MemoryService

        return self._services.get(MemoryService)

    @property
    def _sessions(self) -> Any:
        from harness.modules.session_manager.service import SessionService

        return self._services.get(SessionService)

    @property
    def _providers(self) -> Any:
        try:
            from harness.modules.model_manager.provider_registry import ProviderRegistry

            return self._services.get(ProviderRegistry)
        except Exception:  # noqa: BLE001
            return None

    # ── 游标状态 ──────────────────────────────────────

    def _load_state(self) -> dict[str, int]:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError) as e:
            logger.warning("读取记忆总结状态失败（忽略）: %s", e)
        return {}

    def _save_state(self, state: dict[str, int]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("写入记忆总结状态失败（忽略）: %s", e)

    # ── provider 解析 ─────────────────────────────────

    def _resolve_provider(self) -> tuple[Any, str] | None:
        if self._providers is None:
            return None
        try:
            candidates = [
                p for p in self._providers.list_providers() if p.get("enabled", True) and p.get("has_api_key")
            ]
        except Exception as e:  # noqa: BLE001
            logger.debug("列举 provider 失败: %s", e)
            return None
        if not candidates:
            return None

        target = None
        if self._provider_id:
            target = next((p for p in candidates if p.get("id") == self._provider_id), None)
        if target is None:
            # 优先 deepseek（本项目的默认），否则取第一个可用
            target = next(
                (p for p in candidates if "deepseek" in str(p.get("id", ""))),
                candidates[0],
            )
        try:
            provider = self._providers.get_provider(target["id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("获取 provider 失败: %s", e)
            return None
        models = target.get("models") or []
        model = self._model or (models[0] if models else "deepseek-flash")
        return provider, model

    # ── 总结 ──────────────────────────────────────────

    async def summarize_recent(self, max_sessions: int | None = None, min_new_messages: int = 2) -> SummaryOutcome:
        outcome = SummaryOutcome()
        resolved = self._resolve_provider()
        if resolved is None:
            outcome.skipped_reason = "没有可用的大模型 provider（需已启用且配置 API Key）"
            return outcome
        provider, model = resolved

        try:
            sessions = self._sessions.list_sessions(include_archived=False)
        except Exception as e:  # noqa: BLE001
            outcome.skipped_reason = f"获取会话失败: {e}"
            return outcome

        sessions = sorted(sessions, key=lambda s: getattr(s, "updated_at", "") or "", reverse=True)[
            : (max_sessions or summary_max_sessions())
        ]

        state = self._load_state()
        for session in sessions:
            outcome.scanned += 1
            try:
                messages = self._sessions.list_messages(session.id)
            except Exception:  # noqa: BLE001
                continue
            visible = [m for m in messages if m.role in ("user", "assistant") and (m.content or "").strip()]
            cursor = state.get(session.id, 0)
            new_msgs = visible[cursor:]
            if len(new_msgs) < min_new_messages:
                continue

            try:
                items = await self._summarize_once(provider, model, session, new_msgs)
            except Exception as e:  # noqa: BLE001
                logger.warning("会话 %s 总结失败: %s", session.id, e)
                continue

            saved = 0
            for item in items:
                key = str(item.get("key") or "").strip()
                value = str(item.get("value") or "").strip()
                if not key or not value:
                    continue
                scope = str(item.get("scope") or "global")
                # 必须带上会话归属用户：否则启用认证（E12）后，记忆以 user_id=''
                # 落库，而 list/search/render_hint 都按 user_id 过滤，
                # 自动总结产生的记忆对用户永久不可见，却仍占用存储。
                self._memory.save(
                    key=key,
                    value=value,
                    scope=scope,
                    session_id=session.id if scope == "session" else "",
                    tags=["auto-summary"],
                    user_id=str(getattr(session, "user_id", "") or ""),
                )
                saved += 1

            # 无论是否提炼出记忆，都推进游标，避免重复处理同一批消息
            state[session.id] = len(visible)
            outcome.summarized += 1
            outcome.saved += saved
            outcome.details.append(
                {
                    "session_id": session.id,
                    "title": getattr(session, "title", ""),
                    "new_messages": len(new_msgs),
                    "memories": saved,
                }
            )

        self._save_state(state)
        return outcome

    async def _summarize_once(
        self, provider: Any, model: str, session: Any, messages: list[Any]
    ) -> list[dict[str, Any]]:
        transcript = self._render_transcript(messages)
        prompt = f"会话标题：{getattr(session, 'title', '') or '(无)'}\n以下是新增的对话片段：\n\n{transcript}"
        content = await self._call_model(provider, model, prompt)
        return self._parse_memories(content)

    @staticmethod
    def _render_transcript(messages: list[Any]) -> str:
        lines: list[str] = []
        for m in messages[-_MAX_MESSAGES_PER_SESSION:]:
            role = "用户" if m.role == "user" else "AI"
            text = (m.content or "").strip()
            if len(text) > _MAX_CHARS_PER_MESSAGE:
                text = text[:_MAX_CHARS_PER_MESSAGE] + "…"
            lines.append(f"[{role}] {text}")
        transcript = "\n".join(lines)
        if len(transcript) > _MAX_TRANSCRIPT_CHARS:
            transcript = transcript[-_MAX_TRANSCRIPT_CHARS:]
        return transcript

    @staticmethod
    async def _call_model(provider: Any, model: str, prompt: str) -> str:
        parts: list[str] = []
        async for chunk in provider.chat(
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            model=model,
            stream=True,
            temperature=0.2,
        ):
            if isinstance(chunk, dict) and chunk.get("delta"):
                parts.append(str(chunk["delta"]))
        return "".join(parts).strip()

    @staticmethod
    def _parse_memories(text: str) -> list[dict[str, Any]]:
        """从模型输出里稳健地抽出 JSON 数组。"""
        if not text:
            return []
        candidate = text.strip()
        # 去掉 ```json ``` 包裹
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict) and item.get("key") and item.get("value"):
                out.append(item)
        return out


async def run_summary_loop(summarizer: MemorySummarizer) -> None:
    """后台循环：按间隔执行总结，直到被取消。"""
    interval = summary_interval()
    logger.info("记忆自动总结已启动（间隔 %d 秒）", interval)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("记忆自动总结已停止")
            raise
        try:
            outcome = await summarizer.summarize_recent()
            logger.info(
                "记忆自动总结完成：扫描 %d，总结 %d，新增记忆 %d%s",
                outcome.scanned,
                outcome.summarized,
                outcome.saved,
                f"（跳过：{outcome.skipped_reason}）" if outcome.skipped_reason else "",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆自动总结出错（已忽略，下轮重试）: %s", e)
