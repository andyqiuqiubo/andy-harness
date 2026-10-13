"""出站 URL 安全护栏（SSRF 防护 + 下载规模限制）。

背景（SEC-04/P1）：多处功能会把用户提供的 URL 交给 ``urllib`` / ``httpx``
去拉取——``tool_web_fetch``、``package_installer``、webhook 推送、MCP
HTTP transport、插件外部安装。攻击者（或一段恶意 Skill 说明）可以借此让
harness 去访问：

- 云元数据服务 ``169.254.169.254``（AWS/GCP/Azure 凭据泄露）；
- 本机回环服务（127.0.0.1:<port> 上的其它本地服务）；
- 内网主机（192.168.x / 10.x / 172.16-31.x）。

本模块提供统一判定：先校验 scheme（只放行 http/https），再解析主机名
并检查其解析出的全部 IP 是否落在私有/保留地址段，命中即拒绝。
可用 ``HARNESS_ALLOW_PRIVATE_URL=1`` 显式放开（内网自建源场景，自愿承担风险）。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

logger = logging.getLogger("harness.net_guard")

# 允许的出站协议（file:/ftp:/gopher: 等一律拒绝）
_ALLOWED_SCHEMES = ("http", "https")

# 解析失败时不放行（fail-closed）：无法确认 IP 就当作危险
_DEFAULT_ALLOW_PRIVATE = False

# 单文件下载上限（字节）：200 MB，防磁盘爆满
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

# 下载读取块大小
_DOWNLOAD_CHUNK = 64 * 1024


def allow_private_addresses() -> bool:
    """是否允许访问私有地址（由 HARNESS_ALLOW_PRIVATE_URL 控制）。"""
    return os.environ.get("HARNESS_ALLOW_PRIVATE_URL", "").strip().lower() in ("1", "true", "yes")


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ipversion_special(ip)
    )


def ipversion_special(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """特殊地址段：文档地址（RFC5737/3849）、基准测试段、IPv6 唯一本地地址。"""
    if isinstance(ip, ipaddress.IPv4Address):
        # 169.254.169.254 云元数据：is_link_local 已覆盖
        return False
    # IPv6 唯一本地地址 fc00::/7
    return isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local


def check_url(url: str) -> str | None:
    """校验出站 URL。返回 None 表示允许，否则返回拒绝原因（可直接用于报错）。"""
    try:
        parsed = urlparse(url)
    except ValueError as e:
        return f"URL 解析失败: {e}"

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"不允许的协议: {parsed.scheme or '(空)'}（仅支持 http/https）"

    host = parsed.hostname
    if not host:
        return "URL 缺少主机名"

    # 无法解析主机名 → fail-closed（不发起请求，避免 DNS rebinding 绕过时也无从判断）
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception as e:  # noqa: BLE001 - DNS 失败/超时
        return f"主机名解析失败: {host}（{type(e).__name__}）"

    if not allow_private_addresses():
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_forbidden_ip(ip):
                return f"拒绝访问私有/保留地址: {host} → {ip}"
    return None


# 网页抓取类请求的体积上限（远小于下载上限，避免巨型响应进上下文）
MAX_FETCH_BYTES = 5 * 1024 * 1024

# 重定向最多跟随的跳数（防重定向环 / 重定向链耗尽资源）
MAX_REDIRECTS = 5


async def safe_fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    max_bytes: int = MAX_FETCH_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> tuple[str, str]:
    """带 SSRF 校验的异步 GET，返回 ``(final_url, text)``。

    相比直接 ``httpx.AsyncClient(follow_redirects=True)``：

    - **逐跳校验**：`follow_redirects=True` 只对首跳生效，攻击者可用
      `http://allowed.com` 302 到 `http://169.254.169.254/latest/meta-data/`
      绕过；这里每一跳都重新 `assert_url_safe`；
    - **体积上限**：流式读取并累计，超限即中断（不把 GB 级响应读进内存）。

    Raises:
        ValueError: URL 不安全 / 重定向超限 / 响应超限。
    """
    import httpx

    current = url
    for _hop in range(max_redirects + 1):
        assert_url_safe(current)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(current, headers=headers)
            location = resp.headers.get("location")
            if resp.status_code in (301, 302, 303, 307, 308) and location:
                from urllib.parse import urljoin

                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            final_url = str(resp.url)

        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError(f"响应过大: 声明 {int(declared)} 字节，上限 {max_bytes} 字节")

        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"响应体积超过上限（{max_bytes} 字节）")
            chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return final_url, text
    raise ValueError(f"重定向次数超过上限（{max_redirects}）")


def assert_url_safe(url: str) -> None:
    """校验出站 URL；不安全时抛 ValueError（供调用方统一转成业务错误）。"""
    reason = check_url(url)
    if reason:
        raise ValueError(reason)


def download_to(url: str, dest: str, *, timeout: float, max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
    """带 SSRF 校验与体积上限的流式下载，返回实际写入字节数。

    - 先 :func:`assert_url_safe` 校验目标；
    - ``Content-Length`` 提前拒绝超大响应（不下载即拒绝）；
    - 分块写入并累计，超过 ``max_bytes`` 立即中断并删除半成品。
    """
    import urllib.request

    assert_url_safe(url)
    downloaded = 0
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 已做 SSRF 校验
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError(f"目标文件过大: 声明 {int(declared)} 字节，上限 {max_bytes} 字节")
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    fh.close()
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    raise ValueError(f"下载体积超过上限（{max_bytes} 字节）")
                fh.write(chunk)
    return downloaded
