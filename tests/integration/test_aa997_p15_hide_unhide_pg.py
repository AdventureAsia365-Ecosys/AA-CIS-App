"""AA-997 P15 — the three open defects of CIS #1178 (hide_flagged / unhide), against real Postgres:

1. a reversal (unhide by stamp, restore by id, a relocate reveal) sets back to 'resolved' only the names the hide
   changed, and never shows a row the outlier rule hides;
2. a relocate run that shows a policy-hidden row again, or moves the reason of its hide stamp, writes
   acp_shared.audit_log rows (one per HIDE_BATCH rows), like a hide;
3. the confirm's per-row UPDATE re-checks the stamp version / accept rule / time and the country it read, so a row
   re-judged between the confirm's read and its write is dropped, not hidden.

Each test fails on the code before P15. Invented names and places only; rows tagged and removed at the end; no call."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _audit_cleanup import delete_audit_rows  # noqa: E402

from services.places import geomatch, locate, store  # noqa: E402
from services.places import relocate as R  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
OUTCOMES = ["llm_point", "off_route"]


@pytest_asyncio.fixture
async def env():
    tag = "zp" + uuid.uuid4().hex[:8]
    country = f"Quillmark {tag}"          # a country of its own: every selection is exactly these rows
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    ids, jobs = {}, []

    async def dest(key, *, reason="llm_point", stamp=None, located_by="llm", ctry=None):
        stamp = stamp or {"v": R.RULE_VERSION, "outcome": "llm_point", "accept_rule": "aa997.3",
                          "at": "2026-10-31T01:00:00+00:00"}
        ids[key] = await pool.fetchval(
            """INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, geo_precision,
                                                name_norm, locality_norm, review_reason, geocoder_meta)
               VALUES ($1, $2, 27.7, 85.3, $3, 'ok', 'area', lower($1), '', $4, $5::jsonb) RETURNING id::text""",
            f"{key} {tag}", ctry or country, located_by, reason, json.dumps({"relocate": stamp}))
        return ids[key]

    async def name(key, suffix, status):
        await pool.execute(
            """INSERT INTO geo.place_name (name_norm, country_norm, locality_norm, display_name, country,
                                           destination_id, status, tries, next_retry_at)
               VALUES ($1, lower($2), '', $1, $2, $3::uuid, $4, 0, now())""",
            f"{key} {suffix} {tag}", country, ids[key], status)

    yield {"pool": pool, "tag": tag, "country": country, "ids": ids, "jobs": jobs, "dest": dest, "name": name}
    try:
        all_ids = list(ids.values())
        await pool.execute("DELETE FROM shared.job WHERE id = ANY($1::uuid[])", jobs)
        await pool.execute("DELETE FROM geo.place_outlier WHERE destination_id = ANY($1::uuid[])", all_ids)
        await pool.execute("DELETE FROM geo.place_name WHERE destination_id = ANY($1::uuid[])", all_ids)
        await pool.execute("DELETE FROM shared.destinations WHERE id = ANY($1::uuid[])", all_ids)
        await delete_audit_rows(pool, "resource_type = $1 AND resource_id LIKE $2", R.AUDIT_RESOURCE, f"%{tag}%")
    finally:
        await pool.close()


async def _confirm(e, **kw):
    """A hide_flagged report stored as the finished job a confirm names, then the confirm."""
    pool = e["pool"]
    kw = {"outcomes": OUTCOMES, "country": e["country"], **kw}
    rep = await R.hide_flagged(pool, confirm=False, **kw)
    jid = await pool.fetchval(
        """INSERT INTO shared.job (kind, payload, status, result, created_at, finished_at)
           VALUES ('place_relocate', $1::jsonb, 'succeeded', $2::jsonb, now(), now()) RETURNING id::text""",
        json.dumps({"mode": "hide_flagged", "confirm": False}), json.dumps(rep, default=str))
    e["jobs"].append(jid)
    return await R.hide_flagged(pool, confirm=True, dry_run_job=jid, max_rows=len(rep["ids"]),
                                job_id=f"job-{e['tag']}", actor="admin:test", **kw)


async def _status(pool, did):
    r = await pool.fetchrow("SELECT geo_status, geocoder_meta FROM shared.destinations WHERE id = $1::uuid", did)
    meta = r["geocoder_meta"]
    return r["geo_status"], (json.loads(meta) if isinstance(meta, str) else meta) or {}


async def _names(pool, did):
    rows = await pool.fetch("SELECT name_norm, status FROM geo.place_name WHERE destination_id = $1::uuid", did)
    return {r["name_norm"].split(" ")[1]: r["status"] for r in rows}


async def _audit(pool, action, resource_id):
    rows = await pool.fetch("SELECT details FROM acp_shared.audit_log WHERE resource_type = $1 AND action = $2 "
                            "AND resource_id = $3 ORDER BY created_at", R.AUDIT_RESOURCE, action, resource_id)
    return [r["details"] if isinstance(r["details"], dict) else json.loads(r["details"]) for r in rows]


# ── 1. a reversal restores only what the hide changed ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unhide_restores_only_the_names_the_hide_changed(env):
    """`a` has a resolved name and a name already suspect for another reason; the hide changes only the first and
    records it; the unhide by stamp sets back only that one (the code before P15 set every suspect name resolved)."""
    pool = env["pool"]
    a = await env["dest"]("alder")
    await env["name"]("alder", "main", "resolved")
    await env["name"]("alder", "other", "suspect")
    out = await _confirm(env)
    assert out["hidden"] == 1
    status, meta = await _status(pool, a)
    assert status == "suspect" and meta["hidden"]["prev_geo_status"] == "ok"
    assert meta["hidden"]["names"] == [[f"alder main {env['tag']}", env["country"].lower(), ""]]
    assert await _names(pool, a) == {"main": "suspect", "other": "suspect"}

    un = await R.unhide(pool, country=env["country"], by=[R.HIDE_FLAGGED_STAMP], actor="admin:test")
    assert un["unhidden"] == 1 and un["kept_outlier"] == 0
    assert (await _status(pool, a))[0] == "ok"
    assert await _names(pool, a) == {"main": "resolved", "other": "suspect"}


@pytest.mark.asyncio
async def test_restore_by_id_and_plain_hide_restore_only_their_names(env):
    pool = env["pool"]
    b = await env["dest"]("birch")
    await env["name"]("birch", "main", "resolved")
    await env["name"]("birch", "other", "suspect")
    out = await R.hide(pool, reasons=["llm_point"], country=env["country"], confirm=True, actor="admin:test",
                       job_id=f"job-{env['tag']}")
    assert out["hidden"] == 1
    one = await R.unhide(pool, country=None, destination_ids=[b], actor="admin:test")
    assert one["unhidden"] == 1
    assert await _names(pool, b) == {"main": "resolved", "other": "suspect"}


@pytest.mark.asyncio
async def test_unhide_never_shows_a_row_the_outlier_rule_hides(env):
    """While hidden, the row became an outlier of a tour (the rule hides a model-located row at one tour): the unhide
    lets go of it (stamp closed, `kept_suspect`, audited) but it stays suspect, its names too."""
    pool = env["pool"]
    c = await env["dest"]("cedar")
    await env["name"]("cedar", "main", "resolved")
    await _confirm(env)
    await pool.execute("""INSERT INTO geo.place_outlier (destination_id, source_id, day_index, km, observed_at)
                          VALUES ($1::uuid, $2, 1, 120.0, now())""", c, f"src-{env['tag']}")
    un = await R.unhide(pool, country=env["country"], by=[R.HIDE_FLAGGED_STAMP], actor="admin:test",
                        job_id=f"job-{env['tag']}")
    assert un["unhidden"] == 0 and un["kept_outlier"] == 1 and un["audit_rows"] == 1
    status, meta = await _status(pool, c)
    assert status == "suspect" and meta["hidden"]["kept_suspect"] == R.KEPT_SUSPECT_OUTLIER
    assert meta["hidden"]["unhidden_at"] and meta["hidden"]["unhidden_by"] == "unhide:admin:test"
    assert await _names(pool, c) == {"main": "suspect"}
    details = await _audit(pool, R.AUDIT_UNHIDE, f"unhide:{env['country']}:job-{env['tag']}")
    assert details[0]["kept_outlier_ids"] == [c] and details[0]["destination_ids"] == []
    # nothing left for a second unhide: the policy no longer holds the row
    again = await R.unhide(pool, country=env["country"], by=[R.HIDE_FLAGGED_STAMP])
    assert again["rows"] == 0


# ── 2. a relocate run's reveal and moved hide reason are audited ────────────────────────────────────────────────────

class _Geo:
    """Confirms the names that carry `sure` (a point near the stored one), finds nothing for the rest."""
    calls_external = True
    requests = 0

    async def confirm(self, query, lat, lng, country, **kw):
        return {"hit": True} if "sure" in str(query).lower() else None


@pytest.mark.asyncio
async def test_a_relocate_run_audits_its_reveals_and_moved_hide_reasons(env, monkeypatch):
    """Two aa997.1 llm_point rows hidden by hide_flagged, then rechecked by an apply run: the geocoder confirms
    `sure` (shown again) and not `fern` (flagged off_route: the hide stamp takes the new reason). Each change has its
    audit row; with HIDE_BATCH 1 each row its own."""
    pool, country = env["pool"], env["country"]
    old = {"v": "aa997.1", "outcome": "llm_point", "at": "2026-10-28T01:00:00+00:00"}
    s = await env["dest"]("sure", stamp=old)
    f = await env["dest"]("fern", stamp=old)
    await env["name"]("sure", "main", "resolved")
    assert (await _confirm(env))["hidden"] == 2

    real_classify = R.classify_row
    monkeypatch.setattr(R, "classify_row", lambda r, routes, **k: {**real_classify(r, routes, **k),
                                                                   "on_route": False, "route_km": 80.0,
                                                                   "non_place": None, "query": r["name"]})
    monkeypatch.setattr(locate, "_confirmed", lambda item, res, ctry, **k: (
        locate.Located(27.71, 85.31, ctry, "geoapify", "pid-sure", meta={"provider": "geoapify"},
                       kind=geomatch.AREA) if res else None))
    out = await R.apply(pool, _Geo(), located_by=["llm"], country=country, limit=10, recheck=["llm_point"],
                        actor="admin:test", job_id=f"job-{env['tag']}")
    assert out["confirmed"] == 1 and out["off_route"] == 1
    assert out["unhidden"] == 1 and out["hidden_reason_changed"] == 1 and out["audit_rows"] == 2
    status, meta = await _status(pool, s)
    assert status == "ok" and meta["hidden"]["unhidden_by"] == "relocate:confirmed"
    assert await _names(pool, s) == {"main": "resolved"}
    status, meta = await _status(pool, f)
    assert status == "suspect" and meta["hidden"]["reason"] == "off_route"

    reveal = await _audit(pool, R.AUDIT_REVEAL, f"reveal:{country}:job-{env['tag']}")
    assert len(reveal) == 1 and reveal[0]["destination_ids"] == [s] and reveal[0]["unhidden"] == 1
    assert reveal[0]["changes"][0]["outcome"] == "confirmed"
    assert reveal[0]["changes"][0]["hidden_by"] == R.HIDE_FLAGGED_STAMP
    moved = await _audit(pool, R.AUDIT_HIDDEN_REASON, f"hidden_reason:{country}:job-{env['tag']}")
    assert len(moved) == 1 and moved[0]["destination_ids"] == [f]
    assert moved[0]["changes"][0]["from"] == "llm_point" and moved[0]["changes"][0]["to"] == "off_route"


@pytest.mark.asyncio
async def test_relocate_audit_rows_come_one_per_batch(env, monkeypatch):
    pool, country = env["pool"], env["country"]
    old = {"v": "aa997.1", "outcome": "llm_point", "at": "2026-10-28T01:00:00+00:00"}
    for k in ("fern1", "fern2", "fern3"):
        await env["dest"](k, stamp=old)
    assert (await _confirm(env))["hidden"] == 3
    monkeypatch.setattr(R, "HIDE_BATCH", 2)
    real_classify = R.classify_row
    monkeypatch.setattr(R, "classify_row", lambda r, routes, **k: {**real_classify(r, routes, **k),
                                                                   "on_route": False, "route_km": 80.0,
                                                                   "non_place": None, "query": r["name"]})
    out = await R.apply(pool, _Geo(), located_by=["llm"], country=country, limit=10, recheck=["llm_point"],
                        job_id=f"job-{env['tag']}")
    assert out["hidden_reason_changed"] == 3 and out["audit_rows"] == 2
    moved = await _audit(pool, R.AUDIT_HIDDEN_REASON, f"hidden_reason:{country}:job-{env['tag']}")
    assert [d["rows"] for d in moved] == [2, 1] and [d["batch"] for d in moved] == [0, 1]


# ── 3. the confirm's write re-checks what it read ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["v", "accept_rule", "at", "country"])
async def test_a_row_rejudged_between_the_confirm_read_and_its_write_is_dropped(env, monkeypatch, change):
    """The confirm reads its rows, then (before its batch takes the destinations lock) a relocate run re-stamps one
    with another version / accept rule / time, or moves it to another country: that row is dropped, the other
    hidden. Before P15 the write re-checked only reason, outcome and located_by, and hid it."""
    pool = env["pool"]
    # a selection of this test's rows only, by a stamp accept rule of its own and no country (a country-wide confirm
    # keeps a row selected after its country changes: the write itself must drop it)
    rule = f"rule-{env['tag']}"
    stamp = {"v": R.RULE_VERSION, "outcome": "llm_point", "accept_rule": rule, "at": "2026-10-31T01:00:00+00:00"}
    keep = await env["dest"]("gorse", stamp=stamp)
    moved = await env["dest"]("heath", stamp=stamp)
    real_lock = store.lock_destinations
    sql = {
        "v": """UPDATE shared.destinations SET geocoder_meta = jsonb_set(geocoder_meta, '{relocate,v}', '"aa997.9"')
                 WHERE id = $1::uuid""",
        "accept_rule": """UPDATE shared.destinations
                             SET geocoder_meta = jsonb_set(geocoder_meta, '{relocate,accept_rule}', '"aa997.4"')
                           WHERE id = $1::uuid""",
        "at": """UPDATE shared.destinations
                    SET geocoder_meta = jsonb_set(geocoder_meta, '{relocate,at}', '"2026-11-01T02:00:00+00:00"')
                  WHERE id = $1::uuid""",
        "country": "UPDATE shared.destinations SET country = country || ' North' WHERE id = $1::uuid",
    }[change]
    fired = []

    async def racing_lock(conn):
        if not fired:                     # the first batch only: the change lands after the confirm's read
            fired.append(True)
            await pool.execute(sql, moved)
        await real_lock(conn)

    rep = await R.hide_flagged(pool, confirm=False, outcomes=OUTCOMES, stamp_accept=rule)
    assert sorted(rep["ids"]) == sorted([keep, moved])
    jid = await pool.fetchval(
        """INSERT INTO shared.job (kind, payload, status, result, created_at, finished_at)
           VALUES ('place_relocate', $1::jsonb, 'succeeded', $2::jsonb, now(), now()) RETURNING id::text""",
        json.dumps({"mode": "hide_flagged", "confirm": False}), json.dumps(rep, default=str))
    env["jobs"].append(jid)
    monkeypatch.setattr(store, "lock_destinations", racing_lock)
    out = await R.hide_flagged(pool, confirm=True, outcomes=OUTCOMES, stamp_accept=rule, dry_run_job=jid,
                               max_rows=2, job_id=f"job-{env['tag']}")
    assert fired and out["hidden"] == 1 and out["dropped_ids"] == [moved], out
    assert (await _status(pool, keep))[0] == "suspect" and (await _status(pool, moved))[0] == "ok"
