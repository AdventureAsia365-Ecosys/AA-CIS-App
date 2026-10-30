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
    ids = {}

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
    await pool.execute(
        """INSERT INTO geo.place_name (name_norm, country_norm, locality_norm, display_name, country, destination_id,
                                       status, tries, next_retry_at)
           VALUES ($1, lower($2), '', $1, $2, $3::uuid, 'resolved', 0, now())""",
        f"flagged {tag}", country, ids["flagged"])
    yield pool, tag, country, ids
    try:
        await pool.execute("DELETE FROM geo.place_name WHERE destination_id = ANY($1::uuid[])", list(ids.values()))
        await pool.execute("DELETE FROM shared.destinations WHERE id = ANY($1::uuid[])", list(ids.values()))
        # needs a superuser (CI's postgres user, the documented local cistest role): the table is append-only
        await delete_audit_rows(pool, "resource_type = $1 AND resource_id LIKE $2", R.AUDIT_RESOURCE,
                                f"%:{country}:%")
    finally:
        await pool.close()


async def _state(pool, did):
    r = await pool.fetchrow("SELECT geo_status, geocoder_meta FROM shared.destinations WHERE id = $1::uuid", did)
    meta = r["geocoder_meta"]
    return r["geo_status"], (json.loads(meta) if isinstance(meta, str) else meta) or {}


@pytest.mark.asyncio
async def test_hide_flagged_report_confirm_and_unhide_round_trip(env):
    pool, tag, country, ids = env
    rep = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=False)
    assert rep["rows"] == 2 and rep["hidden"] == 0 and rep["by_outcome"] == {"llm_point": 1, "off_route": 1}
    assert {s["id"] for s in rep["samples"]} == {ids["flagged"], ids["off"]}
    assert (await _state(pool, ids["flagged"]))[0] == "ok"

    narrowed = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], stamp_versions=["aa997.2"],
                                    stamp_accept="aa997.3", country=country, confirm=False)
    assert narrowed["rows"] == 1

    refused = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True,
                                   max_rows=1)
    assert refused["status"] == "refused_selection_grew" and (await _state(pool, ids["off"]))[0] == "ok"

    done = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True, max_rows=2,
                                actor="admin:test", job_id=f"job-{tag}")
    assert done["hidden"] == 2 and done["audit_rows"] == 1
    status, meta = await _state(pool, ids["flagged"])
    assert status == "suspect" and meta["hidden"]["by"] == R.HIDE_FLAGGED_STAMP
    assert meta["hidden"]["reason"] == "llm_point" and meta["relocate"]["outcome"] == "llm_point"
    assert await pool.fetchval("SELECT status FROM geo.place_name WHERE destination_id = $1::uuid",
                               ids["flagged"]) == "suspect"
    for key in ("untouched", "cleared", "confirmed", "manual"):
        assert (await _state(pool, ids[key]))[0] != "suspect", key
    audit = await pool.fetchrow("SELECT action, details FROM acp_shared.audit_log WHERE resource_type = $1 "
                                "AND resource_id = $2", R.AUDIT_RESOURCE, f"hide_flagged:{country}:job-{tag}")
    details = audit["details"] if isinstance(audit["details"], dict) else json.loads(audit["details"])
    assert audit["action"] == R.AUDIT_HIDE and sorted(details["destination_ids"]) == sorted([ids["flagged"],
                                                                                            ids["off"]])
    # a second confirm finds nothing left (idempotent)
    again = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True)
    assert again["rows"] == 0 and again["hidden"] == 0

    # the single-row restore, then the rest by stamp
    one = await R.unhide(pool, country=None, by=[R.HIDE_FLAGGED_STAMP], destination_ids=[ids["flagged"]],
                         actor="admin:test", job_id=f"job-{tag}")
    assert one["unhidden"] == 1 and one["audit_rows"] == 1
    status, meta = await _state(pool, ids["flagged"])
    assert status == "ok" and meta["hidden"]["unhidden_at"] and meta["hidden"]["unhidden_by"] == f"admin:test:job-{tag}"
    assert await pool.fetchval("SELECT status FROM geo.place_name WHERE destination_id = $1::uuid",
                               ids["flagged"]) == "resolved"
    rest = await R.unhide(pool, country=country, by=[R.HIDE_FLAGGED_STAMP], actor="admin:test", job_id=f"job-{tag}")
    assert rest["unhidden"] == 1 and (await _state(pool, ids["off"]))[0] == "ok"
    # a person's restore lasts: the next hide_flagged run skips both restored rows (reported, not hidden)
    skip = await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True)
    assert skip["rows"] == 0 and skip["restored_skipped"] == 2 and skip["hidden"] == 0
    assert (await _state(pool, ids["flagged"]))[0] == "ok" and (await _state(pool, ids["off"]))[0] == "ok"
    # a newer relocate judgement (its stamp later than the restore) may hide it again: the earlier stamp moves into
    # hidden.log
    await pool.execute(
        """UPDATE shared.destinations SET geocoder_meta = jsonb_set(geocoder_meta, '{relocate,at}',
                  to_jsonb((now() + interval '1 minute')::text)) WHERE id = $1::uuid""", ids["flagged"])
    again = await R.hide_flagged(pool, outcomes=["llm_point"], country=country, confirm=True)
    assert again["hidden"] == 1 and again["restored_skipped"] == 0
    status, meta = await _state(pool, ids["flagged"])
    assert status == "suspect" and meta["hidden"]["log"][0]["unhidden_at"]


@pytest.mark.asyncio
async def test_the_current_row_read_and_the_reveal_of_a_confirmed_row(env):
    pool, tag, country, ids = env
    await R.hide_flagged(pool, outcomes=["llm_point"], country=country, confirm=True)
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
    pool, tag, country, ids = env
    await R.hide_flagged(pool, outcomes=["llm_point", "off_route"], country=country, confirm=True)
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

    # another path changed the reason of a hidden row: a stamp unhide keeps it, the explicit id restore reverts it
    await pool.execute("UPDATE shared.destinations SET geocoder_meta = jsonb_set(geocoder_meta, '{relocate,at}', "
                       "to_jsonb((now() + interval '1 minute')::text)) WHERE id = $1::uuid", ids["off"])
    await R.hide_flagged(pool, outcomes=["off_route"], country=country, confirm=True)
    await pool.execute("UPDATE shared.destinations SET review_reason = 'outlier' WHERE id = $1::uuid", ids["off"])
    kept = await R.unhide(pool, country=country, by=[R.HIDE_FLAGGED_STAMP])
    assert kept["unhidden"] == 0 and kept["kept_reason_changed"] == 1
    one = await R.unhide(pool, country=None, destination_ids=[ids["off"]], actor="admin:test")
    assert one["unhidden"] == 1 and one["restored_reason_changed"] == 1
    assert (await _state(pool, ids["off"]))[0] == "ok"
