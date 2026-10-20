"""AA-875 — shared/safe_http.py, the SSRF-safe client for tenant-supplied URLs.

Two layers:
  * pure rules: is_public_ip, validate_url, check_destination (fake DNS);
  * end to end against real local aiohttp servers. 127.0.0.1 stands in for "a public web server"
    (the `servers` fixture marks only that address public) and 127.0.0.2 for an internal address
    (e.g. the metadata service): its server counts every request, so "blocked" is proven by zero hits,
    not only by the exception.
"""
import asyncio
import socket

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from structlog.testing import capture_logs

from shared import safe_http
from shared.safe_http import ResponseTooLarge, TooManyRedirects, UnsafeURLError

HTTP = ("http",)  # the local test servers speak plain http


def _dns(mapping):
    """Fake safe_http._getaddrinfo. mapping: host -> ip | [ips] | callable(call_no) -> ip(s)."""
    calls = []

    async def getaddrinfo(host, port, family=0):
        calls.append(host)
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        value = mapping[host]
        if callable(value):
            value = value(calls.count(host))
        ips = [value] if isinstance(value, str) else value
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ips]
    getaddrinfo.calls = calls
    return getaddrinfo


# ── is_public_ip ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.169.254",
    "169.254.170.2", "100.64.0.1", "0.0.0.0", "224.0.0.1", "239.255.255.250", "255.255.255.255",
    "192.0.2.10", "198.18.0.1", "240.0.0.1",
    "::", "::1", "fd00:ec2::254", "fd12:3456::1", "fc00::1", "fe80::1", "fe80::1%eth0", "ff02::1",
    "::ffff:169.254.169.254", "::ffff:10.0.0.1", "2002:a00:1::1", "64:ff9b::a9fe:a9fe", "2001:0:4136:e378::1",
    "2001:db8::1",
    "::7f00:1", "::a9fe:a9fe", "::a00:1", "::ffff:0:7f00:1", "::ffff:0:a9fe:a9fe",  # IPv4-compatible / SIIT
])
def test_non_public_addresses_are_refused(ip):
    assert safe_http.is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.215.14", "2606:4700:4700::1111", "::ffff:8.8.8.8",
                                "64:ff9b::808:808", "2002:808:808::1"])
def test_public_addresses_are_allowed(ip):
    assert safe_http.is_public_ip(ip) is True


# ── validate_url (static) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("url,match", [
    ("http://example.com", "https://"),
    ("ftp://example.com", "https://"),
    ("file:///etc/passwd", "https://"),
    ("https://", "hostname"),
    ("https://user:pw@example.com/", "username or password"),
    ("https://example.com@169.254.169.254/", "username or password"),
    ("https://localhost/", "local"),
    ("https://LOCALHOST./", "local"),
    ("https://api.localhost/", "local"),
    ("https://printer.local/", "local"),
    ("https://metadata.google.internal/", "local"),
    ("https://ip-10-0-0-5.us-west-1.compute.internal/", "local"),
    ("https://169.254.169.254/latest/meta-data/", "private or reserved"),
    ("https://10.0.0.1:8443/", "private or reserved"),
    ("https://[::1]/", "private or reserved"),
    ("https://[fd00:ec2::254]/", "private or reserved"),
    ("https://[::ffff:127.0.0.1]/", "private or reserved"),
])
def test_validate_url_refuses(url, match):
    with capture_logs() as logs, pytest.raises(UnsafeURLError, match=match):
        safe_http.validate_url(url)
    assert logs and logs[-1]["event"] == "safe_http_blocked"
    assert "latest" not in str(logs[-1]), "never log the path"


# Hosts aiohttp treats as IP literals (all digits and dots -> straight to getaddrinfo, past our resolver)
# but `ipaddress` cannot parse: inet_aton reads them as 127.0.0.1 / 10.0.0.1 / 0.0.0.0.
NONCANONICAL_NUMERIC_HOSTS = ["2130706433", "127.1", "0177.0.0.1", "012.0.0.1", "0", "2130706433.",
                              "167772161", "2852039166"]


@pytest.mark.parametrize("host", NONCANONICAL_NUMERIC_HOSTS)
def test_validate_url_refuses_noncanonical_numeric_host(host, monkeypatch):
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({}))
    with capture_logs() as logs, pytest.raises(UnsafeURLError, match="private or reserved"):
        safe_http.validate_url(f"https://{host}:443/wp-json/")
    assert logs[-1]["reason"] == "noncanonical_ip"
    assert safe_http._getaddrinfo.calls == []


