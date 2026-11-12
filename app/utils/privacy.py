"""What may be recorded about customers (logs, database) under the owner's privacy policy."""

from __future__ import annotations

import ipaddress


def client_ip_for_record(ip: str | None, mode: str) -> str | None:
    """How a customer's IP may be written to logs or the database under the CLIENT_IP_LOGGING policy.

    ``off`` -> None (nothing is kept), ``masked`` -> the network only (``203.0.113.x``, ``2001:db8:1::/48``),
    ``full`` -> unchanged. Unparseable values are dropped unless the policy is ``full``.
    """
    if not ip or mode == "off":
        return None
    if mode == "full":
        return ip
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return ".".join(str(address).split(".")[:3]) + ".x"
    return str(ipaddress.ip_network(f"{address}/48", strict=False))
