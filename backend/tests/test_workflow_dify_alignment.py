"""工作流节点 Dify 对齐能力测试（引擎直跑，不经 HTTP 服务层）。

对齐基线：https://docs.dify.ai/zh/cloud/use-dify/nodes/*
- Start：输入类型转换（number/integer/boolean）
- LLM：模型参数透传 / 推理分离开关 / 重试
- Condition：多条件 AND/OR + 运算符全集
- HTTP：查询参数 / 认证 / 请求体类型 / 输出（status_code/headers/size）
- Template：Jinja2 渲染（回退简单 {{}} 替换）
- Assign：赋值/清空/追加/合并/四则运算
- Code：可配置超时与重试（字符串数值不崩）
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from harness.modules.workflows.engine import GraphEngine


def _graph(*nodes: dict[str, Any], edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"nodes": list(nodes), "edges": edges or []}


def _node(nid: str, ntype: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": data}


def _chain(*nodes: dict[str, Any]) -> dict[str, Any]:
    """直线链式连线：n0 → n1 → n2 …（condition 之后的节点同时接 true/false）。"""
    edges: list[dict[str, Any]] = []
    for i in range(len(nodes) - 1):
        src, tgt = nodes[i], nodes[i + 1]
        if src["type"] == "condition":
            for handle in ("true", "false"):
                edges.append(
                    {
                        "id": f"e{i}{handle[0]}",
                        "source": src["id"],
                        "target": tgt["id"],
                        "sourceHandle": handle,
                        "targetHandle": "in",
                    }
                )
        else:
            edges.append(
                {
                    "id": f"e{i}",
                    "source": src["id"],
                    "target": tgt["id"],
                    "sourceHandle": "out",
                    "targetHandle": "in",
                }
            )
    return _graph(*nodes, edges=edges)


class _MockProvider:
    """记录 chat 收到的 kwargs，并回放预置的流式分片。"""

    def __init__(self, chunks: list[dict[str, Any]] | None = None, fail_times: int = 0) -> None:
        self.chunks = chunks or [{"delta": "ok"}]
        self.fail_times = fail_times
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any):
        self.calls.append(kwargs)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("temporary failure")
        for c in self.chunks:
            yield c


def _engine_with_provider(provider: Any) -> GraphEngine:
    engine = GraphEngine(None)
    engine._get_provider = lambda pid: provider  # type: ignore[method-assign]
    return engine


class TestStartTypeCasting:
    @pytest.mark.asyncio
    async def test_number_integer_boolean_casting(self) -> None:
        engine = GraphEngine(None)
        graph = _chain(
            _node(
                "start",
                "start",
                {
                    "variables": [
                        {"name": "price", "type": "number", "default": "3.5"},
                        {"name": "count", "type": "integer", "default": "7.9"},
                        {"name": "flag", "type": "boolean", "default": "true"},
                    ]
                },
            ),
            _node("end", "end", {"outputs": [{"name": "p", "value": "{{price}}"}]}),
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success"
        outs = engine._node_outputs["start"]
        assert outs["price"] == 3.5 and isinstance(outs["price"], float)
        assert outs["count"] == 7 and isinstance(outs["count"], int)
        assert outs["flag"] is True

    @pytest.mark.asyncio
    async def test_run_inputs_override_defaults(self) -> None:
        engine = GraphEngine(None)
        graph = _chain(
            _node("start", "start", {"variables": [{"name": "n", "type": "number", "default": "1"}]}),
            _node("end", "end", {"outputs": [{"name": "n", "value": "{{n}}"}]}),
        )
        await engine.execute(graph, {"n": "42"})
        assert engine._node_outputs["start"]["n"] == 42.0


class TestLlmAlignment:
    @pytest.mark.asyncio
    async def test_model_params_passed_to_provider(self) -> None:
        provider = _MockProvider([{"delta": "hi"}])
        engine = _engine_with_provider(provider)
        graph = _chain(
            _node("start", "start", {"variables": []}),
            _node(
                "llm1",
                "llm",
                {
                    "model": "m1",
                    "user_prompt": "hello",
                    "temperature": "0.3",
                    "top_p": "0.9",
                    "frequency_penalty": "0.1",
                    "presence_penalty": "0.2",
                    "max_tokens": "128",
                },
            ),
            _node("end", "end", {"outputs": [{"name": "t", "value": "{{llm1.text}}"}]}),
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success"
        sent = provider.calls[0]
        assert sent["temperature"] == 0.3
        assert sent["top_p"] == 0.9
        assert sent["max_tokens"] == 128

    @pytest.mark.asyncio
    async def test_reasoning_merged_into_text(self) -> None:
        provider = _MockProvider([{"reasoning_content": "想一想"}, {"delta": "答案"}])
        engine = _engine_with_provider(provider)
        graph = _chain(
            _node("start", "start", {"variables": []}),
            _node("llm1", "llm", {"user_prompt": "q", "reasoning_format": "merged"}),
            _node("end", "end", {"outputs": [{"name": "t", "value": "{{llm1.text}}"}]}),
        )
        await engine.execute(graph, {})
        outs = engine._node_outputs["llm1"]
        assert outs["reasoning"] == ""
        assert outs["text"] == "想一想答案"

    @pytest.mark.asyncio
    async def test_retry_on_failure(self) -> None:
        provider = _MockProvider([{"delta": "recovered"}], fail_times=2)
        engine = _engine_with_provider(provider)
        graph = _chain(
            _node("start", "start", {"variables": []}),
            _node("llm1", "llm", {"user_prompt": "q", "max_retries": 2, "retry_interval": 0}),
            _node("end", "end", {"outputs": [{"name": "t", "value": "{{llm1.text}}"}]}),
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success"
        assert engine._node_outputs["llm1"]["text"] == "recovered"


class TestConditionAlignment:
    @staticmethod
    def _cond_graph(cond_data: dict[str, Any]) -> dict[str, Any]:
        """start → condition → endT(true) / endF(false)，满足「两分支都须连线」校验。"""
        return _graph(
            _node("start", "start", {"variables": [{"name": "a", "type": "string", "default": "5"}]}),
            _node("cond", "condition", cond_data),
            _node("endT", "end", {"outputs": [{"name": "r", "value": "hit-true"}]}),
            _node("endF", "end", {"outputs": [{"name": "r", "value": "hit-false"}]}),
            edges=[
                {"id": "e1", "source": "start", "target": "cond", "sourceHandle": "out", "targetHandle": "in"},
                {"id": "e2", "source": "cond", "target": "endT", "sourceHandle": "true", "targetHandle": "in"},
                {"id": "e3", "source": "cond", "target": "endF", "sourceHandle": "false", "targetHandle": "in"},
            ],
        )

    @pytest.mark.asyncio
    async def test_multi_condition_and(self) -> None:
        engine = GraphEngine(None)
        graph = self._cond_graph(
            {
                "logic": "and",
                "conditions": [
                    {"left": "{{a}}", "op": ">", "right": "3"},
                    {"left": "{{a}}", "op": "!=", "right": "9"},
                ],
            }
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success"
        assert result["results"]["cond"]["branch"] == "true"
        assert result["results"]["endT"]["status"] == "success"
        assert result["results"]["endF"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_or_logic_takes_false_branch(self) -> None:
        engine = GraphEngine(None)
        graph = self._cond_graph(
            {
                "logic": "or",
                "conditions": [
                    {"left": "{{a}}", "op": ">", "right": "99"},
                    {"left": "{{a}}", "op": "starts_with", "right": "9"},
                ],
            }
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success"
        assert result["results"]["cond"]["branch"] == "false"
        assert result["results"]["endF"]["status"] == "success"
        assert result["results"]["endT"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_legacy_single_condition_still_works(self) -> None:
        """旧图结构（left/op/right 顶层字段）保持兼容。"""
        engine = GraphEngine(None)
        graph = self._cond_graph({"left": "{{a}}", "op": "==", "right": "5"})
        result = await engine.execute(graph, {})
        assert result["results"]["cond"]["branch"] == "true"
        assert result["results"]["endT"]["status"] == "success"

    def test_operator_full_set(self) -> None:
        cmp = GraphEngine._compare
        assert cmp("hello world", "world", "contains") == "true"
        assert cmp("hello", "xyz", "not_contains") == "true"
        assert cmp("hello", "he", "starts_with") == "true"
        assert cmp("hello", "lo", "ends_with") == "true"
        assert cmp("", "", "is_empty") == "true"
        assert cmp("x", "", "is_not_empty") == "true"
        assert cmp("5", "3", ">") == "true"
        assert cmp(5, 5, "==") == "true"
        assert cmp("a", "a", "is") == "true"
        assert cmp("a", "b", "is_not") == "true"


class TestAssignAlignment:
    @pytest.mark.asyncio
    async def test_assign_ops(self) -> None:
        engine = GraphEngine(None)
        graph = _chain(
            _node("start", "start", {"variables": [{"name": "n", "type": "number", "default": "10"}]}),
            _node(
                "as1",
                "assign",
                {
                    "variables": [
                        {"name": "n", "op": "add", "value": "5"},
                        {"name": "log", "op": "append", "value": "a"},
                        {"name": "tmp", "op": "set", "value": "x"},
                        {"name": "tmp", "op": "clear", "value": ""},
                    ]
                },
            ),
            _node("end", "end", {"outputs": [{"name": "n", "value": "{{n}}"}]}),
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success"
        g = engine._globals
        assert g["n"] == 15.0
        assert g["log"] == ["a"]
        assert g["tmp"] is None


class TestTemplateJinja2:
    @pytest.mark.asyncio
    async def test_jinja2_loop_and_condition(self) -> None:
        engine = GraphEngine(None)
        graph = _chain(
            _node("start", "start", {"variables": [{"name": "name", "type": "string", "default": "world"}]}),
            _node(
                "tpl",
                "template",
                {
                    "template": "{% for i in [1, 2] %}{{ name }}{{ i }};{% endfor %}{% if name %}[set]{% endif %}",
                },
            ),
            _node("end", "end", {"outputs": [{"name": "out", "value": "{{tpl.output}}"}]}),
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success"
        assert engine._node_outputs["tpl"]["output"] == "world1;world2;[set]"

    @pytest.mark.asyncio
    async def test_legacy_placeholder_fallback(self) -> None:
        engine = GraphEngine(None)
        graph = _chain(
            _node("start", "start", {"variables": [{"name": "topic", "type": "string", "default": "t1"}]}),
            _node("tpl", "template", {"template": "关于{{ topic }}"}),
            _node("end", "end", {"outputs": [{"name": "out", "value": "{{tpl.output}}"}]}),
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success"
        assert engine._node_outputs["tpl"]["output"] == "关于t1"


class TestCodeTimeoutConfig:
    @pytest.mark.asyncio
    async def test_string_numbers_do_not_crash(self) -> None:
        """timeout/max_retries 为字符串（前端 number input 产出）时不得抛 ValueError。"""
        engine = GraphEngine(None)
        graph = _chain(
            _node("start", "start", {"variables": []}),
            _node("code1", "code", {"code": "output = {'r': 1}", "timeout": "15", "max_retries": "0.0"}),
            _node("end", "end", {"outputs": [{"name": "r", "value": "{{code1.r}}"}]}),
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success", result
        assert engine._node_outputs["code1"]["r"] == 1


class TestHttpAlignment:
    @pytest.mark.asyncio
    async def test_query_params_auth_body_and_outputs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        captured: dict[str, Any] = {}

        class _Resp:
            status = 200

            def read(self) -> bytes:
                return json.dumps({"ok": True}).encode()

            def getheaders(self) -> list[tuple[str, str]]:
                return [("Content-Type", "application/json")]

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

        def fake_urlopen(req: Any, timeout: float = 0, context: Any = None) -> _Resp:
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["method"] = req.get_method()
            captured["data"] = req.data
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        engine = GraphEngine(None)
        graph = _chain(
            _node("start", "start", {"variables": []}),
            _node(
                "http1",
                "http",
                {
                    "method": "POST",
                    "url": "https://example.com/api",
                    "params": [{"key": "q", "value": "中文"}],
                    "headers": [{"key": "X-Trace", "value": "t1"}],
                    "auth": {"type": "api-key", "config": {"kind": "bearer", "token": "sekret"}},
                    "body_type": "json",
                    "body": '{"a": 1}',
                    "timeout": "10",
                    "ssl_verify": True,
                },
            ),
            _node("end", "end", {"outputs": [{"name": "s", "value": "{{http1.status_code}}"}]}),
        )
        result = await engine.execute(graph, {})
        assert result["status"] == "success", result

        assert "q=%E4%B8%AD%E6%96%87" in captured["url"]
        assert captured["method"] == "POST"
        # urllib 会规范化头名大小写（X-Trace → X-trace），统一按小写比较
        hdrs = {k.lower(): v for k, v in captured["headers"].items()}
        assert hdrs["authorization"] == "Bearer sekret"
        assert hdrs["x-trace"] == "t1"
        assert hdrs["content-type"] == "application/json"
        assert captured["data"] == b'{"a": 1}'

        outs = engine._node_outputs["http1"]
        assert outs["status_code"] == 200
        assert outs["headers"]["Content-Type"] == "application/json"
        assert outs["size"] > 0