@pytest.mark.parametrize("url", ["https://8.8.8.8/", "https://8.8.8.8./", "https://[2606:4700:4700::1111]/",
                                 "https://123.example.com/"])
def test_validate_url_accepts_canonical_public_ip_and_digit_labelled_names(url):
    assert safe_http.validate_url(url)


def test_validate_url_accepts_public_https_and_does_no_dns(monkeypatch):
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({}))
    assert str(safe_http.validate_url("https://example-tenant-blog.com/wp")) == "https://example-tenant-blog.com/wp"
    assert safe_http._getaddrinfo.calls == []


# ── check_destination (one DNS lookup) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_destination_refuses_a_name_resolving_to_metadata(monkeypatch):
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({"evil.example": "169.254.169.254"}))
    with capture_logs() as logs, pytest.raises(UnsafeURLError, match="private or reserved"):
        await safe_http.check_destination("https://evil.example/")
    assert logs[-1] == {"event": "safe_http_blocked", "host": "evil.example", "reason": "private_dns",
                        "stage": "dns", "log_level": "warning"}


@pytest.mark.asyncio
async def test_check_destination_refuses_when_any_record_is_private(monkeypatch):
    """Multi-record DNS: one public + one internal record -> the whole host is refused."""
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({"mixed.example": ["93.184.215.14", "10.0.0.7"]}))
    with pytest.raises(UnsafeURLError):
        await safe_http.check_destination("https://mixed.example/")


@pytest.mark.asyncio
async def test_check_destination_numeric_host_name_resolving_to_loopback(monkeypatch):
    """`https://2130706433/` is not an IP literal to the parser but getaddrinfo reads it as 127.0.0.1."""
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({"2130706433": "127.0.0.1"}))
    with pytest.raises(UnsafeURLError):
        await safe_http.check_destination("https://2130706433/")


@pytest.mark.asyncio
async def test_check_destination_unresolved_name(monkeypatch):
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({}))
    assert await safe_http.check_destination("https://not-live-yet.example/", allow_unresolved=True) is None
    with pytest.raises(socket.gaierror):
        await safe_http.check_destination("https://not-live-yet.example/")


@pytest.mark.asyncio
async def test_check_destination_public_name_passes(monkeypatch):
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({"blog.example": ["93.184.215.14", "2606:4700::1"]}))
    assert await safe_http.check_destination("https://blog.example/") is None


# ── end to end against local servers ─────────────────────────────────────────

class _Servers:
    def __init__(self, public, internal, internal_hits, public_hits):
        self.public, self.internal = public, internal
        self.internal_hits, self.public_hits = internal_hits, public_hits

    def pub(self, host, path):
        return f"http://{host}:{self.public.port}{path}"


@pytest_asyncio.fixture
async def servers(monkeypatch):
    public_hits, internal_hits = [], []

    async def ok(request):
        return web.json_response({"ok": True, "auth": request.headers.get("Authorization"),
                                  "method": request.method})

    async def redirect(request):
        raise web.HTTPFound(request.query["to"])

    async def see_other(request):
        raise web.HTTPSeeOther("/ok")

    async def loop(request):
        raise web.HTTPFound("/loop")

    async def big(request):
        return web.Response(body=b"x" * 10_000)

    async def big_chunked(request):
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(10):
            await resp.write(b"y" * 1000)
        await resp.write_eof()
        return resp

    async def slow(request):
        await asyncio.sleep(2)
        return web.Response(text="late")

    async def slow_redirect(request):
        await asyncio.sleep(0.25)
        left = int(request.query.get("n", "0"))
        if left <= 0:
            return web.Response(text="end")
        raise web.HTTPFound(f"/slow-redirect?n={left - 1}")

    @web.middleware
    async def count(request, handler):
        public_hits.append(request.path)
        return await handler(request)

    app = web.Application(middlewares=[count])
    app.router.add_route("*", "/ok", ok)
    app.router.add_get("/redirect", redirect)
    app.router.add_post("/see-other", see_other)
    app.router.add_get("/loop", loop)
    app.router.add_get("/big", big)
    app.router.add_get("/big-chunked", big_chunked)
    app.router.add_get("/slow", slow)
    app.router.add_get("/slow-redirect", slow_redirect)

    async def internal_any(request):
        internal_hits.append(request.path)
        return web.Response(text="SECRET-METADATA")

    internal_app = web.Application()
    internal_app.router.add_route("*", "/{tail:.*}", internal_any)

    public = TestServer(app, host="127.0.0.1")
    internal = TestServer(internal_app, host="127.0.0.2")
    await public.start_server()
    await internal.start_server()

    real_rule = safe_http.is_public_ip

    def rule(ip):  # 127.0.0.1 plays a public server; everything else keeps the real rule
        return str(ip).split("%", 1)[0] == "127.0.0.1" or real_rule(ip)

    monkeypatch.setattr(safe_http, "is_public_ip", rule)
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({
        "wp.test": "127.0.0.1", "other.test": "127.0.0.1", "metadata.test": "127.0.0.2",
        "mixed.test": ["127.0.0.1", "127.0.0.2"],
    }))
    try:
        yield _Servers(public, internal, internal_hits, public_hits)
    finally:
        await public.close()
        await internal.close()


