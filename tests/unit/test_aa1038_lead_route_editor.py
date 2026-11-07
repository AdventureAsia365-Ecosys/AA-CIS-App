"""AA-1038 R5 slice 3 follow-up (S272) — the TripPlanner lead route editor of Tenant 360 refuses a webhook URL that
is not https or names a private / internal host when it is WRITTEN (services/tenant_account/tripplanner_site.py), as
TripPlanner's ``webhook_target_problem`` refuses it at send time. No DNS lookup in CIS. The database path is in
tests/integration/test_aa1038_lead_route_editor_pg.py.
"""
from __future__ import annotations

import ipaddress
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.staff_authz import POLICY, TENANT_WRITE
from services.tenant_account import tripplanner_site as tp

TID = "5b0e2c1a-7d3f-4e6a-9c8b-1f2e3d4c5b6a"

# TripPlanner's vectors (backend/tests/test_aa1038_lead_review.py: the IP literals of
# test_review_repro_a_webhook_route_on_a_non_public_address_refuses_before_storing and test_target_problem_codes)
TRIPPLANNER_NOT_PUBLIC = ["https://127.0.0.1/hook", "https://10.0.3.7:8443/leads", "https://169.254.169.254/x",
                          "https://[::1]/hook"]
TRIPPLANNER_NOT_HTTPS = ["http://hooks.example.invalid/l"]
PUBLIC = ["https://hooks.example.invalid/l", "https://larchfield-tours.example.com/hooks/tripplanner",
          "https://hooks.larchfield-tours.example.com./l",   # a fully qualified name with its root dot
          "https://93.184.215.14/leads", "https://api.wrenhollow.example.org:8443/v2/leads?src=tp"]

NOT_PUBLIC = TRIPPLANNER_NOT_PUBLIC + [
    # IPv4 private, loopback, link-local, CGNAT, multicast, unspecified, broadcast, reserved, documentation
    "https://192.168.1.20/h", "https://172.16.0.9/h", "https://172.31.255.254/h", "https://127.8.9.10/h",
    "https://169.254.10.1/h", "https://100.64.0.1/h", "https://100.127.255.254/h", "https://224.0.0.251/h",
    "https://239.1.2.3/h", "https://0.0.0.0/h", "https://255.255.255.255/h", "https://240.0.0.1/h",
    "https://192.0.2.10/h", "https://198.18.0.1/h",
    # the legacy IPv4 spellings a resolver library accepts
    "https://127.1/h", "https://2130706433/h", "https://0x7f.0.0.1/h", "https://0177.0.0.1/h", "https://0x7f000001/h",
    "https://10.1/h",
    # a trailing root dot: the resolver and URL parsers still read the IPv4 address
    "https://127.0.0.1./h", "https://127.0.0.1../h", "https://10.0.0.1./h", "https://10.0.0.1../h",
    "https://169.254.169.254./latest", "https://0x7f.1./h", "https://2130706433./h",
    # IPv6 loopback, unspecified, unique local, link-local (with a zone), multicast, documentation
    "https://[::]/h", "https://[fd12:3456:789a::1]/h", "https://[fc00::1]/h", "https://[fe80::1]/h",
    "https://[fe80::1%25eth0]/h", "https://[ff02::1]/h", "https://[2001:db8::1]/h",
    # IPv6 carrying a non-public IPv4 address: mapped, NAT64, 6to4, IPv4-compatible
    "https://[::ffff:127.0.0.1]/h", "https://[::ffff:10.0.0.1]/h", "https://[::ffff:a9fe:a9fe]/h",
    "https://[64:ff9b::10.0.0.1]/h", "https://[2002:a00:1::1]/h", "https://[::10.0.0.1]/h",
    # plainly internal host names
    "https://localhost/h", "https://localhost:8443/h", "https://LOCALHOST./h", "https://api.localhost/h",
    "https://printer.local/h", "https://crm.corp.internal/h", "https://metadata.google.internal/h",
    "https://ip-10-0-1-2.us-west-1.compute.internal/h", "https://box.localdomain/h", "https://nas.home.arpa/h",
    "https://router.lan/h", "https://intranet/h", "https://hooks/h",
]


@pytest.mark.parametrize("url", TRIPPLANNER_NOT_PUBLIC)
def test_tripplanner_vectors_are_not_public(url):
    assert tp.webhook_url_problem(url) == tp.URL_NOT_PUBLIC


@pytest.mark.parametrize("url", TRIPPLANNER_NOT_HTTPS + ["HTTPS://hooks.example.com/l", "ftp://hooks.example.com/l",
                                                         "//hooks.example.com/l", "hooks.example.com/l"])
def test_not_https(url):
    assert tp.webhook_url_problem(url) == tp.URL_NOT_HTTPS


@pytest.mark.parametrize("url", NOT_PUBLIC)
def test_private_and_internal_targets_are_refused(url):
    assert tp.webhook_url_problem(url) == tp.URL_NOT_PUBLIC


@pytest.mark.parametrize("url", PUBLIC)
def test_public_targets_pass(url):
    assert tp.webhook_url_problem(url) is None


