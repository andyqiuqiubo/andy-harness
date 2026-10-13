"""安全原语：密码哈希与访问令牌签发（E12）。

零第三方依赖：
- 密码存储用 PBKDF2-HMAC-SHA256（`hashlib.pbkdf2_hmac`），每用户独立随机盐；
- 访问令牌为自签名的无状态 token：`base64url(payload).base64url(hmac-sha256)`，
  服务端只持有密钥，不存会话，重启后旧 token 失效（密钥变更时）。

不做的事：不实现 OAuth 流程、不刷新 token、不依赖外部身份服务。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

# PBKDF2 迭代次数（2023+ OWASP 建议 ≥ 600k）。
_PBKDF2_ITERATIONS = 600_000
# 历史哈希成本：已注册用户仍可能持有 200k 哈希，verify 需兼容（见 verify_password）。
_PBKDF2_LEGACY_ITERATIONS = 200_000

# 令牌默认有效期（分钟）。
DEFAULT_TOKEN_EXPIRE_MINUTES = 60 * 12  # 12 小时


def generate_salt() -> str:
    """生成随机盐（hex）。"""
    return secrets.token_hex(16)


def _pbkdf2(password: str, salt: str, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    )


def hash_password(password: str, salt: str) -> str:
    """用 PBKDF2-HMAC-SHA256 对密码加盐哈希，返回 hex（采用当前迭代成本）。"""
    return _pbkdf2(password, salt, _PBKDF2_ITERATIONS).hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    """校验密码与已存哈希是否一致（常数时间比较）。

    先按当前成本（600k）校验；不匹配时再按历史成本（200k）校验，
    以兼容升级前已注册用户的旧哈希，避免存量用户被锁。
    """
    if hmac.compare_digest(_pbkdf2(password, salt, _PBKDF2_ITERATIONS).hex(), expected_hash):
        return True
    if _PBKDF2_LEGACY_ITERATIONS != _PBKDF2_ITERATIONS:
        return hmac.compare_digest(
            _pbkdf2(password, salt, _PBKDF2_LEGACY_ITERATIONS).hex(), expected_hash
        )
    return False


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def create_access_token(
    claims: dict[str, Any],
    secret: str,
    expires_in_seconds: int = DEFAULT_TOKEN_EXPIRE_MINUTES * 60,
) -> str:
    """签发访问令牌。

    `claims` 至少包含 uid / username；自动补 iat / exp（Unix 秒）。
    """
    now = int(time.time())
    payload = {**claims, "iat": now, "exp": now + max(60, expires_in_seconds)}
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"


def verify_access_token(token: str, secret: str) -> dict[str, Any] | None:
    """校验令牌签名与有效期，返回 claims；无效 / 过期返回 None。"""
    try:
        payload_part, sig_part = token.split(".", 1)
    except ValueError:
        return None
    payload_bytes = _b64url_decode(payload_part)
    expected_sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_b64url_decode(sig_part), expected_sig):
            return None
        claims = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    if int(claims.get("exp", 0)) < int(time.time()):
        return None
    return claims
