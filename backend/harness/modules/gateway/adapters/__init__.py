"""适配器注册表。"""

from __future__ import annotations

from harness.modules.gateway.adapters.base import AdapterRegistry
from harness.modules.gateway.adapters.openai_like import OpenAILikeAdapter

__all__ = ["AdapterRegistry", "OpenAILikeAdapter"]
