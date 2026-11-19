"""AA-1144 (S285 decision D7) — an agency's own Facts Entries are its booking terms only.

- /v1/facts refuses a new own fact of any other kind (422), an edit that sets one, and any edit of an older own fact
  of such a kind; the older fact is still listed (read_only) and may be deleted;
- an older own price / season / ... fact never replaces a platform fact: not in `overridden_by`, not in pick_facts,
  and the writer read SQL leaves it out (T9 and T2 read the same function);
- staff platform facts keep every kind.
The tenant API harness (fake acp_shared.facts behind the tenant pool, real JWT + role middleware) is the AA-814 one.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.acp_content_writing import facts as writer_facts
from services.acp_shared import facts_store as fs
from tests.unit.test_aa814_facts_entry import TENANT_A, _f, _h, tenant_api  # noqa: F401 - tenant_api is a fixture

LOCKED_KINDS = [k for k in fs.FACT_KINDS if k != "booking_terms"]


def _legacy(api, kind="price", fid="fact_aaold01", title="Halong cruise price"):
    """An own fact written before D7, of a kind the agency no longer writes."""
    api.db.add(fact_id=fid, tenant_id=TENANT_A, kind=kind, title=title, body="From $400.", provenance="Old sheet")
    return fid


def test_agency_kinds_are_booking_terms_only():
    assert fs.AGENCY_FACT_KINDS == ("booking_terms",)
    assert set(fs.AGENCY_FACT_KINDS) <= set(fs.FACT_KINDS)
    assert fs.agency_may_write("booking_terms")
    assert not any(fs.agency_may_write(k) for k in LOCKED_KINDS + [None, ""])


@pytest.mark.parametrize("kind", LOCKED_KINDS)
def test_a_new_own_fact_of_another_kind_is_422(tenant_api, kind):  # noqa: F811
    before = set(tenant_api.db.rows)
    r = tenant_api.post("/v1/facts", json={"kind": kind, "title": "t", "body": "b", "provenance": "p"},
                        headers=_h())
    assert r.status_code == 422, r.text
    assert "booking terms only" in r.json()["detail"]
    assert set(tenant_api.db.rows) == before
    tenant_api.audit.assert_not_called()


def test_a_new_own_fact_without_kind_is_other_and_refused(tenant_api):  # noqa: F811
    r = tenant_api.post("/v1/facts", json={"title": "t", "body": "b", "provenance": "p"}, headers=_h())
    assert r.status_code == 422


def test_booking_terms_are_still_written(tenant_api):  # noqa: F811
    r = tenant_api.post("/v1/facts", json={"kind": "booking_terms", "title": "Deposit", "body": "20% at booking.",
                                           "provenance": "Our terms"}, headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["read_only"] is False


def test_an_edit_cannot_set_another_kind(tenant_api):  # noqa: F811
    r = tenant_api.patch("/v1/facts/fact_aaown01", json={"kind": "price"}, headers=_h())
    assert r.status_code == 422
    assert tenant_api.db.rows["fact_aaown01"]["kind"] == "booking_terms"


@pytest.mark.parametrize("kind", LOCKED_KINDS)
def test_an_older_fact_of_another_kind_is_read_only_but_deletable(tenant_api, kind):  # noqa: F811
    fid = _legacy(tenant_api, kind)
    for body in ({"body": "From $350."}, {"kind": "booking_terms"}):
        r = tenant_api.patch(f"/v1/facts/{fid}", json=body, headers=_h())
        assert r.status_code == 422, body
    assert tenant_api.db.rows[fid]["body"] == "From $400." and tenant_api.db.rows[fid]["kind"] == kind
    own = {f["fact_id"]: f for f in tenant_api.get("/v1/facts", headers=_h()).json()["own"]}
    assert own[fid]["read_only"] is True and own["fact_aaown01"]["read_only"] is False
    assert tenant_api.delete(f"/v1/facts/{fid}", headers=_h()).status_code == 200
    assert fid not in tenant_api.db.rows


def test_an_older_fact_no_longer_overrides_the_platform_fact(tenant_api):  # noqa: F811
    tenant_api.db.add(fact_id="fact_plathal", scope="platform", kind="price", title="Halong Cruise Price",
                      body="From $500.", provenance="AA")
    _legacy(tenant_api)
    plat = {f["fact_id"]: f for f in tenant_api.get("/v1/facts", headers=_h()).json()["platform"]}
    assert plat["fact_plathal"]["overridden_by"] is None


def test_pick_facts_leaves_out_tenant_facts_of_other_kinds():
    plat = _f("fact_p1", "platform", title="Mekong cruise price", kind="price")
    own_price = _f("fact_t1", "tenant", title="Mekong cruise price", kind="price")
    own_season = _f("fact_t2", "tenant", title="Best season", kind="season")
    own_terms = _f("fact_t3", "tenant", title="Deposit")
    kept = writer_facts.pick_facts([plat, own_price, own_season, own_terms])
    assert [f["fact_id"] for f in kept] == ["fact_p1", "fact_t3"]   # the platform price stays; AA's fact wins


@pytest.mark.asyncio
async def test_writer_read_asks_only_for_the_agency_kinds_of_tenant_facts():
    conn = AsyncMock()
    conn.fetch.return_value = []
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    await writer_facts.fetch_facts_for_writing(uuid.UUID(TENANT_A), pool, country="Laos")
    sql, _tenant, _country, kinds = conn.fetch.call_args.args
    assert "scope = 'tenant' AND tenant_id = $1 AND kind = ANY($3::text[])" in sql
    assert kinds == ["booking_terms"]


@pytest.mark.asyncio
async def test_platform_facts_keep_every_kind():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "fact_id": "fact_x", "scope": "platform", "tenant_id": None, "kind": "season", "title": "t", "body": "b",
        "provenance": "p", "country": None, "stated_on": None, "valid_until": None, "updated_by": "admin:x",
        "created_at": None, "updated_at": None})
    out = await fs.create_fact(conn, fs.FactFields(kind="season", title="t", body="b", provenance="p"),
                               scope="platform", tenant_id=None, actor="admin:x")
    assert out["kind"] == "season"
