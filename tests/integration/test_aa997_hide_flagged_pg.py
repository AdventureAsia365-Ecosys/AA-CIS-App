"""AA-997 S260 — `relocate.hide_flagged` / `unhide` and the confirmed-relocate reveal against real Postgres: the
selection SQL (only rows a relocate run stamped and left on the model's point), the per-row re-check, the hidden stamp
and its log, the names, the acp_shared.audit_log rows, and the reversal by stamp and by id. Rows are tagged and
removed at the end; no call."""
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

from services.places import relocate as R  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def env():
    tag = "zh" + uuid.uuid4().hex[:8]
    country = f"Hideland {tag}"            # a country of its own: the run's selection is exactly these rows
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    ids, jobs = {}, []

    async def dest(key, located_by, reason, stamp, status="ok"):
        meta = json.dumps({"relocate": stamp} if stamp else {})
        ids[key] = await pool.fetchval(
            """INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, geo_precision,
                                                name_norm, locality_norm, review_reason, geocoder_meta)
               VALUES ($1, $2, 27.7, 85.3, $3, $4, 'area', lower($1), '', $5, $6::jsonb) RETURNING id::text""",
            f"{key} {tag}", country, located_by, status, reason, meta)

    await dest("flagged", "llm", "llm_point", {"v": "aa997.2", "outcome": "llm_point", "accept_rule": "aa997.3"})
    await dest("off", "llm", "unconfirmed_hard_type", {"v": "aa997.1", "outcome": "off_route"})
    await dest("untouched", "llm", "llm_point", None)
    await dest("cleared", "llm", None, {"v": "aa997.2", "outcome": "llm_point"})
    await dest("confirmed", "geoapify", None, {"v": "aa997.2", "outcome": "confirmed"})
    await dest("manual", "manual", "llm_point", {"v": "aa997.2", "outcome": "llm_point"}, "manual")
    await dest("mbx", "mapbox", "llm_point", {"v": "aa997.2", "outcome": "llm_point"})
    await pool.execute(
        """INSERT INTO geo.place_name (name_norm, country_norm, locality_norm, display_name, country, destination_id,
                                       status, tries, next_retry_at)
           VALUES ($1, lower($2), '', $1, $2, $3::uuid, 'resolved', 0, now())""",
        f"flagged {tag}", country, ids["flagged"])
    yield pool, tag, country, ids, jobs
    try:
        await pool.execute("DELETE FROM shared.job WHERE id = ANY($1::uuid[])", jobs)
        await pool.execute("DELETE FROM geo.place_name WHERE destination_id = ANY($1::uuid[])", list(ids.values()))
        await pool.execute("DELETE FROM shared.destinations WHERE id = ANY($1::uuid[])", list(ids.values()))
        # needs a superuser (CI's postgres user, the documented local cistest role): the table is append-only
        await delete_audit_rows(pool, "resource_type = $1 AND resource_id LIKE $2", R.AUDIT_RESOURCE,
                                f"%:{country}:%")
    finally:
        await pool.close()


async def _report(pool, jobs, **kw):
    """A hide_flagged report, stored as the finished shared.job row a confirm names (what the job runner writes)."""
    rep = await R.hide_flagged(pool, confirm=False, **kw)
    jid = await pool.fetchval(
        """INSERT INTO shared.job (kind, payload, status, result, created_at, finished_at)
           VALUES ('place_relocate', $1::jsonb, 'succeeded', $2::jsonb, now(), now()) RETURNING id::text""",
        json.dumps({"mode": "hide_flagged", "confirm": False}), json.dumps(rep, default=str))
    jobs.append(jid)
    return rep, jid


async def _confirm(pool, jobs, actor="job", job_id=None, **kw):
    rep, jid = await _report(pool, jobs, **kw)
    return await R.hide_flagged(pool, confirm=True, dry_run_job=jid, max_rows=len(rep["ids"]), actor=actor,
                                job_id=job_id, **kw)


async def _state(pool, did):
    r = await pool.fetchrow("SELECT geo_status, geocoder_meta FROM shared.destinations WHERE id = $1::uuid", did)
    meta = r["geocoder_meta"]
    return r["geo_status"], (json.loads(meta) if isinstance(meta, str) else meta) or {}


