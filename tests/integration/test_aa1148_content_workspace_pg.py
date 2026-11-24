"""AA-1148 (S290) — the content workspace on real Postgres: a content user creates a 3-day tour from a blank form tied
to an AA supplier, autosaves its facts (with the stale-save refusal), sees its required list, and publishes it through
the AA-1146 draft gate, which refuses an incomplete hand-made tour (409 required_missing). Also: the unlinked tours
report with the suggested supplier, the bulk link and the apply of the suggestions (DRY RUN first), each with its
history row.

Needs CIS 478: the module is skipped on a database without it, so the code PR's own CI (before the migration PR is
merged) skips it and the merge queue (main + this PR) runs it. Booking's view booking.v_supply_suppliers (bk-072) is not
in the CIS test database: a stand-in view with invented suppliers is created inside the test transaction. Each test
runs in one transaction that is rolled back; process_export and the lifecycle side effects are replaced by stand-ins.
Invented names and places only."""
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_pipeline as ap  # noqa: E402
from services.content_workspace import facts as F  # noqa: E402
from services.content_workspace import policy  # noqa: E402
from services.content_workspace import store  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
EDITOR = "00000000-0000-4000-8000-0000000011e1"
DMC = "aa114800-0000-4000-8000-000000000001"
HOTEL = "aa114800-0000-4000-8000-000000000002"
GONE = "aa114800-0000-4000-8000-000000000003"
LIMITS = policy.Limits(max_days=21, min_inclusions=3, min_exclusions=1, bulk_max=2)
ACTOR = f"admin:{EDITOR}"


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


def _request(conn, *, role="content", user=EDITOR):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))), scope={},
                           headers={"x-admin-role": role, "x-admin-user-id": user}, json=AsyncMock(return_value={}))


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    ready = await c.fetchval("SELECT count(*) AS n FROM information_schema.columns WHERE table_schema = "
                             "'silver_aa_internal' AND table_name = 'raw_tours' AND column_name = 'entry_origin'")
    if not ready:
        await c.close()
        pytest.skip("CIS 478 is not applied on this database (the migration PR ships first)")
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("CREATE SCHEMA IF NOT EXISTS booking")
        await c.execute("DROP VIEW IF EXISTS booking.v_supply_suppliers")
        await c.execute(f"""
            CREATE VIEW booking.v_supply_suppliers AS
            SELECT v.id::uuid AS id, v.code, v.company_name, v.categories::text[] AS categories,
                   v.country_code, v.status, now() AS updated_at
              FROM (VALUES ('{DMC}', 'SUP-ZZ1', 'Zephyr Delta Journeys', '{{dmc}}', 'VN', 'active'),
                           ('{HOTEL}', 'SUP-ZZ2', 'Quillmoor Riverside Hotels', '{{hotel}}', 'VN', 'active'),
                           ('{GONE}', 'SUP-ZZ3', 'Ombre Old Travel', '{{dmc}}', 'VN', 'inactive'))
                   AS v (id, code, company_name, categories, country_code, status)""")
        with patch.object(ap, "verify_admin_secret", MagicMock()), \
                patch.object(policy, "limits", AsyncMock(return_value=LIMITS)), \
                patch("services.tour_lifecycle.run_after_commit", AsyncMock()), \
                patch.object(ap.tour_detail, "_source_fanout_raw", AsyncMock()):
            yield c
    finally:
        await tr.rollback()
        await c.close()


def _form(**over):
    base = dict(
        name="Zephyr Delta Three Days", country="VN", supplier_id=DMC, summary="Three slow days on the delta.",
        nights=2,
        days=[{"title": "Arrive in Quillmoor", "places": ["Quillmoor"], "meals": ["dinner"], "overnight": "Lodge",
               "description": "Boat to the old mill."},
              {"title": "Ombre market", "places": ["Ombre Market"], "meals": ["breakfast"], "overnight": "Homestay",
               "description": "Early market."},
              {"title": "Home", "places": ["Fennel City"], "meals": [], "description": "Transfer back."}],
        inclusions=["Transport", "Guide", "Entrance fees"], exclusions=["Drinks"])
    base.update(over)
    return F.FactsIn(**base)


async def _publish(conn, tour_id):
    async def _export(version_id, **kw):     # process_export's own connection would not see the uncommitted rows
        assert kw["contract_exit"] == "human_publish"
        await conn.execute(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
            "master_status) SELECT tour_id, id, tenant_id, aa_name, 'active' FROM silver_aa_internal.generated_content "
            "WHERE id = $1::uuid", version_id)
        return {"catalog_id": "pt-zz", "atomize_job_id": None}
    with patch("services.export.handler.process_export", _export):
        return await ap.publish_tour_draft(tour_id, _request(conn), x_admin_secret="s")


