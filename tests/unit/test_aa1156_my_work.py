"""AA-1156 — My work (GET /admin/my-work/summary and the /admin/my-work page rules). AA-1190: approval, platform and
watch tiles, the bands, the landing page of every role with a tile (admin included).

Covers: the route's capability gate (403 for a role with none of the tile capabilities and for the machine), the tiles
each role gets (shown by capability, never by person), a tile whose table is missing (available false, the page still
answers), and that the frontend (lib/myWork.ts, lib/rbac.ts) names the same tiles, capabilities and roles. The counts
against the target pages' own list queries run on Postgres: tests/integration/test_aa1156_my_work_counts.py.
No network, no AWS, no DB.
"""
from __future__ import annotations

import dataclasses
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core.staff_authz import ROLE_PERMS
from api.routers import admin, admin_my_work

REPO = Path(__file__).resolve().parents[2]
SECRET = "aa1156-unit-secret"
OLDEST = datetime(2026, 11, 2, 8, 30, tzinfo=timezone.utc)

CONTENT_KEYS = ["supply_drafts", "supply_no_description", "aa_content_drafts"]
ALL_KEYS = ["settings_to_approve", "compliance_exceptions_to_approve", "erasure_to_approve", "review_waiting",
            *CONTENT_KEYS, "compliance_holds", "ops_open_problems", "review_held_by_policy", "place_cards_to_review"]
EXPECTED = {
    "admin": ALL_KEYS,
    "content_lead": ["review_waiting", *CONTENT_KEYS, "compliance_holds", "review_held_by_policy",
                     "place_cards_to_review"],
    "content": ["review_waiting", *CONTENT_KEYS, "review_held_by_policy"],
    "tenant_qa": ["compliance_holds", "place_cards_to_review"],
}
REFUSED = ["viewer", "ai_ops", "finance", "support", "customer_success"]


class _Pool:
    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield object()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    calls: list[str] = []

    def counter(key, n):
        async def count(_conn, viewer):
            calls.append(key)
            seen.append(viewer)
            return {"count": n, "oldest_at": OLDEST}
        return count

    seen: list = []
    tiles = tuple(dataclasses.replace(t, count=counter(t.key, i + 1)) for i, t in enumerate(admin_my_work.TILES))
    monkeypatch.setattr(admin_my_work, "TILES", tiles)
    owner = {"value": False}

    async def is_owner(_request, _conn):
        return owner["value"]

    monkeypatch.setattr(admin_my_work, "_is_business_owner", is_owner)
    app = FastAPI()
    app.include_router(admin_my_work.router)
    app.state.pool = _Pool()
    c = TestClient(app)
    c.calls = calls
    c.seen = seen
    c.owner = owner
    return c


def _get(client, role=None):
    headers = {"x-admin-secret": SECRET}
    if role:
        headers["x-admin-role"] = role
    return client.get("/admin/my-work/summary", headers=headers)


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_each_role_gets_the_tiles_of_its_capabilities_in_order(client, role):
    r = _get(client, role)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == role
    assert [t["key"] for t in body["tiles"]] == EXPECTED[role]
    assert client.calls == EXPECTED[role], "a tile the role may not work is never counted"
    first = body["tiles"][0]
    assert first["available"] is True and isinstance(first["count"], int)
    assert first["oldest_at"].startswith("2026-11-02T08:30")


@pytest.mark.parametrize("role", REFUSED)
def test_a_role_with_no_tile_capability_is_refused(client, role):
    r = _get(client, role)
    assert r.status_code == 403
    assert r.json()["detail"].startswith(f"Role '{role}' lacks")
    assert client.calls == []


def test_the_machine_and_a_wrong_secret_are_refused(client):
    assert _get(client).status_code == 403
    assert client.get("/admin/my-work/summary", headers={"x-admin-secret": "nope", "x-admin-role": "admin"}
                      ).status_code == 403


def test_the_filters_are_the_target_pages_filters(client):
    tiles = {t["key"]: t for t in _get(client, "admin").json()["tiles"]}
    assert tiles["settings_to_approve"]["filter"] == {"pending": "1"}           # (the real counter adds the app tab)
    assert tiles["compliance_exceptions_to_approve"]["filter"] == {"tab": "exceptions"}
    assert tiles["erasure_to_approve"]["filter"] == {"tab": "offboarding"}
    assert tiles["review_waiting"]["filter"] == {"policy": "none"}          # ready: without the policy reviews
    assert tiles["review_held_by_policy"]["filter"] == {"policy": "elephant_riding"}
    assert tiles["supply_drafts"]["filter"] == {"has_draft": "1"}    # by open draft, not by entity status
    assert tiles["supply_no_description"]["filter"] == {"status": "draft"}
    assert tiles["aa_content_drafts"]["filter"] == {"status": "draft"}
    assert tiles["place_cards_to_review"]["filter"] == {"status": "auto_checked"}
    assert tiles["compliance_holds"]["filter"] == {}             # Compliance opens on open flags
    assert tiles["ops_open_problems"]["filter"] == {}            # Overview


