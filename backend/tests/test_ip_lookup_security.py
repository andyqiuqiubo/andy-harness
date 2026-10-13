"""P2 #40 / SEC-34：ip_lookup 走统一 SSRF 护栏（net_guard.safe_fetch_text）。

验证：
- 正常响应：护栏返回文本 → 工具解析并回显地理位置；
- 护栏抛 ValueError（SSRF / 重定向 / 体积超限）→ 工具回显「被安全策略拒绝」，
  不再把裸 httpx 请求直接打到用户可控地址。
"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.tool_ip_lookup.main import IpLookupTool


async def test_ip_lookup_uses_guard_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    canned = (
        '{"status":"success","query":"1.2.3.4","country":"中国","countryCode":"CN",'
        '"regionName":"广东","city":"深圳","isp":"电信","timezone":"Asia/Shanghai",'
        '"lat":22.5,"lon":114.0}'
    )

    async def _fake(url: str, **_: Any) -> tuple[str, str]:
        return url, canned

    import plugins.tool_ip_lookup.main as m

    monkeypatch.setattr(m, "safe_fetch_text", _fake)
    tool = IpLookupTool()
    out = await tool.execute({"ip": "1.2.3.4"})
    assert "1.2.3.4" in out
    assert "深圳" in out
    assert "电信" in out


async def test_ip_lookup_safety_denied_on_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(url: str, **_: Any) -> tuple[str, str]:
        raise ValueError("拒绝访问私有/保留地址: evil.host → 127.0.0.1")

    import plugins.tool_ip_lookup.main as m

    monkeypatch.setattr(m, "safe_fetch_text", _fake)
    tool = IpLookupTool()
    out = await tool.execute({"ip": "9.9.9.9"})
    assert "被安全策略拒绝" in out


async def test_ip_lookup_uses_guard_for_empty_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    async def _fake(url: str, **_: Any) -> tuple[str, str]:
        captured["url"] = url
        return url, '{"status":"fail","message":"x"}'

    import plugins.tool_ip_lookup.main as m

    monkeypatch.setattr(m, "safe_fetch_text", _fake)
    tool = IpLookupTool()
    out = await tool.execute({})
    # 空 ip 也走护栏（查询出口 IP 场景），且仍返回失败文案
    assert "ip-api.com/json/" in captured["url"]
    assert "查询失败" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