@pytest.mark.asyncio
async def test_a_content_user_creates_a_3_day_tour_tied_to_a_supplier_and_publishes_it(conn):
    pool = _OneConnPool(conn)
    created = await store.create_tour(pool, _form(), actor=ACTOR)
    tour_id = created["tour_id"]
    row = await conn.fetchrow(
        "SELECT entry_origin, supplier_id::text AS supplier_id, provider, country, country_iso2::text AS iso2, "
        "duration, src_itineraries, inclusions, source_status::text AS s FROM silver_aa_internal.raw_tours "
        "WHERE tour_id = $1::uuid", tour_id)
    assert (row["entry_origin"], row["supplier_id"], row["provider"]) == ("manual", DMC, "Zephyr Delta Journeys")
    assert (row["country"], row["iso2"], row["duration"], row["s"]) == ("Vietnam", "VN", "3 days 2 nights", "active")
    assert row["src_itineraries"].startswith("Day 1 — Arrive in Quillmoor\nPlaces: Quillmoor")
    assert row["inclusions"] == "Transport\nGuide\nEntrance fees"
    draft = await conn.fetchrow("SELECT version_num, status::text AS s, origin, author_id, aa_itineraries FROM "
                                "silver_aa_internal.generated_content WHERE tour_id = $1::uuid", tour_id)
    assert (draft["version_num"], draft["s"], draft["origin"], draft["author_id"]) == (1, "draft", "human", ACTOR)
    assert F.wording_days(draft["aa_itineraries"]) == 3

    view = await store.read_tour(pool, tour_id)
    assert view["state"] == "draft" and view["editable"] and not view["reason_required"]
    assert view["supplier"]["company_name"] == "Zephyr Delta Journeys" and view["completeness"] == 100
    assert view["from_price"] is None                       # no Booking price view here: none shown, no error

    # the wording goes through the AA-1146 draft: checks, then the one gate
    with pytest.raises(HTTPException) as e:
        await ap.publish_tour_draft(tour_id, _request(conn), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "revalidate_required"
    await conn.execute("UPDATE silver_aa_internal.generated_content SET revalidate_passed = true "
                       "WHERE tour_id = $1::uuid AND status = 'draft'", tour_id)
    res = await _publish(conn, tour_id)
    assert res["published"] and res["version_num"] == 1
    after = await store.read_tour(pool, tour_id)
    assert after["state"] == "live" and after["reason_required"]
    assert [x["tour_id"] for x in (await store.list_manual(pool))["items"]][:1] == [tour_id]


@pytest.mark.asyncio
async def test_an_incomplete_hand_made_tour_is_refused_at_publish(conn):
    pool = _OneConnPool(conn)
    tour_id = (await store.create_tour(pool, _form(exclusions=[], supplier_id=None), actor=ACTOR))["tour_id"]
    await conn.execute("UPDATE silver_aa_internal.generated_content SET revalidate_passed = true "
                       "WHERE tour_id = $1::uuid", tour_id)
    with pytest.raises(HTTPException) as e:
        await _publish(conn, tour_id)
    assert e.value.status_code == 409 and e.value.detail["code"] == "required_missing"
    assert {i["key"] for i in e.value.detail["items"]} == {"supplier", "exclusions"}
    assert await conn.fetchval("SELECT count(*) AS n FROM gold_aa_internal.published_tours WHERE tour_id = $1::uuid",
                               tour_id) == 0


@pytest.mark.asyncio
async def test_facts_autosave_history_and_the_stale_save(conn):
    pool = _OneConnPool(conn)
    tour_id = (await store.create_tour(pool, _form(), actor=ACTOR))["tour_id"]
    first = (await store.read_tour(pool, tour_id))["facts"]["saved_at"]
    out = await store.save_facts(pool, tour_id, _form(inclusions=["Transport", "Guide", "Entrance fees", "Water"]),
                                 expected_saved_at=first, reason=None, actor=ACTOR)
    assert out["saved"] and set(out["fields"]) == {"inclusions", "manual_facts"} and out["revision_num"] == 1
    hist = await conn.fetchrow("SELECT source, fields, actor FROM silver_aa_internal.raw_tour_revision "
                               "WHERE tour_id = $1::uuid", tour_id)
    assert (hist["source"], list(hist["fields"]), hist["actor"]) == ("staff_edit", ["inclusions", "manual_facts"],
                                                                       ACTOR)
    # the same form again changes nothing; a save from an older load is refused
    assert not (await store.save_facts(pool, tour_id, _form(inclusions=["Transport", "Guide", "Entrance fees",
                                                                           "Water"]),
                                       expected_saved_at=out["saved_at"], reason=None, actor=ACTOR))["saved"]
    with pytest.raises(store.WorkspaceError) as e:
        await store.save_facts(pool, tour_id, _form(), expected_saved_at=first, reason=None, actor=ACTOR)
    assert e.value.code == "facts_changed"
    with pytest.raises(store.WorkspaceError) as e:
        await store.save_facts(pool, tour_id, _form(supplier_id=GONE), expected_saved_at=None, reason=None,
                               actor=ACTOR)
    assert e.value.code == "supplier_inactive"
    with pytest.raises(store.WorkspaceError) as e:
        await store.create_tour(pool, _form(days=[{"title": "x"}] * 22), actor=ACTOR)
    assert e.value.code == "too_many_days"


@pytest.mark.asyncio
async def test_a_published_tour_needs_a_reason_and_a_sheet_tour_is_not_edited_here(conn):
    pool = _OneConnPool(conn)
    tour_id = (await store.create_tour(pool, _form(), actor=ACTOR))["tour_id"]
    await conn.execute("UPDATE silver_aa_internal.generated_content SET revalidate_passed = true "
                       "WHERE tour_id = $1::uuid", tour_id)
    await _publish(conn, tour_id)
    with pytest.raises(store.WorkspaceError) as e:
        await store.save_facts(pool, tour_id, _form(exclusions=["Drinks", "Tips"]), expected_saved_at=None,
                               reason=None, actor=ACTOR)
    assert e.value.code == "reason_required"
    out = await store.save_facts(pool, tour_id, _form(exclusions=["Drinks", "Tips"]), expected_saved_at=None,
                                 reason="zz the supplier no longer covers tips", actor=ACTOR)
    assert out["saved"] and "exclusions" in out["fields"]
    ap.tour_detail._source_fanout_raw.assert_awaited()
    sheet = uuid.uuid4()
    await conn.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, provider) "
                       "VALUES ($1, $2, $3, 'Ombre Lake Walk', 'Zephyr Delta Journeys Co., Ltd')", sheet, MASTER,
                       uuid.uuid4())
    with pytest.raises(store.WorkspaceError) as e:
        await store.save_facts(pool, str(sheet), _form(), expected_saved_at=None, reason="zz", actor=ACTOR)
    assert e.value.code == "not_hand_made"


