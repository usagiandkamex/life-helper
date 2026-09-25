"""Outbound URL checks shared by web_fetch and the browser tools.

Any public http(s) site may be reached. What stays blocked: internal addresses (SSRF: loopback, private and
link-local ranges such as the metadata endpoint, also when a public name resolves to them), hosts whose URLs carry
API keys (connectors only), and URLs that would carry sensitive data or secrets out of the app.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import unquote_plus, urlsplit

from .security import SENSITIVE_LABELS, SecretMasker, detect_sensitive

# Hosts that carry API keys in the URL: only connectors may call them, never web_fetch or the browser.
CONNECTOR_HOSTS = ("openapi.rakuten.co.jp", "app.rakuten.co.jp", "api.github.com")
ALLOWED_SCHEMES = ("http", "https")
INTERNAL_REASON = "内部のアドレス（{host}）には接続できません"
DNS_CACHE_SECONDS = 60
_DNS_CACHE_MAX = 1024

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_dns_cache: dict[str, tuple[float, str | None]] = {}


def host_matches(host: str, domains: list[str] | tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def is_internal_ip(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def _parse_ipv4_number(part: str) -> int | None:
    try:
        if part[:2].lower() == "0x":
            return int(part[2:] or "0", 16)
        if len(part) > 1 and part.startswith("0"):
            return int(part[1:], 8)
        return int(part, 10) if part.isdigit() else None
    except ValueError:
        return None


def literal_ip(host: str) -> IPAddress | None:
    """Parses the host like a browser does, including shorthand IPv4 forms such as ``2130706433`` or ``0x7f.1``."""
    host = host.strip("[]").rstrip(".")
    try:
        return ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        pass
    parts = host.split(".")
    if not parts or len(parts) > 4:
        return None
    numbers = [_parse_ipv4_number(p) for p in parts]
    if any(n is None for n in numbers):
        return None
    values = [n for n in numbers if n is not None]
    if any(n > 255 for n in values[:-1]) or values[-1] >= 256 ** (5 - len(values)):
        return None
    address = values[-1]
    for i, n in enumerate(values[:-1]):
        address += n << (8 * (3 - i))
    return ipaddress.IPv4Address(address)


def url_rejection(url: str, masker: SecretMasker | None = None) -> str | None:
    """Returns why ``url`` must not be requested, or None. Never echoes the full URL (it may carry sensitive data)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".")
        _ = parts.port
    except ValueError:
        return "URL の形式が正しくありません"
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not host:
        return "http / https の URL だけ参照できます"
    if parts.username or parts.password:
        return "認証情報を含む URL は参照できません"
    if host == "localhost" or host.endswith(".localhost"):
        return INTERNAL_REASON.format(host=host)
    ip = literal_ip(host)
    if ip is not None and is_internal_ip(ip):
        return INTERNAL_REASON.format(host=host)
    if host_matches(host, CONNECTOR_HOSTS):
        return f"{host} は API キーを使うサービスです。用意されたツール（コネクタ）を使ってください"
    decoded = unquote_plus(url)
    kinds = detect_sensitive(decoded)
    if kinds:
        labels = "、".join(SENSITIVE_LABELS[k] for k in kinds)
        return f"機微情報（{labels}）を含む URL は送信できません"
    if masker is not None and (masker.contains_secret(url) or masker.contains_secret(decoded)):
        return "秘密情報を含む URL は送信できません"
    return None


async def _lookup(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def resolved_rejection(url: str) -> str | None:
    """Rejects hosts whose DNS answers include an internal address. Results are cached briefly per host."""
    try:
        host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return "URL の形式が正しくありません"
    if not host or literal_ip(host) is not None:
        return None
    now = time.monotonic()
    cached = _dns_cache.get(host)
    if cached is not None and now - cached[0] < DNS_CACHE_SECONDS:
        return cached[1]
    try:
        addresses = await _lookup(host)
    except (OSError, UnicodeError):
        reason: str | None = f"{host} の名前解決ができませんでした"
    else:
        ips = [literal_ip(a) for a in addresses]
        internal = not ips or any(ip is None or is_internal_ip(ip) for ip in ips)
        reason = INTERNAL_REASON.format(host=host) if internal else None
    if len(_dns_cache) >= _DNS_CACHE_MAX:
        _dns_cache.clear()
    _dns_cache[host] = (now, reason)
    return reason


async def outbound_rejection(url: str, masker: SecretMasker | None = None) -> str | None:
    return url_rejection(url, masker) or await resolved_rejection(url)


def clear_dns_cache() -> None:
    _dns_cache.clear()
