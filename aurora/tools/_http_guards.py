"""HTTP fetch guards — SSRF protection for any tool that fetches a user-controlled URL.

Usage:

    async with safe_httpx_client() as client:
        resp = await client.get(url)

The client disables automatic redirect following and resolves each hop's target
IP against a denylist of private/loopback/link-local/cloud-metadata ranges.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlparse, urljoin

import httpx

logger = logging.getLogger(__name__)


# Cloud-provider metadata endpoints and other hostnames that must never be reached
# regardless of DNS answers.
_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata",
    "169.254.169.254",
    "fd00:ec2::254",
    "100.100.100.200",  # Alibaba
}

# Private / internal IP ranges. Extended beyond RFC1918 to cover every
# well-known unroutable / internal space.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),         # "this network"
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local (incl. cloud metadata)
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918
    ipaddress.ip_network("192.0.0.0/24"),      # IETF protocol assignments
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918
    ipaddress.ip_network("198.18.0.0/15"),     # benchmarking
    ipaddress.ip_network("224.0.0.0/4"),       # multicast
    ipaddress.ip_network("240.0.0.0/4"),       # reserved
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped IPv6
]

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5


class UnsafeURLError(Exception):
    """Raised when a URL resolves to a blocked target."""


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return any(ip in net for net in _BLOCKED_NETWORKS)


def _resolve_all(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve host to every IP it advertises (A + AAAA)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeURLError(f"DNS resolution failed for {host!r}: {exc}") from exc
    addrs: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return addrs


def validate_url(url: str) -> None:
    """Raise UnsafeURLError if the URL is not safe to fetch.

    Checks:
      - scheme is http(s)
      - hostname is not explicitly blocklisted
      - every resolved IP is public (rejects DNS-rebinding races only partially —
        the fetch itself still races; callers that need absolute safety should
        resolve once and connect to that specific IP via the Host header)
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} is not allowed")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise UnsafeURLError("URL has no host")

    if host in _BLOCKED_HOSTNAMES:
        raise UnsafeURLError(f"hostname {host!r} is blocklisted")

    # Directly-specified IP literals
    try:
        literal = ipaddress.ip_address(host)
        if _is_blocked_ip(literal):
            raise UnsafeURLError(f"IP {literal} is in a blocked range")
        return
    except ValueError:
        pass  # not an IP literal; resolve via DNS

    addrs = _resolve_all(host)
    if not addrs:
        raise UnsafeURLError(f"{host!r} has no resolvable addresses")
    for ip in addrs:
        if _is_blocked_ip(ip):
            raise UnsafeURLError(f"{host!r} resolves to blocked IP {ip}")


@asynccontextmanager
async def safe_httpx_client(
    *,
    timeout: float = 12.0,
    headers: dict | None = None,
) -> AsyncIterator["SafeClient"]:
    """Yield a client that validates every outgoing URL (including redirects).

    Redirects are followed manually so each hop passes validate_url().
    """
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers=headers or {},
    ) as client:
        yield SafeClient(client)


class SafeClient:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def get(self, url: str) -> httpx.Response:
        return await self._request("GET", url)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            validate_url(current)
            resp = await self._client.request(method, current, **kwargs)
            if resp.is_redirect and "location" in resp.headers:
                next_url = urljoin(current, resp.headers["location"])
                if next_url == current:
                    return resp
                current = next_url
                method = "GET"
                kwargs.pop("data", None)
                kwargs.pop("json", None)
                continue
            return resp
        raise UnsafeURLError(f"too many redirects starting from {url!r}")
