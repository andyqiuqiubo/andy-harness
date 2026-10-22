"""SSE 流式改写：tool_calls 索引重编号 + 指纹工具抑制。

设计依据见 docs/reports/andy-gateway-竞品与借鉴点分析-2026-10-22.md §4-P0-2，
原始实现在 dsh-our-free-model（`src/forward.js` 头注 + `src/stream.js` `toolKey`）：

1. **索引重编号**：上游块 index 与 reasoning/text 块共享编号空间时，思考回合
   的第一个工具调用常以 index 2+ 到达。按 index 累积 `tool_calls` 的 OpenAI
   客户端会拿到稀疏数组——合并或丢弃参数（dsh issue #20 "forwarded clients
   cannot call tools"）。这里按**首次放行顺序**重映射为 0..n 连续编号；上游
   本就连续时是恒等映射。
2. **指纹工具抑制**：`requires_tools` 通道为过免费档闸门注入的工具
   （read/shell/bash/…）若被模型调用，客户端会收到一个它从未注册的工具，
   回报一个它无力处理的错误。这些调用整条拦掉。仅当调用方自己没有传 tools
   时才抑制——调用方注册了自己的 `read` 时那个调用是合法的。

名字跨 delta 拆分时的未决处理：部分名字若仍是指纹名字的前缀（"gr" 可能长成
"grep"），整条 delta 进缓冲不放行，直到名字"已决"——要么不再是任何指纹名字的
前缀（放行缓冲），要么参数到达（名字已完整，精确匹配则丢弃）。流结束时
`flush()` 对仍未决的缓冲做最终判定。被丢弃的键不占用重编号序号。
"""

from __future__ import annotations

import json
from typing import Any


class ToolCallRewriter:
    """有状态：一个上游 SSE 流一个实例（键空间随消息累积）。"""

    def __init__(self, suppress_names: frozenset[str] | set[str] = frozenset()) -> None:
        self._suppress = frozenset(suppress_names)
        self._remap: dict[str, int] = {}
        self._dropped: set[str] = set()
        self._decided: set[str] = set()
        self._name_buf: dict[str, str] = {}
        self._held: dict[str, list[dict[str, Any]]] = {}
        self._last_key = ""

    # ---- 对外 ---------------------------------------------------------- #
    def rewrite_line(self, line: str) -> list[str]:
        """改写一行 SSE（不含换行）。返回 0..n 行；空列表 = 整行丢弃。"""
        if not line.startswith("data: ") or "tool_calls" not in line:
            return [line]
        raw = line[6:].strip()
        if raw == "[DONE]":
            return [line]
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            return [line]
        if not isinstance(chunk, dict):
            return [line]

        out: list[str] = []
        had_tool_calls = False
        kept_any = False
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            tcs = delta.get("tool_calls")
            if not isinstance(tcs, list) or not tcs:
                continue
            had_tool_calls = True
            kept: list[dict[str, Any]] = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    kept.append(tc)
                    kept_any = True
                    continue
                key = self._key(tc)
                result = self._accept(tc, key)
                if result == "keep":
                    kept.append(self._renumber(tc, key))
                    kept_any = True
                elif result == "flush":
                    # 未决名字刚刚决断为放行：先补放之前缓冲的同键 delta
                    for held in self._held.pop(key, []):
                        out.append(self._frame(self._renumber(held, key)))
                        kept_any = True
                    kept.append(self._renumber(tc, key))
                    kept_any = True
                # "drop" / "drop_hold"：不进 kept（drop_hold 的已进缓冲，待 flush）
            delta["tool_calls"] = kept
            if not kept:
                del delta["tool_calls"]

        if had_tool_calls and not kept_any and self._frame_is_droppable(chunk):
            # 整帧只剩被抑制的工具调用、无 usage / finish_reason / 其它 delta 内容
            return out
        out.append("data: " + json.dumps(chunk, ensure_ascii=False))
        return out

    def flush(self) -> list[str]:
        """流结束：对仍未决的缓冲做最终判定（精确匹配指纹名则丢弃）。"""
        out: list[str] = []
        for key, held in list(self._held.items()):
            name = self._name_buf.get(key, "")
            if name in self._suppress:
                self._dropped.add(key)
            else:
                for tc in held:
                    out.append(self._frame(self._renumber(tc, key)))
            self._held.pop(key, None)
        return out

    # ---- 内部 ---------------------------------------------------------- #
    @staticmethod
    def _frame_is_droppable(chunk: dict[str, Any]) -> bool:
        """整帧是否可以在工具调用被全部抑制后丢弃（不丢 usage/终帧/其它内容）。"""
        if chunk.get("usage"):
            return False
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                return False
            if choice.get("finish_reason"):
                return False
            delta = choice.get("delta")
            if isinstance(delta, dict) and any(k for k in delta if k != "tool_calls"):
                return False
        return True

    def _key(self, tc: dict[str, Any]) -> str:
        """tool_call 的累积键：index → id → 前一个键（dsh toolKey 同序）。"""
        idx = tc.get("index")
        if idx is not None:
            self._last_key = f"i{int(idx)}"
            return self._last_key
        cid = tc.get("id")
        if isinstance(cid, str) and cid:
            self._last_key = f"d{cid}"
            return self._last_key
        return self._last_key or "i0"

    def _renumber(self, tc: dict[str, Any], key: str) -> dict[str, Any]:
        out = dict(tc)
        if key not in self._remap:
            self._remap[key] = len(self._remap)
        out["index"] = self._remap[key]
        return out

    def _accept(self, tc: dict[str, Any], key: str) -> str:
        """返回 keep / drop / drop_hold / flush。"""
        if key in self._dropped:
            return "drop"
        if key in self._decided:
            return "keep"
        if not self._suppress:
            self._decided.add(key)
            return "keep"

        fn = tc.get("function")
        name_frag = fn.get("name") if isinstance(fn, dict) else None
        if name_frag:
            name = self._name_buf.get(key, "") + str(name_frag)
            self._name_buf[key] = name
            if name in self._suppress:
                self._held.setdefault(key, []).append(tc)
                if self._has_args(tc):
                    # 参数已到 = 名字完整且精确命中指纹名 → 丢弃
                    self._dropped.add(key)
                    self._held.pop(key, None)
                    return "drop"
                return "drop_hold"  # 名字还可能继续增长（"grep"→"grep_results"）
            if any(f.startswith(name) for f in self._suppress):
                self._held.setdefault(key, []).append(tc)
                return "drop_hold"
            self._decided.add(key)
            return "flush"
        # 无名字分片：参数到达即视为名字已完整
        if key in self._held:
            name = self._name_buf.get(key, "")
            if name in self._suppress:
                self._dropped.add(key)
                self._held.pop(key, None)
                return "drop"
            self._decided.add(key)
            return "flush"
        self._decided.add(key)
        return "keep"

    @staticmethod
    def _has_args(tc: dict[str, Any]) -> bool:
        fn = tc.get("function")
        return bool(isinstance(fn, dict) and fn.get("arguments"))

    @staticmethod
    def _frame(tc: dict[str, Any]) -> str:
        return "data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [tc]}}]}, ensure_ascii=False)
