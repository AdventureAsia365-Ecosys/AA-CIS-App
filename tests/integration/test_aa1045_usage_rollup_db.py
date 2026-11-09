"""AA-1045 — the hourly usage rollup and the usage explorer against real Postgres (migrations applied by conftest).

- CIS 372: both tables, the rollup key (NULLS NOT DISTINCT), cis_app's grants; re-applying the file is a no-op.
- roll_hours: the rollup of every ledger equals the ledgers' own totals for the same hours (check_totals), a second
  run writes the same rows (idempotent per hour), a late row is counted by the next re-roll; Jev's typesafe rows of
  the LLM log are left out, as in the Calls tab.
- explore: provider x stage, filters, p95 and fallback rate, the time series, the CRM's daily totals merged, cost
  per tour from the raw LLM ledger; the Calls list of one cell (the drill-down) holds the cell's rows exactly.
- saved views: create, replace by name, list per staff member, delete only one's own; invoices read the rollup.

The fixture rows live in August 2024 (no other test writes there) and are deleted at the end. Names are invented.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import ai_gateway_calls as calls  # noqa: E402
from api.services import usage_explorer as ue  # noqa: E402
from api.services import usage_rollup as ur  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "t9_aa1045_probe"            # product tenant_content (prefix t9_)
STAGE2 = "a3_aa1045_probe"           # product catalogue
EP = "aa1045_probe_endpoint"
GEO = "aa1045_probe_geo"
TENANT = "6f9619ff-8b86-d011-b42d-00c04fc96045"
TENANT_NAME = "Ember Coast Tours"
STAFF_A = "00000000-0000-0000-0000-00000000a045"
STAFF_B = "00000000-0000-0000-0000-00000000b045"
T0 = datetime(2024, 8, 10, 0, tzinfo=timezone.utc)
T1 = datetime(2024, 8, 11, 0, tzinfo=timezone.utc)
NOW = datetime(2024, 8, 12, 3, tzinfo=timezone.utc)
MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")


def _at(h, m=0):
    return T0 + timedelta(hours=h, minutes=m)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with p.acquire() as c:
        # test_aa712_migration_226 drops schema geo and re-applies 226 only: put back the geocode_call columns of 255,
        # 319 and 346 (all idempotent), then 372 (idempotent, creates the two tables if a test dropped them).
        for name in ("255_geo_place_shape.sql", "319_llm_ledger_v2.sql", "346_geocode_call_ledger_columns.sql",
                     "372_ai_usage_hourly_and_saved_views.sql"):
            with open(os.path.join(MIGRATIONS, name), encoding="utf-8") as f:
                await c.execute(f.read())
        await _cleanup(c)
        await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                        TENANT, TENANT_NAME, "aa1045-ember-coast")
    yield p
    async with p.acquire() as c:
        await _cleanup(c)
    await p.close()


async def _cleanup(c):
    await c.execute("DELETE FROM shared.llm_call_log WHERE stage = ANY($1::text[])", [STAGE, STAGE2])
    await c.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = $1", EP)
    await c.execute("DELETE FROM geo.geocode_call WHERE provider = $1", GEO)
    await c.execute("DELETE FROM shared.decision_log WHERE stage = $1", STAGE)
    await c.execute("DELETE FROM shared.ai_usage_hourly WHERE hour >= $1 AND hour < $2", T0 - timedelta(days=1),
                    T1 + timedelta(days=2))
    await c.execute("DELETE FROM shared.ai_usage_saved_view WHERE staff_id = ANY($1::uuid[])", [STAFF_A, STAFF_B])
    await c.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", TENANT)


async def _llm(c, at, cost, *, stage=STAGE, provider="bedrock-satellite", connection="acc-probe", model="probe-model",
               status="ok", attempt_no=1, fallback_used=False, latency=400, tenant=None, tour=None, shadow=None,
               error_class=None, tokens=(10, 20)):
    await c.execute(
        """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, provider, connection_key,
                                            created_at, tokens_in, tokens_out, status, attempt_no, fallback_used,
                                            latency_ms, tenant_id, tour_id, is_shadow, error_class, app)
           VALUES ($1, 'writer', $2, $3, '{}'::jsonb, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::uuid, $14::uuid,
                   $15, $16, 'cis')""",
        stage, model, cost, provider, connection, at, tokens[0], tokens[1], status, attempt_no, fallback_used,
        latency, tenant, tour, shadow, error_class)


async def _fixture_rows(c):
    tour_a, tour_b = "6f9619ff-8b86-d011-b42d-00c04fc9a001", "6f9619ff-8b86-d011-b42d-00c04fc9a002"
    await _llm(c, _at(1, 5), 0.010, tenant=TENANT, tour=tour_a, latency=300)
    await _llm(c, _at(1, 10), 0.020, tenant=TENANT, tour=tour_a, latency=900)
    await _llm(c, _at(1, 20), 0.030, tenant=TENANT, tour=tour_b, attempt_no=2, latency=1800)    # a fallback answer
    await _llm(c, _at(1, 25), 0.0, tenant=TENANT, status="refused", error_class="credential", latency=50,
               provider="anthropic", connection="anthropic-api", tokens=(0, 0))
    await _llm(c, _at(2, 0), 0.040, stage=STAGE2, fallback_used=True, latency=2500, shadow=True)
    await _llm(c, _at(2, 5), 0.009, provider="typesafe", connection=None)       # Jev's own row: the jev source's
    await c.execute(
        """INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, keyword_count, created_at,
                                            status, latency_ms, stage, app)
           VALUES ($1, 0.05, false, true, 3, $2, 'ok', 700, $3, 'cis')""", EP, _at(1, 30), STAGE)
    await c.execute(
        """INSERT INTO shared.decision_log (created_at, stage, question_key, subject_key, mode, zone, model,
                                            latency_ms, cost_usd, error, cached)
           VALUES ($1, $2, 'aa1045_q', 'aa1045_subject', 'shadow', 'accept', 'probe-jev', 350, 0.001, NULL, false)""",
        _at(1, 40), STAGE)
    await c.execute(
        """INSERT INTO geo.geocode_call (called_at, provider, requests, cost_usd, endpoint, credits, status, stage,
                                         app, latency_ms)
           VALUES ($1, $2, 4, 0, 'search', 4, 'ok', $3, 'cis', 120)""", _at(1, 45), GEO, STAGE)


async def _ours(pool):
    """The rollup's rows of our fixture (other tests may write ledger rows in the same hours)."""
    async with pool.acquire() as c:
        return [dict(r) for r in await c.fetch(
            """SELECT * FROM shared.ai_usage_hourly WHERE hour >= $1 AND hour < $2
                AND (stage = ANY($3::text[]) OR account = $4) ORDER BY hour, source, stage, status""",
            T0, T1, [STAGE, STAGE2], GEO)]


