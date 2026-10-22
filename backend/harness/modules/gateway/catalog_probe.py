"""模型清单探测（catalog probe）—— 定期核对通道模型是否仍在免费目录中。

与"真实流量冷却"（HealthTracker，被动、按 (channel,model)）互补：本模块走
**只读的 GET 目录端点**（不发起生成、不消耗额度），判断模型是否从上游清单消失。

语义（对齐可用性判定的非对称原则，见 classify.py / probe.js）：
- 只有**成功拿到目录**且模型 id 不在其中，才判为"已下架"（denied）；
- 目录请求失败（网络/5xx/超时）一律返回 None = "无信息"，保留上次判定，
  绝不因一次抓取抖动把可用模型藏起来——隐藏一个能用模型的代价高于保留一个陈条目。

调用方（GatewayService._probe_loop）拿到 present/absent 后写入 model_catalog，
前端通过 `/api/gateway/model-availability` 读取，实现定期检测 + 实时状态。
"""

from __future__ import annotations

import json
from typing import Any

import httpx


def _collect_ids(node: Any, depth: int = 0, out: set[str] | None = None) -> set[str]:
    """广度优先收集所有「带 id 字段的对象」的 id 值（跨三家目录形状通用）。"""
    if out is None:
        out = set()
    if depth > 8:
        return out
    if isinstance(node, list):
        for item in node:
            _collect_ids(item, depth + 1, out)
    elif isinstance(node, dict):
        for field in ("id", "key"):  # Qoder 的清单用 key，其余用 id
            value = node.get(field)
            if isinstance(value, str) and value:
                out.add(value)
        for value in node.values():
            _collect_ids(value, depth + 1, out)
    return out


async def fetch_catalog_ids(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float = 12.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str] | None:
    """GET 目录端点并抽出在线模型 id 集合。

    返回 None 表示"无法判定"（请求失败 / 非 2xx / 无法解析）——调用方应保留
    上次结论，不得据此改变模型可用性。成功则返回 id 集合（可能为空）。
    """
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=transport
        ) as client:
            response = await client.get(url, headers=headers)
        if response.status_code >= 400:
            return None
        data = json.loads(response.text)
    except Exception:  # noqa: BLE001 —— 任何抓取/解析失败都是"无信息"
        return None
    return _collect_ids(data)


def present_ids(catalog: set[str] | None, channel_models: list[str], prefix: str = "") -> set[str] | None:
    """把通道配置的模型名映射到目录 id 口径，返回"确实在线"的通道模型集合。

    catalog 为 None 时透传 None（无从判定）。匹配同时尝试裸名与带前缀名，兼容
    Cline 的 `provider/model` 与 WorkBuddy/Zen 的纯 id 两种目录写法。
    """
    if catalog is None:
        return None
    present: set[str] = set()
    for model in channel_models:
        candidates = {model}
        if prefix and model.startswith(prefix + "/"):
            candidates.add(model[len(prefix) + 1 :])
        elif "/" not in model:
            candidates.add(f"{prefix}/{model}" if prefix else model)
        if candidates & catalog:
            present.add(model)
    return present
