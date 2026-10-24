"""AA-976 (CRM-B4) — the catalog.http.v1 reader against real Postgres, as aa_app_user on the live catalog views.

Lesson S229 / S231: new SQL needs a run on real Postgres. This module reuses the RDS-like state of
test_aa986_migration_274 (253 / 256 / 257 / 258 / 273 / 274 applied as cis_owner, the seeded served / retired /
paused / restored / never-served keys) and runs every statement of services/catalog_http/reader.py through a small
asyncpg-shaped adapter over the test transaction, each one under ``SET LOCAL ROLE aa_app_user`` with
``app.tenant_id`` = '' (what reader.catalog_connection does on the tenant pool). Checks:

- a served tour is assembled from v_tour / v_tour_day / v_tour_place / v_tour_photo / v_tour_component and validates
  against the contract's Tour schema;
- the served state of a key that left v_tour comes from v_tour_retired: retired -> 'retired' (410), paused ->
  'paused' (404 with reason), never served -> no state (404);
- the list pages by source_id with the keyset cursor, filters by country code and never lists a retired or paused key;
- the destination read answers the seeded destination;
- every statement runs as aa_app_user (a missing grant would raise here, not on Dev);
- lesson S238 guard: each detail statement's tour_id filter reaches a table scan inside the view (EXPLAIN),
  so a call never parses or resolves every live tour.
"""
import json
import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
from test_aa986_migration_274 import cur, migration_255  # noqa: E402,F401  (fixtures: seeded RDS-like state)
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401

from services.catalog_http import reader  # noqa: E402

ROLE = "aa_app_user"
BASE = "https://api.example.invalid"
CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "catalog.http.v1.openapi.yaml"
_PARAM = re.compile(r"\$(\d+)")


class PgConn:
    """The asyncpg calls reader.py makes (fetch, fetchrow), run on the psycopg2 test cursor as aa_app_user."""

    def __init__(self, c):
        self.c = c
        self.statements = 0

    def _run(self, sql, args):
        params = {f"p{i + 1}": a for i, a in enumerate(args)}
        self.c.execute("SAVEPOINT aa976")
        try:
            self.c.execute(f"SET LOCAL ROLE {ROLE}")
            self.c.execute("SELECT set_config('app.tenant_id', '', true)")
            self.c.execute(_PARAM.sub(lambda m: f"%(p{m.group(1)})s", sql), params)
            cols = [d[0] for d in self.c.description]
            rows = [dict(zip(cols, r)) for r in self.c.fetchall()]
        finally:
            self.c.execute("ROLLBACK TO SAVEPOINT aa976")
        self.statements += 1
        return rows

    async def fetch(self, sql, *args):
        return self._run(sql, args)

    async def fetchrow(self, sql, *args):
        rows = self._run(sql, args)
        return rows[0] if rows else None


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _tour_validator():
    doc = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return Draft202012Validator({"$ref": "#/components/schemas/Tour", "components": doc["components"]})


@pytest.fixture
def seeded(cur):  # noqa: F811
    """The 274 seed plus, on day 1 of the served tour, an atom naming a place and the pipeline component that links
    that place to a destination (v_tour_place lists the places of the atoms and day texts, resolved through the
    pipeline link first)."""
    tid, grp = cur.seeded["served"]
    name = f"Luang Prabang {grp[:8]}"
    dest = t228._dest(cur, name, country="Laos", lat=19.89, lng=102.13)
    t228._atom(cur, tid, day=1, place=name)
    t228._component(cur, grp, tid, day=1, name=name, destination_id=dest)
    return cur, dest


def test_served_tour_is_assembled_from_the_views_and_matches_the_contract(seeded):
    c, dest = seeded
    _, grp = c.seeded["served"]
    conn = PgConn(c)
    found = _run(reader.get_tour(conn, grp, photo_base=BASE))
    assert found.state is None and found.body is not None
    body = found.body
    assert body["source_id"] == grp and body["country_code"] == "LA"
    assert [d["day_no"] for d in body["days"]] == [1, 2, 3]
    day1 = body["days"][0]
    assert dest in [p["destination_id"] for p in day1["places"]], day1     # v_tour_place: pipeline link first
    assert day1["time_zone"] == "Asia/Vientiane"
    assert body["facets"]["intensity"] == "moderate"                        # v_tour_component.intensity_level
    errors = [e.message for e in _tour_validator().iter_errors(body)]
    assert errors == []
    assert body["content_hash"].startswith("sha256:")
    assert conn.statements == 5     # v_tour + days, places, photos, components (tour_id = ANY each)


