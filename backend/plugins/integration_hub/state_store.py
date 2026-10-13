"""integration_hub 状态文件的安全读写。

历史问题（CODE-REVIEW-2026-10-12 SEC-09/10）：OortCloud 的 access_token、
飞书的 tenant_access_token / app_secret / webhook_secret 曾以**明文**直接
``json.dump`` 到磁盘（权限 644），任何本机进程/备份/同步目录都能读到凭据。

本模块统一收敛为：

1. **敏感字段加密**：经 :class:`harness.infra.crypto.APIKeyEncryptor`
   （Fernet，密钥派生自 machine key，不落盘）加密后存储，密文统一带
   ``enc:v1:`` 前缀，便于识别与版本演进；
2. **兼容历史明文**：读取时对无前缀的值按明文透传（旧文件不炸），同时
   :func:`needs_upgrade` 让调用方在下一次保存时自动把明文升级为密文；
3. **原子写 + 权限收敛**：先写临时文件再 ``os.replace``，避免半截 JSON；
   保存后 ``chmod 0o600``（POSIX 生效；Windows 上 os.chmod 仅映射只读位，
   0o600 不会设只读，真正的防护来自加密本身）；
4. **fail-closed**：密文解密失败（machine key 变更）时字段置 None，
   迫使重新授权，而不是静默使用坏值。
"""

from __future__ import annotations

import json
import logging
import os
import stat
from typing import Any

logger = logging.getLogger("harness.plugin.integration_hub.state_store")

# 加密字段存储前缀（版本化，便于未来升级算法）
_ENC_PREFIX = "enc:v1:"

# 原子写临时文件后缀
_TMP_SUFFIX = ".tmp"


def _get_encryptor() -> Any:
    """获取加密器；machine key 派生失败时返回 None（降级为明文 + 告警）。"""
    try:
        from harness.infra.crypto import APIKeyEncryptor

        return APIKeyEncryptor()
    except Exception as e:  # noqa: BLE001 - machine key 不可用时降级
        logger.warning("APIKeyEncryptor 初始化失败，敏感字段将无法加密（仅收敛文件权限）: %s", e)
        return None


def _is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def encrypt_field(value: Any) -> Any:
    """加密单个字段；非字符串 / 空值 / 加密器不可用时原样返回。"""
    if not isinstance(value, str) or not value:
        return value
    enc = _get_encryptor()
    if enc is None:
        return value
    try:
        return _ENC_PREFIX + enc.encrypt(value)
    except Exception as e:  # noqa: BLE001 - 加密失败不阻断保存，退回明文并告警
        logger.warning("敏感字段加密失败，将以明文写入: %s", e)
        return value


def decrypt_field(value: Any) -> Any:
    """解密单个字段。

    - 无 ``enc:v1:`` 前缀 → 历史明文，原样返回（由调用方决定是否升级）；
    - 解密失败 → 返回 None（fail-closed，调用方按「未授权」处理）。
    """
    if not _is_encrypted(value):
        return value
    enc = _get_encryptor()
    if enc is None:
        logger.warning("无法解密敏感字段（加密器不可用），按未授权处理")
        return None
    try:
        return enc.decrypt(value[len(_ENC_PREFIX) :])
    except Exception as e:  # noqa: BLE001 - CryptoError/InvalidToken 等
        logger.warning("敏感字段解密失败（machine key 可能已变更），按未授权处理: %s", e)
        return None


def _read_raw(path: str) -> dict[str, Any]:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001 - 损坏文件按空状态处理
        logger.warning("读取状态文件失败: %s", e)
        return {}


def load_state(path: str, sensitive_fields: tuple[str, ...]) -> dict[str, Any]:
    """读取状态文件并解密敏感字段（返回内存可用的明文 dict）。"""
    data = _read_raw(path)
    for key in sensitive_fields:
        if key in data:
            data[key] = decrypt_field(data[key])
    return data


def needs_upgrade(path: str, sensitive_fields: tuple[str, ...]) -> bool:
    """检测磁盘文件中是否仍存在明文敏感字段（用于一次性迁移升级）。

    调用方在 ``_load_state`` 末尾调用：为 True 时立即 ``_save_state()``，
    把旧版明文无缝升级为密文（无需用户重新授权）。
    """
    raw = _read_raw(path)
    return any(
        isinstance(raw.get(key), str) and raw[key] and not _is_encrypted(raw[key]) for key in sensitive_fields
    )


def save_state(state_dir: str, path: str, data: dict[str, Any], sensitive_fields: tuple[str, ...]) -> None:
    """加密敏感字段后原子写入状态文件，并收敛文件权限到 0o600。"""
    payload: dict[str, Any] = {
        key: (encrypt_field(value) if key in sensitive_fields else value) for key, value in data.items()
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = path + _TMP_SUFFIX
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        # Windows 兜底：若目标曾被其它工具置为只读，先解除只读再替换
        if os.path.exists(path):
            try:
                os.chmod(path, stat.S_IWRITE)
            except OSError:
                pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001 - 保存失败不抛（与历史行为一致）
        logger.warning("写入状态文件失败: %s", e)
