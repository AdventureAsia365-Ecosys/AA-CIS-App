"""AA-1156 — My work (GET /admin/my-work/summary and the /admin/my-work page rules).

Covers: the route's capability gate (403 for a role with none of the tile capabilities and for the machine), the tiles
each role gets (shown by capability, never by person), a tile whose table is missing (available false, the page still
answers), and that the frontend (lib/myWork.ts, lib/rbac.ts) names the same tiles, capabilities and roles. The counts
against the target pages' own list queries run on Postgres: tests/integration/test_aa1156_my_work_counts.py.
No network, no AWS, no DB.
"""
from __future__ import annotations

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

ALL_KEYS = ["review_waiting", "supply_drafts", "supply_no_description", "aa_content_drafts",
            "place_cards_to_review", "compliance_holds"]
EXPECTED = {
    "admin": ALL_KEYS,
    "content_lead": ALL_KEYS,
    "content": ["review_waiting", "supply_drafts", "supply_no_description", "aa_content_drafts"],
    "tenant_qa": ["place_cards_to_review", "compliance_holds"],
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
        async def count(_conn):
            calls.append(key)
            return {"count": n, "oldest_at": OLDEST}
        return count

    tiles = tuple(admin_my_work.Tile(t.key, t.capability, t.filter, counter(t.key, i + 1))
                  for i, t in enumerate(admin_my_work.TILES))
    monkeypatch.setattr(admin_my_work, "TILES", tiles)
    app = FastAPI()
    app.include_router(admin_my_work.router)
    app.state.pool = _Pool()
    c = TestClient(app)
    c.calls = calls
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
    assert tiles["review_waiting"]["filter"] == {}               # the Review Queue opens on pending
    assert tiles["supply_drafts"]["filter"] == {"status": "in_review"}
    assert tiles["supply_no_description"]["filter"] == {"status": "draft"}
    assert tiles["aa_content_drafts"]["filter"] == {"status": "draft"}
    assert tiles["place_cards_to_review"]["filter"] == {"status": "auto_checked"}
    assert tiles["compliance_holds"]["filter"] == {}             # Compliance opens on open flags


def test_a_missing_table_marks_only_that_tile_unavailable(client, monkeypatch):
    async def missing(_conn):
        raise asyncpg.UndefinedTableError("relation does not exist")

    tiles = tuple(t if t.key != "compliance_holds" else admin_my_work.Tile(t.key, t.capability, t.filter, missing)
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
    assert store._LIST_FROM in store.SUMMARY_SQL and store._LIST_FROM in store.COUNT_SQL
    assert atc.FILTER_SQL in atc.COUNT_SQL and atc.FILTER_SQL in atc.SUMMARY_SQL and atc.FILTER_SQL in atc.LIST_SQL
    cq = (REPO / "api" / "routers" / "admin_content_quality.py").read_text()
    assert "place_card_list.list_filter(status, q)" in cq
    rq = (REPO / "api" / "routers" / "admin_pipeline" / "review.py").read_text()
    assert rq.count("_review_queue_where(") == 3   # the definition, the page, the tile


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
    with_tile = {r for r, perms in ROLE_PERMS.items() if admin_my_work.tiles_for(perms)}
    assert page_roles == with_tile == set(EXPECTED)
    assert '{ prefix: "/admin/my-work", roles: MY_WORK }' in rbac


def test_content_roles_land_on_my_work_and_admin_keeps_overview():
    rbac = (REPO / "frontend" / "lib" / "rbac.ts").read_text()
    body = re.search(r"export function landingPage\([^)]*\): string \{(.*?)\n\}", rbac, re.S).group(1)
    assert 'if (role === "admin") return "/admin/overview";' in body
    assert "MY_WORK_LANDING" in body
    landing = set(re.findall(r'"([a-z_]+)"', re.search(r"const MY_WORK_LANDING[^=]*=\s*\[([^\]]*)\]", rbac).group(1)))
    assert landing == {"content", "content_lead", "tenant_qa"}