@pytest.mark.asyncio
async def test_migration_372_tables_grants_and_reapply(pool):
    async with pool.acquire() as c:
        for t in ("shared.ai_usage_hourly", "shared.ai_usage_saved_view"):
            assert await c.fetchval("SELECT to_regclass($1) IS NOT NULL", t)
            if await c.fetchval("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cis_app')"):
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert await c.fetchval("SELECT has_table_privilege('cis_app', $1, $2) AS ok", t, priv)
        assert await c.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '372'") == 1
        with open(os.path.join(MIGRATIONS, "372_ai_usage_hourly_and_saved_views.sql"), encoding="utf-8") as f:
            await c.execute(f.read())        # re-apply: no error, nothing new
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("""INSERT INTO shared.ai_usage_hourly (hour, source, app, provider, status, traffic,
                                   latency_hist) VALUES ($1, 'llm', 'cis', 'x', 'ok', 'live', '{1}')""", T0)
        hist = [0] * 20
        ins = """INSERT INTO shared.ai_usage_hourly (hour, source, app, provider, status, traffic, latency_hist)
                 VALUES ($1, 'llm', 'cis', 'x', 'ok', 'live', $2)"""
        await c.execute(ins, T0 - timedelta(hours=3), hist)
        with pytest.raises(asyncpg.UniqueViolationError):     # NULL dimensions are one key (NULLS NOT DISTINCT)
            await c.execute(ins, T0 - timedelta(hours=3), hist)
        with pytest.raises(asyncpg.CheckViolationError):      # not a whole hour
            await c.execute(ins, T0 - timedelta(hours=3, minutes=-5), hist)


