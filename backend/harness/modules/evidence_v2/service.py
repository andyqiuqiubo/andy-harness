"""evidence_v2 服务 —— 版本化、防篡改、可离线校验的证据固化。

设计要点（v2 schema）：
1. **每条证据自证**：`payload_sha256` 为原始产物的 SHA-256，产物以独立文件落盘
   （`payload_ref` 指向它），校验时重算比对，杜绝"结论与产物不符"。
2. **哈希链防篡改**：第 n 条的 `hash = sha256(prev_hash|seq|stage|kind|payload_sha256)`，
   `prev_hash` 指向上一条，链首为 64 个 0（genesis）。任何一条被改，其后的链全部断裂。
3. **溯源清单 provenance**：记录工具链版本、环境指纹、输入指纹，保证证据可复现。
4. **双产物**：`evidence.json`（机器可读）+ `evidence.md`（人读报告），同一份数据两次呈现。
5. **纯本地**：不依赖网络与外部服务，可在任意时刻离线校验。

与 v1 式"把日志打包"的差别即上述 2/3 条：v2 要求证据之间有序且互相锚定。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.evidence_v2")

SCHEMA_ID = "andy-harness.evidence/v2"
GENESIS = "0" * 64
_CHUNK = 1 << 20


def sha256_file(path: str | Path) -> str:
    """按 1MB 分块计算文件 SHA-256（大文件不会占满内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_bytes(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))


