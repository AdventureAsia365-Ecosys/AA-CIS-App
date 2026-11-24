"""AA-1141 (S291) — the P1 reads and writes of the Ecosystem status page on a real Postgres (conftest replays every
migration, CIS 482 and 483 included): an ack is written with its audit row, replaces the one in force, is read back in
force with the actor's name, ends by itself and is cleared on demand; saved views are per staff member, replaced by
name and deleted only by their owner; What changed reads deploys, migrations and waves of the range; the freshness
footer reads the sampler's newest row. Invented names only; rows are removed by the test's own keys."""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services.ops_status import acks, changes, pipelines, views  # noqa: E402
from api.services.ops_status.ranges import TimeRange  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ITEM = "errors-zz1141"
STAFF_A = "00000000-0000-4000-8000-0000000a1141"
STAFF_B = "00000000-0000-4000-8000-0000000b1141"
REASON = "zz1141 an invented reason long enough"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.execute("UPDATE acp_shared.ops_alert_ack SET cleared_at = now(), cleared_by = 'zz' "
                        "WHERE item_id LIKE '%zz1141%' AND cleared_at IS NULL")
        await p.execute("DELETE FROM acp_shared.ops_saved_view WHERE staff_id = ANY($1::uuid[])", [STAFF_A, STAFF_B])
        await p.execute("DELETE FROM acp_shared.ops_event WHERE dedupe_key LIKE 'zz1141%'")
        await p.execute("DELETE FROM shared.rerun_wave WHERE name LIKE 'zz1141%'")
        await p.close()


def _item(iid: str = ITEM) -> dict:
    return {"id": iid, "app": "cis", "severity": "sev2", "title": "zz1141 invented title"}


@pytest.mark.asyncio
async def test_ack_snooze_clear_round_trip_with_audit(pool):
    now = datetime.now(timezone.utc)
    first = await acks.set_ack(pool, item=_item(), action="ack", until=None, reason=REASON, actor=f"admin:{STAFF_A}",
                               now=now)
    assert first["state"] == "acked"
    second = await acks.set_ack(pool, item=_item(), action="snooze", until=None, snooze_hours=4, reason=REASON,
                                actor=f"admin:{STAFF_A}", now=now)
    assert second["until"] == (now + timedelta(hours=4)).isoformat()
    in_force, _names = await acks.read_in_force(pool, now + timedelta(minutes=1))
    assert in_force[ITEM]["ack_id"] == second["ack_id"] and in_force[ITEM]["action"] == "snooze"
    # the severity it was given at is read back: a worse item under the same id is open again
    assert in_force[ITEM]["severity"] == _item()["severity"]
    worse = acks.apply([{**_item(), "severity": "sev1", "status": "open"}], in_force)[0]
    assert worse["status"] == "open" and worse["escalated"]["from"] == _item()["severity"]
    rows = await pool.fetch("SELECT ack_id, cleared_at IS NOT NULL AS cleared FROM acp_shared.ops_alert_ack "
                            "WHERE item_id = $1 ORDER BY ack_id", ITEM)
    assert [r["cleared"] for r in rows][-2:] == [True, False]   # the first one was replaced
    audit = await pool.fetch("SELECT action, details FROM acp_shared.audit_log WHERE resource_type = 'ops_alert' "
                             "AND resource_id = $1", ITEM)
    assert sum(1 for a in audit if a["action"] == "ops_alert_ack") >= 2
    detail_rows = [json.loads(a["details"]) if isinstance(a["details"], str) else a["details"] for a in audit]
    snoozes = [d for d in detail_rows if d and d.get("action") == "snooze"]   # picked by content, not by row order
    assert len(snoozes) == 1
    details = snoozes[0]
    assert details["replaced"] == [first["ack_id"]] and details["snooze_hours"] == 4
    # ends by itself: 5 hours later nothing is in force
    later, _ = await acks.read_in_force(pool, now + timedelta(hours=5))
    assert ITEM not in later
    trail = await acks.history(pool, now - timedelta(minutes=1))
    assert any(r["item_id"] == ITEM and r["state"] == "snoozed" for r in trail)
    cleared = await acks.clear(pool, item_id=ITEM, actor=f"admin:{STAFF_A}", reason="zz1141 back to open",
                               now=now + timedelta(minutes=2))
    assert cleared["cleared"] == 1
    assert await acks.clear(pool, item_id=ITEM, actor=f"admin:{STAFF_A}", now=now + timedelta(minutes=3)) is None


