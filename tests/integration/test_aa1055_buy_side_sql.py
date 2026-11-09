"""AA-1055 PR 2 — the buy side's queries on the real schema (CIS 393 applied by conftest): the research scope through
acp_contract.v_active_tour_atoms, the per-tier research log, the one-SERP upsert and its freshness read, the stored
keyword map, the landing sweep's candidate query and the free seo_context copy.

Everything runs on one asyncpg connection inside a transaction that is rolled back. No DataForSEO, LLM or Jev call:
the exclusions are read with cached_only=True (no stored pick in this database = the rule's answer). Invented names
and places only.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID  # noqa: E402
from services.acp_contract import landing_sweep  # noqa: E402
from services.acp_contract import segment_research as sr  # noqa: E402
from services.acp_contract import segment_research_batch as rb  # noqa: E402
from services.acp_contract import seo_context_copy as copy  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def fetchval(self, *a):
        return await self.conn.fetchval(*a)

    async def fetch(self, *a):
        return await self.conn.fetch(*a)


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_ID)  # raw_tours / seo_context RLS
    yield c
    await t.rollback()
    await c.close()


async def _tour(conn, country: str = "Mongolia", *, active: bool = True) -> uuid.UUID:
    tour_id = uuid.uuid4()
    await conn.execute("""INSERT INTO silver_aa_internal.raw_tours
                          (tour_id, tenant_id, batch_id, src_name, country, source_group_id, source_status)
                          VALUES ($1, $2, $3, 'Zarvok trek zz1055', $4, $5, 'active')""",
                       tour_id, uuid.UUID(TENANT_ID), uuid.UUID(BATCH_ID), country, uuid.uuid4())
    await conn.execute("""INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, aa_name,
                          master_status) VALUES ($1, gen_random_uuid(), 'Zarvok trek zz1055', $2)""",
                       tour_id, "active" if active else "inactive")
    return tour_id


async def _segment(conn, tour_id, seg: str, place: str, *, count=None, computed_at=None) -> None:
    atom = f"{seg}_atom"
    await conn.execute("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place,
                          activity_type) VALUES ($1, $2, 'atom text', 2, $3, 'sightseeing')""", atom, tour_id, place)
    await conn.execute("""INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action,
                          questions_count, questions_computed_at) VALUES ($1, $2, 'visit', $3, $4)""",
                       seg, place, count, computed_at)
    await conn.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)",
                       seg, atom)


@pytest.mark.asyncio
async def test_scope_reads_only_active_masters(tx):
    live = await _tour(tx)
    gone = await _tour(tx, active=False)
    await _segment(tx, live, "zz1055_live", "Zarvok Lake")
    await _segment(tx, gone, "zz1055_gone", "Quellan Gorge")
    by_place = await sr._places_in_scope(_Pool(tx), None, [str(live), str(gone)], None, cached_only=True)
    assert by_place == {"Zarvok Lake": ["visit"]}
    by_country = await sr._places_in_scope(_Pool(tx), None, None, "mongolia", cached_only=True)
    assert "Zarvok Lake" in by_country and "Quellan Gorge" not in by_country


@pytest.mark.asyncio
async def test_log_tiers_and_their_freshness(tx):
    pool = _Pool(tx)
    await rb._write_logs(pool, [("Zarvok Lake", "US", "volume"), ("Zarvok Lake", "US", "serp"),
                                ("Zarvok Lake", "UK", "volume")])
    await rb._write_logs(pool, [("Zarvok Lake", "US", "serp")])          # re-written, same key
    await tx.execute("""INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at)
                        VALUES ('Quellan Gorge', 'US', 'volume', now() - interval '400 days')""")
    fresh = await sr._fresh_tiers(pool, ["Zarvok Lake", "Quellan Gorge"])
    assert fresh == {"Zarvok Lake": {("US", "volume"), ("US", "serp"), ("UK", "volume")}}


