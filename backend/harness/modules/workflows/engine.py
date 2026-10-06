"""工作流图执行引擎（Dify 风格：Variable Pool + 节点端口 + 分支感知拓扑调度）。

设计参考 Dify 工作流：
- 左侧控件列表拖入节点，无限画布上自由排布，节点通过输入/输出端口连线；
- 节点间通过统一的 Variable Pool 传递数据，引用语法 ``{{nodeId.field}}`` 或全局变量 ``{{varName}}``；
- 每个节点执行后产生输出字段，下游节点可引用；
- 条件分支节点（condition）有两个输出端口 ``true`` / ``false``，仅沿命中的分支继续。

支持的节点类型：
    start      入口：定义输入变量（同时写入全局变量池）
    end        出口：声明最终输出（引用上游变量）
    llm        调用大模型（复用 ProviderRegistry，流式聚合）
    code       执行 Python 代码（子进程沙箱，超时隔离）
    condition  IF/ELSE 分支（true/false 两个输出端口）
    http       发起 HTTP 请求（urllib，零额外依赖）
    template   模板渲染（支持 {{ node.field }} 占位）
    assign     变量赋值（Variable Assigner，写入全局变量池）

图结构：
    {"nodes": [{"id","type","position":{"x","y"},"data":{...}}],
     "edges": [{"id","source","target","sourceHandle","targetHandle"}]}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("harness.workflow.engine")

_REF_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


class WorkflowValidationError(Exception):
    """工作流图不合法。"""


class WorkflowExecutionError(Exception):
    """节点执行失败。"""


class GraphEngine:
    """有向无环图的执行引擎（分支感知拓扑调度）。"""

    def __init__(self, services: Any | None = None) -> None:
        self._services = services
        self._node_outputs: dict[str, dict[str, Any]] = {}
        self._globals: dict[str, Any] = {}

    # ── 公共入口 ────────────────────────────────────────
    async def execute(self, graph: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        nodes = {n["id"]: n for n in graph.get("nodes", [])}
        edges = graph.get("edges", [])

        self._validate(nodes, edges)
        self._node_outputs = {}
        self._globals = dict(inputs or {})

        incoming: dict[str, list[dict[str, Any]]] = {nid: [] for nid in nodes}
        outgoing: dict[str, list[dict[str, Any]]] = {nid: [] for nid in nodes}
        for e in edges:
            if e["source"] in outgoing and e["target"] in incoming:
                outgoing[e["source"]].append(e)
                incoming[e["target"]].append(e)

        results: dict[str, dict[str, Any]] = {
            nid: {"status": "pending", "outputs": {}, "error": "", "elapsed_ms": 0} for nid in nodes
        }
        taken: dict[str, bool] = {}

        indeg = {nid: len(incoming[nid]) for nid in nodes}
        executed: set[str] = set()
        # 处理顺序（执行先后，便于调试展示）
        exec_order: list[str] = []

        errored = False
        while True:
            candidates = [nid for nid in nodes if nid not in executed and indeg.get(nid, 0) == 0]
            if not candidates:
                break
            nid = candidates[0]
            # 所有入边必须「已判定且命中」，否则该节点不可达（跳过）
            ins = incoming[nid]
            if ins and not all(taken.get(e["id"], False) for e in ins):
                executed.add(nid)
                results[nid]["status"] = "skipped"
                continue

            node = nodes[nid]
            t0 = time.time()
            try:
                outputs, branch = await self._run_node(node)
                status = "success"
                error = ""
            except Exception as e:  # noqa: BLE001
                outputs, branch = {}, None
                status = "error"
                error = str(e)
                logger.warning("工作流节点 %s(%s) 执行失败: %s", nid, node.get("type"), e)
            elapsed = int((time.time() - t0) * 1000)

            results[nid] = {
                "status": status,
                "outputs": outputs,
                "error": error,
                "elapsed_ms": elapsed,
                "branch": branch,
            }
            self._node_outputs[nid] = outputs
            if node.get("type") == "start":
                self._globals.update(outputs)
            if node.get("type") == "assign":
                self._globals.update(outputs)
            executed.add(nid)
            exec_order.append(nid)

            if status == "error":
                errored = True
                break

            # 判定该节点出边是否命中
            for edge in outgoing[nid]:
                if node.get("type") == "condition":
                    is_taken = edge.get("sourceHandle") == branch
                else:
                    is_taken = True
                taken[edge["id"]] = is_taken
                if is_taken:
                    indeg[edge["target"]] -= 1

        # 未执行的剩余节点标记为 skipped
        for nid in nodes:
            if nid not in executed:
                results[nid]["status"] = "skipped"

        # 汇总 end 节点输出
        end_outputs: dict[str, dict[str, Any]] = {}
        for nid, node in nodes.items():
            if node.get("type") == "end" and results[nid]["status"] == "success":
                end_outputs[nid] = self._collect_end_outputs(node)

        overall = "error" if errored else "success"
        return {
            "status": overall,
            "results": results,
            "end_outputs": end_outputs,
            "order": exec_order,
        }

    # ── 校验 ────────────────────────────────────────────
    def _validate(self, nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        if not nodes:
            raise WorkflowValidationError("工作流为空：至少需要一个节点")
        starts = [n for n in nodes.values() if n.get("type") == "start"]
        if len(starts) != 1:
            raise WorkflowValidationError("工作流必须且只能有一个「开始」节点")
        ends = [n for n in nodes.values() if n.get("type") == "end"]
        if not ends:
            raise WorkflowValidationError("工作流至少需要一个「结束」节点")
        for n in nodes.values():
            if n.get("type") == "condition":
                handles = {e.get("sourceHandle") for e in edges if e.get("source") == n["id"]}
                if "true" not in handles or "false" not in handles:
                    raise WorkflowValidationError(f"条件分支节点 {n['id']} 必须同时连接 true 与 false 两个分支")

    # ── 节点分发 ────────────────────────────────────────
    async def _run_node(self, node: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        ntype = node.get("type")
        data = node.get("data", {}) or {}
        if ntype == "start":
            return self._run_start(data), None
        if ntype == "end":
            return {}, None
        if ntype == "llm":
            return await self._run_llm(data), None
        if ntype == "code":
            return await self._run_code(data), None
        if ntype == "condition":
            return self._run_condition(data)
        if ntype == "http":
            return await self._run_http(data), None
        if ntype == "template":
            return self._run_template(data), None
        if ntype == "assign":
            return self._run_assign(data), None
        raise WorkflowValidationError(f"未知节点类型: {ntype}")

    # ── 各节点实现 ──────────────────────────────────────
    def _run_start(self, data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for v in data.get("variables", []) or []:
            name = str(v.get("name", "")).strip()
            if not name:
                continue
            # 优先使用运行输入值，缺失时回退到节点默认值
            value = self._globals.get(name, v.get("default", ""))
            vtype = str(v.get("type", "string")).lower()
            if vtype in ("number", "float"):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = 0.0
            elif vtype == "integer":
                try:
                    value = int(float(value))
                except (TypeError, ValueError):
                    value = 0
            elif vtype in ("boolean", "bool"):
                # Dify 布尔输入：兼容字符串 "true"/"1"/"yes" 等写法
                value = str(value).strip().lower() in ("1", "true", "yes", "on")
            out[name] = value
        return out

    def _run_end(self, data: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - 占位
        return {}

    def _collect_end_outputs(self, node: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for o in node.get("data", {}).get("outputs", []) or []:
            name = str(o.get("name", "")).strip()
            if not name:
                continue
            try:
                out[name] = self.resolve(str(o.get("value", "")))
            except Exception:  # noqa: BLE001
                out[name] = self.render(str(o.get("value", "")))
        return out

    async def _run_llm(self, data: dict[str, Any]) -> dict[str, Any]:
        provider_id = str(data.get("provider_id") or "deepseek")
        model = str(data.get("model") or "deepseek-flash")
        system = self.render(str(data.get("system_prompt", "") or ""))
        user = self.render(str(data.get("user_prompt", "") or ""))
        provider = self._get_provider(provider_id)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        # 模型参数（对齐 Dify：温度 / 核采样 / 频率惩罚 / 存在惩罚 / 最大令牌）
        params: dict[str, Any] = {}
        for key, caster in (
            ("temperature", float),
            ("top_p", float),
            ("frequency_penalty", float),
            ("presence_penalty", float),
            ("max_tokens", int),
        ):
            raw = data.get(key)
            if raw not in (None, ""):
                try:
                    params[key] = caster(raw)
                except (TypeError, ValueError):
                    pass
        # 推理分离开关（对齐 Dify reasoning_format：separated 默认分离，merged 合并进正文）
        reasoning_format = str(data.get("reasoning_format") or "separated")
        # 错误处理：重试（对齐 Dify 最大重试 + 间隔）
        max_retries = self._to_int(data.get("max_retries"), 0)
        retry_interval = self._to_float(data.get("retry_interval"), 1.0)

        async def _call() -> tuple[str, str]:
            tp: list[str] = []
            rp: list[str] = []
            async for chunk in provider.chat(messages, model, stream=True, **params):
                delta = chunk.get("delta")
                if isinstance(delta, str) and delta:
                    tp.append(delta)
                rc = chunk.get("reasoning_content")
                if isinstance(rc, str) and rc:
                    rp.append(rc)
            return "".join(tp), "".join(rp)

        text, reasoning = await self._retryable(_call, max_retries, retry_interval)
        if reasoning_format == "merged":
            text = (reasoning + text) if reasoning else text
            reasoning = ""
        return {"text": text, "reasoning": reasoning}

    async def _retryable(self, coro_fn: Callable[[], Awaitable[Any]], max_retries: int, interval: float) -> Any:
        """对协程函数做最多 max_retries 次重试（指数退避），全失败则抛出最后一次异常。"""
        last: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return await coro_fn()
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt >= max_retries:
                    break
                await asyncio.sleep(interval * (2**attempt))
        assert last is not None
        raise last

    async def _run_code(self, data: dict[str, Any]) -> dict[str, Any]:
        # 与系统其余节点保持一致的引用语法：支持 {{ node.field }} 模板替换
        code = self.render(str(data.get("code", "") or ""))
        if not code.strip():
            return {}
        # 注入可用变量：全局变量 + 各节点输出（以 nodeId.field 形式）
        env: dict[str, Any] = dict(self._globals)
        for nid, outs in self._node_outputs.items():
            if isinstance(outs, dict):
                for k, v in outs.items():
                    env[f"{nid}.{k}"] = v
        script = self._build_code_script(code, env)
        timeout = self._to_float(data.get("timeout"), 15.0)
        max_retries = self._to_int(data.get("max_retries"), 0)
        retry_interval = self._to_float(data.get("retry_interval"), 1.0)

        async def _call() -> dict[str, Any]:
            proc = await asyncio.to_thread(self._run_subprocess, script, timeout)
            if proc.returncode != 0:
                raise WorkflowExecutionError(
                    f"代码执行失败 (exit={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
                )
            try:
                parsed = json.loads(proc.stdout)
            except json.JSONDecodeError as e:
                raise WorkflowExecutionError(f"代码输出不是合法 JSON: {e}") from e
            return parsed if isinstance(parsed, dict) else {"result": parsed}

        out = await self._retryable(_call, max_retries, retry_interval)
        return dict(out)

    @staticmethod
    def _build_code_script(code: str, env: dict[str, Any]) -> str:
        return (
            "import json, math, sys\n"
            "_inputs = " + json.dumps(env, ensure_ascii=False) + "\n"
            "globals().update(_inputs)\n"
            "output = {}\n" + code + "\n"
            "print(json.dumps(output if isinstance(output, dict) else {'result': output}, ensure_ascii=False))\n"
        )

    @staticmethod
    def _run_subprocess(script: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        fd, path = None, None
        try:
            import tempfile

            fd, path = tempfile.mkstemp(suffix=".py", prefix="wf_code_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(script)
            fd = None
            return subprocess.run(
                [sys.executable, path],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tempfile.gettempdir(),
            )
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _run_condition(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        # 兼容旧结构（单条件 left/op/right）；新结构支持多条件 + logic(and/or)
        conds = data.get("conditions")
        if not isinstance(conds, list) or not conds:
            conds = [
                {
                    "left": data.get("left", ""),
                    "op": data.get("op", "=="),
                    "right": data.get("right", ""),
                }
            ]
        logic = str(data.get("logic", "and")).lower()
        detail: list[dict[str, Any]] = []
        results: list[bool] = []
        for c in conds:
            left = self._operand(str(c.get("left", "") or ""))
            right = self._operand(str(c.get("right", "") or ""))
            op = str(c.get("op", "=="))
            ok = self._compare(left, right, op) == "true"
            results.append(ok)
            detail.append({"left": left, "op": op, "right": right, "result": ok})
        branch = "true" if (all(results) if logic == "and" else any(results)) else "false"
        return {"branch": branch, "conditions": detail}, branch

    async def _run_http(self, data: dict[str, Any]) -> dict[str, Any]:
        url = self.render(str(data.get("url", "") or ""))
        if not url:
            raise WorkflowValidationError("HTTP 节点缺少 url")
        method = str(data.get("method", "GET")).upper()
        # 请求头兼容两种形态：前端 [{key,value}] 数组 / 传统 dict
        raw_headers = data.get("headers", {}) or {}
        if isinstance(raw_headers, list):
            hdrs_in = {
                str(h.get("key", "")): str(h.get("value", ""))
                for h in raw_headers
                if isinstance(h, dict) and str(h.get("key", "")).strip()
            }
        else:
            hdrs_in = {str(k): str(v) for k, v in raw_headers.items()}
        headers: dict[str, str] = {k: self.render(v) for k, v in hdrs_in.items()}
        # 查询参数
        params = data.get("params") or []
        if params:
            from urllib.parse import urlencode

            q = urlencode(
                {str(p["key"]): self.render(str(p["value"])) for p in params if str(p.get("key", "")).strip()}
            )
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{q}"
        # 身份认证（对齐 Dify：no-auth / api-key basic|bearer|custom）
        auth = data.get("auth") or {"type": "no-auth"}
        if auth.get("type") == "api-key":
            self._apply_auth(headers, auth.get("config") or {})
        # 请求体（body_type: json|form|raw|binary）
        body_type = str(data.get("body_type") or "raw").lower()
        body_raw = data.get("body", "")
        body_bytes: bytes | None = None
        if body_raw not in (None, ""):
            rendered = self.render(str(body_raw))
            if body_type == "json":
                headers.setdefault("Content-Type", "application/json")
                body_bytes = rendered.encode("utf-8")
            elif body_type == "form":
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                from urllib.parse import urlencode as _ue

                form = {
                    str(p.get("key", "")): self.render(str(p.get("value", "")))
                    for p in (data.get("form", []) or [])
                    if str(p.get("key", "")).strip()
                }
                body_bytes = _ue(form).encode("utf-8")
            else:  # raw / binary 均按原始文本发送
                if body_type == "raw" and "Content-Type" not in {k.title() for k in headers}:
                    headers.setdefault("Content-Type", "text/plain")
                body_bytes = rendered.encode("utf-8")

        req = urllib.request.Request(url, data=body_bytes, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        timeout = self._to_float(data.get("timeout"), 15.0)
        ssl_verify = bool(data.get("ssl_verify", True))
        max_retries = self._to_int(data.get("max_retries"), 0)
        retry_interval = self._to_float(data.get("retry_interval"), 1.0)

        async def _call() -> tuple[int, str, dict[str, str]]:
            # urllib 为阻塞调用，丢线程池避免卡事件循环；包成协程以走 _retryable 重试
            def _blocking() -> tuple[int, str, dict[str, str]]:
                ctx = None
                if not ssl_verify:
                    import ssl

                    ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    status = resp.status
                    text = resp.read().decode("utf-8", "replace")
                    hdrs = {k: v for k, v in resp.getheaders()}
                return status, text, hdrs

            return await asyncio.to_thread(_blocking)

        status, text, hdrs = (0, "", {})
        try:
            status, text, hdrs = await self._retryable(_call, max_retries, retry_interval)
        except urllib.error.HTTPError as e:
            status = e.code
            text = e.read().decode("utf-8", "replace")
            hdrs = {k: v for k, v in e.headers.items()}
        except Exception as e:  # noqa: BLE001
            raise WorkflowExecutionError(f"HTTP 请求失败: {e}") from e
        return {
            "status_code": status,
            "body": text,
            "headers": hdrs,
            "size": len(text.encode("utf-8", "replace")),
        }

    @staticmethod
    def _apply_auth(headers: dict[str, str], config: dict[str, Any]) -> None:
        kind = str(config.get("kind") or "bearer").lower()
        if kind == "basic":
            import base64

            user = str(config.get("username", ""))
            pwd = str(config.get("password", ""))
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        elif kind == "custom":
            name = str(config.get("header_name") or "Authorization")
            headers[name] = str(config.get("header_value", ""))
        else:  # bearer
            headers["Authorization"] = f"Bearer {config.get('token', '')}"

    def _run_template(self, data: dict[str, Any]) -> dict[str, Any]:
        tmpl = str(data.get("template", "") or "")
        try:
            from jinja2 import Environment

            env = Environment(autoescape=False)
            rendered = env.from_string(tmpl).render(**self._jinja_context())
        except Exception:  # noqa: BLE001
            # 回退：简单 {{ node.field }} 文本替换（向后兼容非 Jinja2 模板）
            rendered = self.render(tmpl)
        return {"output": rendered}

    def _jinja_context(self) -> dict[str, Any]:
        """构造模板/表达式渲染上下文。

        每个上游节点输出以 nodeId 暴露（支持 {{node.field}}），
        并以 nodeId_field 扁平键冗余。
        """
        ctx: dict[str, Any] = {}
        for nid, outs in self._node_outputs.items():
            if isinstance(outs, dict):
                ctx[nid] = outs
                for k, v in outs.items():
                    ctx[f"{nid}_{k}"] = v
        ctx.update(self._globals)
        return ctx

    def _run_assign(self, data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for v in data.get("variables", []) or []:
            name = str(v.get("name", "")).strip()
            if not name:
                continue
            op = str(v.get("op") or "set")
            try:
                val = self.resolve(str(v.get("value", "") or ""))
            except Exception:  # noqa: BLE001
                val = self.render(str(v.get("value", "") or ""))
            cur = self._globals.get(name)
            if op == "clear":
                out[name] = None
            elif op == "append":
                lst = list(cur) if isinstance(cur, list) else []
                lst.append(val)
                out[name] = lst
            elif op == "extend":
                lst = list(cur) if isinstance(cur, list) else []
                if isinstance(val, list):
                    lst.extend(val)
                out[name] = lst
            elif op in ("add", "sub", "mul", "div"):
                a = GraphEngine._coerce_num(cur if cur is not None else 0) or 0.0
                b = GraphEngine._coerce_num(val) or 0.0
                if op == "add":
                    out[name] = a + b
                elif op == "sub":
                    out[name] = a - b
                elif op == "mul":
                    out[name] = a * b
                else:
                    out[name] = a / b if b else 0.0
            else:  # set / overwrite
                out[name] = val
        return out

    # ── 变量解析与渲染 ──────────────────────────────────
    def resolve(self, ref: str) -> Any:
        """解析变量引用：优先 nodeId.field，回退全局变量，再回退单字段节点输出。"""
        ref = str(ref).strip()
        if ref.startswith("{{") and ref.endswith("}}"):
            ref = ref[2:-2].strip()
        if "." in ref:
            nid, fld = ref.split(".", 1)
            outs = self._node_outputs.get(nid)
            if isinstance(outs, dict) and fld in outs:
                return outs[fld]
        if ref in self._globals:
            return self._globals[ref]
        outs = self._node_outputs.get(ref)
        if isinstance(outs, dict) and len(outs) == 1:
            return next(iter(outs.values()))
        raise WorkflowValidationError(f"未找到变量引用: {ref}")

    def render(self, text: str) -> str:
        """将文本中的 {{ ref }} 替换为解析后的字符串值。"""
        if not isinstance(text, str) or "{{" not in text:
            return text
        return _REF_RE.sub(lambda m: self._to_str(self._safe_resolve(m.group(1))), text)

    def _safe_resolve(self, ref: str) -> Any:
        try:
            return self.resolve(ref)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _to_str(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return str(v)

    def _operand(self, raw: str) -> Any:
        s = str(raw).strip()
        if s.startswith("{{") and s.endswith("}}"):
            try:
                return self.resolve(s)
            except Exception:  # noqa: BLE001
                pass
        # 尝试数值化
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return self.render(s)

    @staticmethod
    def _compare(left: Any, right: Any, op: str) -> str:
        try:
            op = str(op)
            if op in ("==", "is"):
                # 数值化不对称兜底（{{a}} 解析为 "5"、右侧字面量转成 5 时按字符串再比一次）
                ok = left == right or str(left) == str(right)
                return "true" if ok else "false"
            if op in ("!=", "is_not"):
                ok = left == right or str(left) == str(right)
                return "false" if ok else "true"
            if op in (">", ">=", "<", "<="):
                lv, rv = GraphEngine._coerce_num(left), GraphEngine._coerce_num(right)
                if lv is None or rv is None:
                    # 数值化失败退化为字符串比较
                    ls, rs = str(left), str(right)
                    if op == ">":
                        return "true" if ls > rs else "false"
                    if op == ">=":
                        return "true" if ls >= rs else "false"
                    if op == "<":
                        return "true" if ls < rs else "false"
                    return "true" if ls <= rs else "false"
                if op == ">":
                    return "true" if lv > rv else "false"
                if op == ">=":
                    return "true" if lv >= rv else "false"
                if op == "<":
                    return "true" if lv < rv else "false"
                return "true" if lv <= rv else "false"
            if op == "contains":
                return "true" if str(right) in str(left) else "false"
            if op == "not_contains":
                return "true" if str(right) not in str(left) else "false"
            if op == "starts_with":
                return "true" if str(left).startswith(str(right)) else "false"
            if op == "ends_with":
                return "true" if str(left).endswith(str(right)) else "false"
            if op in ("empty", "is_empty"):
                return "true" if str(left).strip() == "" else "false"
            if op in ("not_empty", "is_not_empty"):
                return "true" if str(left).strip() != "" else "false"
        except Exception:  # noqa: BLE001
            return "false"
        return "false"

    @staticmethod
    def _coerce_num(v: Any) -> float | None:
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _to_int(v: Any, default: int = 0) -> int:
        """容错取整（前端数字输入可能给出 \"2.5\" / \"\" 等，直接 int() 会抛错）。"""
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(v: Any, default: float = 0.0) -> float:
        """容错取浮点（同 _to_int，空串/None 回退默认值）。"""
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _get_provider(self, provider_id: str) -> Any:
        if self._services is None:
            raise WorkflowValidationError("服务注册表不可用，无法调用大模型")
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        reg = self._services.get(ProviderRegistry)
        return reg.get_provider(provider_id, include_disabled=True)
