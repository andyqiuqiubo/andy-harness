"""E6 外部包安装（zip / git）单元测试 + REST 集成测试。

不触碰网络：zip 用例用本地构建的归档，git 用例用本地 git 仓库浅克隆；
REST 用例通过 monkeypatch 把安装目标目录重定向到临时目录，避免污染
真实的 backend/plugins 与 ~/.andy-harness/skills。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── 辅助：构建包与归档 ────────────────────────────────
def _build_plugin_pkg(base: Path, plugin_id: str, *, wrap: bool = False) -> Path:
    """在 base/pkg 下构建插件包；wrap=True 时再包一层 plugin_id 目录。"""
    root = base / "pkg"
    target = root / plugin_id if wrap else root
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "0.1.0",
                "type": "tool",
                "entry": f"plugins.{plugin_id}.main:DemoPlugin",
                "permissions": [],
                "description": "demo plugin",
            }
        ),
        encoding="utf-8",
    )
    (target / "main.py").write_text("class DemoPlugin:\n    pass\n", encoding="utf-8")
    return root


def _zip_dir(directory: Path, zip_path: Path) -> Path:
    """把 directory 内容打成 zip。"""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for item in directory.rglob("*"):
            if item.is_file():
                zf.write(item, arcname=str(item.relative_to(directory)))
    return zip_path


def _make_git_repo(base: Path, plugin_id: str) -> Path:
    """在 base 下建一个含插件包的本地 git 仓库（供浅克隆测试）。"""
    repo = base / "repo_src"
    pkg = repo / plugin_id
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "0.1.0",
                "type": "tool",
                "entry": f"plugins.{plugin_id}.main:DemoPlugin",
            }
        ),
        encoding="utf-8",
    )
    (pkg / "main.py").write_text("class DemoPlugin:\n    pass\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    return repo


# ── 安装器核心 ────────────────────────────────────────
def test_materialize_zip_local(tmp_path: Path) -> None:
    """从本地 zip 解压出插件包。"""
    from harness.modules.package_installer.service import PackageInstaller

    root = _build_plugin_pkg(tmp_path / "b", "ext_zip")
    zf = _zip_dir(root, tmp_path / "ext_zip.zip")

    inst = PackageInstaller(workdir=tmp_path / "work")
    res = inst.materialize_zip(str(zf))
    assert res.success, res.error
    assert (Path(res.path) / "plugin.json").is_file()
    assert (Path(res.path) / "main.py").is_file()


def test_materialize_zip_wrapped_folder(tmp_path: Path) -> None:
    """zip 外层包裹目录应被自动下沉。"""
    from harness.modules.package_installer.service import PackageInstaller

    root = _build_plugin_pkg(tmp_path / "b", "ext_wrap", wrap=True)
    zf = _zip_dir(root, tmp_path / "ext_wrap.zip")

    inst = PackageInstaller(workdir=tmp_path / "work")
    res = inst.materialize_zip(str(zf))
    assert res.success, res.error
    assert (Path(res.path) / "plugin.json").is_file()


def test_materialize_zip_missing_file(tmp_path: Path) -> None:
    """本地不存在的 zip 应失败。"""
    from harness.modules.package_installer.service import PackageInstaller

    inst = PackageInstaller(workdir=tmp_path / "work")
    res = inst.materialize_zip(str(tmp_path / "nope.zip"))
    assert not res.success
    assert "不存在" in res.error


def test_materialize_zip_slip_rejected(tmp_path: Path) -> None:
    """包含路径穿越成员的 zip 应被拒绝。"""
    from harness.modules.package_installer.service import PackageInstaller

    zf = tmp_path / "evil.zip"
    with zipfile.ZipFile(zf, "w") as zf_obj:
        zf_obj.writestr("../../etc/passwd", "hacked")
    inst = PackageInstaller(workdir=tmp_path / "work")
    res = inst.materialize_zip(str(zf))
    assert not res.success
    assert "路径穿越" in res.error


@pytest.mark.skipif(not shutil.which("git"), reason="git 不可用")
def test_materialize_git_local(tmp_path: Path) -> None:
    """从本地 git 仓库浅克隆出插件包。"""
    from harness.modules.package_installer.service import PackageInstaller

    repo = _make_git_repo(tmp_path, "ext_git")
    inst = PackageInstaller(workdir=tmp_path / "work")
    res = inst.materialize_git(str(repo))
    assert res.success, res.error
    assert (Path(res.path) / "plugin.json").is_file()


@pytest.mark.skipif(not shutil.which("git"), reason="git 不可用")
def test_materialize_git_clone_failure(tmp_path: Path) -> None:
    """克隆不存在的仓库应失败且不挂起（显式短超时避免整批卡死）。"""
    from harness.modules.package_installer.service import PackageInstaller

    inst = PackageInstaller(workdir=tmp_path / "work", timeout=2)
    res = inst.materialize_git(str(tmp_path / "no-such-repo"))
    assert not res.success
    assert "git 拉取失败" in res.error


@pytest.mark.skipif(not shutil.which("git"), reason="git 不可用")
def test_materialize_git_timeout_is_bounded(tmp_path: Path) -> None:
    """不可达的 git 源必须在超时内返回失败，绝不能无限挂起（B5 回归）。"""
    import time

    from harness.modules.package_installer.service import PackageInstaller

    inst = PackageInstaller(workdir=tmp_path / "work", timeout=2)
    start = time.monotonic()
    res = inst.materialize_git(str(tmp_path / "no-such-repo"))
    elapsed = time.monotonic() - start
    assert elapsed < 30, f"git 拉取未在超时内返回（耗时 {elapsed:.1f}s）"
    assert not res.success


# ── REST 集成 ────────────────────────────────────────
@pytest.fixture(scope="session")
def client() -> TestClient:
    """创建测试客户端（触发 lifespan 加载全部插件，会话级只启动一次）。

    各用例通过 monkeypatch 重定向安装目标目录，端点处理时在请求内读取
    模块级全局变量，因此会话级 client 仍能正确隔离。
    """
    from harness.main import app

    with TestClient(app) as c:
        yield c


class _FakeLoader:
    """插件端点测试用的假 loader（避免真实导入插件包）。"""

    def get_plugin(self, plugin_id: str) -> object | None:
        return None

    def load(self, manifest: object, base: object) -> None:
        self.manifest = manifest

    async def activate(self, plugin_id: str) -> None:
        pass


class TestExternalPluginInstall:
    """插件从 zip / git 安装接口测试。"""

    def test_install_from_zip(self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.api.rest import plugins as plugins_api

        monkeypatch.setattr(plugins_api, "_PLUGINS_DIR", tmp_path)
        monkeypatch.setattr(plugins_api, "_get_loader", lambda reg: _FakeLoader())

        root = _build_plugin_pkg(tmp_path / "b", "ext_zip")
        zf = _zip_dir(root, tmp_path / "ext_zip.zip")

        resp = client.post(
            "/api/plugins/install-external",
            json={"source": str(zf), "kind": "zip"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["plugin_id"] == "ext_zip"

        installed = tmp_path / "ext_zip"
        assert (installed / "plugin.json").is_file()
        assert (installed / "main.py").is_file()
        meta = json.loads((installed / "plugin.json").read_text(encoding="utf-8"))
        assert meta["source"] == "external"

    def test_install_invalid_package(self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.api.rest import plugins as plugins_api

        monkeypatch.setattr(plugins_api, "_PLUGINS_DIR", tmp_path)
        monkeypatch.setattr(plugins_api, "_get_loader", lambda reg: _FakeLoader())

        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "readme.txt").write_text("x", encoding="utf-8")
        zf = _zip_dir(bad, tmp_path / "bad.zip")

        resp = client.post("/api/plugins/install-external", json={"source": str(zf)})
        assert resp.status_code == 400
        assert "plugin.json" in resp.json()["message"]

    @pytest.mark.skipif(not shutil.which("git"), reason="git 不可用")
    def test_install_from_git(self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.api.rest import plugins as plugins_api

        monkeypatch.setattr(plugins_api, "_PLUGINS_DIR", tmp_path)
        monkeypatch.setattr(plugins_api, "_get_loader", lambda reg: _FakeLoader())

        repo = _make_git_repo(tmp_path, "ext_git")
        resp = client.post(
            "/api/plugins/install-external",
            json={"source": str(repo), "kind": "git"},
        )
        assert resp.status_code == 200, resp.text
        assert (tmp_path / "ext_git" / "plugin.json").is_file()


class TestExternalSkillInstall:
    """Skill 从 zip / git 安装接口测试。"""

    def test_install_from_zip(self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.api.rest import skills as skills_api

        monkeypatch.setattr(skills_api, "_user_skills_dir", lambda: tmp_path)

        pkg = tmp_path / "skpkg"
        pkg.mkdir()
        (pkg / "SKILL.md").write_text(
            "---\nname: ext-skill\ndescription: 外部技能\n---\nbody\n",
            encoding="utf-8",
        )
        zf = _zip_dir(pkg, tmp_path / "sk.zip")

        resp = client.post(
            "/api/skills/install-external",
            json={"source": str(zf), "kind": "zip"},
        )
        assert resp.status_code == 200, resp.text
        assert (tmp_path / "ext-skill" / "SKILL.md").is_file()

    def test_install_missing_skill_md(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harness.api.rest import skills as skills_api

        monkeypatch.setattr(skills_api, "_user_skills_dir", lambda: tmp_path)

        pkg = tmp_path / "skpkg2"
        pkg.mkdir()
        (pkg / "readme.txt").write_text("x", encoding="utf-8")
        zf = _zip_dir(pkg, tmp_path / "sk2.zip")

        resp = client.post("/api/skills/install-external", json={"source": str(zf)})
        assert resp.status_code == 400
        assert "SKILL.md" in resp.json()["detail"]

    @pytest.mark.skipif(not shutil.which("git"), reason="git 不可用")
    def test_install_from_git(self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.api.rest import skills as skills_api

        monkeypatch.setattr(skills_api, "_user_skills_dir", lambda: tmp_path)

        repo = tmp_path / "skill_repo_src"
        (repo / "ext-git-skill").mkdir(parents=True)
        (repo / "ext-git-skill" / "SKILL.md").write_text(
            "---\nname: ext-git-skill\ndescription: x\n---\nbody\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=repo,
            check=True,
            capture_output=True,
            env=env,
        )

        resp = client.post(
            "/api/skills/install-external",
            json={"source": str(repo), "kind": "git"},
        )
        assert resp.status_code == 200, resp.text
        assert (tmp_path / "ext-git-skill" / "SKILL.md").is_file()
