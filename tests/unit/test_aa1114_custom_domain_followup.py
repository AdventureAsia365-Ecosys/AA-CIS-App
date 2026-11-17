"""AA-1114 — the TripPlanner custom-domain follow-up, no database.

- The AA-subject gate (services/acp_shared/subject_gate.py) allows a sales link to the tenant's OWN verified
  TripPlanner host (exact host; never a sub-domain of it, never another tenant's host); load_targets reads the hosts
  of that tenant only, and not at all when the piece has no link.
- The two CodeQL py/polynomial-redos patterns are gone: the custom-domain shape (tripplanner_domain.domain_shape_ok,
  alert #62) and the legacy IPv4 spelling (tripplanner_site.is_legacy_ipv4_spelling, alert #60) are linear passes
  that agree with the former regexes on a fixed corpus and answer a long adversarial string at once.

The add_domain entitlement refusal (409 custom_domain_not_included) is in test_aa1090_custom_domain.py
(test_add_refusals) and on Postgres in tests/integration/test_aa1114_custom_domain_followup_pg.py.
Invented hosts only (example.invalid, example.test).
"""
from __future__ import annotations

import asyncio
import itertools
import re
import time

import pytest

from services.acp_shared import subject_gate as sg
from services.tenant_account import tripplanner_domain as tpd
from services.tenant_account import tripplanner_site as tps

TID = "11140000-0000-4000-8000-000000000001"
OWN = "trip.quillmere.example.invalid"
OTHER = "go.larchfield.example.invalid"

