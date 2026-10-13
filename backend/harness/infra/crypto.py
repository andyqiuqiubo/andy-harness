"""API Key 加密存储 —— Fernet 对称加密，密钥派生自本地 machine key。

machine key 生成与存储：
- Windows: winreg 中的 MachineGuid
- Linux: /etc/machine-id
- macOS: IOPlatformUUID (ioreg 命令)
密钥不落盘明文，每次启动动态派生。密钥丢失时提示用户重新输入 API Key。
"""

from __future__ import annotations

import hashlib
import logging
import platform
import re
import subprocess
import uuid

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("harness.crypto")


class CryptoError(Exception):
    """加密/解密错误。"""


class MachineKeyDerivationError(CryptoError):
    """machine key 派生失败。"""


def get_machine_id() -> str:
    """获取操作系统级机器标识。

    Returns:
        机器标识字符串

    Raises:
        MachineKeyDerivationError: 无法获取机器标识
    """
    system = platform.system()

    if system == "Windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\\Microsoft\\Cryptography",
            ) as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                return str(guid)
        except Exception as e:
            raise MachineKeyDerivationError(f"无法获取 Windows MachineGuid: {e}") from e

    elif system == "Linux":
        try:
            # 尝试 /etc/machine-id
            try:
                with open("/etc/machine-id") as f:
                    return f.read().strip()
            except FileNotFoundError:
                pass
            # 回退到 /var/lib/dbus/machine-id
            with open("/var/lib/dbus/machine-id") as f:
                return f.read().strip()
        except Exception as e:
            raise MachineKeyDerivationError(f"无法获取 Linux machine-id: {e}") from e

    elif system == "Darwin":
        try:
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                check=True,
            )
            for line in result.stdout.split("\n"):
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
            raise MachineKeyDerivationError("无法从 ioreg 输出中解析 IOPlatformUUID")
        except Exception as e:
            raise MachineKeyDerivationError(f"无法获取 macOS IOPlatformUUID: {e}") from e

    else:
        # 回退：使用 Python uuid.getnode()（基于 MAC 地址）
        return str(uuid.getnode())


def derive_fernet_key(machine_id: str) -> bytes:
    """从 machine_id 派生 Fernet 密钥。

    Args:
        machine_id: 机器标识

    Returns:
        Fernet 密钥（32 字节 base64 编码）
    """
    # 使用 SHA-256 哈希派生固定长度的密钥种子
    hash_bytes = hashlib.sha256(machine_id.encode("utf-8")).digest()
    # Fernet 密钥需要是 32 字节的 base64 编码
    import base64

    return base64.urlsafe_b64encode(hash_bytes)


class APIKeyEncryptor:
    """API Key 加密器。

    使用 Fernet 对称加密，密钥派生自本地 machine key。
    """

    def __init__(self, machine_id: str | None = None) -> None:
        """初始化加密器。

        Args:
            machine_id: 可选的机器标识（测试时注入），默认自动获取
        """
        mid = machine_id or get_machine_id()
        key = derive_fernet_key(mid)
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """加密 API Key。

        Args:
            plaintext: 明文 API Key

        Returns:
            加密后的字符串
        """
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """解密 API Key。

        Args:
            ciphertext: 加密后的字符串

        Returns:
            明文 API Key

        Raises:
            CryptoError: 解密失败（如 machine key 变更）
        """
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as e:
            raise CryptoError("API Key 解密失败——machine key 可能已变更，请重新输入 API Key") from e


def mask_api_key(api_key: str) -> str:
    """脱敏 API Key 用于日志输出（只显示前 4 位 + ****）。"""
    if len(api_key) <= 8:
        return "****"
    return api_key[:4] + "****"


# 需要抹除其值的 URL 查询参数 / 头字段名（大小写不敏感）。
_SENSITIVE_KEY_NAMES = (
    "api_key", "apikey", "access_token", "accesstoken", "token", "secret",
    "secretkey", "secret_key", "password", "passwd", "auth", "authorization",
    "bearer", "sk", "key", "credential", "credentials", "x-api-key",
)

# 头字段形态专用名表：仅含真正的 HTTP 头名（排除 bearer / sk，分别由步骤 1 / 2
# 处理；也不含 api_key/token 等查询参数名，避免与步骤 3 重复处理）。
_SENSITIVE_HEADER_NAMES = (
    "accesstoken", "secretkey", "secret_key", "authorization", "x-api-key",
)

# 兜底：连续 >=20 位的高熵 base64/hex 串，常见于 API Key / 令牌。
_SECRET_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")


def redact_secrets(text: str) -> str:
    """把文本中可能泄露的密钥/令牌抹成 ****（SEC-32）。

    用于错误消息与日志，避免 ``sk-`` 密钥、``Bearer`` 凭据、``accesstoken`` /
    ``secretkey`` 头、URL 查询串中的令牌经 HTTP 响应或落盘日志外泄。

    策略（按顺序，避免重复替换）：
    1. ``Bearer <token>``（先于 sk-，整体吞掉后续高熵 token）。
    2. ``sk-`` 前缀密钥（保留前缀便于定位，主体抹除）。
    3. URL 查询串中敏感键对应的值（``?api_key=...``，值不跨 ``&``/空格）。
    4. ``Header: <value>`` / ``Header=<value>`` 形态的凭据头（值不跨 ``&``/空格）。
    5. 兜底：连续 >=20 位的高熵串（避免遗漏自定义命名）。

    注意：仅做文本级抹除，不解析结构；返回文本若本就无密钥则原样返回。
    """
    if not text:
        return text
    out = text
    # 1. Bearer <token>（先于 sk-，避免 Bearer sk-xxx 被拆成两段 ****）
    out = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._\-]+", "Bearer ****", out)
    # 2. sk- 密钥
    out = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-****", out, flags=re.IGNORECASE)
    # 3. URL 查询串敏感键值（?x= / &x=，值不跨 & 与空格）
    out = re.sub(
        rf"(?i)(?<=[?&])({'|'.join(_SENSITIVE_KEY_NAMES)})=[^&#\s]+",
        lambda m: f"{m.group(1)}=****",
        out,
    )
    # 4. 头字段形态（accesstoken: xxx / secretkey=xxx / Authorization: ...，值不跨 & 与空格）
    out = re.sub(
        rf"(?i)({'|'.join(_SENSITIVE_HEADER_NAMES)})\s*[:=]\s*[^&\s]+",
        lambda m: f"{m.group(1)}: ****",
        out,
    )
    # 5. 兜底高熵长串
    out = _SECRET_TOKEN_RE.sub("****", out)
    return out