@pytest.mark.asyncio
async def test_unlinked_report_bulk_link_and_suggestions(conn):
    pool = _OneConnPool(conn)
    ids = [uuid.uuid4() for _ in range(4)]
    for tid, provider in zip(ids, ["Zephyr Delta Journeys Co., Ltd", "ZEPHYR DELTA JOURNEYS", "Zz Unknown Trips",
                                   "Quillmoor Riverside Hotels"]):
        await conn.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, "
                           "provider, country) VALUES ($1, $2, $3, 'Zz Fennel Loop', $4, 'Vietnam')", tid, MASTER,
                           uuid.uuid4(), provider)
    rep = await store.unlinked_report(pool, q="Zz Fennel Loop")
    mine = {x["tour_id"]: x for x in rep["items"]}
    assert set(mine) == {str(t) for t in ids} and rep["suppliers_available"]
    assert mine[str(ids[0])]["suggestion"]["id"] == DMC and mine[str(ids[2])]["suggestion"] is None
    # the bulk link: at most bulk_max tours, history + audit per tour
    with pytest.raises(store.WorkspaceError) as e:
        await store.link_tours(pool, [str(t) for t in ids[:3]], HOTEL, reason="zz bulk", actor=ACTOR)
    assert e.value.code == "too_many_tours"
    out = await store.link_tours(pool, [str(ids[2])], HOTEL, reason="zz chosen by hand", actor=ACTOR)
    assert out["linked"] == [str(ids[2])]
    assert await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE action = "
                               "'raw_tour.supplier_linked' AND resource_id = $1", str(ids[2])) == 1
    # suggestions: DRY RUN writes nothing, the apply links at most bulk_max
    dry = await store.apply_suggestions(pool, dry_run=True, reason="zz match", actor=ACTOR)
    assert dry["dry_run"] and dry["matches"] >= 3 and dry["would_link"] == 2
    assert await conn.fetchval("SELECT count(*) AS n FROM silver_aa_internal.raw_tours WHERE tour_id = ANY($1::uuid[]) "
                               "AND supplier_id IS NOT NULL", ids) == 1
    done = await store.apply_suggestions(pool, dry_run=False, reason="zz match", actor=ACTOR)
    assert done["linked"] == 2 and done["remaining"] == done["matches"] - 2
