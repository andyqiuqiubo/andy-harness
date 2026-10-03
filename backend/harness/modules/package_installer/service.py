"""外部包安装器：从 zip / git 拉取插件或技能包到本地。

生态分发（E6）：除内置市场「本地目录复制」外，支持从 zip 归档或 git
仓库安装第三方发布的插件 / 技能。本模块只做「拉取 + 落盘」，包的语义校验
（plugin.json / SKILL.md）与加载 / 激活由各调用方（插件 / 技能路由）负责。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("harness.package_installer")


@dataclass
class MaterializeResult:
    """拉取结果。"""

    success: bool
    path: str = ""
    message: str = ""
    error: str = ""


def _looks_like_url(value: str) -> bool:
    """判断字符串是否为 http(s) URL。"""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https")


def copy_tree(src: Path, dst: Path) -> None:
    """将 src 目录内容合并复制到 dst（dst 可已存在）。"""
    if not dst.exists():
        dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def resolve_package_root(pkg_dir: Path, subdir: str | None = None) -> Path:
    """解析包根目录。

    处理 zip 常见的「外层包裹目录」：若目录内只有一个子目录且没有任何文件，
    视为包裹目录并自动下沉一层，避免把外层目录名当成了包根。
    """
    root = pkg_dir
    if subdir:
        root = root / subdir
        if not root.is_dir():
            raise ValueError(f"subdir 不存在: {subdir}")
    files = [p for p in root.iterdir() if p.is_file()]
    # 忽略 VCS / 隐藏目录（如 git clone 产生的 .git），避免干扰包裹目录判定
    dirs = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    # 外层只有一个目录且无任何文件 → 视为包裹目录，自动下沉
    if not files and len(dirs) == 1:
        root = dirs[0]
    return root


class PackageInstaller:
    """从 zip / git 拉取外部包到本地临时工作区。"""

    def __init__(self, workdir: Path | None = None) -> None:
        self._workdir = workdir or (Path(tempfile.gettempdir()) / "harness_pkg_install")
        self._workdir.mkdir(parents=True, exist_ok=True)

    def _stage(self, prefix: str) -> Path:
        """在 workdir 下创建一个临时阶段目录。"""
        return Path(tempfile.mkdtemp(prefix=prefix + "_", dir=str(self._workdir)))

    def _download(self, url: str, dest: Path) -> None:
        """下载远程 zip 到本地（仅 http/https）。"""
        import urllib.request

        logger.info("下载外部包: %s", url)
        urllib.request.urlretrieve(url, dest)  # noqa: S310

    def _run_git(self, cmd: list[str]) -> None:
        """执行 git 命令（浅克隆等）。"""
        logger.info("执行 git: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    def materialize_zip(self, source: str, *, subdir: str | None = None) -> MaterializeResult:
        """解压 zip（本地路径或 http(s) URL）。"""
        stage = self._stage("zip")
        try:
            if _looks_like_url(source):
                archive = stage / "_pkg.zip"
                self._download(source, archive)
            else:
                archive = Path(source)
                if not archive.is_file():
                    return MaterializeResult(False, error=f"zip 文件不存在: {source}")
            extract_dir = stage / "extracted"
            extract_dir.mkdir()
            with zipfile.ZipFile(archive) as zf:
                self._safe_extract(zf, extract_dir)
            root = resolve_package_root(extract_dir, subdir)
            return MaterializeResult(True, path=str(root))
        except Exception as e:  # noqa: BLE001
            return MaterializeResult(False, error=f"解压失败: {e}")

    def materialize_git(
        self,
        repo_url: str,
        *,
        ref: str | None = None,
        subdir: str | None = None,
    ) -> MaterializeResult:
        """克隆 git 仓库（默认浅克隆）。"""
        stage = self._stage("git")
        try:
            clone_target = stage / "repo"
            cmd = ["git", "clone", "--depth", "1"]
            if ref:
                cmd += ["--branch", ref]
            cmd += [repo_url, str(clone_target)]
            self._run_git(cmd)
            root = resolve_package_root(clone_target, subdir)
            return MaterializeResult(True, path=str(root))
        except Exception as e:  # noqa: BLE001
            return MaterializeResult(False, error=f"git 拉取失败: {e}")

    def materialize(
        self,
        source: str,
        kind: str,
        *,
        ref: str | None = None,
        subdir: str | None = None,
    ) -> MaterializeResult:
        """按类型拉取外部包。kind 为 'zip' 或 'git'。"""
        if kind == "zip":
            return self.materialize_zip(source, subdir=subdir)
        if kind == "git":
            return self.materialize_git(source, ref=ref, subdir=subdir)
        return MaterializeResult(False, error=f"未知来源类型: {kind}")

    @staticmethod
    def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
        """安全解压，防御 zip slip 路径穿越。"""
        dest_resolved = dest.resolve()
        for member in zf.namelist():
            target = (dest / member).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError(f"非法 zip 路径（疑似路径穿越）: {member}")
            zf.extract(member, dest)