def test_bands_and_the_total_flag(client):
    tiles = {t["key"]: t for t in _get(client, "admin").json()["tiles"]}
    groups = {k: t["group"] for k, t in tiles.items()}
    assert {k for k, g in groups.items() if g == "approve"} == {
        "settings_to_approve", "compliance_exceptions_to_approve", "erasure_to_approve", "review_waiting"}
    assert {k for k, g in groups.items() if g == "watch"} == {"review_held_by_policy", "place_cards_to_review"}
    assert groups["ops_open_problems"] == "platform"
    # backlog and policy decisions are shown, never added to the total
    assert {k for k, t in tiles.items() if not t["counts_toward_total"]} == {
        "review_held_by_policy", "place_cards_to_review"}
    assert set(admin_my_work.GROUPS) == set(groups.values())


def test_the_business_owner_gets_no_platform_tile(client):
    client.owner["value"] = True
    keys = [t["key"] for t in _get(client, "admin").json()["tiles"]]
    assert "ops_open_problems" not in keys and "settings_to_approve" in keys


def test_a_custom_tenant_write_role_gets_no_erasure_tile():
    """Approving an erasure needs the administrator role (admin_tenant_erasure.approve), not only tenant.write."""
    perms = frozenset({"tenant.write", "ops.read"})
    assert [t.key for t in admin_my_work.tiles_for(perms, "custom_desk")] == []
    assert [t.key for t in admin_my_work.tiles_for(perms, "admin")] == ["erasure_to_approve"]


def test_a_counter_may_answer_its_own_filter(client, monkeypatch):
    async def settings_counter(_conn, _viewer):
        return {"count": 2, "oldest_at": OLDEST, "filter": {"pending": "1", "app": "tripplanner"}}

    tiles = tuple(dataclasses.replace(t, count=settings_counter) if t.key == "settings_to_approve" else t
                  for t in admin_my_work.TILES)
    monkeypatch.setattr(admin_my_work, "TILES", tiles)
    by = {t["key"]: t for t in _get(client, "admin").json()["tiles"]}
    assert by["settings_to_approve"]["filter"] == {"pending": "1", "app": "tripplanner"}
    assert by["settings_to_approve"]["count"] == 2


def test_an_ops_read_role_reaches_the_route_with_no_tile(monkeypatch):
    """AA-1190: a custom role with ops.read lands on My work: the route answers it (empty), never 403."""
    from api.core.staff_authz import POLICY, policy_capabilities
    caps = policy_capabilities(POLICY[("GET", "/admin/my-work/summary")])
    assert set(caps) == {*admin_my_work.CAPABILITIES, "ops.read"} == set(admin_my_work.ROUTE_CAPABILITIES)
    assert admin_my_work.tiles_for(frozenset({"ops.read"}), "custom_ops") == []


def test_a_missing_table_marks_only_that_tile_unavailable(client, monkeypatch):
    async def missing(_conn, _viewer):
        raise asyncpg.UndefinedTableError("relation does not exist")

    tiles = tuple(t if t.key != "compliance_holds" else dataclasses.replace(t, count=missing)
                  for t in admin_my_work.TILES)
    monkeypatch.setattr(admin_my_work, "TILES", tiles)
    body = _get(client, "tenant_qa").json()
    by = {t["key"]: t for t in body["tiles"]}
    assert by["compliance_holds"]["available"] is False and by["compliance_holds"]["count"] is None
    assert by["place_cards_to_review"]["available"] is True