@pytest.mark.asyncio
async def test_e2e_public_get(servers):
    resp = await safe_http.request("GET", servers.pub("wp.test", "/ok"), allowed_schemes=HTTP,
                                   headers={"Authorization": "Basic abc"})
    assert resp.status == 200 and resp.headers.get("Content-Type", "").startswith("application/json")
    assert '"auth": "Basic abc"' in resp.text()


@pytest.mark.asyncio
async def test_e2e_internal_name_is_refused_before_any_connection(servers):
    with capture_logs() as logs, pytest.raises(UnsafeURLError):
        await safe_http.request("GET", f"http://metadata.test:{servers.internal.port}/latest/meta-data/",
                                allowed_schemes=HTTP)
    assert servers.internal_hits == []
    assert any(e["event"] == "safe_http_blocked" and e["host"] == "metadata.test" for e in logs)


@pytest.mark.asyncio
async def test_e2e_redirect_to_internal_name_is_refused(servers):
    target = f"http://metadata.test:{servers.internal.port}/latest/meta-data/iam"
    with pytest.raises(UnsafeURLError):
        await safe_http.request("GET", servers.pub("wp.test", "/redirect"), params={"to": target},
                                allowed_schemes=HTTP)
    assert servers.public_hits == ["/redirect"] and servers.internal_hits == []


@pytest.mark.asyncio
async def test_e2e_redirect_to_internal_ip_literal_is_refused(servers):
    for target in (f"http://127.0.0.2:{servers.internal.port}/", "http://169.254.169.254/latest/meta-data/"):
        with capture_logs() as logs, pytest.raises(UnsafeURLError):
            await safe_http.request("GET", servers.pub("wp.test", "/redirect"), params={"to": target},
                                    allowed_schemes=HTTP)
        assert logs[-1]["stage"] == "redirect"
    assert servers.internal_hits == []


@pytest.mark.asyncio
async def test_e2e_redirect_scheme_outside_allowlist_is_refused(servers):
    with pytest.raises(UnsafeURLError, match="http://"):
        await safe_http.request("GET", servers.pub("wp.test", "/redirect"), params={"to": "file:///etc/passwd"},
                                allowed_schemes=HTTP)


@pytest.mark.asyncio
async def test_e2e_dns_rebinding_is_refused_at_connect(servers, monkeypatch):
    """First answer public (passes the save-time check), next answer internal: the connection uses
    the connect-time answer, which is checked again -> refused, the internal server never hit."""
    monkeypatch.setattr(safe_http, "_getaddrinfo", _dns({
        "rebind.test": lambda n: "127.0.0.1" if n == 1 else "127.0.0.2",
    }))
    url = f"http://rebind.test:{servers.internal.port}/latest/meta-data/"
    assert await safe_http.check_destination(url, allowed_schemes=HTTP) is None
    with pytest.raises(UnsafeURLError):
        await safe_http.request("GET", url, allowed_schemes=HTTP)
    assert servers.internal_hits == []


@pytest.mark.asyncio
async def test_e2e_multi_record_dns_with_one_internal_record_is_refused(servers):
    with pytest.raises(UnsafeURLError):
        await safe_http.request("GET", servers.pub("mixed.test", "/ok"), allowed_schemes=HTTP)
    assert servers.public_hits == [] and servers.internal_hits == []


@pytest.mark.asyncio
async def test_e2e_same_origin_redirect_followed_and_keeps_auth(servers):
    resp = await safe_http.request("GET", servers.pub("wp.test", "/redirect"), params={"to": "/ok"},
                                   allowed_schemes=HTTP, headers={"Authorization": "Basic abc"})
    assert resp.status == 200 and resp.history == (302,)
    assert '"auth": "Basic abc"' in resp.text()


