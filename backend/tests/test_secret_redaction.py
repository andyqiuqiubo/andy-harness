"""SEC-32：secret 脱敏工具 redact_secrets 的单元测试。

验证 redact_secrets 能抹除各类密钥形态，且不对明文文本造成破坏。
独立文件，不触碰正在编辑中的其它测试。
"""

from __future__ import annotations

import pytest

from harness.infra.crypto import redact_secrets


def test_sk_prefix_redacted() -> None:
    out = redact_secrets("领取失败: sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert "sk-****" in out
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in out


def test_bearer_standalone_redacted() -> None:
    out = redact_secrets("Bearer sk-1234567890abcdef token end")
    assert "Bearer ****" in out
    assert "sk-1234567890abcdef" not in out


def test_authorization_header_redacted() -> None:
    out = redact_secrets("Authorization: Bearer sk-1234567890abcdef token end")
    # 无论 Bearer 是否紧跟 Authorization，原始令牌都必须消失
    assert "sk-1234567890abcdef" not in out
    assert "****" in out


def test_url_query_token_redacted() -> None:
    url = "https://api.example.com/data?api_key=supersecretkey12345&foo=bar"
    out = redact_secrets(f"抓取失败: {url}")
    assert "api_key=****" in out
    assert "supersecretkey12345" not in out
    # 非敏感参数保留
    assert "foo=bar" in out


def test_accesstoken_header_redacted() -> None:
    out = redact_secrets("accesstoken: abcdef0123456789abcdef0123456789 leaked")
    assert "accesstoken: ****" in out
    assert "abcdef0123456789abcdef0123456789" not in out


def test_secretkey_header_redacted() -> None:
    out = redact_secrets("secretkey=myverylongsecretkeyvalue0000000000 end")
    assert "secretkey: ****" in out
    assert "myverylongsecretkeyvalue0000000000" not in out


def test_high_entropy_fallback_redacted() -> None:
    # 一个 >=20 位的无上下文高熵串应被兜底抹除
    out = redact_secrets("token is AAAAAAAAAABBBBBBBBBBCCCCCCCCC and done")
    assert "AAAAAABBBBBBBBBBCCCCCCCCC" not in out or "****" in out


def test_plain_text_untouched() -> None:
    msg = "连接超时（ConnectTimeout），请稍后重试；首次调用可能较慢"
    assert redact_secrets(msg) == msg


def test_empty_and_none_like() -> None:
    assert redact_secrets("") == ""
    # 不含敏感串的短字符串原样返回（兜底阈值 20，不会误伤）
    assert redact_secrets("short text here") == "short text here"


def test_oortcloud_describe_error_safe() -> None:
    """模拟 OortCloud 异常文本经 redact_secrets 后不含 sk-/Bearer 凭据。"""
    raw = "HTTPStatusError: 401 for url https://x/v1/chat with Bearer sk-xyzverylongkey0000000000000000"
    out = redact_secrets(raw)
    assert "sk-xyzverylongkey" not in out
    assert "Bearer ****" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