def test_the_real_tiles_use_the_target_pages_count_helpers():
    """Each real tile counts through the helper that sits next to its page's list query (same WHERE text)."""
    from api.routers import admin_tenant_compliance as atc
    from services.aa_content import store
    # the supply list and its summary both read LIST_FILTER_SQL (the list query and the summary are f-strings)
    src = (REPO / "services" / "supply_content" / "review.py").read_text()
    assert src.count("WHERE {LIST_FILTER_SQL}") == 2
    assert "{OPEN_DRAFT_SQL}" in src.split("LIST_FILTER_SQL = ", 1)[1].split("\n\n", 1)[0]
    assert store._LIST_FROM in store.SUMMARY_SQL and store._LIST_FROM in store.COUNT_SQL
    assert atc.FILTER_SQL in atc.COUNT_SQL and atc.FILTER_SQL in atc.SUMMARY_SQL and atc.FILTER_SQL in atc.LIST_SQL
    cq = (REPO / "api" / "routers" / "admin_content_quality.py").read_text()
    assert "place_card_list.list_filter(status, q)" in cq
    rq = (REPO / "api" / "routers" / "admin_pipeline" / "review.py").read_text()
    assert rq.count("_review_queue_where(") == 3   # the definition, the page, the tile
    # AA-1190: the approval tiles count with the WHERE text of their pages' lists
    from api.services import compliance_exceptions as ce
    from api.services import settings_registry as reg
    from api.services.ops_status import store as ops_store
    assert reg.OPEN_WHERE in reg._OPEN_SQL and reg.OPEN_WHERE in reg.OPEN_SUMMARY_SQL
    assert "status = 'proposed'" in ce.PROPOSED_SUMMARY_SQL
    assert "ended_at IS NULL" in ops_store.OPEN_PROBLEMS_SQL


def test_the_supply_list_route_reads_the_drafts_tile_filter(monkeypatch):
    """The drafts tile sends ?has_draft=1: the list route turns it into has_draft=True for the shared WHERE."""
    from api.routers import admin_supply_content
    from services.supply_content import review

    seen: list[dict] = []

    async def fake_list(_pool, **kw):
        seen.append(kw)
        return {"items": [], "total": 0, "by_status": {}}

    monkeypatch.setattr(review, "list_entities", fake_list)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    app = FastAPI()
    app.include_router(admin_supply_content.router)
    app.state.pool = _Pool()
    c = TestClient(app)
    headers = {"x-admin-secret": SECRET, "x-admin-role": "content"}
    for query, want in (("?has_draft=1", True), ("?has_draft=0", False), ("", None)):
        r = c.get(f"/admin/supply-content{query}", headers=headers)
        assert r.status_code == 200, r.text
        assert seen[-1]["has_draft"] is want, query
    assert c.get("/admin/supply-content?has_draft=maybe", headers=headers).status_code == 422


def _ts_tiles() -> dict[str, str]:
    src = (REPO / "frontend" / "lib" / "myWork.ts").read_text()
    block = re.search(r"export const MY_WORK_TILES[^=]*=\s*\[(.*?)\n\];", src, re.S)
    assert block, "MY_WORK_TILES not found in frontend/lib/myWork.ts"
    return dict(re.findall(r'key:\s*"([a-z_]+)",\s*capability:\s*"([a-z_.]+)"', block.group(1)))


def test_the_frontend_lists_the_same_tiles_and_capabilities():
    assert _ts_tiles() == {t.key: t.capability for t in admin_my_work.TILES}
    assert list(_ts_tiles()) == [t.key for t in admin_my_work.TILES]


def test_the_page_rule_opens_my_work_to_exactly_the_roles_with_a_tile():
    rbac = (REPO / "frontend" / "lib" / "rbac.ts").read_text()
    roles = re.search(r"const MY_WORK: StaffRole\[\] = \[([^\]]*)\]", rbac)
    assert roles, "MY_WORK roles not found in frontend/lib/rbac.ts"
    page_roles = set(re.findall(r'"([a-z_]+)"', roles.group(1)))
    route = set(admin_my_work.ROUTE_CAPABILITIES)
    with_tile = {r for r, perms in ROLE_PERMS.items() if admin_my_work.tiles_for(perms, r)}
    with_route = {r for r, perms in ROLE_PERMS.items() if route & set(perms)}
    assert page_roles == with_tile == with_route == set(EXPECTED)
    # AA-1155: the rule is decided by the capabilities; MY_WORK_CAPS = the backend route's any_of
    assert '{ prefix: "/admin/my-work", caps: MY_WORK_CAPS, roles: MY_WORK }' in rbac
    caps = re.search(r"const MY_WORK_CAPS: Capability\[\] = \[([^\]]*)\]", rbac)
    assert caps and set(re.findall(r'"([a-z_.]+)"', caps.group(1))) == route