@pytest.mark.asyncio
async def test_e2e_cross_origin_redirect_drops_credentials(servers):
    resp = await safe_http.request("GET", servers.pub("wp.test", "/redirect"),
                                   params={"to": servers.pub("other.test", "/ok")},
                                   allowed_schemes=HTTP, headers={"Authorization": "Basic abc"})
    assert resp.status == 200 and '"auth": null' in resp.text()


@pytest.mark.asyncio
async def test_e2e_303_turns_post_into_get(servers):
    resp = await safe_http.request("POST", servers.pub("wp.test", "/see-other"), json={"a": 1},
                                   allowed_schemes=HTTP)
    assert resp.status == 200 and '"method": "GET"' in resp.text()


@pytest.mark.asyncio
async def test_e2e_redirect_loop_is_capped(servers):
    with pytest.raises(TooManyRedirects):
        await safe_http.request("GET", servers.pub("wp.test", "/loop"), allowed_schemes=HTTP, max_redirects=3)
    assert servers.public_hits.count("/loop") == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/big", "/big-chunked"])
async def test_e2e_body_over_cap_is_refused(servers, path):
    with pytest.raises(ResponseTooLarge):
        await safe_http.request("GET", servers.pub("wp.test", path), allowed_schemes=HTTP, max_bytes=1000)
    ok = await safe_http.request("GET", servers.pub("wp.test", path), allowed_schemes=HTTP, max_bytes=20_000)
    assert len(ok.body) == 10_000


@pytest.mark.asyncio
async def test_e2e_timeout(servers):
    with pytest.raises(asyncio.TimeoutError):
        await safe_http.request("GET", servers.pub("wp.test", "/slow"), allowed_schemes=HTTP, timeout=0.3)


@pytest.mark.asyncio
async def test_e2e_timeout_is_one_deadline_across_redirect_hops(servers):
    """Each hop (0.25 s) is under the budget, the chain (4 x 0.25 s) is not: the whole exchange must
    stop at `timeout`, not at (max_redirects + 1) x timeout (API Gateway cuts at 29 s)."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await safe_http.request("GET", servers.pub("wp.test", "/slow-redirect"), params={"n": 3},
                                allowed_schemes=HTTP, timeout=0.6)
    assert loop.time() - started < 0.9
    assert len(servers.public_hits) <= 3


@pytest_asyncio.fixture
async def raw_listener():
    """A bare TCP listener on 127.0.0.1 counting accepted connections: proves "refused before any
    connection" even for a TLS client (no HTTP request would ever be logged by a web server)."""
    accepted = []

    async def on_conn(reader, writer):
        accepted.append(1)
        writer.close()

    server = await asyncio.start_server(on_conn, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, accepted
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", NONCANONICAL_NUMERIC_HOSTS[:6])
async def test_e2e_noncanonical_numeric_host_as_start_url_is_refused(host, raw_listener):
    port, accepted = raw_listener
    with pytest.raises(UnsafeURLError):
        await safe_http.request("GET", f"https://{host}:{port}/", timeout=1.0)
    await asyncio.sleep(0.05)
    assert accepted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", NONCANONICAL_NUMERIC_HOSTS[:6])
async def test_e2e_redirect_to_noncanonical_numeric_host_is_refused(host, servers, raw_listener):
    port, accepted = raw_listener
    with capture_logs() as logs, pytest.raises(UnsafeURLError):
        await safe_http.request("GET", servers.pub("wp.test", "/redirect"), params={"to": f"https://{host}:{port}/"},
                                allowed_schemes=("http", "https"), timeout=1.0)
    await asyncio.sleep(0.05)
    assert accepted == [] and servers.public_hits == ["/redirect"]
    assert logs[-1]["stage"] == "redirect" and logs[-1]["reason"] == "noncanonical_ip"


# ── guard: tenant-URL modules use safe_http only ──────────────────────────────

TENANT_URL_MODULES = [
    "api/routers/v1_integrations.py",
    "services/acp_s4_blog/cms/wordpress.py",
    "services/acp_publish/facebook.py",
]


@pytest.mark.parametrize("path", TENANT_URL_MODULES)
def test_tenant_url_modules_open_no_http_client_of_their_own(path):
    """A tenant-URL call that bypasses safe_http would skip the address check (AA-875)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / path).read_text()
    for forbidden in ("ClientSession(", "httpx.", "requests.", "urlopen(", "TCPConnector("):
        assert forbidden not in src, f"{path} uses {forbidden}; call shared.safe_http.request() instead"
    assert "safe_http.request(" in src
