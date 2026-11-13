"""AA-741 S271 — the Blog Segment Slate's SQL on the real schema (services/acp_shared/slate.py).

1. `_SEGMENT_CANDIDATES_SQL`: a Segment's demand is the highest over the tenant's markets (AA-1055 PR 1, with the
   market that gives it) and questions the highest; total_rank / said stay the MIN over every row.
2. `_HELD_SUBJECTS_SQL` + `_fetch_held_subjects`: the tenant's picked / used / cut Blog Segment Subjects with their
   place; proposed rows, other Channels and Route Subjects hold nothing.
3. `_SEGMENT_ROUTES_SQL`: the current Routes of the given tours that hold the Segment, under its live id or an id
   that merged into it; a superseded Route and another tour's Route are not returned.

Invented tours, places and ids; everything is rolled back.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_shared import slate  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
OLD = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    yield c
    await t.rollback()
    await c.close()


async def _tour(c, name):
    tour_id = str(uuid.uuid4())
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER)  # raw_tours RLS
    await c.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                           source_group_id, source_status)
                       VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'Laos', $5::uuid, 'active')""",
                    tour_id, MASTER, BATCH_ID, name, str(uuid.uuid4()))
    return tour_id


async def _segments(c, *rows):
    await c.executemany("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                        "VALUES ($1, $2, 'visit')", rows)


async def _rank(c, rows):
    # (market, tour, segment, total_rank, demand_volume, questions, said)
    await c.executemany("""
        INSERT INTO acp_contract.atom_ranking (market, tour_id, segment_id, total_rank, demand_volume, questions,
                                               said, recurrence, computed_at, version)
        VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, 1, now(), 1)""", rows)


@pytest.mark.asyncio
async def test_segment_candidates_take_the_highest_demand_across_markets(tx):
    """AA-1055 PR 1: demand is the MAX over the tenant's markets (with the market that gives it), questions the MAX;
    a market with no number (median rank) no longer wins and hides the others. total_rank / said stay the MIN."""
    a, b = await _tour(tx, "zz741 Valdoria lakes"), await _tour(tx, "zz741 Valdoria passes")
    await _segments(tx, ("zz741_lake", "Lake Aurel"), ("zz741_pass", "Corvin Pass"), ("zz741_bay", "Selva Bay"),
                    ("zz741_moor", "Brenn Moor"))
    await _rank(tx, [
        # Lake Aurel: US has the most searches and questions; UK is weak.
        ("US", a, "zz741_lake", 4, 2400, 5, 300), ("UK", a, "zz741_lake", 9, 300, 1, 280),
        ("US", b, "zz741_lake", 6, 2400, 4, 500),
        # Corvin Pass: UK ranks best (2) but US has more searches: US's 9000 is read (was UK's 400 until AA-1055).
        ("US", a, "zz741_pass", 7, 9000, 8, 200), ("UK", a, "zz741_pass", 2, 400, 1, 190),
        # Selva Bay: DE has no number and the best (median) rank; UK's real number is read.
        ("DE", a, "zz741_bay", 1, None, 0, 90), ("UK", a, "zz741_bay", 5, 1800, 3, 95),
        # Brenn Moor: no market has a number -> NULL demand and no demand market (Slate reason no_data)
        ("US", a, "zz741_moor", 3, None, 2, 40), ("UK", a, "zz741_moor", 3, None, 0, 40),
    ])
    markets = ["US", "UK", "DE"]
    rows = {r["segment_id"]: r for r in await tx.fetch(slate._SEGMENT_CANDIDATES_SQL, [a, b], markets)}
    lake, pas, bay, moor = (rows[k] for k in ("zz741_lake", "zz741_pass", "zz741_bay", "zz741_moor"))
    assert (lake["total_rank"], lake["demand_volume"], lake["questions"], lake["said"]) == (4, 2400, 5, 280)
    assert lake["demand_market"] == "US"
    assert lake["canonical_place"] == "Lake Aurel" and lake["canonical_action"] == "visit"
    assert (pas["total_rank"], pas["demand_volume"], pas["demand_market"], pas["questions"], pas["said"]) == \
        (2, 9000, "US", 8, 190)
    assert (bay["total_rank"], bay["demand_volume"], bay["demand_market"], bay["questions"]) == (1, 1800, "UK", 3)
    assert (moor["demand_volume"], moor["demand_market"], moor["questions"]) == (None, None, 2)
    # the market list still scopes the read: UK only -> Corvin Pass's UK numbers
    uk = {r["segment_id"]: r for r in await tx.fetch(slate._SEGMENT_CANDIDATES_SQL, [a, b], ["UK"])}
    assert (uk["zz741_pass"]["demand_volume"], uk["zz741_pass"]["questions"]) == (400, 1)
    # a tie on demand goes to the tenant's first market
    await _rank(tx, [("UK", b, "zz741_pass", 8, 9000, 1, 100)])
    tie = {r["segment_id"]: r for r in await tx.fetch(slate._SEGMENT_CANDIDATES_SQL, [a, b], ["UK", "US"])}
    assert tie["zz741_pass"]["demand_market"] == "UK"


@pytest.mark.asyncio
async def test_the_aa1055_compare_script_reads_both_rules(tx):
    """scripts/aa1055/slate_bar_compare.py: its before rule (best-ranked market) and after rule (MAX) on the schema."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "aa1055", "slate_bar_compare.py")
    spec = importlib.util.spec_from_file_location("aa1055_slate_bar_compare", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    a = await _tour(tx, "zz1055 Valdoria coast")
    await _segments(tx, ("zz1055_bay", "Selva Bay"))
    await _rank(tx, [("DE", a, "zz1055_bay", 1, None, 0, 90), ("UK", a, "zz1055_bay", 5, 1800, 3, 95)])
    before = await tx.fetch(script.BEFORE_SQL, [a], ["US", "UK", "DE"])
    after = await tx.fetch(script.AFTER_SQL, [a], ["US", "UK", "DE"])
    blog = {"blog": slate.bar("blog")}   # S279 L2: the Bar in force (numbers from the settings registry)
    assert script.count_statuses(before, ["blog"], blog)["blog"]["no_data"] == 1   # DE (no number) won on rank
    assert script.count_statuses(after, ["blog"], blog)["blog"]["cleared"] == 1    # UK's 1800 / 3 questions


@pytest.mark.asyncio
async def test_held_blog_subjects_and_their_places(tx):
    tenant = str(uuid.uuid4())
    await tx.execute("""INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                            posts_per_week) VALUES ($1::uuid, 'zz741', $2, 'starter', 60, true, 3)""",
                     tenant, f"zz741-{tenant[:8]}")
    tour = await _tour(tx, "zz741 Valdoria loop")
    await _segments(tx, ("zz741_h_lake", "Lake Aurel"), ("zz741_h_gorge", "Mirsa Gorge"),
                    ("zz741_h_pass", "Corvin Pass"), ("zz741_h_old", "Old Velk"), ("zz741_h_new", "Old Velk Market"))
    await tx.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical, merged_at) "
                     "VALUES ('zz741_h_old', 'zz741_h_new', now())")
    await tx.execute("""INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids, first_day,
                            last_day, version, created_at)
                        VALUES ('zz741_h_r', $1::uuid, 'Loop', '["zz741_h_lake", "zz741_h_gorge"]', 1, 3, 1, now())""",
                     tour)
    await tx.executemany("""INSERT INTO acp_shared.subject (tenant_id, segment_id, route_id, channel, state,
                                cleared_bar_reason) VALUES ($1::uuid, $2, $3, $4, $5, '{}'::jsonb)""", [
        (tenant, "zz741_h_lake", None, "blog", "used"),
        (tenant, "zz741_h_old", None, "blog", "cut"),
        (tenant, "zz741_h_gorge", None, "blog", "proposed"),      # still on offer: holds nothing
        (tenant, "zz741_h_pass", None, "linkedin", "used"),       # another Channel
        (tenant, None, "zz741_h_r", "blog", "used"),              # a pre-S271 Route Subject: no place
    ])
    held = await slate._fetch_held_subjects(uuid.UUID(tenant), tx, ["blog"])
    assert set(held) == {"blog"}
    assert held["blog"].segment_ids == {"zz741_h_lake", "zz741_h_old", "zz741_h_new"}
    assert held["blog"].places == {"lake aurel", "old velk", "old velk market"}


@pytest.mark.asyncio
async def test_segment_routes_follow_aliases_and_skip_superseded_and_other_tours(tx):
    a, b, c = (await _tour(tx, "zz741 r a"), await _tour(tx, "zz741 r b"), await _tour(tx, "zz741 r c"))
    await _segments(tx, ("zz741_r_lake", "Lake Aurel"), ("zz741_r_mid", "Lake Aurel"), ("zz741_r_first", "Lake Aurel"),
                    ("zz741_r_pass", "Corvin Pass"))
    # first -> mid -> lake: a Route stored on the oldest id still holds the live Segment
    await tx.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical, merged_at) "
                     "VALUES ('zz741_r_mid', 'zz741_r_lake', now()), ('zz741_r_first', 'zz741_r_mid', now())")
    await tx.executemany("""INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids,
                                first_day, last_day, version, superseded_at, created_at)
                            VALUES ($1, $2::uuid, 'H', $3::jsonb, $4::smallint, ($4 + 2)::smallint, 1, $5, now())""", [
        ("zz741_ra", a, json.dumps(["zz741_r_pass", "zz741_r_lake"]), 1, None),
        ("zz741_ra_old", a, json.dumps(["zz741_r_lake", "zz741_r_pass"]), 5, OLD),
        ("zz741_rb", b, json.dumps(["zz741_r_first", "zz741_r_pass"]), 2, None),
        ("zz741_rc", c, json.dumps(["zz741_r_lake", "zz741_r_pass"]), 3, None),     # not a tenant tour
        ("zz741_ra2", a, json.dumps(["zz741_r_pass"]), 9, None),                   # no Lake Aurel
    ])
    rows = await tx.fetch(slate._SEGMENT_ROUTES_SQL, "zz741_r_lake", [a, b])
    assert {r["route_id"] for r in rows} == {"zz741_ra", "zz741_rb"}
    by_id = {r["route_id"]: r for r in rows}
    assert by_id["zz741_ra"]["tour_id"] == a and by_id["zz741_ra"]["first_day"] == 1
    assert json.loads(by_id["zz741_rb"]["ordered_segment_ids"]) == ["zz741_r_first", "zz741_r_pass"]
    # the pick runs on the tenant pool (aa_app_user, SUBJECT_DECIDE_FLAG): the same read works there
    await tx.execute("SET LOCAL ROLE aa_app_user")
    as_tenant = await tx.fetch(slate._SEGMENT_ROUTES_SQL, "zz741_r_lake", [a, b])
    assert {r["route_id"] for r in as_tenant} == {"zz741_ra", "zz741_rb"}


@pytest.mark.asyncio
async def test_subject_focus_segment_reads_the_live_segment_of_a_segment_subject_only(tx):
    """AA-741 S271 review — walk_focus.subject_focus_segment: a Segment Subject's own Segment (an id that merged into
    another resolves to the live one); a Route Subject and another tenant's Subject give None. It also runs on the
    tenant pool (aa_app_user, RLS tenant_isolation), where T8 / T9 may read it."""
    from services.acp_shared import walk_focus
    tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
    for t in (tenant, other):
        await tx.execute("""INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                posts_per_week) VALUES ($1::uuid, 'zz741f', $2, 'starter', 60, true, 3)""",
                         t, f"zz741f-{t[:8]}")
    tour = await _tour(tx, "zz741 focus loop")
    await _segments(tx, ("zz741_f_old", "Lake Aurel"), ("zz741_f_live", "Lake Aurel"), ("zz741_f_pass", "Corvin Pass"))
    await tx.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical, merged_at) "
                     "VALUES ('zz741_f_old', 'zz741_f_live', now())")
    await tx.execute("""INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids, first_day,
                            last_day, version, created_at)
                        VALUES ('zz741_f_r', $1::uuid, 'Loop', '["zz741_f_live", "zz741_f_pass"]', 1, 3, 1, now())""",
                     tour)
    ids = {}
    for key, t, seg, route in (("seg", tenant, "zz741_f_old", None), ("route", tenant, None, "zz741_f_r"),
                               ("other", other, "zz741_f_pass", None)):
        ids[key] = await tx.fetchval("""INSERT INTO acp_shared.subject (tenant_id, segment_id, route_id, channel,
                                            state, cleared_bar_reason)
                                        VALUES ($1::uuid, $2, $3, 'blog', 'picked', '{}'::jsonb)
                                        RETURNING subject_id""", t, seg, route)
    assert await walk_focus.subject_focus_segment(tx, tenant, ids["seg"]) == "zz741_f_live"
    assert await walk_focus.subject_focus_segment(tx, tenant, ids["route"]) is None
    assert await walk_focus.subject_focus_segment(tx, tenant, ids["other"]) is None     # the tenant filter
    await tx.execute("SELECT set_config('app.tenant_id', $1, true)", tenant)
    await tx.execute("SET LOCAL ROLE aa_app_user")
    assert await walk_focus.subject_focus_segment(tx, tenant, ids["seg"]) == "zz741_f_live"
    assert await walk_focus.subject_focus_segment(tx, other, ids["other"]) is None      # RLS: not this tenant's row
