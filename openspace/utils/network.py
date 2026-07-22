"""Network binding helpers shared by local control-plane servers."""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    """Return whether *host* is an explicit IPv4/IPv6 loopback address."""

    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized.split("%", 1)[0]).is_loopback
    except ValueError:
        return False