@dataclass
class EvidenceItem:
    """单条证据。hash 由 prev_hash + 自身字段派生，构成哈希链的一环。"""

    seq: int
    stage: str
    kind: str
    summary: str
    payload_sha256: str
    payload_bytes: int
    payload_ref: str = ""
    created_at: str = ""
    prev_hash: str = GENESIS
    hash: str = ""

    def compute_hash(self) -> str:
        material = "|".join(
            [
                self.prev_hash,
                str(self.seq),
                self.stage,
                self.kind,
                self.payload_sha256,
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class EvidenceBundle:
    """v2 证据包。"""

    schema: str = SCHEMA_ID
    bundle_id: str = ""
    created_at: str = ""
    subject: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    items: list[EvidenceItem] = field(default_factory=list)
    chain_head: str = GENESIS

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["items"] = [asdict(i) for i in self.items]
        return data


class EvidenceV2Service:
    """证据固化服务：建包 → 逐条追加 → 落盘 → 校验。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        # 默认落在本仓库 rev_eng/runs/ 下，保证全部资源在项目目录内
        repo_root = Path(__file__).resolve().parents[4]
        self.base_dir = Path(base_dir) if base_dir else repo_root / "rev_eng" / "runs"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._bundles: dict[str, EvidenceBundle] = {}

    # ── 建包 ──────────────────────────────────────────────────────────
    def create_bundle(
        self,
        subject_path: str | Path | None = None,
        *,
        subject_type: str = "unknown",
        provenance: dict[str, Any] | None = None,
    ) -> EvidenceBundle:
        subject: dict[str, Any] = {"type": subject_type}
        if subject_path and os.path.isfile(subject_path):
            p = Path(subject_path)
            subject.update(
                {
                    "name": p.name,
                    "path": str(p),
                    "size": p.stat().st_size,
                    "sha256": sha256_file(p),
                }
            )
        bundle = EvidenceBundle(
            bundle_id=uuid.uuid4().hex[:16],
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            subject=subject,
            provenance=provenance or {},
        )
        bundle.provenance.setdefault("environment", self.environment_fingerprint())
        self._bundles[bundle.bundle_id] = bundle
        return bundle

    @staticmethod
    def environment_fingerprint() -> dict[str, Any]:
        return {
            "os": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "hostname": platform.node(),
        }

    # ── 追加证据 ──────────────────────────────────────────────────────
    def add_item(
        self,
        bundle: EvidenceBundle,
        *,
        stage: str,
        kind: str,
        summary: str,
        payload: bytes | str | dict[str, Any] | list[Any],
        payload_name: str | None = None,
    ) -> EvidenceItem:
        """把一条产物固化进证据包：产物落盘 + 计算指纹 + 接入哈希链。"""
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            ext = ".json"
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
            ext = ".txt"
        else:
            raw = payload
            ext = ".bin"

        run_dir = self.base_dir / bundle.bundle_id
        payload_dir = run_dir / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)

        name = payload_name or f"{len(bundle.items) + 1:02d}-{stage}-{kind}{ext}"
        # 避免重名覆盖导致证据链指向被替换的产物
        name = name.replace("/", "_").replace("\\", "_")
        target = payload_dir / name
        if target.exists():
            target = payload_dir / f"{len(bundle.items) + 1:02d}-{uuid.uuid4().hex[:6]}-{name}"
        target.write_bytes(raw)

        item = EvidenceItem(
            seq=len(bundle.items) + 1,
            stage=stage,
            kind=kind,
            summary=summary,
            payload_sha256=sha256_bytes(raw),
            payload_bytes=len(raw),
            payload_ref=str(target.relative_to(run_dir)).replace("\\", "/"),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            prev_hash=bundle.chain_head,
        )
        item.hash = item.compute_hash()
        bundle.items.append(item)
        bundle.chain_head = item.hash
        return item

    # ── 落盘 ──────────────────────────────────────────────────────────
    def save(self, bundle: EvidenceBundle) -> dict[str, str]:
        run_dir = self.base_dir / bundle.bundle_id
        run_dir.mkdir(parents=True, exist_ok=True)
        json_path = run_dir / "evidence.json"
        md_path = run_dir / "evidence.md"
        json_path.write_text(
            json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        md_path.write_text(self.render_markdown(bundle), encoding="utf-8")
        return {"dir": str(run_dir), "json": str(json_path), "markdown": str(md_path)}

    def render_markdown(self, bundle: EvidenceBundle) -> str:
        subj = bundle.subject
        lines: list[str] = [
            f"# Evidence v2 证据包 `{bundle.bundle_id}`",
            "",
            f"- Schema：{bundle.schema}",
            f"- 生成时间：{bundle.created_at}",
            f"- 证据条数：{len(bundle.items)}",
            f"- 链头（chain_head）：`{bundle.chain_head}`",
            "",
            "## 分析对象",
            "",
            f"- 名称：{subj.get('name', '—')}",
            f"- 类型：{subj.get('type', '—')}",
            f"- 大小：{subj.get('size', '—')} 字节",
            f"- SHA-256：`{subj.get('sha256', '—')}`",
            "",
            "## 溯源（provenance）",
            "",
        ]
        for k, v in (bundle.provenance or {}).items():
            lines.append(f"- **{k}**：{json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}")
        lines += [
            "",
            "## 证据链",
            "",
            "| # | 阶段 | 类型 | 说明 | 产物 | SHA-256（前16） | 链哈希（前16） |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for it in bundle.items:
            lines.append(
                f"| {it.seq} | {it.stage} | {it.kind} | {it.summary} | `{it.payload_ref}` "
                f"| `{it.payload_sha256[:16]}` | `{it.hash[:16]}` |"
            )
        lines += ["", "> 每条证据的链哈希 = sha256(prev_hash|seq|stage|kind|payload_sha256)，"
                       "任一环被篡改都会导致后续链断裂。校验：`verify_bundle()`。", ""]
        return "\n".join(lines)

    # ── 校验 ──────────────────────────────────────────────────────────
    @staticmethod
    def verify(bundle_or_path: EvidenceBundle | str | Path) -> dict[str, Any]:
        """离线校验：产物指纹 + 哈希链 + schema 版本。返回结构化报告。"""
        if isinstance(bundle_or_path, EvidenceBundle):
            data = bundle_or_path.to_dict()
            base = Path(bundle_or_path.payload_dir) if hasattr(bundle_or_path, "payload_dir") else None
            run_dir = base
        else:
            p = Path(bundle_or_path)
            if p.is_dir():
                run_dir = p
                data = json.loads((p / "evidence.json").read_text(encoding="utf-8"))
            else:
                run_dir = p.parent
                data = json.loads(p.read_text(encoding="utf-8"))
        return EvidenceV2Service._verify_data(data, run_dir)

    @staticmethod
    def _verify_data(data: dict[str, Any], run_dir: Path | None) -> dict[str, Any]:
        problems: list[str] = []
        if data.get("schema") != SCHEMA_ID:
            problems.append(f"schema 不是 {SCHEMA_ID}")

        prev = GENESIS
        checked = 0
        for raw in data.get("items", []):
            item = EvidenceItem(**raw)
            if item.prev_hash != prev:
                problems.append(f"#{item.seq} prev_hash 不连续（期望 {prev[:12]}… 实际 {item.prev_hash[:12]}…）")
            expect = item.compute_hash()
            if expect != item.hash:
                problems.append(f"#{item.seq} 链哈希不匹配（期望 {expect[:12]}… 实际 {item.hash[:12]}…）")
            if run_dir is not None:
                fp = run_dir / item.payload_ref
                if not fp.is_file():
                    problems.append(f"#{item.seq} 产物缺失：{item.payload_ref}")
                else:
                    actual = sha256_file(fp)
                    if actual != item.payload_sha256:
                        problems.append(
                            f"#{item.seq} 产物指纹不符"
                            f"（期望 {item.payload_sha256[:12]}… 实际 {actual[:12]}…）"
                        )
            prev = item.hash
            checked += 1

        if data.get("chain_head") != prev:
            problems.append("chain_head 与链尾不一致")

        return {
            "ok": not problems,
            "schema": data.get("schema"),
            "bundle_id": data.get("bundle_id"),
            "items_checked": checked,
            "chain_head": data.get("chain_head"),
            "problems": problems,
        }

    def load(self, bundle_id_or_dir: str | Path) -> EvidenceBundle:
        p = Path(bundle_id_or_dir)
        run_dir = p if p.is_dir() else (self.base_dir / str(p))
        data = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
        bundle = EvidenceBundle(
            schema=data["schema"],
            bundle_id=data["bundle_id"],
            created_at=data["created_at"],
            subject=data.get("subject", {}),
            provenance=data.get("provenance", {}),
            items=[EvidenceItem(**i) for i in data.get("items", [])],
            chain_head=data.get("chain_head", GENESIS),
        )
        return bundle