# The former patterns, kept here only as the reference the linear checks must agree with.
OLD_DOMAIN_SHAPE = re.compile(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
OLD_LEGACY_IPV4 = re.compile(r"(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+)){0,3}")


# ── the subject gate: the tenant's own verified host ────────────────────────────────────────────────────────────

def _targets(own=frozenset({OWN})):
    return sg.AllowedTargets(aa_hosts=frozenset({"adventure.asia"}), trip_page_hosts=frozenset(),
                             tripplanner_hosts=frozenset({"tripplanner.lumiguides.it.com"}),
                             tenant_tripplanner_hosts=own)


@pytest.mark.parametrize("url,allowed", [
    (f"https://{OWN}/plan", True),
    (f"https://{OWN.upper()}/", True),
    (f"https://www.{OWN}/plan", False),                 # the www. twin was not verified: exact host as stored
    (f"https://{OWN}./plan", True),                     # a trailing root dot is the same host
    (f"https://evil.{OWN}/book", False),                # a sub-domain of the agency host is not the planner
    (f"https://{OTHER}/plan", False),                   # another tenant's host
    ("https://quillmere.example.invalid/book", False),  # the agency's apex is not its planner host
])
def test_own_verified_host_is_an_exact_sales_target(url, allowed):
    assert sg.allowed_sales_target(url, _targets()) is allowed


WWW_OWN = "www.marrowgate.example.invalid"


@pytest.mark.parametrize("url,allowed", [
    (f"https://{WWW_OWN}/book", True),                   # the stored www. host itself
    (f"https://{WWW_OWN.upper()}./book", True),          # case and the trailing root dot do not matter
    ("https://marrowgate.example.invalid/book", False),  # the apex (the agency's own shop) was never verified
    ("https://shop.marrowgate.example.invalid/book", False),
])
def test_a_stored_www_host_never_allows_the_apex(url, allowed):
    assert sg.allowed_sales_target(url, _targets(frozenset({WWW_OWN}))) is allowed


def test_a_booking_link_to_the_own_host_passes_and_the_others_are_held():
    ok = sg.evaluate(f"[Book your trip](https://{OWN}/plan)", goal="conversion", cta=None, subject_ok=True,
                     targets=_targets())
    assert ok.ok, ok.detail
    held = sg.evaluate(f"[Book your trip](https://{OTHER}/plan)", goal="conversion", cta=None, subject_ok=True,
                       targets=_targets())
    assert not held.ok and held.codes == [sg.OFF_CATALOG_LINK]
    # without the host in the tenant's set (pending, failed, removed) the same link is held
    held = sg.evaluate(f"[Book your trip](https://{OWN}/plan)", goal="conversion", cta=None, subject_ok=True,
                       targets=_targets(frozenset()))
    assert not held.ok and held.codes == [sg.OFF_CATALOG_LINK]


class FakeConn:
    """Answers the three reads of load_targets; records which tenant the host read asked for."""

    def __init__(self, hosts):
        self.hosts = hosts
        self.host_reads: list[tuple] = []

    async def fetch(self, sql, *args):
        if sql == sg.TENANT_PAGES_SQL:
            return []
        if sql == sg.TENANT_TRIPPLANNER_HOSTS_SQL:
            self.host_reads.append(args)
            return [{"domain": h} for h in self.hosts]
        raise AssertionError(sql)


@pytest.fixture
def no_served(monkeypatch):
    async def served_states(conn, ids):
        return {}
    monkeypatch.setattr(sg._tl, "served_states", served_states)
    monkeypatch.delenv("TRIPPLANNER_SITE_HOSTS", raising=False)
    monkeypatch.delenv("TRIPPLANNER_REVALIDATE_URL", raising=False)


def test_load_targets_reads_the_tenants_active_hosts(no_served):
    conn = FakeConn([OWN, "www.plan.quillmere.example.invalid", "Go.Quillmere.Example.Invalid."])
    links = sg.extract_links(f"Book: https://{OWN}/plan")
    targets = asyncio.run(sg.load_targets(conn, TID, links))
    assert conn.host_reads == [(TID,)]
    # stored as verified: lower-case, no trailing dot, a leading www. kept
    assert targets.tenant_tripplanner_hosts == {OWN, "www.plan.quillmere.example.invalid",
                                                "go.quillmere.example.invalid"}
    assert {"tripplanner.lumiguides.it.com"} <= set(targets.tripplanner_hosts)  # set membership, not a URL check


def test_the_query_is_this_tenants_active_rows_only():
    sql = " ".join(sg.TENANT_TRIPPLANNER_HOSTS_SQL.split())
    assert "FROM shared.tenant_tripplanner_domain d" in sql
    assert "WHERE d.tenant_id = $1::uuid AND d.status = 'active'" in sql


def test_no_link_no_host_read(no_served):
    conn = FakeConn([OWN])
    targets = asyncio.run(sg.load_targets(conn, TID, []))
    assert conn.host_reads == [] and targets.tenant_tripplanner_hosts == frozenset()


def test_a_malformed_tenant_id_allows_no_own_host(no_served):
    conn = FakeConn([OWN])
    assert asyncio.run(sg.tenant_tripplanner_hosts(conn, "not-a-uuid")) == frozenset()
    assert conn.host_reads == []


# ── CodeQL #62: the custom-domain shape ─────────────────────────────────────────────────────────────────────────

def _corpus(alphabet: str, max_len: int):
    for n in range(0, max_len + 1):
        for chars in itertools.product(alphabet, repeat=n):
            yield "".join(chars)


SHAPE_CASES = [
    OWN, "plan.larchfield-tours.example.org", "a.b", "a.b1", "a.1b", "1.b", "a-.b", "a.-b", "a..b", ".a.b", "a.b.",
    "trip_x.example.invalid", "-trip.example.invalid", "a" * 63 + ".example", "a" * 64 + ".example",
    "x." * 126 + "invalid", "a.b-c", "a.b-", "xn--bcher-kva.example", "A.b", "a.b\n", "a.b c", "é.example",
]


@pytest.mark.parametrize("host", SHAPE_CASES)
def test_domain_shape_agrees_with_the_428_check(host):
    assert tpd.domain_shape_ok(host) is bool(OLD_DOMAIN_SHAPE.fullmatch(host))


def test_domain_shape_agrees_on_every_short_string():
    for host in _corpus("a0-.", 6):
        assert tpd.domain_shape_ok(host) is bool(OLD_DOMAIN_SHAPE.fullmatch(host)), host


def test_domain_problem_answers_a_long_adversarial_host_at_once():
    started = time.perf_counter()
    for host in ("0." * 50_000 + "!", "a" * 100_000, "a-" * 50_000 + "."):
        assert tpd.domain_problem(host) == tpd.DOMAIN_INVALID
        assert tpd.domain_shape_ok(host) is False
    assert time.perf_counter() - started < 1.0


# ── CodeQL #60: the legacy IPv4 spelling ────────────────────────────────────────────────────────────────────────

LEGACY_CASES = [
    "127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "0x7f000001", "0x", "0x.0x.0x.0x", "1.2.3.4", "1.2.3.4.5",
    "", ".", "1.", ".1", "1..2", "0X7f.0.0.1", "0xg", "a.b", "999.1.1.1", "12a", "١٢٣",
]


@pytest.mark.parametrize("host", LEGACY_CASES)
def test_legacy_ipv4_spelling_agrees_with_the_former_pattern(host):
    assert tps.is_legacy_ipv4_spelling(host) is bool(OLD_LEGACY_IPV4.fullmatch(host))


def test_legacy_ipv4_spelling_agrees_on_every_short_string():
    for host in _corpus("0x1f.", 6):
        assert tps.is_legacy_ipv4_spelling(host) is bool(OLD_LEGACY_IPV4.fullmatch(host)), host


def test_legacy_ipv4_spelling_answers_a_long_adversarial_host_at_once():
    started = time.perf_counter()
    for host in ("0" * 100_000 + "!", "0." * 50_000, "0x" + "0" * 100_000 + "g"):
        assert tps.is_legacy_ipv4_spelling(host) is False
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("url,code", [
    ("https://127.1/h", tps.URL_NOT_PUBLIC),
    ("https://0x7f.0.0.1/h", tps.URL_NOT_PUBLIC),
    ("https://999.1.1.1/h", tps.URL_INVALID),
    ("https://hooks.quillmere.example.invalid/lead", None),
])
def test_webhook_check_still_reads_legacy_spellings(url, code):
    assert tps.webhook_url_problem(url) == code