@pytest.mark.asyncio
async def test_hide_flagged_report_confirm_and_unhide_round_trip(env):
    pool, tag, country, ids, jobs = env
    rep, rep_id = await _report(pool, jobs, outcomes=["llm_point", "off_route"], country=country)
    assert rep["rows"] == 2 and rep["hidden"] == 0 and rep["by_outcome"] == {"llm_point": 1, "off_route": 1}
    assert {s["id"] for s in rep["samples"]} == {ids["flagged"], ids["off"]} and rep["located_by"] == ["llm"]
    assert rep["ids"] == sorted([ids["flagged"], ids["off"]]) and len(rep["row_digests"]) == 2
    assert (await _state(pool, ids["flagged"]))[0] == "ok"

    narrowed = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], stamp_versions=["aa997.2"],
                                    stamp_accept="aa997.3", country=country, confirm=False)
    assert narrowed["rows"] == 1

    # a confirm without its report, or with another row count than its report, writes nothing
    no_report = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True)
    assert no_report["status"] == "refused_no_report"
    refused = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True,
                                   dry_run_job=rep_id, max_rows=1)
    assert refused["status"] == "refused_report_row_count_differs" and (await _state(pool, ids["off"]))[0] == "ok"

    done = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True,
                                dry_run_job=rep_id, max_rows=2, actor="admin:test", job_id=f"job-{tag}")
    assert done["hidden"] == 2 and done["audit_rows"] == 1 and done["dropped_ids"] == []
    status, meta = await _state(pool, ids["flagged"])
    assert status == "suspect" and meta["hidden"]["by"] == R.HIDE_FLAGGED_STAMP and meta["hidden"]["report"] == rep_id
    assert meta["hidden"]["reason"] == "llm_point" and meta["relocate"]["outcome"] == "llm_point"
    assert await pool.fetchval("SELECT status FROM geo.place_name WHERE destination_id = $1::uuid",
                               ids["flagged"]) == "suspect"
    for key in ("untouched", "cleared", "confirmed", "manual", "mbx"):
        assert (await _state(pool, ids[key]))[0] != "suspect", key
    audit = await pool.fetchrow("SELECT action, details FROM acp_shared.audit_log WHERE resource_type = $1 "
                                "AND resource_id = $2", R.AUDIT_RESOURCE, f"hide_flagged:{country}:job-{tag}")
    details = audit["details"] if isinstance(audit["details"], dict) else json.loads(audit["details"])
    assert audit["action"] == R.AUDIT_HIDE and sorted(details["destination_ids"]) == sorted([ids["flagged"],
                                                                                            ids["off"]])
    # a second confirm finds nothing left (idempotent)
    again = await _confirm(pool, jobs, outcomes=["llm_point", "off_route"], country=country)
    assert again["rows"] == 0 and again["hidden"] == 0

    # the single-row restore (a person's: sticky), then the rest by stamp (bulk: not sticky)
    one = await R.unhide(pool, country=None, by=[R.HIDE_FLAGGED_STAMP], destination_ids=[ids["flagged"]],
                         actor="admin:test", job_id=f"job-{tag}")
    assert one["unhidden"] == 1 and one["audit_rows"] == 1
    status, meta = await _state(pool, ids["flagged"])
    assert status == "ok" and meta["hidden"]["unhidden_at"] and meta["hidden"]["unhidden_by"] == "restore:admin:test"
    assert meta["hidden"]["unhidden_job"] == f"job-{tag}"
    assert await pool.fetchval("SELECT status FROM geo.place_name WHERE destination_id = $1::uuid",
                               ids["flagged"]) == "resolved"
    rest = await R.unhide(pool, country=country, by=[R.HIDE_FLAGGED_STAMP], actor="admin:test", job_id=f"job-{tag}")
    assert rest["unhidden"] == 1 and (await _state(pool, ids["off"]))[0] == "ok"
    assert (await _state(pool, ids["off"]))[1]["hidden"]["unhidden_by"] == "unhide:admin:test"
    # the next run skips the restored row, and hides the bulk-unhidden one again
    skip = await _confirm(pool, jobs, outcomes=["llm_point", "off_route"], country=country)
    assert skip["rows"] == 1 and skip["restored_skipped"] == 1 and skip["hidden"] == 1
    assert (await _state(pool, ids["flagged"]))[0] == "ok" and (await _state(pool, ids["off"]))[0] == "suspect"
    # a newer relocate judgement (its stamp later than the restore) may hide it again: the earlier stamp moves into
    # hidden.log
    await pool.execute(
        """UPDATE shared.destinations SET geocoder_meta = jsonb_set(geocoder_meta, '{relocate,at}',
                  to_jsonb((now() + interval '1 minute')::text)) WHERE id = $1::uuid""", ids["flagged"])
    again = await _confirm(pool, jobs, outcomes=["llm_point"], country=country)
    assert again["hidden"] == 1 and again["restored_skipped"] == 0
    status, meta = await _state(pool, ids["flagged"])
    assert status == "suspect" and meta["hidden"]["log"][0]["unhidden_at"]


