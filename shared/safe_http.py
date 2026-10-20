"""shared/safe_http.py — AA-875: the one outbound HTTP client for tenant-supplied URLs (SSRF guard).

Use `request()` for every call to a URL a tenant typed or can influence (WordPress site, anything a
tenant saves). Never open an `aiohttp.ClientSession` / `httpx.AsyncClient` on such a URL directly.

What it enforces, on the first request and on every redirect hop:
  * scheme allowlist (https only by default) and no user-info in the URL;
  * the host must be public: an IP literal is checked before the hop; a name is resolved by
    `_PublicOnlyResolver`, which refuses the host when ANY record is private, loopback, link-local
    (169.254.169.254 metadata), CGNAT, ULA (fd00::/8, fd00:ec2::254), multicast or reserved;
  * aiohttp connects only to the addresses that resolver returned (DNS cache off), so the address
    checked is the address connected — a DNS answer that changes after an earlier check (DNS
    rebinding) cannot reach an internal address;
  * redirects followed by hand (at most `max_redirects`), credentials dropped on a cross-origin hop;
  * numeric hosts only in canonical form (`2130706433`, `127.1`, `0177.0.0.1` are refused: aiohttp
    sends them straight to getaddrinfo, past the resolver);
  * one total deadline over all hops, a connect timeout, a response body cap (on the decompressed
    stream), no proxy from env.

Every rejection logs `safe_http_blocked` (host, reason, stage) — never the path or query, which can
carry credentials. `UnsafeURLError` messages are safe to show the tenant.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Union
from urllib.parse import urljoin

import aiohttp
import structlog
from aiohttp.abc import AbstractResolver, ResolveResult
from multidict import CIMultiDict
from yarl import URL

logger = structlog.get_logger()

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_SCHEMES = ("https",)

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CREDENTIAL_HEADERS = {"authorization", "cookie", "proxy-authorization"}
_CHUNK = 64 * 1024

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Explicit deny list on top of `is_global`, so the rule does not depend on how a Python version
# classifies a range. Anything in here is never a tenant's public web server.
_DENY_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8", "2001:db8::/32",
    # IPv4-compatible (deprecated, ::a.b.c.d) and SIIT (::ffff:0:a.b.c.d): `is_global` calls them
    # public, and some stacks map them to the embedded IPv4 (::7f00:1 -> 127.0.0.1). Never a web host.
    "::/96", "::ffff:0:0:0/96",
))
_NAT64_NETS = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48"))
_BLOCKED_NAMES = {"localhost"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


class UnsafeURLError(ValueError):
    """The URL or the address it resolves to is not allowed. The message is safe to show a tenant."""


class ResponseTooLarge(Exception):
    """The response body is larger than the cap."""


class TooManyRedirects(Exception):
    """More redirect hops than allowed."""


@dataclass(frozen=True)
class SafeResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""
    url: str = ""
    encoding: str = "utf-8"
    history: Sequence[int] = field(default_factory=tuple)

    def text(self) -> str:
        try:
            return self.body.decode(self.encoding or "utf-8", errors="replace")
        except LookupError:  # unknown charset in the response header
            return self.body.decode("utf-8", errors="replace")


# ── address rules ─────────────────────────────────────────────────────────────────────────────

def is_public_ip(ip: Union[str, IPAddress]) -> bool:
    """True only for a globally routable unicast address."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            return False
        if any(ip in net for net in _NAT64_NETS):
            return is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.is_multicast or any(ip in net for net in _DENY_NETS if net.version == ip.version):
        return False
    return ip.is_global


def _block(reason: str, host: str, stage: str, message: str) -> UnsafeURLError:
    logger.warning("safe_http_blocked", host=host, reason=reason, stage=stage)
    return UnsafeURLError(message)


def validate_url(url: str, *, allowed_schemes: Iterable[str] = DEFAULT_SCHEMES, stage: str = "url") -> URL:
    """Static checks, no network: scheme, user-info, host name, IP literal. Returns the parsed URL.
    A name is checked against DNS later (`check_destination` / the connector's resolver)."""
    schemes = tuple(allowed_schemes)
    try:
        parsed = URL(url.strip())
    except (ValueError, TypeError):
        raise _block("unparseable", "", stage, "URL is not valid")
    host = (parsed.host or "").rstrip(".").lower()
    if parsed.scheme not in schemes:
        allowed = " or ".join(f"{s}://" for s in schemes)
        raise _block("scheme", host, stage, f"URL must start with {allowed}")
    if not host:
        raise _block("no_host", "", stage, "URL must include a valid hostname")
    if parsed.user is not None or parsed.password is not None:
        raise _block("userinfo", host, stage, "URL must not contain a username or password")
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise _block("local_name", host, stage, "URL cannot point to a local address")
    try:
        literal = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is None and _looks_like_ip_to_aiohttp(parsed.host or ""):
        # aiohttp skips the resolver for any all-digits-and-dots host and hands it to getaddrinfo,
        # whose inet_aton reads `2130706433`, `127.1`, `0177.0.0.1`, `0` as 127.0.0.1 / 0.0.0.0. Only
        # the canonical forms `ipaddress` parses (checked above) are allowed.
        raise _block("noncanonical_ip", host, stage, "URL cannot point to a private or reserved IP address")
    if literal is not None and not is_public_ip(literal):
        raise _block("private_ip", host, stage, "URL cannot point to a private or reserved IP address")
    return parsed


