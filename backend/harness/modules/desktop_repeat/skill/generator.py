"""工作流 → SKILL.md 技能封装（§8.5 / T4.1）。

- frontmatter 用项目自有极简 YAML 约定（skill_manager._parse_simple_yaml：
  支持单行 key: value 与块标量），不引入 YAML 依赖；
- 技能绑定 workflow_id + workflow_version + graph_sha256（版本固定）；
- L1 描述 ~100 token；正文给"调 repeat_run 执行、零 token 回放"的用法与参数表；
- 敏感参数：值永不写进技能文件，只写"须运行时提供"。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]{2,63}$")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    if not s:
        s = f"flow-{int(datetime.now().timestamp())}"
    s = s[:56]
    if not _NAME_RE.match(s):
        s = "repeat-" + re.sub(r"[^a-z0-9]+", "", s)[:40]
    return s


def graph_sha256(graph: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(graph, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def generate_skill(workflow: dict[str, Any], *, skills_root: Path,
                   slug: str | None = None,
                   description_hint: str = "") -> Path:
    """把（已发布）工作流导出为技能目录，返回技能目录路径。

    workflow: {id, name, description, graph, version?}
    """
    wf_id = str(workflow.get("id", ""))
    name = str(workflow.get("name", "桌面流程"))
    graph = workflow.get("graph") or {}
    version = int(workflow.get("version", 1) or 1)
    gsha = graph_sha256(graph)
    slug_final = slugify(slug or name)
    sdir = Path(skills_root) / f"repeat-{slug_final}"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "references").mkdir(exist_ok=True)

    params = _start_params(graph)
    steps = _gui_steps(graph)
    labels = [str(s.get("label") or s.get("action") or "") for s in steps][:12]
    desc = _compose_description(name, description_hint, params)
    body = _compose_body(name=name, wf_id=wf_id, version=version, gsha=gsha,
                         description=desc, steps=steps, labels=labels, params=params)

    fm = [
        "---",
        f"name: repeat-{slug_final}",
        f"description: {desc}",
        "version: 1.0.0",
        "license: MIT",
        'allowed-tools: "repeat_run repeat_dry_run"',
        "metadata:",
        f"  workflow_id: {wf_id}",
        f"  workflow_version: {version}",
        f"  graph_sha256: {gsha}",
        f"  source_session: {_session_id(graph)}",
        f"  generated_at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "---",
        "",
    ]
    (sdir / "SKILL.md").write_text("\n".join(fm) + "\n" + body + "\n", encoding="utf-8")
    # references/params.md（L3）
    (sdir / "references" / "params.md").write_text(_params_reference(params) + "\n", encoding="utf-8")
    # 可选：前置检查脚本
    (sdir / "scripts").mkdir(exist_ok=True)
    (sdir / "scripts" / "check.py").write_text(_check_script(wf_id, gsha), encoding="utf-8")
    return sdir


# ── 抽取工作流要素 ──────────────────────────────────────────────
def _start_params(graph: dict[str, Any]) -> list[dict[str, Any]]:
    for n in graph.get("nodes", []) or []:
        if n.get("type") == "start":
            data = n.get("data") or {}
            return list(data.get("params") or data.get("variables") or [])
    return []


def _gui_steps(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [n.get("data") or {} for n in (graph.get("nodes") or []) if n.get("type") == "gui"]


def _session_id(graph: dict[str, Any]) -> str:
    return str((graph.get("meta") or {}).get("session_id", ""))


def _compose_description(name: str, hint: str, params: list[dict[str, Any]]) -> str:
    core = (hint or f"执行桌面流程「{name}」。Use when 用户要求运行/重放「{name}」流程时。")[:90]
    tail = "回放为本地确定性执行，不调用模型。"
    if params:
        req = [p.get("name") for p in params if p.get("required") and not p.get("sensitive")]
        if req:
            tail += f"必填参数：{','.join(str(x) for x in req[:3])}。"
    text = f"{core}{tail}"
    return text.replace("\n", " ")[:200]


def _compose_body(*, name: str, wf_id: str, version: int, gsha: str,
                  description: str, steps: list[dict[str, Any]],
                  labels: list[str], params: list[dict[str, Any]]) -> str:
    lines = [
        f"# {name}",
        "",
        "## 何时使用",
        f"用户要求执行「{name}」桌面自动化流程时。",
        "",
        "## 参数",
    ]
    if params:
        lines.append("| 参数 | 必填 | 默认 | 敏感 | 说明 |")
        lines.append("|---|---|---|---|---|")
        for p in params:
            sensitive = bool(p.get("sensitive")) or str(p.get("type", "")).lower() in ("secret", "password")
            default = "（每次运行时提供）" if sensitive or str(p.get("source", "")) == "runtime_only" \
                else str(p.get("default", "") or "—")
            lines.append(f"| `{p.get('name')}` | {'是' if p.get('required') else '否'} | {default} "
                         f"| {'是' if sensitive else '否'} | {p.get('description') or ''} |")
    else:
        lines.append("本流程无参数。")
    lines += [
        "",
        "## 执行",
        f"调用 `repeat_run(workflow_id=\"{wf_id}\", params={{...}}, expected_graph_sha256=\"{gsha[:16]}…\")`。",
        "回放为确定性执行（定位链 + UIA Pattern），**不消耗模型 tokens、断网可跑**。",
        "",
        "## 流程步骤",
    ]
    for i, lb in enumerate(labels, 1):
        lines.append(f"{i}. {lb or '（未命名步骤）'}")
    risky = [s for s in steps if str(s.get("risk")) == "dangerous"]
    lines += [
        "",
        "## 前置条件",
        "- 目标应用已启动且窗口可交互（若流程首步为 launch_app 则自动启动）；",
        "- 屏幕缩放与录制时一致（差异过大时坐标兜底会被禁用并挂起）；",
        "- 目标窗口不得以管理员权限运行（UIPI 限制，无法自动操作提权窗口）。",
    ]
    if risky:
        lines += ["", "## 风险提示",
                  f"- 本流程含 {len(risky)} 个危险动作（删除/提交/支付/发送等），首次执行需人工确认。"]
    lines += [
        "",
        "## 失败处理",
        "失败时返回 `run_id` 与诊断包路径；先看 `expected.png`/`actual.png` 对比，"
        "再用 `repeat_dry_run` 复检定位链。",
    ]
    return "\n".join(lines)


def _params_reference(params: list[dict[str, Any]]) -> str:
    out = ["# 参数详解", ""]
    for p in params:
        out.append(f"## {p.get('name')}")
        out.append(f"- 类型：{p.get('type', 'string')}")
        out.append(f"- 必填：{bool(p.get('required'))}")
        out.append(f"- 来源：{p.get('source', 'literal')}")
        if p.get("pattern"):
            out.append(f"- 格式：`{p['pattern']}`")
        if p.get("expression"):
            out.append(f"- 表达式：`{p['expression']}`")
        out.append(f"- 说明：{p.get('description') or ''}")
        if str(p.get("type", "")).lower() in ("secret", "password") or p.get("sensitive"):
            out.append("- **敏感**：值只运行时注入，绝不写入文件/日志。")
        out.append("")
    return "\n".join(out)


def _check_script(wf_id: str, gsha: str) -> str:
    return (
        "#!/usr/bin/env python\n"
        '"""技能前置检查（L3 资源，按需运行）：校验工作流存在且版本匹配。"""\n'
        "import json, sys\n\n"
        f"EXPECTED_WF = {json.dumps(wf_id)}\n"
        f"EXPECTED_SHA = {json.dumps(gsha)}\n\n\n"
        "def main() -> int:\n"
        "    # 由 repeat_dry_run / repeat_run 内部做完整校验；这里仅占位示例。\n"
        "    print(json.dumps({'workflow_id': EXPECTED_WF, 'graph_sha256_prefix': EXPECTED_SHA[:16]}))\n"
        "    return 0\n\n\n"
        'if __name__ == "__main__":\n    sys.exit(main())\n'
    )