@pytest.mark.asyncio
async def test_a_confirm_hides_only_the_rows_its_report_listed(env):
    """Same count, other rows: the report saw `flagged` + `off`; before the confirm `off` is cleared by a reviewer and
    the chain stamps `untouched` llm_point. The confirm hides `flagged` only: `off` is dropped, `untouched` (new since
    the report) is never hidden."""
    pool, tag, country, ids, jobs = env
    rep, rep_id = await _report(pool, jobs, outcomes=["llm_point", "off_route"], country=country)
    assert rep["rows"] == 2
    await pool.execute("UPDATE shared.destinations SET review_reason = NULL WHERE id = $1::uuid", ids["off"])
    await pool.execute("""UPDATE shared.destinations SET geocoder_meta = '{"relocate": {"v": "aa997.2",
                          "outcome": "llm_point"}}'::jsonb WHERE id = $1::uuid""", ids["untouched"])
    out = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True,
                               dry_run_job=rep_id, max_rows=2)
    assert out["rows"] == 2 and out["hidden"] == 1 and out["dropped_ids"] == [ids["off"]]
    assert out["new_since_report"] == 1
    assert (await _state(pool, ids["flagged"]))[0] == "suspect"
    assert (await _state(pool, ids["untouched"]))[0] == "ok" and (await _state(pool, ids["off"]))[0] == "ok"


@pytest.mark.asyncio
async def test_hide_flagged_takes_the_requested_located_by(env):
    pool, tag, country, ids, jobs = env
    rep, rep_id = await _report(pool, jobs, outcomes=["llm_point"], located_by=["mapbox"], country=country)
    assert rep["ids"] == [ids["mbx"]] and rep["located_by"] == ["mapbox"]
    # the report of mapbox rows does not back a confirm of llm rows
    other = await R.hide_flagged(pool, outcomes=["llm_point"], country=country, confirm=True, dry_run_job=rep_id,
                                 max_rows=1)
    assert other["status"] == "refused_report_selection_differs" and other["hidden"] == 0
    out = await R.hide_flagged(pool, outcomes=["llm_point"], located_by=["mapbox"], country=country, confirm=True,
                               dry_run_job=rep_id, max_rows=1)
    assert out["hidden"] == 1 and (await _state(pool, ids["mbx"]))[0] == "suspect"
    assert (await _state(pool, ids["flagged"]))[0] == "ok"


@pytest.mark.asyncio
async def test_plain_hide_keeps_a_restored_row_shown_and_is_audited(env):
    pool, tag, country, ids, jobs = env
    out = await R.hide(pool, reasons=["llm_point"], country=country, confirm=True, actor="admin:test",
                       job_id=f"job-{tag}")
    # flagged, untouched, mbx (manual is not geo_status ok)
    assert out["hidden"] == 3 and out["audit_rows"] == 1
    audit = await pool.fetchrow("SELECT action, details FROM acp_shared.audit_log WHERE resource_type = $1 "
                                "AND resource_id = $2", R.AUDIT_RESOURCE, f"hide:{country}:job-{tag}")
    assert audit["action"] == R.AUDIT_HIDE_POLICY
    await R.unhide(pool, country=None, destination_ids=[ids["untouched"]], actor="admin:test")
    await R.unhide(pool, country=country, by=[R.HIDE_STAMP], actor="admin:test")
    again = await R.hide(pool, reasons=["llm_point"], country=country, confirm=True)
    assert again["restored_skipped"] == 1 and again["hidden"] == 2
    assert (await _state(pool, ids["untouched"]))[0] == "ok" and (await _state(pool, ids["mbx"]))[0] == "suspect"