def _looks_like_ip_to_aiohttp(host: str) -> bool:
    """Mirror of `aiohttp.helpers.is_ip_address` (3.x): such a host never reaches our resolver."""
    return ":" in host or host.replace(".", "").isdigit()


async def _getaddrinfo(host: str, port: int, family: int = socket.AF_UNSPEC) -> list:
    """The single DNS lookup primitive (unit tests replace it)."""
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family,
                                  flags=socket.AI_ADDRCONFIG)


async def _resolve_public(host: str, port: int, family: int, stage: str) -> List[ResolveResult]:
    infos = await _getaddrinfo(host, port, family)
    results: List[ResolveResult] = []
    for fam, _type, proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        if not is_public_ip(address):
            raise _block("private_dns", host, stage, "URL cannot point to a private or reserved IP address")
        results.append(ResolveResult(hostname=host, host=address, port=sockaddr[1] or port, family=fam,
                                     proto=proto, flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV))
    if not results:
        raise OSError(f"no address for {host}")
    return results


async def check_destination(url: str, *, allowed_schemes: Iterable[str] = DEFAULT_SCHEMES,
                            allow_unresolved: bool = False, timeout: float = 5.0) -> None:
    """`validate_url` + one DNS lookup: refuses a host with any non-public record. Advisory (for a
    clear error at save time) — `request()` checks again at connect, which is the real control.
    `allow_unresolved`: a name that does not resolve (yet) or a lookup that times out passes."""
    parsed = validate_url(url, allowed_schemes=allowed_schemes)
    host = (parsed.host or "").rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
        return  # IP literal, already checked
    except ValueError:
        pass
    try:
        await asyncio.wait_for(_resolve_public(host, parsed.port or 443, socket.AF_UNSPEC, "dns"), timeout)
    except UnsafeURLError:
        raise
    except (OSError, asyncio.TimeoutError):
        if not allow_unresolved:
            raise


class _PublicOnlyResolver(AbstractResolver):
    """aiohttp resolver that only ever hands back public addresses (else raises UnsafeURLError)."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> List[ResolveResult]:
        return await _resolve_public(host, port, family, "dns")

    async def close(self) -> None:
        return None


# ── the client ────────────────────────────────────────────────────────────────────────────────

def _origin(url: URL) -> tuple:
    return (url.scheme, (url.host or "").lower(), url.port)


async def _read_capped(resp: aiohttp.ClientResponse, max_bytes: int, host: str) -> bytes:
    declared = resp.content_length
    if declared is not None and declared > max_bytes:
        logger.warning("safe_http_response_too_large", host=host, declared=declared, cap=max_bytes)
        raise ResponseTooLarge(f"response larger than {max_bytes} bytes")
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(_CHUNK):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            logger.warning("safe_http_response_too_large", host=host, cap=max_bytes)
            raise ResponseTooLarge(f"response larger than {max_bytes} bytes")
    return bytes(buf)


async def request(
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, Any]] = None,
    json: Any = None,
    data: Any = None,
    auth: Optional[aiohttp.BasicAuth] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    allowed_schemes: Iterable[str] = DEFAULT_SCHEMES,
) -> SafeResponse:
    """One HTTP exchange with a tenant-supplied URL. Returns the final response with the whole
    body read (≤ max_bytes). Raises UnsafeURLError (blocked destination, any hop), TooManyRedirects,
    ResponseTooLarge, asyncio.TimeoutError, or aiohttp.ClientError (network)."""
    schemes = tuple(allowed_schemes)
    method = method.upper()
    current = validate_url(url, allowed_schemes=schemes)
    if params:
        current = current.update_query(params)
    req_headers = CIMultiDict(headers or {})
    history: List[int] = []

    # aiohttp's `total` applies per session.request() call, i.e. per hop; `asyncio.timeout` makes
    # `timeout` one deadline for the whole exchange (all hops + body), so callers' budgets hold.
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=min(DEFAULT_CONNECT_TIMEOUT_S, timeout))
    connector = aiohttp.TCPConnector(resolver=_PublicOnlyResolver(), use_dns_cache=False, force_close=True)
    async with (
        asyncio.timeout(timeout),
        aiohttp.ClientSession(connector=connector, timeout=client_timeout, trust_env=False) as session,
    ):
        while True:
            async with session.request(method, current, headers=req_headers, json=json, data=data,
                                       auth=auth, allow_redirects=False) as resp:
                location = resp.headers.get("Location")
                if resp.status in _REDIRECT_STATUSES and location:
                    history.append(resp.status)
                    status = resp.status
                else:
                    body = await _read_capped(resp, max_bytes, current.host or "")
                    return SafeResponse(status=resp.status, headers=CIMultiDict(resp.headers), body=body,
                                        url=str(current), encoding=resp.charset or "utf-8",
                                        history=tuple(history))
            if len(history) > max_redirects:
                logger.warning("safe_http_too_many_redirects", host=current.host, hops=len(history))
                raise TooManyRedirects(f"more than {max_redirects} redirects")
            nxt = validate_url(urljoin(str(current), location), allowed_schemes=schemes, stage="redirect")
            if _origin(nxt) != _origin(current):
                for name in list(req_headers):
                    if name.lower() in _CREDENTIAL_HEADERS:
                        del req_headers[name]
                auth = None
            if status == 303 or (status in (301, 302) and method == "POST"):
                if method != "HEAD":
                    method = "GET"
                json = data = None
            current = nxt
