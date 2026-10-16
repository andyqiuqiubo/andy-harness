"""工程强逆向 —— 数据模型。

七阶段之间靠 **facts（结构化事实）** 与 **artifacts（产物）** 两条线流转：

- facts：跨阶段传递的可计算结论（函数清单、字符串、运行时行为、风险发现），
  后一阶段消费前一阶段的事实，而不是重新分析一遍；
- artifacts：落盘的原始产物（JSON / 文本），最终由 evidence_v2 阶段逐条固化。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from harness.modules.reverse_engineering.progress import NULL_REPORTER, ProgressReporter

# ── 阶段标识（顺序即执行顺序） ─────────────────────────────────────────
STAGE_REA = "rea_static"
STAGE_JAVA_DECOMPILE = "java_decompile"
STAGE_FRIDA = "frida_dynamic"
STAGE_FIRECRACKER = "firecracker_sandbox"
STAGE_PENTAGI = "pentagi_analysis"
STAGE_REBUILD = "rebuild_exe"
STAGE_EVIDENCE = "evidence_v2"

STAGE_ORDER: list[str] = [
    STAGE_REA,
    STAGE_JAVA_DECOMPILE,
    STAGE_FRIDA,
    STAGE_FIRECRACKER,
    STAGE_PENTAGI,
    STAGE_REBUILD,
    STAGE_EVIDENCE,
]

STAGE_LABELS: dict[str, str] = {
    STAGE_REA: "REA 静态逆向",
    STAGE_JAVA_DECOMPILE: "Java class 反编译",
    STAGE_FRIDA: "Frida MCP 动态插桩",
    STAGE_FIRECRACKER: "Firecracker 沙箱执行",
    STAGE_PENTAGI: "PentAGI 自动化分析",
    STAGE_REBUILD: "功能等价 exe 重建",
    STAGE_EVIDENCE: "Evidence v2 证据固化",
}


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    SUBSTITUTED = "substituted"  # 目标引擎不可用，已用本地等价替代执行
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class ArtifactRef:
    """落盘产物的引用。"""

    stage: str
    kind: str
    name: str
    path: str
    summary: str = ""
    sha256: str = ""
    bytes: int = 0


@dataclass
class StageResult:
    """单个阶段的执行结果。"""

    stage: str
    status: StageStatus
    engine: str = ""  # 实际生效的引擎：rea / frida / firecracker / local-substitute / ...
    target_engine: str = ""  # 期望的目标引擎（用于记录是否发生降级替代）
    summary: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class RunContext:
    """一次流水线运行的共享上下文。"""

    run_id: str
    subject: Path
    work_dir: Path
    facts: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    # 实时进展发射器：默认静默 no-op；流水线运行时由 pipeline 注入真实 reporter，
    # 各阶段据此推送阶段/步骤进度（非阻塞，不干扰主流程）。
    reporter: ProgressReporter = NULL_REPORTER

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def merge_facts(self, delta: dict[str, Any]) -> None:
        for k, v in delta.items():
            if isinstance(v, dict) and isinstance(self.facts.get(k), dict):
                self.facts[k].update(v)
            elif isinstance(v, list) and isinstance(self.facts.get(k), list):
                self.facts[k].extend(v)
            else:
                self.facts[k] = v


@dataclass
class PipelineRun:
    """流水线运行记录。"""

    run_id: str
    subject_name: str = ""
    subject_path: str = ""
    subject_sha256: str = ""
    created_at: str = ""
    finished_at: str = ""
    status: str = "running"
    stages: dict[str, StageResult] = field(default_factory=dict)
    evidence_bundle_id: str = ""
    evidence_dir: str = ""
    evidence_report: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""
    # 运行级错误（编排器自身异常时记录；阶段级错误在 StageResult.error）
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "subject_name": self.subject_name,
            "subject_path": self.subject_path,
            "subject_sha256": self.subject_sha256,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_dir": self.evidence_dir,
            "evidence_report": self.evidence_report,
            "run_dir": self.run_dir,
            "error": self.error,
        }