@pytest.mark.asyncio
async def test_the_current_row_read_and_the_reveal_of_a_confirmed_row(env):
    pool, tag, country, ids, jobs = env
    await _confirm(pool, jobs, outcomes=["llm_point"], country=country)
    async with pool.acquire() as conn:
        cur = dict(await conn.fetchrow(R._CURRENT_SQL, ids["flagged"]))
        assert R._policy_hidden(cur)["by"] == R.HIDE_FLAGGED_STAMP
        async with conn.transaction():
            assert await R._reveal_if_policy_hidden(conn, ids["flagged"], cur, "confirmed")
    status, meta = await _state(pool, ids["flagged"])
    assert status == "ok" and meta["hidden"]["unhidden_by"] == "relocate:confirmed"


class _NoHit:
    calls_external = True

    async def confirm(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_a_hidden_row_a_recheck_flags_with_another_reason_can_still_be_restored(env, monkeypatch):
    """hide_flagged hides an llm_point row; an aa997.1-style recheck finds no geocoder hit and flags it off_route (it
    stays suspect): the hide stamp follows the new reason, so unhide by stamp reverts it; and an explicit id restore
    reverts a row whose reason changed some other way."""
    pool, tag, country, ids, jobs = env
    await _confirm(pool, jobs, outcomes=["llm_point", "off_route"], country=country)
    row = dict(await pool.fetchrow(
        """SELECT id::text AS id, name, country, lat, lng, located_by, geo_status, geo_precision, review_reason,
                  name_norm, locality_norm, geocoder_meta FROM shared.destinations WHERE id = $1::uuid""",
        ids["flagged"]))
    real = R.classify_row
    monkeypatch.setattr(R, "classify_row", lambda r, routes, **k: {**real(r, routes, **k), "on_route": False,
                                                                     "route_km": 80.0, "non_place": None})
    async with pool.acquire() as conn:
        r = await R.apply_row(conn, row, (), _NoHit(), hide_unconfirmed=False)
    assert r["outcome"] == "off_route"
    status, meta = await _state(pool, ids["flagged"])
    assert status == "suspect" and meta["hidden"]["reason"] == "off_route"
    assert meta["hidden"]["reason_was"] == "llm_point" and meta["hidden"]["reason_by"] == "relocate:off_route"
    out = await R.unhide(pool, country=country, by=[R.HIDE_FLAGGED_STAMP], actor="admin:test")
    assert out["unhidden"] == 2 and out["kept_reason_changed"] == 0
    assert (await _state(pool, ids["flagged"]))[0] == "ok"

    # another path changed the reason of a hidden row: a stamp unhide keeps it, the explicit id restore reverts it.
    # The bulk unhide above is not sticky: both off_route rows (`off`, and `flagged` after its recheck) hide again.
    again = await _confirm(pool, jobs, outcomes=["off_route"], country=country)
    assert again["hidden"] == 2 and again["restored_skipped"] == 0
    await pool.execute("UPDATE shared.destinations SET review_reason = 'outlier' WHERE id = $1::uuid", ids["off"])
    kept = await R.unhide(pool, country=country, by=[R.HIDE_FLAGGED_STAMP])
    assert kept["unhidden"] == 1 and kept["kept_reason_changed"] == 1
    assert (await _state(pool, ids["off"]))[0] == "suspect"
    one = await R.unhide(pool, country=None, destination_ids=[ids["off"]], actor="admin:test")
    assert one["unhidden"] == 1 and one["restored_reason_changed"] == 1
    assert (await _state(pool, ids["off"]))[0] == "ok"