@pytest.mark.asyncio
async def test_serp_upsert_keeps_earlier_paa_and_reads_fresh_only_with_related(tx):
    pool = _Pool(tx)
    await tx.execute("""INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask,
                        retrieved_on) VALUES ('zarvok lake', 'US', 1200, '["is zarvok lake cold?"]', now())""")
    # a SERP with no PAA box keeps the stored PAA; related searches are stored
    await rb._store_serp(pool, "zarvok lake", "US", [], ["zarvok lake map"], ["example.test"])
    # a SERP on a keyword with no row yet: no volume, epoch date (the volume pass still buys it)
    await rb._store_serp(pool, "zarvok lake hike", "US", ["how long is the zarvok hike?"], [], [])
    rows = {r["keyword"]: r for r in await tx.fetch(
        "SELECT keyword, search_volume, people_also_ask, related_searches, serp_location, "
        "retrieved_on < now() - interval '1 year' AS volume_stale, paa_retrieved_on IS NOT NULL AS has_serp "
        "FROM acp_contract.search_demand WHERE keyword LIKE 'zarvok lake%'")}
    assert json.loads(rows["zarvok lake"]["people_also_ask"]) == ["is zarvok lake cold?"]
    assert json.loads(rows["zarvok lake"]["related_searches"]) == ["zarvok lake map"]
    assert rows["zarvok lake"]["serp_location"] == "US" and rows["zarvok lake"]["search_volume"] == 1200
    assert rows["zarvok lake hike"]["search_volume"] is None and rows["zarvok lake hike"]["volume_stale"]
    assert rows["zarvok lake hike"]["has_serp"]
    state = {(r["keyword"], r["market"]): (r["serp_fresh"], r["volume_fresh"]) for r in await tx.fetch(
        rb._SERP_STATE_SQL, ["zarvok lake", "zarvok lake hike"], rb.SERP_FRESH_FOR, sr.FRESH_FOR)}
    # the SERP-only row holds a fresh SERP but no measured volume (epoch date): not a known zero
    assert state == {("zarvok lake", "US"): (True, True), ("zarvok lake hike", "US"): (True, False)}
    await tx.execute("UPDATE acp_contract.search_demand SET related_searches = NULL WHERE keyword = 'zarvok lake'")
    state = {r["keyword"]: r["serp_fresh"] for r in await tx.fetch(rb._SERP_STATE_SQL, ["zarvok lake"],
                                                                    rb.SERP_FRESH_FOR, sr.FRESH_FOR)}
    assert state == {"zarvok lake": False}


@pytest.mark.asyncio
async def test_question_landing_reads_a_serp_row_without_volume(tx):
    await rb._store_serp(_Pool(tx), "zarvok lake hike", "US", ["how long is the zarvok hike?"], [], [])
    rows = await tx.fetch("SELECT keyword FROM acp_contract.search_demand "
                          "WHERE search_volume IS NOT NULL OR paa_retrieved_on IS NOT NULL")
    assert "zarvok lake hike" in {r["keyword"] for r in rows}


@pytest.mark.asyncio
async def test_keyword_map_round_trip_keeps_other_sources(tx):
    pool = _Pool(tx)
    await tx.execute("""INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, rank, source,
                        prompt_version) VALUES ('place', 'Zarvok Lake', 'zarvok lake ferry', 9, 'seo_context_copy',
                        'copy')""")
    p = rb._Place("Zarvok Lake", ["visit"], ["US"])
    await rb._store_maps([p], {"Zarvok Lake": ["zarvok lake", "zarvok lake hike"]}, pool)
    await rb._store_maps([p], {"Zarvok Lake": ["zarvok lake", "zarvok lake boat"]}, pool)   # replaced
    rows = (await rb.load_maps(pool, ["Zarvok Lake"]))["Zarvok Lake"]
    assert [(r["keyword"], r["jev_zone"]) for r in rows] == [
        ("zarvok lake", "not_asked"), ("zarvok lake boat", None), ("zarvok lake ferry", None)]
    # a map holding a row of another prompt version is proposed again
    assert rb.keywords_from_map(rows) is None
    assert rb.keywords_from_map([r for r in rows if r["prompt_version"] == rb.PROMPT_VERSION]) == [
        "zarvok lake", "zarvok lake boat"]


