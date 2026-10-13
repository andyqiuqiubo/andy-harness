"""integration_hub 状态文件安全存储测试（SEC-09/10 修复验证）。

覆盖：敏感字段加密落盘、明文兼容与自动升级、解密往返、fail-closed、
损坏文件容错、原子写不残留 .tmp。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from plugins.integration_hub.oortcloud_provider import OortCloudIntegration  # noqa: E402
from plugins.integration_hub.state_store import (  # noqa: E402
    decrypt_field,
    encrypt_field,
    load_state,
    needs_upgrade,
    save_state,
)

FIELDS = ("access_token",)


def test_encrypt_decrypt_roundtrip():
    cipher = encrypt_field("live-token-123")
    assert cipher.startswith("enc:v1:")
    assert cipher != "live-token-123"
    assert decrypt_field(cipher) == "live-token-123"


def test_plaintext_passthrough_and_non_str_untouched():
    # 历史明文（无前缀）读取时原样返回
    assert decrypt_field("legacy-plain") == "legacy-plain"
    # 非字符串 / 空值不加密
    assert encrypt_field(None) is None
    assert encrypt_field("") == ""
    assert encrypt_field(123) == 123


def test_save_state_encrypts_and_restricts_permission(tmp_path):
    path = str(tmp_path / "s.json")
    save_state(str(tmp_path), path, {"access_token": "tok", "state": "csrf"}, FIELDS)
    raw = json.loads(open(path, encoding="utf-8").read())
    assert raw["access_token"].startswith("enc:v1:")
    assert raw["state"] == "csrf"  # 非敏感字段保持明文
    if os.name != "nt":
        assert (os.stat(path).st_mode & 0o777) == 0o600
    assert not os.path.exists(path + ".tmp")  # 原子写不残留临时文件
    # 往返：读回明文
    data = load_state(path, FIELDS)
    assert data["access_token"] == "tok"


def test_needs_upgrade_detects_plaintext_only(tmp_path):
    path = str(tmp_path / "s.json")
    save_state(str(tmp_path), path, {"access_token": "tok"}, FIELDS)
    assert needs_upgrade(path, FIELDS) is False
    # 手工写回明文
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"access_token": "plain"}, f)
    assert needs_upgrade(path, FIELDS) is True
    # 空值不算需要升级
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"access_token": ""}, f)
    assert needs_upgrade(path, FIELDS) is False


def test_corrupt_file_loads_as_empty(tmp_path):
    path = str(tmp_path / "s.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not-json")
    assert load_state(path, FIELDS) == {}
    assert needs_upgrade(path, FIELDS) is False


def test_decrypt_failure_fails_closed(monkeypatch, tmp_path):
    """machine key 变更导致解密失败时，字段必须置 None（fail-closed）。"""
    from plugins.integration_hub import state_store

    class _BrokenEnc:
        def encrypt(self, s):
            return "AAAA"

        def decrypt(self, s):
            raise ValueError("InvalidToken")

    monkeypatch.setattr(state_store, "_get_encryptor", lambda: _BrokenEnc())
    cipher = state_store.encrypt_field("tok")
    assert state_store.decrypt_field(cipher) is None


def test_oortcloud_state_encrypted_on_disk(tmp_path):
    """OortCloud access_token 不得明文落盘（SEC-09）。"""
    state_dir = str(tmp_path / "ih")
    integ = OortCloudIntegration(state_dir=state_dir)
    integ._state = {"access_token": "oc-token", "state": "csrf", "tenant_id": "t1"}
    integ._save_state()
    raw = json.loads(open(integ._state_file, encoding="utf-8").read())
    assert raw["access_token"].startswith("enc:v1:")
    assert raw["access_token"] != "oc-token"
    assert raw["state"] == "csrf"  # CSRF nonce 非凭据，保持明文
    # 重载后内存中恢复明文
    integ2 = OortCloudIntegration(state_dir=state_dir)
    assert integ2._state.get("access_token") == "oc-token"
    assert integ2.is_authenticated() is True


def test_oortcloud_legacy_plaintext_migrated(tmp_path):
    """旧版明文 oortcloud_state.json：读取兼容 + 自动升级密文。"""
    state_dir = str(tmp_path / "ih")
    os.makedirs(state_dir, exist_ok=True)
    state_file = os.path.join(state_dir, "oortcloud_state.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"access_token": "legacy-oc"}, f)
    integ = OortCloudIntegration(state_dir=state_dir)
    assert integ._state.get("access_token") == "legacy-oc"
    raw = json.loads(open(state_file, encoding="utf-8").read())
    assert raw["access_token"].startswith("enc:v1:")


def test_oortcloud_missing_state_file_ok(tmp_path):
    integ = OortCloudIntegration(state_dir=str(tmp_path / "empty"))
    assert integ._state == {}
    assert integ.is_authenticated() is False


# ── SEC-21：授权令牌回填 providers 表 + 自愈恢复 ──────────────


def _make_stub_provider_cls():
    from harness.modules.model_manager.openai_compatible import OpenAICompatibleProvider

    class _StubProvider(OpenAICompatibleProvider):
        base_url = "https://oort.example/v1"
        default_models = ["m1"]
        provider_name = "OortCloud"

    return _StubProvider


def _make_registry_with_db(tmp_path):
    from harness.infra.database import Database
    from harness.kernel.services import ServiceRegistry
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    db = Database(str(tmp_path / "providers.db"))
    reg = ProviderRegistry()
    services = ServiceRegistry()
    services.register(ProviderRegistry, reg, owner="test")
    services.register(Database, db, owner="test")
    return reg, services


async def test_upsert_provider_backfills_encrypted_access_token(tmp_path):
    """SEC-21：authenticate → _upsert_provider 必须把 access_token 密文回填 providers 表。"""
    from harness.infra.crypto import APIKeyEncryptor

    reg, services = _make_registry_with_db(tmp_path)
    integ = OortCloudIntegration(services=services, state_dir=str(tmp_path / "state"))

    await integ._upsert_provider(
        "https://oort.example/v1", "sk-test-key", ["m1"], access_token="user-login-tok"
    )

    cfg = reg.get_provider_config("custom_oortcloud")
    assert cfg is not None
    enc = (cfg.get("extra_params") or {}).get("oortcloud_access_token_encrypted")
    assert enc, "access_token 未回填 providers 表"
    assert enc != "user-login-tok", "回填的是明文（应为密文）"
    assert APIKeyEncryptor().decrypt(enc) == "user-login-tok"


def test_state_file_loss_self_heals_from_provider_table(tmp_path):
    """SEC-21：state 文件丢失后，重启即可从 providers 表密文自愈恢复，无需重走 oauth。"""
    from harness.infra.crypto import APIKeyEncryptor

    reg, services = _make_registry_with_db(tmp_path)
    # 预置已回填的 provider（模拟此前授权过）
    reg.register_provider(
        "custom_oortcloud",
        _make_stub_provider_cls(),
        {
            "api_key_encrypted": APIKeyEncryptor().encrypt("sk-x"),
            "extra_params": {
                "oortcloud_access_token_encrypted": APIKeyEncryptor().encrypt("tok-recovered")
            },
            "enabled": True,
        },
    )
    # state 目录为空（文件被删 / 换机）
    integ = OortCloudIntegration(services=services, state_dir=str(tmp_path / "fresh-state"))
    assert integ._state.get("access_token") == "tok-recovered"
    assert integ.is_authenticated() is True
    # 恢复后已落盘（下次启动直接读文件）
    assert os.path.exists(os.path.join(str(tmp_path / "fresh-state"), "oortcloud_state.json"))


def test_extra_params_private_keys_excluded_from_request_body():
    """SEC-21：*_encrypted / _ 前缀私有键绝不进请求体（防止令牌密文泄漏给上游）。"""
    from harness.modules.model_manager.openai_compatible import OpenAICompatibleProvider

    p = OpenAICompatibleProvider(
        api_key="sk",
        base_url="https://x/v1",
        extra_params={
            "temperature": 0.7,
            "top_p": 0.9,
            "oortcloud_access_token_encrypted": "enc:v1:secret-blob",
            "_integration_meta": "internal",
        },
    )
    body = p._build_request_body([{"role": "user", "content": "hi"}], "m", stream=False)
    # 公开请求参数保留
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.9
    # 私有键被过滤
    assert "oortcloud_access_token_encrypted" not in body
    assert "_integration_meta" not in body


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
