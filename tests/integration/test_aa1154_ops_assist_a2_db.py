"""AA-1154 (A2) — the assistant's run record and its metric reads on a real Postgres (conftest replays every migration):
a card marked as done (baselines read, the open incident linked, a second click refused), the sampler's checkpoints
(+10 then +30, the verdict), the hit rate and the track record, every metric SQL against today's columns, all read
and written as cis_app. Needs CIS 490 (acp_shared.ops_assist_run): skipped on a branch whose migrations do not have it
yet (the code PR is tested again after the migration PR merges). Runs in a transaction that is rolled back; invented
values only."""
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import ops_assist  # noqa: E402
from api.services.ops_assist import outcome  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
LIMITS = {"evidence_window_min": 30, "changes_window_min": 120, "refused_trigger_calls": 3,
          "throttled_trigger_calls": 3, "spend_spike_factor": 3.0, "spend_baseline_days": 7,
          "spend_spike_floor_usd_h": 0.1, "wide_scope_stages": 5, "small_money_usd": 0.1, "history_min_n": 5,
          "card_ttl_min": 30, "reason_min_chars": 20, "auto_severities": ["sev1", "sev2"], "auto_incident_gap_min": 240,
          "deep_model": "sonnet-5-5", "lookup_windows_min": [15, 60, 360, 1440],
          "check_after_min": [10, 30], "digest_days": 7, "build_timeout_s": 20}
JOB_ID = "00000000-0000-0000-0000-00000000a154"
WAVE_ID = "00000000-0000-0000-0000-00000000b154"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    if not await c.fetchval("SELECT to_regclass('acp_shared.ops_assist_run') IS NOT NULL AS ok"):
        await c.close()
        pytest.skip("CIS 490 (acp_shared.ops_assist_run) is not in this branch's migrations yet")
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _as_cis_app(conn) -> None:
    # The test database has no Dev grants (Dev: cis_app reads these tables and writes the run record,
    # db-schema-reference); give them inside the rolled-back transaction only.
    await conn.execute("GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app")
    await conn.execute("GRANT SELECT ON shared.llm_call_log, shared.job, shared.rerun_wave, acp_shared.ops_event, "
                       "acp_shared.ops_sample, acp_shared.ops_incident TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE ON acp_shared.ops_assist_run TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")


async def _seed(conn, now: datetime) -> None:
    await conn.execute(
        "INSERT INTO shared.job (id, kind, payload, status, run_after, created_at) "
        "VALUES ($1::uuid, 'zz_kind', '{}'::jsonb, 'queued', $2, $2)", JOB_ID, now - timedelta(minutes=40))
    await conn.execute(
        "INSERT INTO shared.rerun_wave (id, name, expected_tour_ids, status) "
        "VALUES ($1::uuid, 'zz wave', ARRAY['00000000-0000-0000-0000-00000000c154']::uuid[], 'open')", WAVE_ID)
    for i, (status, cls) in enumerate((("refused", "access_denied"), ("refused", "access_denied"), ("ok", None))):
        await conn.execute(
            "INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, created_at, connection_key, status, "
            "error_class, cost_usd, app) VALUES ('zz_stage', 'writer', 'zz-model', '{}'::jsonb, $1, 'zz-conn', $2, $3, "
            "0.01, 'cis')", now - timedelta(minutes=5 + i), status, cls)
    await conn.execute(
        "INSERT INTO acp_shared.ops_sample (sampled_at, kind, subject, app, state, value) VALUES "
        "($1, 'queue', 'shared.job', 'cis', 'warn', '{\"live_workers\": 2}'::jsonb)", now - timedelta(minutes=2))
    await conn.execute(
        "INSERT INTO acp_shared.ops_incident (title, severity, app, item_id, owner, declared_by) "
        "VALUES ('zz stuck queue', 'sev2', 'cis', 'queue-shared', 'admin:zz', 'admin:zz')")


def _answer() -> dict:
    s = {"as_of": "2026-11-25T09:00:00+00:00", "range": "24h", "item_id": "queue-shared",
         "window": {"since": "2026-11-25T08:30:00+00:00", "minutes": 30},
         "changes_window": {"since": "2026-11-25T07:00:00+00:00", "minutes": 120}, "status": None,
         "item": {"id": "queue-shared", "severity": "sev2", "app": "cis", "title": "zz queue", "detail": "",
                  "since": None, "source": "test", "status": "open", "links": [], "msg": None},
         "connections": [], "calls": {}, "spend": {}, "routes": None, "route_versions": [],
         "jobs": {"denied": [], "stuck": [{"id": JOB_ID, "kind": "zz_kind", "run_after": None, "registered": False}],
                  "queues": []},
         "waves": [], "probe": {"switch_on": False, "last": None}, "changes": [], "history": {}, "unavailable": {}}
    return ops_assist.analyse_snapshot(copy.deepcopy(s), LIMITS)