@pytest.mark.parametrize("url", [
    "https://", "https:///path", "https://hooks.example.com:99999/l", "https://hooks.example.com:x/l",
    "https://user@hooks.example.com/l", "https://user:pw@hooks.example.com/l", "https://hooks.example.com/a b",
    "https://hooks.example.com/l\n", "https://hooks_x.example.com/l", "https://hööks.example.com/l",
    "https://[2606:4700:4700::1111]/h",   # a public IPv6 literal: the CIS 390 CHECK takes no bracketed host
    "https://999.1.1.1/h", "https://hooks.example.123/h", "https://hooks.example.123./h", "https://./h",
    "https://../h", "https://[::1]./h",
])
def test_malformed_targets_are_invalid(url):
    assert tp.webhook_url_problem(url) == tp.URL_INVALID


def test_userinfo_cannot_hide_a_private_host():
    # urllib connects to the host after the '@': judged as that host
    assert tp.webhook_url_problem("https://hooks.example.com@127.0.0.1/l") == tp.URL_NOT_PUBLIC


def test_length_is_checked_first():
    url = "https://hooks.example.com/" + "a" * 480
    assert tp.webhook_url_problem(url) == tp.URL_TOO_LONG
    assert tp.webhook_url_problem(url[:500]) is None


def test_never_resolves_dns(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no DNS lookup in CIS")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    for url in PUBLIC + NOT_PUBLIC:
        tp.webhook_url_problem(url)


def test_every_accepted_url_satisfies_the_390_check_shape():
    import re

    check = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s]*)?$")  # migration 390 lead_webhook_chk
    for url in PUBLIC:
        assert check.match(url) and len(url) <= 500


def test_ip_is_public_matches_tripplanner_rule_on_ipv4():
    # TripPlanner: `not ip.is_global or ip.is_multicast` (after unwrapping IPv4-mapped) refuses
    for raw in ("8.8.8.8", "93.184.215.14", "10.0.0.1", "100.64.1.1", "224.0.0.1", "0.0.0.0", "203.0.113.7"):
        ip = ipaddress.ip_address(raw)
        assert tp.ip_is_public(ip) is (ip.is_global and not ip.is_multicast)
    assert tp.ip_is_public(ipaddress.ip_address("::ffff:8.8.8.8")) is True


@pytest.mark.parametrize("email,ok", [
    ("desk@larchfield-tours.example.com", True), ("a.b+tp@wrenhollow.example.org", True),
    ("no-at-sign.example.com", False), ("two@@example.com", False), ("x@example", False),
    ("x@exa mple.com", False), ("x" * 65 + "@example.com", False), ("x@" + "a" * 250 + ".com", False),
    ("desk@example.com\n", False),
])
def test_email_shape(email, ok):
    assert (tp.email_problem(email) is None) is ok


def test_validate_route_rules():
    assert tp.validate_route("none", None, None) == ("none", None, None)
    assert tp.validate_route("webhook", "  https://hooks.example.com/l ", "") == \
        ("webhook", "https://hooks.example.com/l", None)
    assert tp.validate_route("email", None, " desk@example.com ") == ("email", None, "desk@example.com")
    cases = [
        (("webhook", None, None), tp.URL_REQUIRED, "lead_webhook_url"),
        (("webhook", "   ", None), tp.URL_REQUIRED, "lead_webhook_url"),
        (("email", "https://hooks.example.com/l", None), tp.EMAIL_REQUIRED, "lead_email"),
        (("bogus", None, None), "lead_channel_invalid", "lead_channel"),
        # a stored-but-unused target is checked too: it goes live when the channel is switched
        (("email", "https://10.0.0.5/h", "desk@example.com"), tp.URL_NOT_PUBLIC, "lead_webhook_url"),
        (("none", None, "not-an-address"), tp.EMAIL_INVALID, "lead_email"),
        (("webhook", 42, None), tp.URL_INVALID, "lead_webhook_url"),
    ]
    for args, code, field in cases:
        with pytest.raises(tp.LeadRouteError) as e:
            tp.validate_route(*args)
        assert (e.value.code, e.value.field, e.value.status) == (code, field, 422)
        assert e.value.message


# ── the service against a fake connection ──────────────────────────────────────────────────────────────────────

class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, status="active", row=None):
        self.status = status
        self.row = row
        self.writes: list[tuple] = []

    def transaction(self):
        return _Tx()

    async def execute(self, sql, *args):
        if "INSERT INTO shared.tenant_tripplanner_site" in sql:
            self.writes.append(args)
            self.row = {"lead_channel": args[1], "lead_webhook_url": args[2], "lead_email": args[3],
                        "updated_at": None, "updated_by": args[4]}
        elif "INSERT INTO acp_shared.audit_log" in sql:
            self.writes.append(("audit",) + args)

    async def fetchval(self, sql, *args):
        if "shared.tenants" in sql:
            return self.status
        if "tenant_subscription" in sql:
            return True
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        return self.row


