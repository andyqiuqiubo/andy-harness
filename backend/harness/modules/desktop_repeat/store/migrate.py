"""Schema 迁移器（§4.2 / §8.4）。

当前只有 v1：加字段不升版（读取端忽略未知、写端入 ext）；语义变更才升版并
在此登记 migrate_N_to_N+1 链。保留 API 形状，升版时只需注册新函数。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

CURRENT = 1

_MIGRATORS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register_migrator(from_version: int) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]],
                                Callable[[dict[str, Any]], dict[str, Any]]]:
    def deco(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        _MIGRATORS[from_version] = fn
        return fn
    return deco


def migrate_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """链式升到 CURRENT。缺 schema 字段视为 1。"""
    v = int(doc.get("schema", doc.get("v", 1)) or 1)
    if v > CURRENT:
        from ..errors import RepeatError
        raise RepeatError("RP-6002", f"数据版本过新（v={v} > {CURRENT}），请升级程序")
    while v < CURRENT:
        fn = _MIGRATORS.get(v)
        if fn is None:
            from ..errors import RepeatError
            raise RepeatError("RP-6002", f"缺少 v{v} → v{v + 1} 迁移器")
        doc = fn(doc)
        v += 1
        doc["schema"] = v
    return doc