@pytest.mark.asyncio
async def test_landing_sweep_candidates(tx):
    tour = await _tour(tx)
    gone = await _tour(tx, active=False)
    await _segment(tx, tour, "zz1055_never", "Zarvok Lake")
    await _segment(tx, tour, "zz1055_before", "Quellan Gorge", count=2, computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    await _segment(tx, tour, "zz1055_after", "Brindle Falls", count=1, computed_at=datetime(2026, 12, 1, tzinfo=timezone.utc))
    await _segment(tx, gone, "zz1055_retired", "Ostrel Bay")
    await tx.execute("""INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at)
                        VALUES ('Quellan Gorge', 'US', 'serp', '2026-06-01'),
                               ('Brindle Falls', 'US', 'volume', '2026-06-01')""")
    ids = await landing_sweep.find_unlanded(_Pool(tx))
    planted = [i for i in ids if i.startswith("zz1055_")]
    assert planted == ["zz1055_never", "zz1055_before"]          # never-landed first
    await tx.fetchval(landing_sweep.ACTIVE_RECOMPUTE_SQL)          # runs on the real shared.job
    assert await tx.fetchval(landing_sweep.RECENT_SWEEP_SQL, landing_sweep.REASON, landing_sweep.MIN_GAP_H) is None


@pytest.mark.asyncio
async def test_seo_context_copy_merges_without_overwriting_newer_rows(tx):
    tour = await _tour(tx)
    await tx.execute("""INSERT INTO silver_aa_internal.seo_context (tour_id, tenant_id, keyword_search, keyword_ideas,
                        people_also_ask, related_keywords, cache_key, provider, fetched_at)
                        VALUES ($1, $2, 'zarvok trek', $3::jsonb, $4::jsonb, $5::jsonb, 'seo:zarvok trek:2840:v3',
                                'dataforseo', '2026-10-05')""",
                     tour, uuid.UUID(TENANT_ID),
                     json.dumps([{"keyword": "Zarvok Trek", "search_volume": 300},
                                 {"keyword": "zarvok lake", "search_volume": 50},
                                 {"keyword": "zarvok pass", "search_volume": None}]),
                     json.dumps(["how hard is the zarvok trek?"]), json.dumps(["zarvok trek map"]))
    # the store already holds a newer volume for one keyword
    await tx.execute("""INSERT INTO acp_contract.search_demand (keyword, market, search_volume, retrieved_on)
                        VALUES ('zarvok lake', 'US', 70, '2026-11-01')""")
    pool = _Pool(tx)
    dry = await copy.run_copy(pool)
    assert dry["apply"] is False and dry["volume_candidates"] >= 2
    assert await tx.fetchval("SELECT count(*) FROM acp_contract.search_demand WHERE keyword = 'zarvok trek'") == 0
    done = await copy.run_copy(pool, apply=True)
    assert done["written"] is True
    rows = {r["keyword"]: r for r in await tx.fetch(
        "SELECT keyword, search_volume, people_also_ask, related_searches, serp_location "
        "FROM acp_contract.search_demand WHERE market = 'US' AND keyword LIKE 'zarvok%'")}
    assert rows["zarvok lake"]["search_volume"] == 70                     # newer value kept
    assert rows["zarvok trek"]["search_volume"] == 300
    assert json.loads(rows["zarvok trek"]["people_also_ask"]) == ["how hard is the zarvok trek?"]
    assert json.loads(rows["zarvok trek"]["related_searches"]) == ["zarvok trek map"]
    assert rows["zarvok trek"]["serp_location"] == "US"
    assert "zarvok pass" not in rows


async def _card(conn, atom_id: str, key: str, *, locality=None, country="Brindland", status="auto_checked",
                facts=None) -> None:
    card_id = await conn.fetchval("""INSERT INTO acp_shared.place_card (place_key, display_name, locality, country,
                                     facts, status) VALUES ($1, $1, $2, $3, $4::jsonb, $5) RETURNING card_id""",
                                  key, locality, country, json.dumps(facts or []), status)
    await conn.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES ($1, $2)", atom_id, card_id)


@pytest.mark.asyncio
async def test_place_locations_read_the_majority_card_and_its_what_it_is_fact(tx):
    """AA-1055 (S274) — keyword_qualify reads where a place is from its place card (not a rejected one), else only
    the tour country."""
    tour = await _tour(tx)
    await _segment(tx, tour, "zz1055_q1", "Holy Trinity Chapel zz")
    await _card(tx, "zz1055_q1_atom", "holy trinity chapel zz|zarvoka|brindland", locality="Zarvoka",
                facts=[{"text": "A stone chapel.", "kind": "history"},
                       {"text": "Holy Trinity Chapel is a stone chapel.", "kind": "what_it_is"}])
    await _segment(tx, tour, "zz1055_q2", "Quellberry tree zz")
    await _card(tx, "zz1055_q2_atom", "quellberry tree zz|brindland", status="rejected",
                facts=[{"text": "Quellberry is a species of palm.", "kind": "what_it_is"}])
    await _segment(tx, tour, "zz1055_q3", "Golden Lotus Hotel zz")
    where = await rb.load_locations(_Pool(tx), ["Holy Trinity Chapel zz", "Quellberry tree zz",
                                                "Golden Lotus Hotel zz"])
    chapel = where["Holy Trinity Chapel zz"]
    assert (chapel.locality, chapel.country, chapel.what_it_is) == (
        "Zarvoka", "Brindland", "Holy Trinity Chapel is a stone chapel.")
    # the rejected card is ignored; both fall back to the tour country (tidied)
    assert where["Quellberry tree zz"].what_it_is is None and where["Quellberry tree zz"].country == "mongolia"
    assert where["Golden Lotus Hotel zz"].country == "mongolia"


@pytest.mark.asyncio
async def test_rule_rows_round_trip_and_plan_requalifies_only_affected_fresh_places(tx):
    pool = _Pool(tx)
    tour = await _tour(tx)
    await _segment(tx, tour, "zz1055_r1", "Holy Trinity Chapel")
    await _segment(tx, tour, "zz1055_r2", "Zarvok Temple")
    await tx.execute("""INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, rank, source,
                        prompt_version, jev_zone) VALUES
                        ('place', 'Holy Trinity Chapel', 'holy trinity chapel', 0, 'place_name', 'a3_propose@old',
                         'not_asked'),
                        ('place', 'Zarvok Temple', 'zarvok temple', 0, 'place_name', 'a3_propose@old', 'not_asked')""")
    await tx.execute("""INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at)
                        SELECT p, 'US', t, now() FROM unnest(ARRAY['Holy Trinity Chapel', 'Zarvok Temple']) p,
                                                       unnest(ARRAY['volume', 'serp']) t""")
    stale = await sr._plan(pool, {"Holy Trinity Chapel": ["visit"], "Zarvok Temple": ["visit"]}, ["US"],
                           ("volume", "serp"))
    assert [(p.name, p.requalify, p.markets, p.serp) for p in stale] == [("Holy Trinity Chapel", True, ["US"], True)]

    p = stale[0]
    p.plain, p.dropped = "holy trinity chapel zarvoka", {"holy trinity chapel": "unqualified"}
    await rb._store_maps([p], {"Holy Trinity Chapel": ["holy trinity chapel zarvoka"]}, pool)
    rows = (await rb.load_maps(pool, ["Holy Trinity Chapel"]))["Holy Trinity Chapel"]
    assert [(r["keyword"], r["jev_zone"], r["jev_mode"]) for r in rows] == [
        ("holy trinity chapel zarvoka", "not_asked", None), ("holy trinity chapel", "skipped", "rule:unqualified")]
    assert rb.keywords_from_map(rows) == ["holy trinity chapel zarvoka"]
    # rebuilt under the current prompt: the next plan leaves it alone
    assert await sr._plan(pool, {"Holy Trinity Chapel": ["visit"]}, ["US"], ("volume", "serp")) == []