@pytest.mark.asyncio
async def test_rollup_equals_the_ledgers_idempotent_and_late_rows(pool):
    async with pool.acquire() as c:
        await _fixture_rows(c)
    out = await ur.roll_hours(pool, T0, T1)
    assert out["hours"] == 24
    check = await ur.check_totals(pool, T0, T1)
    assert check["ok"], check
    rows = await _ours(pool)
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    assert set(by_source) == {"llm", "jev", "dfs", "geocoder"}
    assert sum(r["ledger_rows"] for r in by_source["llm"]) == 5          # the typesafe row is not an llm row
    assert [r["requests"] for r in by_source["geocoder"]] == [4]          # a geocoder row is a batch of requests
    assert by_source["geocoder"][0]["account"] == GEO and by_source["dfs"][0]["model"] == EP
    llm = {(r["stage"], r["status"], r["traffic"], r["provider"]): r for r in by_source["llm"]}
    live = llm[(STAGE, "ok", "live", "bedrock")]
    assert live["requests"] == 3 and live["fallback_requests"] == 1 and str(live["tenant_id"]) == TENANT
    assert live["latency_ms_count"] == 3 and sum(live["latency_hist"]) == 3
    assert llm[(STAGE2, "ok", "shadow", "bedrock")]["fallback_requests"] == 1
    assert llm[(STAGE, "refused", "live", "anthropic")]["error_class"] == "credential"

    # idempotent per hour
    before = await _ours(pool)
    await ur.roll_hours(pool, T0, T1)
    after = await _ours(pool)
    strip = [{k: v for k, v in r.items() if k != "refreshed_at"} for r in before]
    assert strip == [{k: v for k, v in r.items() if k != "refreshed_at"} for r in after]

    # a late row (written after the roll, in an hour already rolled) is counted by the next re-roll of that hour
    async with pool.acquire() as c:
        await _llm(c, _at(1, 50), 0.5, tenant=TENANT)
    assert not (await ur.check_totals(pool, T0, T1))["ok"]
    await ur.roll_hours(pool, _at(1), _at(2))
    assert (await ur.check_totals(pool, T0, T1))["ok"]


def _q(**params):
    return ue.parse_query({"range": "custom", "from": "2024-08-10", "to": "2024-08-10", **params})


@pytest.mark.asyncio
async def test_explore_groups_filters_series_crm_and_drill_down(pool):
    async with pool.acquire() as c:
        await _fixture_rows(c)
    # the ICT day 2024-08-10 is [2024-08-09 17:00, 2024-08-10 17:00) UTC: roll the hours around it
    await ur.roll_hours(pool, T0 - timedelta(hours=7), T1)

    crm_calls = []

    async def crm_reader(a, b):
        crm_calls.append((a, b))
        return {"rows": [{"day": "2024-08-10", "stage": "crm_draft_reply", "model": "probe-model", "calls": 3,
                          "failed_calls": 1, "input_tokens": 30, "output_tokens": 15, "cost_usd": "0.120000"}]}, {}

    q = _q(group="provider,stage", **{"f_stage": f"{STAGE},{STAGE2},crm_draft_reply"})
    out = await ue.explore(pool, q, crm_usage_rows=crm_reader, now=NOW)
    assert out["crm"]["status"] == "ok" and crm_calls == [(date(2024, 8, 10), date(2024, 8, 10))]
    rows = {(r["dims"]["provider"], r["dims"]["stage"]): r for r in out["rows"]}
    bedrock = rows[("bedrock", STAGE)]
    assert bedrock["requests"] == 3 and bedrock["fallback_rate"] == round(1 / 3, 4)
    assert 1500 <= bedrock["p95_latency_ms"] <= 2000 and bedrock["error_rate"] == 0
    assert rows[("anthropic", STAGE)]["error_rate"] == 1.0
    assert rows[("jev", STAGE)]["requests"] == 1 and rows[("geocoder", STAGE)]["requests"] == 4
    crm = rows[("", "crm_draft_reply")]
    assert crm["requests"] == 3 and crm["error_rate"] == round(1 / 3, 4) and crm["cost_usd"] == 0.12
    costs = [r["cost_usd"] for r in out["rows"]]
    assert costs == sorted(costs, reverse=True) and out["rows"][0]["dims"]["stage"] == "crm_draft_reply"
    assert out["series"] and sum(s["requests"] for s in out["series"]) == sum(r["requests"] for r in out["rows"])
    assert out["totals"]["requests"] == sum(r["requests"] for r in out["rows"])

    # a tenant filter: only LLM rows of the tenant; the CRM is not asked (it has no CIS tenant)
    q = _q(group="model", f_tenant=TENANT, per_unit="1")
    out = await ue.explore(pool, q, crm_usage_rows=crm_reader, now=NOW)
    assert out["crm"]["reason"] == "filter_tenant" and out["labels"]["tenant"] == {TENANT: TENANT_NAME}
    row = out["rows"][0]
    assert row["dims"]["model"] == "probe-model" and row["requests"] == 4
    assert row["tours"] == 2 and row["cost_per_tour"] == round(0.06 / 2, 6)

    # product and family dimensions, time buckets of an hour
    q = _q(group="product,time", granularity="hour", f_source="llm")
    out = await ue.explore(pool, q, now=NOW)
    prods = {(r["dims"]["product"], r["bucket"]["label"]) for r in out["rows"] if r["dims"]["product"] != "other"}
    assert ("tenant_content", "2024-08-10T08:00+07:00") in prods and ("catalogue", "2024-08-10T09:00+07:00") in prods
    out = await ue.explore(pool, _q(group="family", f_stage=STAGE), now=NOW)
    assert out["rows"]

    # the drill-down: the Calls list of the cell (provider bedrock, stage, the day) holds exactly the cell's rows
    since, until = ue.resolve_range(_q(), NOW)
    f = calls.parse_filters(since=since.isoformat(), until=until.isoformat(), provider="bedrock", stage=STAGE,
                            tenant=TENANT, model="probe-model", account="acc-probe", product="tenant_content")
    listed = await calls.list_calls(pool, f, now=NOW, limit=200)
    assert listed["errors"] == [] and listed["window"]["key"] == "custom"
    assert len([r for r in listed["rows"] if r["source"] == "llm"]) == bedrock["requests"]