@pytest.mark.asyncio
async def test_a_card_marked_as_done_is_checked_and_counted(conn):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await _seed(conn, now)
    await _as_cis_app(conn)
    a = _answer()
    card = next(c for c in a["cards"] if c["action"] == "job_cancel")
    run = await outcome.record(conn, a, card, actor="admin:zz", limits=LIMITS, now=now)
    assert run["incident_id"] is not None
    assert [m["baseline"] for m in run["measures"]][0] == 1.0          # the job is queued
    assert run["measures"][1]["baseline"] >= 39.0                       # the oldest wait, minutes
    with pytest.raises(outcome.AlreadyRecorded):
        await outcome.record(conn, a, card, actor="admin:zz", limits=LIMITS, now=now + timedelta(minutes=1))

    await conn.execute("RESET ROLE")
    await conn.execute("UPDATE shared.job SET status = 'cancelled', finished_at = now() WHERE id = $1::uuid", JOB_ID)
    await conn.execute("SET LOCAL ROLE cis_app")
    first = await outcome.check_due(conn, limits=LIMITS, now=now + timedelta(minutes=11))
    assert first == {"checked": 1, "finished": 0}
    last = await outcome.check_due(conn, limits=LIMITS, now=now + timedelta(minutes=31))
    assert last == {"checked": 1, "finished": 1}
    row = await conn.fetchrow("SELECT verdict, checks, checked_at FROM acp_shared.ops_assist_run WHERE run_id = $1",
                              run["run_id"])
    assert row["verdict"] == "hit" and row["checked_at"] is not None
    assert [c["after_min"] for c in json.loads(row["checks"])] == [10, 30]

    t = await outcome.track(conn, days=7, now=now + timedelta(minutes=40))
    assert (t["recorded"], t["checked"], t["hits"]) == (1, 1, 1) and "right 1 of 1" in t["text"]
    assert (await outcome.history(conn))["job_cancel"] == {"n": 1, "hits": 1}
    listed = await outcome.runs(conn, "queue-shared")
    assert listed[0]["run_id"] == run["run_id"] and listed[0]["verdict"] == "hit"


@pytest.mark.asyncio
async def test_every_metric_reads_against_todays_columns(conn):
    now = datetime.now(timezone.utc)
    await _seed(conn, now)
    await _as_cis_app(conn)
    ctx = {"now": now, "since": now - timedelta(hours=1), "limits": LIMITS, "memo": {}}
    got = {}
    for ms in (outcome.m("refused_calls", "down", "zz", connection_key="zz-conn"),
               outcome.m("throttled_calls", "down", "zz", connection_key="zz-conn"),
               outcome.m("answered_calls", "nonzero", "zz", connections=["zz-conn"]),
               outcome.m("spend_per_hour", "down", "zz", connections=["zz-conn"]),
               outcome.m("app_spend_per_hour", "down", "zz", app="cis"),
               outcome.m("denied_jobs", "down", "zz"),
               outcome.m("wave_paused", "nonzero", "zz", wave_id=WAVE_ID),
               outcome.m("job_queued", "zero", "zz", job_id=JOB_ID),
               outcome.m("oldest_wait_min", "down", "zz"),
               outcome.m("probe_finished", "nonzero", "zz"),
               outcome.m("live_workers", "nonzero", "zz"),
               outcome.m("item_open", "zero", "zz", item_id="queue-shared"),
               outcome.m("item_open", "zero", "zz", item_id="alarm-e999999")):
        got[(ms["metric"], json.dumps(ms["params"]))] = await outcome.METRICS[ms["metric"]](conn, ms["params"], ctx)
    vals = list(got.values())
    assert vals[0] == 2.0 and vals[1] == 0.0 and vals[2] == 1.0     # refused, throttled, answered on zz-conn
    assert vals[3] == pytest.approx(0.06) and vals[4] == pytest.approx(0.06)   # 0.03 USD in 30 min, per hour
    assert vals[5] == 0.0 and vals[6] == 0.0 and vals[7] == 1.0     # denied jobs, wave not paused, job queued
    assert vals[8] >= 39.0 and vals[9] == 0.0 and vals[10] == 2.0   # oldest wait, no probe, live workers
    assert vals[11] == 1.0 and vals[12] is None                     # the queue warns; an unknown event


@pytest.mark.asyncio
async def test_the_sampler_has_nothing_to_check_without_an_open_run(conn):
    await _as_cis_app(conn)
    assert await outcome.check_due(conn, limits=LIMITS) == {"checked": 0, "finished": 0}
