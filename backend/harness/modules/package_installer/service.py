"""外部包安装器：从 zip / git 拉取插件或技能包到本地。

生态分发（E6）：除内置市场「本地目录复制」外，支持从 zip 归档或 git
仓库安装第三方发布的插件 / 技能。本模块只做「拉取 + 落盘」，包的语义校验
（plugin.json / SKILL.md）与加载 / 激活由各调用方（插件 / 技能路由）负责。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from harness.infra.net_guard import assert_url_safe, download_to

logger = logging.getLogger("harness.package_installer")

#: git / HTTP 拉取的默认超时（秒）。源不可达时必须能自限，否则会永久挂起。
DEFAULT_TIMEOUT = 120.0

#: 解压体积上限（zip bomb 防护）：单文件 100MB / 总量 500MB / 成员 1 万
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_MEMBERS = 10_000

#: git 允许的传输协议白名单（SEC-04：ext:: 会直接执行 shell 命令 → RCE）
_ALLOWED_GIT_SCHEMES = ("http", "https", "git", "ssh")

#: Windows 盘符路径（urlparse 会把 C:\ 误判成 scheme="c"，需单独识别为本地路径）
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def validate_git_source(repo_url: str) -> None:
    """校验 git 源（SEC-04 修复）。

    三道拦截：
    1. 拒绝以 ``-`` 开头的值——``git clone --depth 1 <url>`` 会把 ``--upload-pack=...``
       当成选项，进而执行任意命令（git 参数注入 RCE）；
    2. 拒绝 ``ext::`` / ``file::`` 传输（``ext::sh -c ...`` 直接 shell 执行）；
    3. scheme 白名单（http/https/git/ssh）+ http(s) 走 SSRF 校验。
    """
    url = (repo_url or "").strip()
    if not url:
        raise ValueError("git 源不能为空")
    if url.startswith("-"):
        raise ValueError("git 源不得以 '-' 开头（防参数注入）")
    lowered = url.lower()
    if "ext::" in lowered:
        raise ValueError("禁止 ext:: 传输（可导致任意命令执行）")
    # 本地路径：绝对路径 / 相对路径 / Windows 盘符 / file:// —— 走本地克隆，无网络风险
    if (
        lowered.startswith("file://")
        or url.startswith(("/", "./", "../"))
        or _WIN_DRIVE_RE.match(url)
    ):
        return
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    # scp 语法（git@host:repo）无 scheme，按 ssh 处理，无需额外校验
    if scheme and scheme not in _ALLOWED_GIT_SCHEMES:
        raise ValueError(f"不支持的 git 协议: {scheme}（允许 {', '.join(_ALLOWED_GIT_SCHEMES)}）")
    if scheme in ("http", "https"):
        assert_url_safe(url)


def validate_git_ref(ref: str) -> None:
    """校验 git 分支 / tag / sha：禁止以 '-' 开头（--branch 参数注入）。"""
    if ref and ref.startswith("-"):
        raise ValueError("git ref 不得以 '-' 开头（防参数注入）")


def _resolve_timeout(timeout: float | None) -> float:
    """解析超时：显式参数 > 环境变量 HARNESS_PKG_INSTALL_TIMEOUT > 默认值。"""
    if timeout is not None:
        try:
            return max(1.0, float(timeout))
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT
    raw = os.environ.get("HARNESS_PKG_INSTALL_TIMEOUT", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            return DEFAULT_TIMEOUT
    return DEFAULT_TIMEOUT


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

    def __init__(
        self,
        workdir: Path | None = None,
        *,
        timeout: float | None = None,
    ) -> None:
        self._workdir = workdir or (Path(tempfile.gettempdir()) / "harness_pkg_install")
        self._workdir.mkdir(parents=True, exist_ok=True)
        # 网络/子进程必须有超时：git clone 与 HTTP 下载在源不可达（离线、
        # 代理 502、私有仓库等待凭据）时会无限期阻塞，进而把 REST 请求线程
        # 与单元测试一起挂死。可用 HARNESS_PKG_INSTALL_TIMEOUT 覆盖。
        self._timeout = _resolve_timeout(timeout)

    def _stage(self, prefix: str) -> Path:
        """在 workdir 下创建一个临时阶段目录。"""
        return Path(tempfile.mkdtemp(prefix=prefix + "_", dir=str(self._workdir)))

    def _download(self, url: str, dest: Path) -> None:
        """下载远程 zip 到本地（仅 http/https，SSRF 校验 + 体积上限 + 超时）。"""
        logger.info("下载外部包: %s", url)
        size = download_to(url, str(dest), timeout=self._timeout)
        logger.info("下载完成: %s（%d 字节）", url, size)

    def _run_git(self, cmd: list[str]) -> None:
        """执行 git 命令（浅克隆等），带超时且禁止交互式凭据提示。"""
        logger.info("执行 git: %s", " ".join(cmd))
        env = dict(os.environ)
        # 非交互场景：禁止 git 弹出终端凭据提示（否则会静默等待输入而挂死）
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"git 命令超时（{self._timeout:g}s）: {' '.join(cmd)}") from e

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
            validate_git_source(repo_url)
            if ref:
                validate_git_ref(ref)
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
        """安全解压：防 zip slip 路径穿越 + 防 zip bomb 体积放大。

        遍历时用 ``infolist()`` 读取声明的未压缩大小，不做实际解压即可
        判定总量/单文件/成员数是否越界（zip bomb 的特点是解压后才爆开，
        必须提前拦截）。
        """
        dest_resolved = dest.resolve()
        members = zf.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError(f"zip 成员过多（{len(members)} > {MAX_MEMBERS}）")
        total = 0
        for info in members:
            # 路径穿越检查（含绝对路径与 ../ 逃逸）
            target = (dest / info.filename).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError(f"非法 zip 路径（疑似路径穿越）: {info.filename}")
            size = info.file_size or 0
            if size > MAX_MEMBER_BYTES:
                raise ValueError(f"zip 内单个文件过大: {info.filename}（{size} 字节）")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"zip 解压后总体积超上限（{MAX_TOTAL_BYTES} 字节）")
        for info in members:
            zf.extract(info, dest)