@pytest.mark.asyncio
async def test_a_refused_ack_writes_nothing(pool):
    before = await pool.fetchval("SELECT count(*) AS n FROM acp_shared.ops_alert_ack WHERE item_id = 'x-zz1141'")
    with pytest.raises(acks.AckError):
        await acks.set_ack(pool, item=_item("x-zz1141"), action="mute", until=datetime.now(timezone.utc) + timedelta(
            days=30), reason=REASON, actor=f"admin:{STAFF_A}")
    after = await pool.fetchval("SELECT count(*) AS n FROM acp_shared.ops_alert_ack WHERE item_id = 'x-zz1141'")
    assert after == before


@pytest.mark.asyncio
async def test_saved_views_are_per_staff_member(pool):
    a = await views.save_view(pool, STAFF_A, "Wave watch", {"range": "6h", "apps": "cis"})
    again = await views.save_view(pool, STAFF_A, " wave  WATCH", {"range": "1h"})
    assert again["id"] == a["id"] and again["query"] == {"range": "1h"}
    await views.save_view(pool, STAFF_B, "Wave watch", {"range": "7d"})
    mine = await views.list_views(pool, STAFF_A)
    assert mine["ready"] is True and [v["name"] for v in mine["views"]] == ["wave WATCH"]
    assert await views.delete_view(pool, STAFF_B, a["id"]) is None     # not B's view
    assert await views.delete_view(pool, STAFF_A, a["id"]) is not None
    assert (await views.list_views(pool, STAFF_A))["views"] == []
    assert len((await views.list_views(pool, STAFF_B))["views"]) == 1


@pytest.mark.asyncio
async def test_what_changed_reads_deploys_migrations_and_waves(pool):
    now = datetime.now(timezone.utc)
    await pool.execute(
        "INSERT INTO acp_shared.ops_event (occurred_at, ended_at, kind, severity, app, subject, title, details, "
        "dedupe_key) VALUES ($1, $1, 'deploy', 'info', 'cis', 'aa-cis-zz1141-api', 'zz deploy', $2::jsonb, "
        "'zz1141-deploy')", now - timedelta(minutes=30), json.dumps({"image_tag": "dev-abcdef1"}))
    await pool.execute(
        "INSERT INTO shared.rerun_wave (name, expected_tour_ids, status, created_by, created_at) "
        "VALUES ('zz1141 wave', ARRAY[$1::uuid], 'open', 'zz', $2)", uuid.UUID(STAFF_A), now - timedelta(minutes=20))
    rng = TimeRange("24h", now - timedelta(hours=24), now + timedelta(minutes=1), 3600)
    out = await changes.collect(pool, rng)
    assert out["unavailable"] == {}
    by_type = {t: [r for r in out["rows"] if r["type"] == t] for t in changes.TYPES}
    deploy = next(r for r in by_type["deploy"] if "zz1141" in r["title"])
    assert deploy["href"].endswith("/AA-CIS-App/commit/abcdef1")
    assert any(r["title"] == "Wave zz1141 wave opened" for r in by_type["wave"])
    # the test database registered every migration just now: the migrations of the range include CIS 482
    assert any(r["ref"] == "482" for r in by_type["migration"])


@pytest.mark.asyncio
async def test_freshness_footer_reads_the_sampler(pool):
    now = datetime.now(timezone.utc)
    rows = {r["key"]: r for r in await pipelines.collect(pool, None, now, 3.0)}
    assert set(rows) == {"sampler", "digest", "guards", "delivery", "schema_dump"}
    assert rows["schema_dump"]["state"] == "not_measured"
    assert rows["sampler"]["state"] in ("ok", "stale", "unknown")
