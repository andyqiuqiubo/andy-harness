"""AuthService —— 认证与多用户服务（E12）。

设计原则：
- **默认关闭**：本地单用户工具定位下，不启用认证时行为与过去完全一致；
- **显式开启**：`HARNESS_AUTH=1`（或 true/on/yes）后，所有 `/api/**`（除
  health / status / login / 渠道入站）均须携带 Bearer token；
- **管理员引导**：启用后若尚无用户，创建管理员（用户名 / 密码来自环境变量，
  未提供密码则生成随机密码并打印到日志）；
- **数据归属迁移**：启用认证时，把历史无主会话 / 记忆划归首位管理员，
  保证旧数据可见；新会话按登录用户归属。
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harness.infra.database import Database
from harness.infra.security import (
    DEFAULT_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    generate_salt,
    hash_password,
    verify_access_token,
    verify_password,
)

logger = logging.getLogger("harness.auth")

# 开启认证的环境变量与取值。
_ENV_ENABLED = "HARNESS_AUTH"
# 签名密钥（未设置则每次启动随机生成，重启后旧 token 失效）。
_ENV_SECRET = "HARNESS_AUTH_SECRET"
# 管理员引导凭据。
_ENV_ADMIN_USERNAME = "HARNESS_AUTH_ADMIN_USERNAME"
_ENV_ADMIN_PASSWORD = "HARNESS_AUTH_ADMIN_PASSWORD"
# 渠道会话归属用户（默认首位管理员）。
_ENV_CHANNEL_USERNAME = "HARNESS_CHANNEL_USERNAME"
# 令牌有效期（分钟）。
_ENV_TOKEN_EXPIRE = "HARNESS_AUTH_TOKEN_EXPIRE_MINUTES"

_TRUE = {"1", "true", "yes", "on"}


@dataclass
class User:
    """一个用户账户。"""

    id: str
    username: str
    is_admin: bool = False
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """对外公开信息（绝不含密码 / 盐）。"""
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "created_at": self.created_at,
        }


class AuthService(Protocol):
    """认证服务接口。"""

    @property
    def enabled(self) -> bool: ...

    @property
    def secret(self) -> str: ...

    def bootstrap(self) -> None:
        """初始化：建管理员、迁移历史数据。"""
        ...

    def authenticate(self, username: str, password: str) -> User | None:
        """校验用户名密码，返回用户；失败返回 None。"""
        ...

    def login(self, username: str, password: str) -> dict[str, Any] | None:
        """登录，返回 {token, expires_at, user}；失败返回 None。"""
        ...

    def user_from_token(self, token: str) -> User | None:
        """从 token 解析用户；无效 / 过期返回 None。"""
        ...

    def create_user(self, username: str, password: str, is_admin: bool = False) -> User:
        """创建用户（重名 / 非法输入抛 ValueError）。"""
        ...

    def list_users(self) -> list[User]:
        """列出全部用户。"""
        ...

    def delete_user(self, user_id: str) -> bool:
        """删除用户（保留至少一个管理员；其数据不随之删除）。"""
        ...

    def default_user_id(self) -> str:
        """渠道 / 系统级会话的归属用户 id（禁用时返回 ''）。"""
        ...


class AuthServiceImpl(AuthService):
    """认证服务实现。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._enabled = _env_bool(_ENV_ENABLED)
        env_secret = os.environ.get(_ENV_SECRET, "")
        if env_secret:
            # 显式配置优先，且每次启动稳定（不受文件影响）。
            self._secret = env_secret
        else:
            # P2-2：未显式配置时，密钥持久化到 data/auth_secret.txt，
            # 避免重启随机重新生成导致已签发 token 全部失效、全员被踢回登录页。
            self._secret = self._load_or_create_secret()
        try:
            self._expire_minutes = int(os.environ.get(_ENV_TOKEN_EXPIRE, str(DEFAULT_TOKEN_EXPIRE_MINUTES)))
        except ValueError:
            self._expire_minutes = DEFAULT_TOKEN_EXPIRE_MINUTES
        self._bootstrapped = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def secret(self) -> str:
        return self._secret

    @staticmethod
    def _secret_file() -> Path:
        """密钥落地文件（与数据库同目录的 data/）。"""
        # auth_manager/service.py → modules → harness → backend
        return Path(__file__).resolve().parents[3] / "data" / "auth_secret.txt"

    @classmethod
    def _load_or_create_secret(cls) -> str:
        """读取已落地的密钥；不存在则生成并持久化。

        仅当未显式配置 HARNESS_AUTH_SECRET 时调用，保证重启后 token 不失效。
        """
        path = cls._secret_file()
        try:
            if path.exists():
                stored = path.read_text(encoding="utf-8").strip()
                if stored:
                    return stored
        except OSError:
            pass
        secret = secrets.token_hex(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 仅属主可读写，降低明文密钥泄露面。
            path.write_text(secret, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as e:  # noqa: BLE001
            logger.warning("持久化认证密钥失败，本次启动将使用临时密钥: %s", e)
        return secret

    # ── 初始化 ──────────────────────────────────────

    def bootstrap(self) -> None:
        """启用后建管理员并迁移历史无主数据；禁用时为空操作。"""
        if self._bootstrapped:
            return
        self._bootstrapped = True
        if not self._enabled:
            return

        if not self.list_users():
            username = os.environ.get(_ENV_ADMIN_USERNAME, "admin").strip() or "admin"
            password = os.environ.get(_ENV_ADMIN_PASSWORD, "")
            generated = False
            if not password:
                password = secrets.token_urlsafe(16)
                generated = True
            self.create_user(username, password, is_admin=True)
            if generated:
                logger.warning(
                    "已启用认证但未提供管理员密码，已生成初始管理员 %r / %s（请立即登录并修改），也可用 %s 预设",
                    username,
                    password,
                    _ENV_ADMIN_PASSWORD,
                )
            else:
                logger.info("已创建初始管理员 %r", username)

        admin = self._first_admin()
        if admin is not None:
            # 历史无主数据划归首位管理员，保证启用认证后旧数据可见。
            self._db.execute("UPDATE sessions SET user_id=? WHERE user_id=''", (admin.id,))
            self._db.execute("UPDATE memories SET user_id=? WHERE user_id=''", (admin.id,))

    # ── 认证 ────────────────────────────────────────

    def authenticate(self, username: str, password: str) -> User | None:
        row = self._db.query_one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        if row is None:
            return None
        if not verify_password(password, row["salt"], row["password_hash"]):
            return None
        return self._row_to_user(row)

    def login(self, username: str, password: str) -> dict[str, Any] | None:
        user = self.authenticate(username, password)
        if user is None:
            return None
        token = create_access_token(
            {"uid": user.id, "username": user.username, "is_admin": user.is_admin},
            self._secret,
            expires_in_seconds=self._expire_minutes * 60,
        )
        return {
            "token": token,
            "expires_in_seconds": self._expire_minutes * 60,
            "user": user.to_dict(),
        }

    def user_from_token(self, token: str) -> User | None:
        claims = verify_access_token(token, self._secret)
        if claims is None:
            return None
        row = self._db.query_one("SELECT * FROM users WHERE id = ?", (str(claims.get("uid", "")),))
        return self._row_to_user(row) if row is not None else None

    # ── 用户管理 ─────────────────────────────────────

    def create_user(self, username: str, password: str, is_admin: bool = False) -> User:
        username = (username or "").strip()
        if not username or len(username) > 64:
            raise ValueError("用户名非法（1–64 个字符）")
        if not password or len(password) < 6:
            raise ValueError("密码至少 6 个字符")
        if self._db.query_one("SELECT id FROM users WHERE username = ?", (username,)):
            raise ValueError(f"用户名已存在: {username}")

        uid = f"user_{uuid.uuid4().hex[:12]}"
        salt = generate_salt()
        self._db.execute(
            "INSERT INTO users (id, username, password_hash, salt, is_admin) VALUES (?, ?, ?, ?, ?)",
            (uid, username, hash_password(password, salt), salt, 1 if is_admin else 0),
        )
        logger.info("已创建用户: %s (admin=%s)", username, is_admin)
        return self._must_get(uid)

    def list_users(self) -> list[User]:
        rows = self._db.query("SELECT * FROM users ORDER BY created_at ASC, rowid ASC")
        return [self._row_to_user(r) for r in rows]

    def delete_user(self, user_id: str) -> bool:
        row = self._db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            return False
        remaining_admins = self._db.query_one(
            "SELECT COUNT(*) AS cnt FROM users WHERE is_admin = 1 AND id != ?",
            (user_id,),
        )
        if row["is_admin"] and int((remaining_admins or {"cnt": 0})["cnt"]) < 1:
            raise ValueError("不能删除最后一个管理员")
        self._db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        logger.info("已删除用户: %s", row["username"])
        return True

    # ── 系统会话归属 ─────────────────────────────────

    def default_user_id(self) -> str:
        """渠道会话归属用户：指定用户名优先，否则首位管理员；禁用返回 ''。"""
        if not self._enabled:
            return ""
        username = os.environ.get(_ENV_CHANNEL_USERNAME, "").strip()
        if username:
            row = self._db.query_one("SELECT id FROM users WHERE username = ?", (username,))
            if row is not None:
                return str(row["id"])
        admin = self._first_admin()
        return admin.id if admin is not None else ""

    # ── 内部辅助 ─────────────────────────────────────

    def _first_admin(self) -> User | None:
        row = self._db.query_one("SELECT * FROM users WHERE is_admin = 1 ORDER BY created_at ASC, rowid ASC")
        return self._row_to_user(row) if row is not None else None

    def _must_get(self, uid: str) -> User:
        row = self._db.query_one("SELECT * FROM users WHERE id = ?", (uid,))
        assert row is not None
        return self._row_to_user(row)

    @staticmethod
    def _row_to_user(row: Any) -> User:
        return User(
            id=str(row["id"]),
            username=str(row["username"]),
            is_admin=bool(row["is_admin"]),
            created_at=str(row["created_at"] or ""),
        )


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE
