from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import unquote, urlparse


_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}
_METADATA_HOSTS = {"169.254.169.254"}
_SECRET_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:^|[?&#;])(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|signature|sig)=",
        r"sk-[A-Za-z0-9_-]{6,}",
        r"fc-[A-Za-z0-9_-]{6,}",
        r"tvly-[A-Za-z0-9_-]{6,}",
        r"exa_[A-Za-z0-9_-]{6,}",
    )
]


def _ip_is_unsafe(ip: ipaddress._BaseAddress) -> bool:
    return any(
        (
            ip.is_loopback,
            ip.is_private,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_unspecified,
            ip.is_reserved,
        )
    )


def contains_embedded_secret(url: str) -> bool:
    raw = str(url or "")
    decoded = unquote(raw)
    return any(pattern.search(raw) or pattern.search(decoded) for pattern in _SECRET_PATTERNS)


def _literal_ip(hostname: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return None


def is_safe_url(url: str) -> tuple[bool, str | None]:
    value = str(url or "").strip()
    if not value:
        return False, "URL is required"
    if contains_embedded_secret(value):
        return False, "URL contains embedded credential material"

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False, "URL scheme must be http or https"
    if not parsed.hostname:
        return False, "URL hostname is required"
    if parsed.username or parsed.password:
        return False, "URL must not include embedded credentials"

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in _LOCAL_HOSTNAMES:
        return False, "localhost URLs are not allowed"
    if hostname in _METADATA_HOSTS:
        return False, "metadata service URLs are not allowed"

    literal = _literal_ip(hostname)
    if literal is not None:
        if _ip_is_unsafe(literal):
            return False, "private or local IP URLs are not allowed"
        return True, None

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False, "URL port is invalid"
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        return True, None

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _ip_is_unsafe(ip):
            return False, "hostname resolves to a private or local IP"
    return True, None