@pytest.mark.asyncio
async def test_drill_down_of_a_cut_day_bucket_stays_inside_the_range(pool):
    """A 24h range at day granularity cuts its day buckets: the cell and its Calls list count the same rows."""
    async with pool.acquire() as c:
        await _fixture_rows(c)
        await _llm(c, _at(3), 0.070)         # 10:00 ICT: same ICT day and cell, after the range's end
    await ur.roll_hours(pool, T0 - timedelta(hours=7), T1)
    asked_at = _at(1, 15)                    # 08:15 ICT: the range is [08-09 09:00, 08-10 09:00) ICT
    q = ue.parse_query({"range": "24h", "granularity": "day", "group": "provider,time", "f_stage": STAGE})
    out = await ue.explore(pool, q, now=asked_at)
    cell = next(r for r in out["rows"] if r["dims"]["provider"] == "bedrock")
    assert cell["requests"] == 3
    assert cell["bucket"] == {"start": "2024-08-10T00:00:00+07:00", "end": "2024-08-10T09:00:00+07:00",
                              "label": "2024-08-10–2024-08-10 09:00", "partial": True}
    assert cell["dims"]["time"] == cell["bucket"]["label"]
    assert [p["partial"] for p in out["series"]] == [True]
    f = calls.parse_filters(since=cell["bucket"]["start"], until=cell["bucket"]["end"], provider="bedrock",
                            stage=STAGE)
    listed = await calls.list_calls(pool, f, now=NOW, limit=200)    # opened later: the late row exists by then
    assert len([r for r in listed["rows"] if r["source"] == "llm"]) == cell["requests"]


@pytest.mark.asyncio
async def test_options_invoices_and_saved_views(pool):
    async with pool.acquire() as c:
        await _fixture_rows(c)
    await ur.roll_hours(pool, T0 - timedelta(hours=7), T1)
    opts = await ue.options(pool, _q(), now=NOW)
    assert STAGE in opts["values"]["stage"] and "crm" in opts["values"]["source"] and TENANT in opts["values"]["tenant"]
    inv = await ue.invoices(pool, "2024-08", now=NOW)
    acc = {r["account"]: r for r in inv["rows"]}
    assert acc["acc-probe"]["ledger_usd"] >= 0.1 and acc["acc-probe"]["invoice_usd"] is None and inv["missing"]

    v1 = await ue.save_view(pool, STAFF_A, "Weekly cost", {"range": "7d", "group": "provider"})
    v2 = await ue.save_view(pool, STAFF_A, "weekly COST", {"range": "30d", "group": "stage"})   # same name: replaced
    assert v1["id"] == v2["id"] and v2["query"] == {"range": "30d", "group": "stage"}
    await ue.save_view(pool, STAFF_B, "Weekly cost", {"range": "7d"})
    mine = await ue.list_views(pool, STAFF_A)
    assert [v["name"] for v in mine] == ["weekly COST"]
    assert not await ue.delete_view(pool, STAFF_B, v1["id"])           # not B's
    assert await ue.delete_view(pool, STAFF_A, v1["id"])
    assert await ue.list_views(pool, STAFF_A) == []
    for i in range(ue.VIEW_LIMIT):
        await ue.save_view(pool, STAFF_A, f"v{i}", {"range": "7d"})
    with pytest.raises(LookupError):
        await ue.save_view(pool, STAFF_A, "one too many", {"range": "7d"})
    assert json.dumps(mine[0]["query"])
