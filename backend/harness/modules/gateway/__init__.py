"""gateway —— LLM 网关中心。

对外：OpenAI 兼容 `/v1`；对内：Channel/凭据池/路由/多租户/用量/审计。
详见 docs/andy-gateway-设计方案.md。
"""

from __future__ import annotations

from harness.modules.gateway.service import ChatResult, GatewayService

__all__ = ["ChatResult", "GatewayService"]
