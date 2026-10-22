"""路径安全守卫：所有「用户输入 → 文件路径」的拼接必须走这里。

背景（体检 P0-1 / P1-5）：录制回放模块把 REST 路径参数 ``sid`` 直接与
``sessions_root`` 拼接后交给 ``shutil.rmtree``。FastAPI/Starlette 的路径参数
正则是 ``[^/]+``，``..`` 与 ``%2e%2e`` 都能匹配，于是：

    DELETE /api/desktop-repeat/sessions/..      → 删掉 sessions_root 的父目录
    DELETE /api/desktop-repeat/sessions/../victim → 删掉同级任意目录

对比：逆向模块的 ``run_id`` 走 ``_validate_run_id`` 白名单（安全），录制回放
没有做这一步——**同一仓库两套标准**。

本模块提供统一入口 ``safe_join``：先做标识符白名单校验，再 ``resolve()``
确认目标仍在根目录内（``is_relative_to``），最后才允许调用方使用该路径。
凡是把「外部可控字符串」拼进磁盘路径的地方，一律改用它。
"""

from __future__ import annotations

import re
from pathlib import Path

# 标识符白名单：只允许字母、数字、连字符、下划线与点，且不以点开头/结尾。
# 刻意保守：会「看起来像路径」的输入一律拒绝，而不是尝试清洗。
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class UnsafePathError(ValueError):
    """路径越界/非法标识符（调用方转成 4xx 业务错误）。"""


def is_safe_identifier(name: str) -> bool:
    """标识符是否满足白名单（不含路径分隔符、不含 ``..``、不以点开头）。"""
    if not isinstance(name, str) or not name:
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    return bool(_IDENT_RE.match(name))


def safe_join(root: Path, *parts: str) -> Path:
    """在 ``root`` 下安全拼接路径。

    - ``parts`` 中任一元素不是合法标识符 → 抛 :class:`UnsafePathError`；
    - 拼接结果 resolve 后不在 ``root`` 内 → 抛 :class:`UnsafePathError`；
    - 返回已 resolve 的绝对路径。

    >>> safe_join(Path("/tmp/root"), "a").name
    'a'
    """
    if not parts:
        raise UnsafePathError("路径片段为空")
    for p in parts:
        if not is_safe_identifier(p):
            raise UnsafePathError(f"非法路径标识符: {p!r}")
    root_resolved = Path(root).resolve()
    target = root_resolved
    for p in parts:
        target = target / p
    target = target.resolve()
    # 二次保险：即使白名单被绕过，resolve 后的路径必须仍在根目录内。
    if target != root_resolved and not target.is_relative_to(root_resolved):
        raise UnsafePathError(f"路径越界: {'/'.join(parts)}")
    return target


def safe_join_or_none(root: Path, *parts: str) -> Path | None:
    """同 :func:`safe_join`，但越界时返回 ``None`` 而不是抛异常。"""
    try:
        return safe_join(root, *parts)
    except UnsafePathError:
        return None
