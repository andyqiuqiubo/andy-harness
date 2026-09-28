"""Skill 管理服务 —— SKILL.md 渐进式披露（Agent Skills 开放标准）。

三级渐进式披露（Progressive Disclosure）：
- **L1 metadata**：仅 frontmatter 的 name + description，常驻 system prompt，
  每个 skill 约 100 token，装几十个也不撑爆上下文。
- **L2 body**：模型判断命中后，调用 `use_skill` 工具加载 SKILL.md 正文。
- **L3 resources**：`scripts/` `references/` `assets/` 下的文件，
  仅在正文明确要求时才读取或执行。

扫描位置（优先级由高到低，同名先到先得）：
1. 内置：`<backend>/skills/<skill>/SKILL.md`
2. 用户级：`~/.andy-harness/skills/<skill>/SKILL.md`
3. 插件自带：`<backend>/plugins/<plugin>/skills/<skill>/SKILL.md`
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.skills")

# Skill 主文件名（大小写敏感，遵循开放标准）
SKILL_FILENAME = "SKILL.md"

# 允许作为 L3 资源的子目录
RESOURCE_DIRS = ("scripts", "references", "assets")

# 单次读取资源文件的最大字符数（防止巨型文件撑爆上下文）
MAX_RESOURCE_CHARS = 20000

# 单个 skill 目录最多登记的资源条目数
MAX_RESOURCE_ENTRIES = 50

# L1 目录里单条 description 的最大字符数。
# 装得越多，目录本身越占上下文（每个 skill 约 100 token），
# 因此超长描述在目录里截断，完整内容仍在 L2 正文中。
CATALOG_DESCRIPTION_MAX = 160


@dataclass
class SkillMeta:
    """Skill 元数据（L1）。"""

    name: str
    description: str
    path: Path
    source: str = "builtin"  # builtin / user / plugin
    version: str = ""
    license: str = ""
    allowed_tools: str = ""
    enabled: bool = True
    body_chars: int = 0
    resources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 友好字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "version": self.version,
            "license": self.license,
            "allowed_tools": self.allowed_tools,
            "enabled": self.enabled,
            "body_chars": self.body_chars,
            "resources": list(self.resources),
            "path": str(self.path),
        }


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 YAML frontmatter，返回 (metadata, body)。

    仅支持 `key: value` 形式的简单 YAML（涵盖 SKILL.md 规范所需字段），
    避免为此引入 YAML 依赖。未检测到 frontmatter 时返回 ({}, 原文)。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            raw_meta = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :]).strip()
            return _parse_simple_yaml(raw_meta), body

    # 只有开头没有结尾，视为无 frontmatter
    return {}, text


def _parse_simple_yaml(raw: str) -> dict[str, str]:
    """解析 `key: value` 形式的简单 YAML 映射。"""
    data: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        # 剥离成对引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            data[key] = value
    return data


def default_skill_roots() -> list[Path]:
    """默认扫描根：内置 skills 目录 + 用户级 skills 目录。"""
    backend_dir = Path(__file__).resolve().parents[3]
    return [
        backend_dir / "skills",
        Path.home() / ".andy-harness" / "skills",
    ]


def default_plugin_dirs() -> list[Path]:
    """默认插件目录（扫描其中的 `<plugin>/skills/*`）。"""
    backend_dir = Path(__file__).resolve().parents[3]
    return [backend_dir / "plugins"]


class SkillService(ABC):
    """Skill 服务契约 —— 面向接口编程，可被第三方插件整体替换。"""

    @abstractmethod
    def list_skills(self) -> list[SkillMeta]:
        """列出所有已发现 Skill 的元数据。"""

    @abstractmethod
    def get_skill(self, name: str) -> SkillMeta | None:
        """按名称获取 Skill 元数据。"""

    @abstractmethod
    def render_catalog(self, names: list[str] | None = None) -> str:
        """渲染 L1 目录（注入 system prompt）。无可用 skill 时返回空串。

        `names=None` 表示全部启用；传入列表则只渲染这些。
        """

    @abstractmethod
    def load_body(self, name: str) -> str:
        """加载 L2 正文。不存在/已禁用时返回空串。"""

    @abstractmethod
    def list_resources(self, name: str) -> list[str]:
        """列出 L3 资源相对路径。"""

    @abstractmethod
    def read_resource(self, name: str, rel_path: str) -> str:
        """读取 L3 资源内容（会被截断到 MAX_RESOURCE_CHARS）。"""

    @abstractmethod
    def reload(self) -> int:
        """重新扫描磁盘，返回可用 skill 数量。"""

    @abstractmethod
    def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/停用 Skill（持久化到状态文件）。"""


class SkillServiceImpl(SkillService):
    """Skill 服务默认实现。"""

    def __init__(
        self,
        roots: list[Path] | None = None,
        plugin_dirs: list[Path] | None = None,
        state_file: Path | None = None,
    ) -> None:
        self._roots = roots if roots is not None else default_skill_roots()
        self._plugin_dirs = (
            plugin_dirs if plugin_dirs is not None else default_plugin_dirs()
        )
        self._state_file = state_file or (
            Path(__file__).resolve().parents[3] / "data" / "skill_state.json"
        )
        self._skills: dict[str, SkillMeta] = {}
        self.reload()

    # ── 扫描 ──────────────────────────────────────────

    def reload(self) -> int:
        """重新扫描磁盘上的所有 Skill。"""
        disabled = self._load_disabled()
        found: dict[str, SkillMeta] = {}

        # 1) 内置 + 用户级：root/<skill>/SKILL.md
        for idx, root in enumerate(self._roots):
            source = "builtin" if idx == 0 else "user"
            for skill_dir in self._iter_dirs(root):
                meta = self._load_skill(skill_dir, source)
                self._collect(found, meta)

        # 2) 插件自带：plugins/<plugin>/skills/<skill>/SKILL.md
        for plugin_dir in self._plugin_dirs:
            for plugin_root in self._iter_dirs(plugin_dir):
                skills_dir = plugin_root / "skills"
                if not skills_dir.is_dir():
                    continue
                for skill_dir in self._iter_dirs(skills_dir):
                    meta = self._load_skill(skill_dir, "plugin")
                    self._collect(found, meta)

        for name, meta in found.items():
            meta.enabled = name not in disabled

        self._skills = found
        logger.info("Skill 扫描完成：共 %d 个", len(found))
        return len(found)

    @staticmethod
    def _collect(found: dict[str, SkillMeta], meta: SkillMeta | None) -> None:
        """收集 skill，同名先到先得。"""
        if meta is None:
            return
        if meta.name in found:
            logger.warning(
                "Skill 重名，已忽略: %s (%s)", meta.name, meta.path
            )
            return
        found[meta.name] = meta

    @staticmethod
    def _iter_dirs(root: Path) -> list[Path]:
        """列出 root 下的直接子目录（不存在则返回空）。"""
        if not root.is_dir():
            return []
        return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)

    def _load_skill(self, skill_dir: Path, source: str) -> SkillMeta | None:
        """加载单个 Skill 目录。缺少 name/description 的会被跳过。"""
        skill_file = skill_dir / SKILL_FILENAME
        if not skill_file.is_file():
            return None
        try:
            text = skill_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("读取 Skill 失败 (%s): %s", skill_file, e)
            return None

        meta_raw, body = parse_frontmatter(text)
        name = meta_raw.get("name", "").strip() or skill_dir.name
        description = meta_raw.get("description", "").strip()
        if not description:
            logger.warning(
                "Skill 缺少 description，已跳过: %s", skill_file
            )
            return None

        return SkillMeta(
            name=name,
            description=description,
            path=skill_dir,
            source=source,
            version=meta_raw.get("version", ""),
            license=meta_raw.get("license", ""),
            allowed_tools=meta_raw.get("allowed-tools", ""),
            body_chars=len(body),
            resources=self._scan_resources(skill_dir),
        )

    @staticmethod
    def _scan_resources(skill_dir: Path) -> list[str]:
        """扫描 scripts/ references/ assets/ 下的资源相对路径。"""
        resources: list[str] = []
        for sub in RESOURCE_DIRS:
            base = skill_dir / sub
            if not base.is_dir():
                continue
            for file_path in sorted(base.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(skill_dir).as_posix()
                if rel not in resources:
                    resources.append(rel)
                if len(resources) >= MAX_RESOURCE_ENTRIES:
                    return resources
        return resources

    # ── 查询 ──────────────────────────────────────────

    def list_skills(self) -> list[SkillMeta]:
        """列出所有 Skill 元数据。"""
        return sorted(self._skills.values(), key=lambda m: m.name)

    def get_skill(self, name: str) -> SkillMeta | None:
        """按名称获取 Skill。"""
        return self._skills.get(name)

    def render_catalog(self, names: list[str] | None = None) -> str:
        """渲染 L1 目录（仅 name + description）。

        `names` 为 None 时列出全部启用的 Skill；传入列表则只列这些
        （空列表 → 返回空串），供定时任务等需要限定 Skill 范围的场景使用。
        """
        allowed = None if names is None else {n for n in names}
        available = [
            m
            for m in self.list_skills()
            if m.enabled and (allowed is None or m.name in allowed)
        ]
        if not available:
            return ""

        lines = [
            "## 可用 Skills（按需加载）",
            "下面是你已安装的 Skills。每个 Skill 是一套标准作业流程，"
            "包含详细步骤、规范与可选脚本。",
            "规则：",
            "- 仅当用户任务**明确匹配**某个 Skill 的 description 时，"
            "才用 use_skill 工具加载它（传入 name）。",
            "- 不要预先加载，不要加载与当前任务无关的 Skill。",
            "- 加载后严格按其中的步骤执行。",
            "",
        ]
        for meta in available:
            lines.append(f"- `{meta.name}`: {self._brief(meta.description)}")
        return "\n".join(lines)

    @staticmethod
    def _brief(description: str) -> str:
        """目录中的简短描述（超长截断，避免目录膨胀）。"""
        desc = " ".join(description.split())
        if len(desc) <= CATALOG_DESCRIPTION_MAX:
            return desc
        return desc[: CATALOG_DESCRIPTION_MAX - 1].rstrip() + "…"

    def load_body(self, name: str) -> str:
        """加载 L2 正文（不含 frontmatter）。"""
        meta = self._skills.get(name)
        if meta is None or not meta.enabled:
            return ""
        skill_file = meta.path / SKILL_FILENAME
        try:
            _meta, body = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("读取 Skill 正文失败 (%s): %s", name, e)
            return ""
        header = f"# Skill: {meta.name}\n\n"
        if meta.allowed_tools:
            header += f"> 建议工具范围: {meta.allowed_tools}\n\n"
        return header + body

    def list_resources(self, name: str) -> list[str]:
        """列出 L3 资源相对路径。"""
        meta = self._skills.get(name)
        return list(meta.resources) if meta else []

    def read_resource(self, name: str, rel_path: str) -> str:
        """读取 L3 资源内容（含路径越界防护）。"""
        meta = self._skills.get(name)
        if meta is None:
            return ""
        try:
            target = (meta.path / rel_path).resolve()
        except Exception:
            return ""
        root = meta.path.resolve()
        # 不能用字符串前缀判断："/skills/foo" 也是 "/skills/foo-bar" 的前缀，
        # 会把兄弟目录误判为合法。必须按路径分段比较。
        if not target.is_relative_to(root):
            return f"错误: 资源路径越界: {rel_path}"
        if not target.is_file():
            return f"错误: 资源不存在: {rel_path}"

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"错误: 读取资源失败: {e}"

        if len(text) > MAX_RESOURCE_CHARS:
            text = (
                text[:MAX_RESOURCE_CHARS]
                + f"\n\n... [已截断，剩余 {len(text) - MAX_RESOURCE_CHARS} 字符未显示]"
            )
        return text

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/停用 Skill 并持久化。"""
        if name not in self._skills:
            return False
        self._skills[name].enabled = enabled
        self._save_disabled(
            {n for n, m in self._skills.items() if not m.enabled}
        )
        return True

    # ── 状态持久化 ────────────────────────────────────

    def _load_disabled(self) -> set[str]:
        """读取被停用的 skill 名称集合。"""
        try:
            if self._state_file.is_file():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                return set(data.get("disabled", []))
        except Exception as e:
            logger.warning("读取 skill 状态文件失败: %s", e)
        return set()

    def _save_disabled(self, disabled: set[str]) -> None:
        """写入被停用的 skill 名称集合。"""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps({"disabled": sorted(disabled)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写入 skill 状态文件失败: %s", e)