def test_every_role_with_a_tile_lands_on_my_work():
    """AA-1190 (S295 D1): admin lands on My work like the content roles; the viewer keeps the Review Queue."""
    rbac = (REPO / "frontend" / "lib" / "rbac.ts").read_text()
    body = re.search(r"export function landingPage\([^)]*\): string \{(.*?)\n\}", rbac, re.S).group(1)
    assert '"/admin/overview"' not in body
    assert "MY_WORK_LANDING" in body
    landing = set(re.findall(r'"([a-z_]+)"', re.search(r"const MY_WORK_LANDING[^=]*=\s*\[([^\]]*)\]", rbac).group(1)))
    assert landing == set(EXPECTED)


# ── AA-1190 review fix: the settings tile leaves out exactly the own proposals the approve route refuses ──

_ME = "admin:00000000-0000-0000-0000-0000000000c1"
_OTHER = "admin:00000000-0000-0000-0000-0000000000c2"
_SINGLE = "cis.compliance.applicability_review_max_age_days"   # approval "single" (compiled defaults)
_CHECKED = "cis.a1.attempts_before_review"                     # approval "maker_checker"
_PUBLISH = "cis.content.publish_two_person"                    # maker_checker, also guarded by its own switch


class _Rows:
    """A connection whose fetch answers the open proposals (the summary's one read)."""

    def __init__(self, rows):
        self.rows = rows
        self.fetches = 0

    async def fetch(self, _sql, *_args):
        self.fetches += 1
        return self.rows


def _open(key: str, by: str, minute: int) -> dict:
    return {"key": key, "proposed_by": by, "proposed_at": datetime(2026, 11, 20, 9, minute, tzinfo=timezone.utc)}


@pytest.mark.parametrize(
    ("two_person", "publish_switch", "expected"),
    [
        # two-person on: own maker_checker proposals (the checked key, the publish switch) out; own single key in
        (True, False, {_SINGLE}),
        # both off: every own proposal is a recorded self-approval, so all count
        (False, False, {_SINGLE, _CHECKED, _PUBLISH}),
        # two-person off, publish switch on: only the publish switch's own proposal is refused
        (False, True, {_SINGLE, _CHECKED}),
    ],
)
@pytest.mark.asyncio
async def test_the_settings_tile_follows_the_approve_routes_self_approval_rule(
        monkeypatch, two_person, publish_switch, expected):
    from api.services import settings_registry as reg
    from shared import settings as shared_settings

    assert shared_settings.definition(_SINGLE).approval == "single"
    assert shared_settings.definition(_CHECKED).approval == "maker_checker"
    assert shared_settings.definition(_PUBLISH).approval == "maker_checker"
    reads: list[str] = []

    async def two(_conn):
        reads.append("two_person")
        return two_person

    async def publish(_conn):
        reads.append("publish")
        return publish_switch

    monkeypatch.setattr(reg, "two_person_on", two)
    monkeypatch.setattr(reg, "_content_switch_on", publish)
    mine = [_open(_SINGLE, _ME, 1), _open(_CHECKED, _ME, 2), _open(_PUBLISH, _ME, 3)]
    got = await reg.open_proposals_summary(_Rows([*mine, _open(_CHECKED, _OTHER, 4)]), viewer=_ME)
    counted = {r["key"] for r in mine if (r["key"] in expected)}
    assert got["count"] == len(counted) + 1 and got["by_app"] == {"cis": len(counted) + 1}
    # each switch is read at most once per summary (the publish one only when the two-person one is off)
    assert reads.count("two_person") == 1 and reads.count("publish") == (0 if two_person else 1)
    # the approve route decides with the same predicate: refused exactly for the left-out keys
    for key in (_SINGLE, _CHECKED, _PUBLISH):
        assert await reg._own_refused(None, shared_settings.definition(key), {}) is (key not in expected), key


@pytest.mark.asyncio
async def test_the_settings_tile_reads_no_switch_without_an_own_proposal(monkeypatch):
    from api.services import settings_registry as reg

    async def boom(_conn):
        raise AssertionError("no switch read needed")

    monkeypatch.setattr(reg, "two_person_on", boom)
    monkeypatch.setattr(reg, "_content_switch_on", boom)
    got = await reg.open_proposals_summary(_Rows([_open(_CHECKED, _OTHER, 5)]), viewer=_ME)
    assert got["count"] == 1 and got["app"] == "cis"
    assert (await reg.open_proposals_summary(_Rows([_open(_CHECKED, _ME, 6)]), viewer=None))["count"] == 1
