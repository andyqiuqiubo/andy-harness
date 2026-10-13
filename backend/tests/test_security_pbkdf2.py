"""SEC-34 PBKDF2 成本提升 + 历史哈希后向兼容测试。"""

from __future__ import annotations

import hashlib
import secrets

from harness.infra import security


def test_hash_and_verify_current_cost() -> None:
    salt = secrets.token_hex(16)
    h = security.hash_password("correct horse", salt)
    assert security.verify_password("correct horse", salt, h) is True
    assert security.verify_password("wrong", salt, h) is False


def test_verify_accepts_legacy_200k_hash() -> None:
    """升级前已注册用户持有的 200k 哈希必须仍可校验（不锁存量用户）。"""
    salt = secrets.token_hex(16)
    legacy_hash = hashlib.pbkdf2_hmac(
        "sha256", b"legacy pw", bytes.fromhex(salt), 200_000
    ).hex()
    assert security.verify_password("legacy pw", salt, legacy_hash) is True
    assert security.verify_password("not legacy", salt, legacy_hash) is False


def test_new_hash_differs_from_legacy_for_same_input() -> None:
    salt = secrets.token_hex(16)
    new_hash = security.hash_password("same", salt)
    legacy_hash = hashlib.pbkdf2_hmac(
        "sha256", b"same", bytes.fromhex(salt), 200_000
    ).hex()
    assert new_hash != legacy_hash