@pytest.mark.parametrize("key, state, reason", [
    ("retired", "retired", "source_deleted"),
    ("paused", "paused", "master_inactive"),
    ("old_retired", "retired", "master_trashed"),
    ("never", None, None),
])
def test_not_served_keys_take_their_state_from_the_tombstone(seeded, key, state, reason):
    c, _ = seeded
    _, grp = c.seeded[key]
    found = _run(reader.get_tour(PgConn(c), grp, photo_base=BASE))
    assert found.body is None
    assert (found.state, found.reason) == (state, reason)


def test_restored_key_is_served(seeded):
    c, _ = seeded
    _, grp = c.seeded["restored"]
    assert _run(reader.get_tour(PgConn(c), grp, photo_base=BASE)).body is not None


def test_list_pages_by_source_id_and_never_lists_a_key_that_is_not_served(seeded):
    c, _ = seeded
    conn = PgConn(c)
    seen, after = [], None
    for _ in range(50):
        page = _run(reader.list_tours(conn, after=after, updated_since=None, country="LA", limit=2,
                                      photo_base=BASE))
        seen += [body["source_id"] for body, _ in page.items]
        after = page.next_after
        if after is None:
            break
    assert seen == sorted(seen) and len(seen) == len(set(seen))
    served = {c.seeded[k][1] for k in ("served", "restored", "supersede_served", "supersede_gap",
                                       "restored_then_superseded", "retired_then_republished")}
    not_served = {c.seeded[k][1] for k in ("retired", "paused", "old_retired", "never")}
    assert served <= set(seen)
    assert not (not_served & set(seen))


def test_list_country_filter_and_updated_since(seeded):
    c, _ = seeded
    conn = PgConn(c)
    page = _run(reader.list_tours(conn, after=None, updated_since=None, country="KR", limit=50, photo_base=BASE))
    assert all(body["country_code"] == "KR" for body, _ in page.items)
    from datetime import datetime, timezone
    future = datetime(2100, 1, 1, tzinfo=timezone.utc)
    page = _run(reader.list_tours(conn, after=None, updated_since=future, country=None, limit=50, photo_base=BASE))
    assert page.items == [] and page.next_after is None


def test_destination_read(seeded):
    c, dest = seeded
    row = _run(reader.get_destination(PgConn(c), dest))
    assert row is not None and str(row["destination_id"]) == dest and row["country"] == "Laos"


def test_catalog_connection_shape_on_a_fake_pool_matches_the_adapter():
    """The adapter above mirrors catalog_connection: one READ ONLY, REPEATABLE READ transaction, tenant GUC cleared
    first."""
    calls = []

    class Conn:
        @asynccontextmanager
        async def transaction(self, isolation=None, readonly=False):
            calls.append(("tx", isolation, readonly))
            yield

        async def execute(self, sql, *a):
            calls.append(("exec", sql))

    class Pool:
        async def acquire(self, timeout=None):
            return Conn()

        async def release(self, conn):
            calls.append(("release",))

    async def go():
        reader.reset_gate()
        async with reader.catalog_connection(Pool()):
            pass

    _run(go())
    assert calls == [("tx", "repeatable_read", True), ("exec", "SELECT set_config('app.tenant_id', '', true)"),
                     ("release",)]


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


@pytest.mark.parametrize("name", sorted(reader._DETAIL_SQL))  # noqa: SLF001
def test_detail_filter_is_pushed_down_to_the_tour_scan(seeded, name):
    """Lesson S238: `source_id = ANY` stays above catalog._tour_day_text's DISTINCT ON and v_tour_place's window
    functions (every live tour parsed per call); the reader's `tour_id = ANY` must reach a table scan inside the
    view (raw_tours, or place_photo for the photos), not stay on the view's top node."""
    c, _ = seeded
    tid, _ = c.seeded["served"]
    sql = reader._DETAIL_SQL[name].format(cond=reader.DETAIL_COND)  # noqa: SLF001
    c.execute("SAVEPOINT aa976_plan")
    try:
        c.execute(f"SET LOCAL ROLE {ROLE}")
        c.execute("EXPLAIN (FORMAT JSON, COSTS OFF) " + _PARAM.sub("%(p1)s", sql), {"p1": [str(tid)]})
        raw = c.fetchone()[0]
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa976_plan")
    plan = (raw if isinstance(raw, list) else json.loads(raw))[0]["Plan"]
    scans = [n for n in _nodes(plan) if n.get("Relation Name")]
    conds = " ".join(f"{n.get('Filter', '')} {n.get('Index Cond', '')} {n.get('Recheck Cond', '')}" for n in scans)
    assert "tour_id = ANY" in conds, (name, json.dumps(plan)[:2000])
