"""AA-1055 (S274) — the read-only research quality report (scripts/aa1055/research_quality.py) on the real schema.

One invented segment_research job with a queued run's rows (bought volumes, SERPs, research log, keyword map, ranking,
ledger) plus an older S1 seo_context purchase; the report runs as cis_app (the container's role) and prints the
volume agreement, SERP richness, map kept / rejected, demand landing and the bill, and a sample of at most SAMPLE
keyword|place lines between its markers. Every row is dated in early 2001 (a window no other test writes in), inside
a transaction that is rolled back. Invented names and places only.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID  # noqa: E402
from scripts.aa1055 import research_quality as rq  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_ID)
    yield c
    await t.rollback()
    await c.close()


async def _fixtures(conn) -> str:
    job = await conn.fetchval(
        "INSERT INTO shared.job (kind, payload, status, result, created_at, started_at, finished_at) "
        "VALUES ('segment_research', $1::jsonb, 'succeeded', $2::jsonb, '2001-03-01 10:00+00', "
        "'2001-03-01 10:00+00', '2001-03-01 12:00+00') RETURNING id::text",
        json.dumps({"markets": ["US", "UK"], "dfs_queue": "standard"}),
        json.dumps({"dfs_queue": "standard", "places_selected": 2,
                    "estimate": {"dfs_usd": 0.5, "dfs_usd_cap": 0.8,
                                 "unit_usd": {"volume_task": 0.06, "serp": 0.0006}}}))
    await conn.executemany(
        "INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, keyword_count, job_id, stage, "
        "app) VALUES ($1, $2, false, true, $3, $4::uuid, $5, 'cis')",
        [("search_volume_bulk_queue", 0.06, 6, job, "a3_search_demand"),
         ("serp_advanced_queue", 0.0012, 2, job, "a3_search_demand"),
         ("appendix_user_data", 0.0, 0, job, "dfs_balance")])
    # the run's purchases: volumes in the window (US, UK), SERPs on the US rows
    await conn.executemany(
        "INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, retrieved_on, "
        "serp_domains, related_searches, paa_retrieved_on, serp_location) "
        "VALUES ($1, $2, $3, $4::jsonb, '2001-03-01 10:30+00', $5::jsonb, $6::jsonb, $7, $8)",
        [("zzq zarvok lake", "US", 50, json.dumps(["a?", "b?", "c?"]), json.dumps(["x.test", "y.test"]),
          json.dumps(["zzq zarvok lake map"]), None, None),
         ("zzq zarvok lake hike", "US", 110, "[]", None, None, None, None),
         ("zzq melnith gorge", "US", 0, "[]", None, None, None, None),
         ("zzq zarvok lake", "UK", 20, "[]", None, None, None, None)])
    await conn.execute("UPDATE acp_contract.search_demand SET paa_retrieved_on = '2001-03-01 10:40+00', "
                       "serp_location = 'US' WHERE keyword = 'zzq zarvok lake' AND market = 'US'")
    await conn.execute(
        "INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, retrieved_on, "
        "related_searches, paa_retrieved_on, serp_location) VALUES ('zzq melnith gorge trail', 'US', NULL, "
        "'[\"d?\"]'::jsonb, 'epoch', '[]'::jsonb, '2001-03-01 10:50+00', 'US')")
    # an older row outside the window is not the run's
    await conn.execute("INSERT INTO acp_contract.search_demand (keyword, market, search_volume, retrieved_on) "
                       "VALUES ('zzq vorrin peak', 'US', 900, '2001-01-15 10:00+00')")
    # S1's older live purchase (US): one equal volume, one 10 % off, one keyword the run did not buy
    tour = uuid.uuid4()
    await conn.execute("""INSERT INTO silver_aa_internal.raw_tours
                          (tour_id, tenant_id, batch_id, src_name, country, source_group_id, source_status)
                          VALUES ($1, $2, $3, 'Zarvok trek zz1055q', 'Mongolia', $4, 'active')""",
                       tour, uuid.UUID(TENANT_ID), uuid.UUID(BATCH_ID), uuid.uuid4())
    await conn.execute(
        """INSERT INTO silver_aa_internal.seo_context (tour_id, tenant_id, keyword_search, keyword_ideas,
           people_also_ask, related_keywords, cache_key, provider, fetched_at)
           VALUES ($1, $2, 'zzq zarvok lake', $3::jsonb, '[]'::jsonb, '[]'::jsonb, 'seo:zzq zarvok lake:2840:v3',
                   'dataforseo', '2001-02-01 10:00+00')""",
        tour, uuid.UUID(TENANT_ID),
        json.dumps([{"keyword": "zzq zarvok lake", "search_volume": 50},
                    {"keyword": "zzq zarvok lake hike", "search_volume": 100},
                    {"keyword": "zzq vorrin peak", "search_volume": 900}]))
    # the places the run logged, their maps and their Segments' ranking
    await conn.executemany(
        "INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at) "
        "VALUES ($1, $2, $3, '2001-03-01 11:00+00')",
        [("Zzq Zarvok Lake", "US", "volume"), ("Zzq Zarvok Lake", "US", "serp"), ("Zzq Zarvok Lake", "UK", "volume"),
         ("Zzq Melnith Gorge", "US", "volume")])
    await conn.executemany(
        "INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, rank, source, prompt_version, "
        "jev_zone, jev_mode, proposed_at) VALUES ('place', $1, $2, $3, 'llm_propose', 'a3_propose@zzq', $4, $5, now())",
        [("Zzq Zarvok Lake", "zzq zarvok lake", 1, "accept", "enforce"),
         ("Zzq Zarvok Lake", "zzq zarvok lake hike", 2, "grey", "enforce"),
         ("Zzq Zarvok Lake", "zzq lake hotels", 3, "reject", "enforce"),
         ("Zzq Melnith Gorge", "zzq melnith gorge", 1, "accept", "enforce"),
         ("Zzq Melnith Gorge", "zzq gorge", 2, "reject", "shadow")])
    await conn.executemany(
        "INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
        "VALUES ($1, $2, 'visit')",
        [("zzq_seg_lake", "Zzq Zarvok Lake"), ("zzq_seg_gorge", "Zzq Melnith Gorge")])
    await conn.executemany(
        "INSERT INTO acp_contract.atom_ranking (market, tour_id, segment_id, demand_volume, recurrence, questions, "
        "said, computed_at, version) VALUES ($1, $2, $3, $4, 1, 1, 1, $5, 1)",
        [("US", tour, "zzq_seg_lake", 160, datetime(2001, 3, 1, 12, 30, tzinfo=timezone.utc)),
         ("UK", tour, "zzq_seg_lake", None, datetime(2001, 2, 20, 10, tzinfo=timezone.utc)),
         ("US", tour, "zzq_seg_gorge", None, datetime(2001, 2, 20, 10, tzinfo=timezone.utc))])
    return job


def _metrics(text: str) -> dict:
    return {line[3:].split("=", 1)[0]: line.split("=", 1)[1] for line in text.splitlines() if line.startswith("RQ ")}


@pytest.mark.asyncio
async def test_report_reads_every_section_as_cis_app(tx, capsys):
    job = await _fixtures(tx)
    # The replay's cis_app lacks some of the live grants (db-schema-reference: SELECT on each of these for cis_app);
    # granted inside the rolled-back transaction only.
    await tx.execute("GRANT USAGE ON SCHEMA shared, acp_contract, silver_aa_internal TO cis_app; "
                     "GRANT SELECT ON shared.job, shared.dfs_call_log, acp_contract.search_demand, "
                     "acp_contract.segment_research_log, acp_contract.place_keyword, acp_contract.atom_segment, "
                     "acp_contract.atom_ranking, silver_aa_internal.seo_context TO cis_app")
    await tx.execute("SET LOCAL ROLE cis_app")
    await rq.report(tx, job, 30)
    text = capsys.readouterr().out
    assert "ERR " not in text, text
    m = _metrics(text)
    assert (m["job.queue"], m["job.markets"], m["job.estimate_dfs_usd_cap"]) == ("standard", "US,UK", "0.8000")
    assert m["bill.a3_search_demand.search_volume_bulk_queue"].startswith("rows=1 cost=0.0600")
    assert m["bill.no_stage_rows"] == "0"
    # (a) the run bought 3 US + 1 UK volumes in the window; 2 US have an older S1 value: one equal, one 10 % off
    assert (m["volume.US.bought_keywords"], m["volume.UK.bought_keywords"]) == ("3", "1")
    assert m["volume.US.vs_live.n"] == "2"
    assert (m["volume.US.vs_live.equal_share"], m["volume.US.vs_live.near_share"]) == ("0.500", "1.000")
    assert m["volume.US.vs_live.median_rel_diff"] == "0.050"
    assert m["volume.UK.vs_live.n"] == "0"
    # (b) two SERPs stored in the window (US): one with 3 PAA + related + 2 domains, one with 1 PAA and nothing else
    assert m["serp.US.serps"] == "2"
    assert (m["serp.US.paa_ge3_share"], m["serp.US.related_ge1_share"]) == ("0.500", "0.500")
    assert (m["serp.US.domains_ge1_share"], m["serp.US.distinct_domains"]) == ("0.500", "2")
    # (c) US: 2 places, 5 keywords, 1 enforced reject (the shadow reject is kept); UK: 1 place, 3 keywords
    assert (m["map.US.places"], m["map.US.keywords"], m["map.US.rejected"], m["map.US.kept"]) == ("2", "5", "1", "4")
    assert m["map.US.rejected_not_enforced"] == "1"
    assert (m["map.UK.places"], m["map.UK.kept_share"]) == ("1", "0.667")
    # (d) US: both Segments ranked, the lake one recomputed since the run with demand; UK: no demand yet
    assert (m["landing.US.segments_ranked"], m["landing.US.with_demand"]) == ("2", "1")
    assert m["landing.US.recomputed_with_demand"] == "1"
    assert (m["landing.UK.segments_ranked"], m["landing.UK.with_demand"]) == ("1", "0")
    # the sample: between markers, keyword|place|kept|rejected, never more than asked
    us = text.split("=== RQ SAMPLE BEGIN market=US n=5 ===\n", 1)[1].split("=== RQ SAMPLE END market=US ===", 1)[0]
    lines = us.strip().splitlines()
    assert len(lines) == 5 and all(line.count("|") == 2 for line in lines)
    assert "zzq lake hotels|Zzq Zarvok Lake|rejected" in lines


@pytest.mark.asyncio
async def test_report_caps_the_sample_and_knows_a_missing_job(tx, capsys):
    job = await _fixtures(tx)
    await rq.report(tx, job, 2)
    text = capsys.readouterr().out
    assert "=== RQ SAMPLE BEGIN market=US n=2 ===" in text
    await rq.report(tx, str(uuid.uuid4()), 30)
    assert "RQ job=not_found" in capsys.readouterr().out
