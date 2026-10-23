"""网关自有凭据库（加密存储）——让用户不必安装 Magpie 也能使用免费通道。

背景：Qoder CN / WorkBuddy 的 token 原先只能"借"本机 Magpie 写下的
`~/.config/magpie/plugin-auth.json`，迫使用户先装一个第三方工具。本模块把
"登录授权"这一能力收进网关自身：用户在管理台点「登录」→ 跳浏览器完成厂商
授权 → token 由网关用 `APIKeyEncryptor`（Fernet，密钥派生自本机 machine key）
加密后写入 `gw_credentials` 表。Magpie 的登录态文件退化为可选兜底。

设计约束：
- 表由本模块自建（`ensure_schema`），不依赖 GatewayStore 的 DDL 顺序；
- 密文解密失败（如 machine key 变更）按"无凭据"处理，绝不向上游泄漏异常；
- 只存 token 与最小账号信息（uid/name/email/machine_id），不存口令。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from harness.infra.crypto import APIKeyEncryptor, CryptoError

logger = logging.getLogger("harness.gateway.credential_store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS gw_credentials (
    channel_id TEXT PRIMARY KEY,
    payload_enc TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class SqlDB(Protocol):
    """gateway 存储用到的最小数据库协议（与 store.py 一致）。"""

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any: ...

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]: ...

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> Any: ...


class GatewayCredentialStore:
    """按通道加密保存/读取网关自登录得到的凭据。"""

    def __init__(self, db: SqlDB, encryptor: APIKeyEncryptor | None = None) -> None:
        self.db = db
        self._enc = encryptor or APIKeyEncryptor()

    def ensure_schema(self) -> None:
        self.db.execute(SCHEMA)

    # ---- 读写 ---------------------------------------------------------- #
    def save(self, channel_id: str, payload: dict[str, Any]) -> None:
        """加密保存某通道的凭据载荷（明文只在调用栈内存里存在）。"""
        blob = self._enc.encrypt(json.dumps(payload, ensure_ascii=False))
        self.db.execute(
            "INSERT OR REPLACE INTO gw_credentials (channel_id, payload_enc, updated_at) "
            "VALUES (?, ?, ?)",
            (channel_id, blob, datetime.now(UTC).isoformat()),
        )

    def load(self, channel_id: str) -> dict[str, Any] | None:
        """读取并解密；无记录或解密失败都返回 None（按"未登录"处理）。"""
        row = self.db.query_one(
            "SELECT payload_enc FROM gw_credentials WHERE channel_id = ?", (channel_id,)
        )
        if not row:
            return None
        try:
            plain = self._enc.decrypt(str(row["payload_enc"]))
        except (CryptoError, KeyError, ValueError) as exc:
            logger.warning("通道 %s 凭据解密失败（machine key 变更？）: %s", channel_id, exc)
            return None
        try:
            data = json.loads(plain)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def delete(self, channel_id: str) -> None:
        self.db.execute("DELETE FROM gw_credentials WHERE channel_id = ?", (channel_id,))

    def channels(self) -> list[str]:
        rows = self.db.query("SELECT channel_id FROM gw_credentials ORDER BY channel_id")
        return [str(r["channel_id"]) for r in rows]
