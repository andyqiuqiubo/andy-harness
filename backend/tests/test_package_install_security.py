"""外部包安装 / 插件安装的安全回归测试（SEC-04 / SEC-05 修复验证）。

覆盖：
- git 源 RCE：``ext::`` 传输、``-`` 开头参数注入、非法协议；
- SSRF：云元数据、回环、内网地址一律拒绝（含环境变量放开开关）；
- zip bomb：单文件 / 总量 / 成员数上限；
- zip slip：路径穿越；
- 插件 id 路径穿越（/install、/marketplace/{id}/install、卸载）；
- 自定义插件源码执行门槛（confirm_dangerous + 语法校验）。
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.infra import net_guard
from harness.modules.package_installer.service import (
    PackageInstaller,
    validate_git_ref,
    validate_git_source,
)


# ── git 源校验 ─────────────────────────────────────
class TestGitSourceValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            'ext::sh -c "touch /tmp/pwned"',
            "ext::sh -c id",
            "--upload-pack=touch /tmp/pwned",
            "-u /tmp/pwned",
            "ftp://example.com/repo.git",
        ],
    )
    def test_dangerous_sources_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_git_source(bad)

    def test_file_url_allowed_as_local_clone(self) -> None:
        """file:// 属本地克隆（不走网络、无 SSRF 面），放行但不含恶意执行能力。"""
        validate_git_source("file:///srv/repos/demo.git")

    @pytest.mark.parametrize(
        "good",
        [
            "https://github.com/example/repo.git",
            "http://example.com/repo.git",
            "git://example.com/repo.git",
            "ssh://git@example.com/repo.git",
            "git@github.com:example/repo.git",
            "/abs/path/repo",
            "./relative/repo",
        ],
    )
    def test_legitimate_sources_allowed(self, good: str) -> None:
        validate_git_source(good)

    def test_local_windows_path_allowed(self) -> None:
        # Windows 盘符会被 urlparse 误判为 scheme，需按本地路径放行
        validate_git_source("C:\\repos\\myrepo")

    def test_ref_option_injection_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_git_ref("--upload-pack=evil")
        validate_git_ref("main")  # 正常 ref 不受影响


# ── SSRF ───────────────────────────────────────────
class TestNetGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # 云元数据
            "http://127.0.0.1:8000/api/health",  # 回环
            "http://localhost:8000/",
            "http://192.168.1.10/x",  # 内网
            "http://10.0.0.5/x",
            "http://172.16.0.1/x",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "http://[::1]:8000/",
        ],
    )
    def test_blocked(self, url: str) -> None:
        assert net_guard.check_url(url) is not None

    def test_public_url_allowed(self) -> None:
        # example.com 解析为公网地址（离线时解析失败 → fail-closed，需跳过断言）
        result = net_guard.check_url("https://example.com/pkg.zip")
        assert result is None or "解析失败" in result

    def test_dns_failure_is_fail_closed(self) -> None:
        reason = net_guard.check_url("https://no-such-host-xyz.invalid/x")
        assert reason is not None and "解析失败" in reason

    def test_allow_private_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URL", "1")
        assert net_guard.allow_private_addresses() is True
        assert net_guard.check_url("http://127.0.0.1:8000/x") is None


# ── zip 炸弹 / zip slip ────────────────────────────
class TestZipExtractionGuards:
    def _zip_with_member(self, name: str, size: int) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(name, "0" * size)
        return buf.getvalue()

    def test_zip_slip_rejected(self, tmp_path: Path) -> None:
        data = self._zip_with_member("../../evil.txt", 10)
        inst = PackageInstaller(workdir=tmp_path / "w")
        archive = tmp_path / "slip.zip"
        archive.write_bytes(data)
        res = inst.materialize_zip(str(archive))
        assert res.success is False and "路径穿越" in res.error

    def test_zip_bomb_total_size_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.modules.package_installer import service as pkg_service

        # 把上限调到极小以便快速构造（压缩后仅几十 KB）
        monkeypatch.setattr(pkg_service, "MAX_TOTAL_BYTES", 1024)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", "0" * (50 * 1024))
        archive = tmp_path / "bomb.zip"
        archive.write_bytes(buf.getvalue())

        inst = PackageInstaller(workdir=tmp_path / "w")
        res = inst.materialize_zip(str(archive))
        assert res.success is False and "上限" in res.error

    def test_member_count_limit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.modules.package_installer import service as pkg_service

        monkeypatch.setattr(pkg_service, "MAX_MEMBERS", 5)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(10):
                zf.writestr(f"f{i}.txt", "x")
        archive = tmp_path / "many.zip"
        archive.write_bytes(buf.getvalue())

        inst = PackageInstaller(workdir=tmp_path / "w")
        res = inst.materialize_zip(str(archive))
        assert res.success is False and "成员过多" in res.error

    def test_normal_zip_still_works(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "plugin.json",
                json.dumps({"id": "zip_ok", "name": "zip_ok", "type": "tool", "entry": "x:Demo"}),
            )
            zf.writestr("main.py", "class Demo:\n    pass\n")
        archive = tmp_path / "ok.zip"
        archive.write_bytes(buf.getvalue())

        inst = PackageInstaller(workdir=tmp_path / "w")
        res = inst.materialize_zip(str(archive))
        assert res.success is True
        assert (Path(res.path) / "plugin.json").is_file()


