"""gateway 认证与多租户 —— agw- Key 生命周期、租户隔离、RPM/TPM 限流。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from harness.modules.gateway.contracts import GatewayError
from harness.modules.gateway.store import SqlDB, now_iso

logger = logging.getLogger("harness.gateway.auth")


@dataclass(slots=True)
class KeyContext:
    key_id: str
    tenant_id: str
    name: str
    scopes: list[str]
    rpm: int
    tpm: int
    allowed_models: list[str] = field(default_factory=lambda: ["*"])
    allowed_channels: list[str] = field(default_factory=lambda: ["*"])

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


@dataclass(slots=True)
class IssuedKey:
    key_id: str
    token: str
    prefix: str
    tenant_id: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_iso(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class GatewayAuthService:
    """数据面 Key 校验 + 租户管理。管理面鉴权仍由 auth_manager 负责。"""

    def __init__(self, db: SqlDB) -> None:
        self.db = db
        self._last_touch: dict[str, float] = {}
        self.limiter = RateLimiter()

    # ---- 初始化 -------------------------------------------------------- #
    def ensure_default_tenant(self) -> None:
        row = self.db.query_one("SELECT id FROM gw_tenants WHERE id = 'default'")
        if row is None:
            self.db.execute(
                "INSERT INTO gw_tenants (id, name, status, allowed_models_json, allowed_channels_json, created_at) "
                "VALUES ('default', '本地默认租户', 'active', '[\"*\"]', '[\"*\"]', ?)",
                (now_iso(),),
            )

    def bootstrap(self, bootstrap_key: str | None = None, key_file: Path | None = None) -> str | None:
        """无 Key 时创建引导 Key；返回明文（仅本次调用可见）。"""
        self.ensure_default_tenant()
        row = self.db.query_one("SELECT COUNT(*) AS n FROM gw_keys")
        count = int(row["n"]) if row is not None else 0
        if count > 0:
            return None
        token = bootstrap_key or os.environ.get("GATEWAY_BOOTSTRAP_KEY") or "agw-" + secrets.token_urlsafe(32)
        self.issue_key("default", "bootstrap", ["chat", "models", "admin"], 0, 0, plaintext=token)
        if key_file is not None:
            try:
                key_file.parent.mkdir(parents=True, exist_ok=True)
                key_file.write_text(token + "\n", encoding="utf-8")
                try:
                    key_file.chmod(0o600)
                except OSError:
                    pass
                logger.info("gateway 引导 Key 已写入: %s", key_file)
            except OSError as e:
                logger.warning("gateway 引导 Key 写盘失败: %s", e)
        return token

    def _require_tenant(self, tenant_id: str) -> None:
        row = self.db.query_one("SELECT id, status FROM gw_tenants WHERE id = ?", (tenant_id,))
        if row is None:
            raise ValueError(f"租户不存在: {tenant_id}")
        if str(row["status"]) != "active":
            raise ValueError(f"租户已停用: {tenant_id}")

    # ---- Key ----------------------------------------------------------- #
    def issue_key(
        self,
        tenant_id: str,
        name: str,
        scopes: list[str],
        rpm: int = 0,
        tpm: int = 0,
        *,
        expires_at: str | None = None,
        plaintext: str | None = None,
    ) -> IssuedKey:
        self._require_tenant(tenant_id)
        token = plaintext or "agw-" + secrets.token_urlsafe(32)
        key_id = "gwk_" + uuid.uuid4().hex[:12]
        prefix = token[:12]
        self.db.execute(
            "INSERT INTO gw_keys (id, tenant_id, name, prefix, key_hash, scopes_json, rpm, tpm, "
            " expires_at, revoked_at, last_used_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                key_id,
                tenant_id,
                name,
                prefix,
                _hash(token),
                json.dumps(scopes, ensure_ascii=False),
                max(0, int(rpm)),
                max(0, int(tpm)),
                expires_at,
                now_iso(),
            ),
        )
        return IssuedKey(key_id=key_id, token=token, prefix=prefix, tenant_id=tenant_id)

    def verify(self, token: str) -> KeyContext | None:
        if not token or not token.startswith("agw-"):
            return None
        prefix = token[:12]
        rows = self.db.query("SELECT * FROM gw_keys WHERE prefix = ? AND revoked_at IS NULL", (prefix,))
        digest = _hash(token)
        for row in rows:
            if not secrets.compare_digest(str(row["key_hash"]), digest):
                continue
            if _parse_iso(row["expires_at"]) and _parse_iso(row["expires_at"]) < time.time():
                return None
            tenant = self.db.query_one("SELECT * FROM gw_tenants WHERE id = ?", (row["tenant_id"],))
            if tenant is None or str(tenant["status"]) != "active":
                return None
            self._touch(str(row["id"]))
            return KeyContext(
                key_id=str(row["id"]),
                tenant_id=str(row["tenant_id"]),
                name=str(row["name"]),
                scopes=_json_list(row["scopes_json"]),
                rpm=int(row["rpm"] or 0),
                tpm=int(row["tpm"] or 0),
                allowed_models=_json_list(tenant["allowed_models_json"]),
                allowed_channels=_json_list(tenant["allowed_channels_json"]),
            )
        return None

    def _touch(self, key_id: str) -> None:
        last = self._last_touch.get(key_id, 0.0)
        if time.time() - last < 60:
            return
        self._last_touch[key_id] = time.time()
        try:
            self.db.execute("UPDATE gw_keys SET last_used_at = ? WHERE id = ?", (now_iso(), key_id))
        except Exception:  # noqa: BLE001
            pass

    def list_keys(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        if tenant_id:
            rows = self.db.query("SELECT * FROM gw_keys WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,))
        else:
            rows = self.db.query("SELECT * FROM gw_keys ORDER BY created_at DESC")
        return [_public_key(r) for r in rows]

    def rotate(self, key_id: str, grace_hours: int = 24) -> IssuedKey | None:
        """轮换：签发同权限新 Key，旧 Key 进入 24h 宽限期后失效。"""
        row = self.db.query_one("SELECT * FROM gw_keys WHERE id = ? AND revoked_at IS NULL", (key_id,))
        if row is None:
            return None
        issued = self.issue_key(
            str(row["tenant_id"]),
            f"{row['name']}-rotated",
            _json_list(row["scopes_json"]),
            int(row["rpm"] or 0),
            int(row["tpm"] or 0),
        )
        expires = (datetime.now(UTC) + timedelta(hours=grace_hours)).isoformat(timespec="seconds")
        self.db.execute("UPDATE gw_keys SET expires_at = ? WHERE id = ?", (expires, key_id))
        return issued

    def revoke(self, key_id: str) -> bool:
        row = self.db.query_one("SELECT id FROM gw_keys WHERE id = ?", (key_id,))
        if row is None:
            return False
        self.db.execute("UPDATE gw_keys SET revoked_at = ? WHERE id = ?", (now_iso(), key_id))
        return True

    # ---- 租户 ---------------------------------------------------------- #
    def create_tenant(
        self,
        tenant_id: str,
        name: str = "",
        allowed_models: list[str] | None = None,
        allowed_channels: list[str] | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO gw_tenants (id, name, status, allowed_models_json, allowed_channels_json, created_at) "
            "VALUES (?, ?, 'active', ?, ?, ?)",
            (
                tenant_id,
                name or tenant_id,
                json.dumps(allowed_models or ["*"], ensure_ascii=False),
                json.dumps(allowed_channels or ["*"], ensure_ascii=False),
                now_iso(),
            ),
        )

    def list_tenants(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM gw_tenants ORDER BY created_at ASC")
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "allowed_models": _json_list(r["allowed_models_json"]),
                "allowed_channels": _json_list(r["allowed_channels_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


class RateLimiter:
    """进程内 RPM/TPM 令牌窗口（单节点够用；多节点替换为 Redis）。"""

    def __init__(self) -> None:
        self._events: dict[str, deque[tuple[float, int]]] = {}

    def check(self, ctx: KeyContext, estimated_tokens: int = 0) -> None:
        now = time.time()
        if len(self._events) > 256:  # 清理已离开窗口的 key，避免无界增长
            for key in [k for k, dq in self._events.items() if not dq]:
                del self._events[key]
        window = self._events.setdefault(ctx.key_id, deque())
        while window and now - window[0][0] > 60:
            window.popleft()

        if ctx.rpm > 0 and len(window) >= ctx.rpm:
            raise GatewayError("GW_RATE_LIMITED", f"超过 Key 的 RPM 限制（{ctx.rpm}/min）", 429)

        if ctx.tpm > 0:
            used = sum(t for _, t in window)
            if used + estimated_tokens > ctx.tpm:
                raise GatewayError("GW_RATE_LIMITED", f"超过 Key 的 TPM 限制（{ctx.tpm}/min）", 429)

        window.append((now, max(0, estimated_tokens)))


def _json_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(value, list):
            return [str(v) for v in value]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _public_key(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "scopes": _json_list(row["scopes_json"]),
        "rpm": int(row["rpm"] or 0),
        "tpm": int(row["tpm"] or 0),
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
    }
