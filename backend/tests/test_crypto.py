"""加密基础设施测试。"""

import pytest

from harness.infra.crypto import APIKeyEncryptor, CryptoError, mask_api_key


class TestAPIKeyEncryptor:
    """APIKeyEncryptor 测试。"""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        """加密后解密，恢复原文。"""
        encryptor = APIKeyEncryptor(machine_id="test-machine-12345")
        plaintext = "sk-1234567890abcdef"

        encrypted = encryptor.encrypt(plaintext)
        decrypted = encryptor.decrypt(encrypted)

        assert decrypted == plaintext
        assert encrypted != plaintext  # 密文与明文不同

    def test_encrypted_is_not_plaintext(self) -> None:
        """加密后的密文中不包含明文。"""
        encryptor = APIKeyEncryptor(machine_id="test-machine-12345")
        plaintext = "sk-my-secret-key-12345"

        encrypted = encryptor.encrypt(plaintext)

        assert plaintext not in encrypted

    def test_different_machine_key_cannot_decrypt(self) -> None:
        """不同 machine key 无法解密。"""
        encryptor1 = APIKeyEncryptor(machine_id="machine-A")
        encryptor2 = APIKeyEncryptor(machine_id="machine-B")

        plaintext = "sk-secret-key"
        encrypted = encryptor1.encrypt(plaintext)

        with pytest.raises(CryptoError):
            encryptor2.decrypt(encrypted)

    def test_wrong_ciphertext_raises_error(self) -> None:
        """无效密文抛出 CryptoError。"""
        encryptor = APIKeyEncryptor(machine_id="test-machine")

        with pytest.raises(CryptoError):
            encryptor.decrypt("invalid-ciphertext")


class TestMaskAPIKey:
    """API Key 脱敏测试。"""

    def test_mask_long_key(self) -> None:
        """长 key 只显示前 4 位。"""
        assert mask_api_key("sk-1234567890abcdef") == "sk-1****"

    def test_mask_short_key(self) -> None:
        """短 key 完全隐藏。"""
        assert mask_api_key("short") == "****"
        assert mask_api_key("12345678") == "****"

    def test_mask_empty_key(self) -> None:
        """空 key 完全隐藏。"""
        assert mask_api_key("") == "****"