def test_set_lead_route_refuses_before_any_write(monkeypatch):
    audit = AsyncMock()
    monkeypatch.setattr(tp, "write_audit_log", audit)
    conn = _Conn()
    import asyncio

    with pytest.raises(tp.LeadRouteError) as e:
        asyncio.run(tp.set_lead_route(conn, TID, channel="webhook", webhook_url="https://192.168.0.7/h", email=None,
                                      actor="admin:u1"))
    assert e.value.code == tp.URL_NOT_PUBLIC and conn.writes == [] and not audit.await_count


def test_set_lead_route_stores_and_audits(monkeypatch):
    import asyncio

    audit = AsyncMock()
    monkeypatch.setattr(tp, "write_audit_log", audit)
    conn = _Conn()
    out = asyncio.run(tp.set_lead_route(conn, TID, channel="webhook", webhook_url="https://hooks.example.com/l",
                                        email=None, actor="admin:u1"))
    assert out["lead_channel"] == "webhook" and out["has_tripplanner"] is True and out["site"] is True
    assert conn.writes[0] == (TID, "webhook", "https://hooks.example.com/l", None, "admin:u1")
    details = audit.await_args.kwargs["details"]
    assert details["before"] == {"lead_channel": "none", "lead_webhook_url": None, "lead_email": None}
    assert details["after"]["lead_webhook_url"] == "https://hooks.example.com/l"
    assert audit.await_args.kwargs["action"] == "tenant.tripplanner_lead_route_changed"


@pytest.mark.parametrize("status,code,http", [(None, "tenant_not_found", 404), ("offboarding", "tenant_offboarded", 409)])
def test_set_lead_route_tenant_refusals(monkeypatch, status, code, http):
    import asyncio

    monkeypatch.setattr(tp, "write_audit_log", AsyncMock())
    with pytest.raises(tp.LeadRouteError) as e:
        asyncio.run(tp.set_lead_route(_Conn(status=status), TID, channel="none", webhook_url=None, email=None,
                                      actor="admin:u1"))
    assert (e.value.code, e.value.status) == (code, http)


# ── the admin routes ────────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "test-admin-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _h(role="admin"):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "u-admin"}
    if role is not None:
        h["x-admin-role"] = role
    return h


PATH = f"/admin/tenants/{TID}/tripplanner-site/lead-route"


def test_policy_entries():
    assert POLICY[("PUT", "/admin/tenants/{tenant_id}/tripplanner-site/lead-route")] == TENANT_WRITE
    assert POLICY[("GET", "/admin/tenants/{tenant_id}/tripplanner-site/lead-route")] == \
        POLICY[("GET", "/admin/tenants/{tenant_id}/subscription")]


@pytest.mark.parametrize("url", TRIPPLANNER_NOT_PUBLIC + ["https://100.64.0.1/h", "https://[::ffff:10.0.0.1]/h",
                                                          "https://printer.local/h", "https://crm.corp.internal/h",
                                                          "https://localhost/h"])
def test_put_refuses_a_non_public_webhook_with_a_field_error(client, monkeypatch, url):
    stored = AsyncMock()
    monkeypatch.setattr(tp, "write_audit_log", stored)
    resp = client.put(PATH, headers=_h(), json={"lead_channel": "webhook", "lead_webhook_url": url})
    assert resp.status_code == 422
    body = resp.json()["detail"]
    assert body["detail"] == tp.URL_NOT_PUBLIC and body["field"] == "lead_webhook_url" and "private" in body["message"]
    assert resp.headers["X-Error-Code"] == tp.URL_NOT_PUBLIC and not stored.await_count


def test_put_refuses_http(client):
    resp = client.put(PATH, headers=_h(), json={"lead_channel": "webhook",
                                                "lead_webhook_url": "http://hooks.example.invalid/l"})
    assert resp.status_code == 422 and resp.json()["detail"]["detail"] == tp.URL_NOT_HTTPS
    assert resp.json()["detail"]["field"] == "lead_webhook_url"


def test_put_passes_a_public_route_to_the_service(client, monkeypatch):
    setter = AsyncMock(return_value={"tenant_id": TID, "lead_channel": "webhook"})
    monkeypatch.setattr(tp, "set_lead_route", setter)
    resp = client.put(PATH, headers=_h(), json={"lead_channel": "webhook",
                                                "lead_webhook_url": "https://hooks.example.com/l"})
    assert resp.status_code == 200 and setter.await_args.kwargs["actor"] == "admin:u-admin"
    assert setter.await_args.kwargs["webhook_url"] == "https://hooks.example.com/l"


@pytest.mark.parametrize("role", ["content", "customer_success", None])
def test_put_is_admin_only(client, role):
    assert client.put(PATH, headers=_h(role), json={"lead_channel": "none"}).status_code == 403


def test_put_refuses_an_unknown_channel(client):
    assert client.put(PATH, headers=_h(), json={"lead_channel": "crm"}).status_code == 422


def test_get(client, monkeypatch):
    monkeypatch.setattr(tp, "lead_route", AsyncMock(return_value=None))
    assert client.get(PATH, headers=_h()).status_code == 404
    monkeypatch.setattr(tp, "lead_route", AsyncMock(return_value={"tenant_id": TID, "lead_channel": "none"}))
    assert client.get(PATH, headers=_h()).json()["lead_channel"] == "none"