# ── 插件 id 路径穿越 / 源码执行门槛 ─────────────────
class _FakeLoader:
    def get_plugin(self, plugin_id: str) -> object | None:
        return None

    def load(self, manifest: object, base: object) -> None:
        self.manifest = manifest

    async def activate(self, plugin_id: str) -> None:
        pass


@pytest.fixture(scope="module")
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


class TestPluginIdTraversal:
    """plugin_id 不得越过 plugins / marketplace 目录。"""

    def test_install_rejects_traversal_id(self, client: TestClient, tmp_path: Path, monkeypatch) -> None:
        from harness.api.rest import plugins as plugins_api

        monkeypatch.setattr(plugins_api, "_PLUGINS_DIR", tmp_path)
        monkeypatch.setattr(plugins_api, "_get_loader", lambda reg: _FakeLoader())

        resp = client.post(
            "/api/plugins/install",
            json={
                "plugin_id": "../pwned",
                "name": "x",
                "entry": "plugins.x.main:Demo",
                "plugin_code": "class Demo:\n    pass\n",
                "confirm_dangerous": True,
            },
        )
        assert resp.status_code == 400
        assert "id" in resp.json()["message"]

    def test_marketplace_install_rejects_traversal_id(self, client: TestClient, monkeypatch) -> None:
        from harness.api.rest import plugins as plugins_api

        monkeypatch.setattr(plugins_api, "_get_loader", lambda reg: _FakeLoader())
        resp = client.post("/api/plugins/marketplace/..%2F..%2Fevil/install")
        # 400（id 非法）或 404（路由不匹配）都算拦截，绝不能是 200
        assert resp.status_code in (400, 404)


class TestPluginCodeGate:
    def test_requires_explicit_confirmation(self, client: TestClient, tmp_path: Path, monkeypatch) -> None:
        from harness.api.rest import plugins as plugins_api

        monkeypatch.setattr(plugins_api, "_PLUGINS_DIR", tmp_path)
        monkeypatch.setattr(plugins_api, "_get_loader", lambda reg: _FakeLoader())

        resp = client.post(
            "/api/plugins/install",
            json={
                "plugin_id": "valid_id",
                "name": "x",
                "entry": "plugins.valid_id.main:Demo",
                "plugin_code": "class Demo:\n    pass\n",
            },
        )
        assert resp.status_code == 400
        assert "confirm_dangerous" in resp.text

    def test_rejects_syntax_error_code(self, client: TestClient, tmp_path: Path, monkeypatch) -> None:
        from harness.api.rest import plugins as plugins_api

        monkeypatch.setattr(plugins_api, "_PLUGINS_DIR", tmp_path)
        monkeypatch.setattr(plugins_api, "_get_loader", lambda reg: _FakeLoader())

        resp = client.post(
            "/api/plugins/install",
            json={
                "plugin_id": "valid_id",
                "name": "x",
                "entry": "plugins.valid_id.main:Demo",
                "plugin_code": "class Broken(:",
                "confirm_dangerous": True,
            },
        )
        assert resp.status_code == 400
        assert "语法" in resp.json()["message"]

    def test_confirmed_install_succeeds(self, client: TestClient, tmp_path: Path, monkeypatch) -> None:
        from harness.api.rest import plugins as plugins_api

        monkeypatch.setattr(plugins_api, "_PLUGINS_DIR", tmp_path)
        monkeypatch.setattr(plugins_api, "_get_loader", lambda reg: _FakeLoader())

        resp = client.post(
            "/api/plugins/install",
            json={
                "plugin_id": "valid_id",
                "name": "x",
                "entry": "plugins.valid_id.main:Demo",
                "plugin_code": "class Demo:\n    pass\n",
                "confirm_dangerous": True,
            },
        )
        assert resp.status_code == 200, resp.text
        assert (tmp_path / "valid_id" / "main.py").is_file()


def test_download_size_guard_uses_env_default() -> None:
    """下载上限常量存在且合理（200MB）。"""
    assert net_guard.MAX_DOWNLOAD_BYTES == 200 * 1024 * 1024
    assert os.environ.get("HARNESS_ALLOW_PRIVATE_URL", "") in ("", "0", "false")
