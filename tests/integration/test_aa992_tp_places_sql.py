"""AA-992 (S252 live fix) — the tp_places statements of the data coverage page on the RDS-like catalog state.

On Dev ``catalog.v_tour_place`` read whole took 6.1 s and timed out the whole ``tp_layers`` statement, so it moved to
its own source (``tp_places``). ``queries.TENANT_TP_PLACES_WHOLE`` (one pass over the view) and
``queries.TENANT_TP_PLACES_PER_TOUR`` (per served tour, the first located place by ``tour_id``) must give the same
numbers; which one ships is chosen by timing both live. This module reuses the seeded state of
test_aa986_migration_274 (253 / 256 / 257 / 258 / 273 / 274 applied as cis_owner) plus, as in
test_aa976_catalog_http_reader, one atom on the served tour whose place the pipeline links to a located destination.

Checks: both statements run as aa_app_user, agree, and count the located tour; the per-tour form's ``tour_id`` filter
reaches a table scan inside the view (lesson S238, as the CRM reader's detail reads), so it never resolves the places
of every live tour once per served tour.
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
from test_aa986_migration_274 import cur, migration_255  # noqa: E402,F401  (fixtures: seeded RDS-like state)
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401

from api.services.data_coverage import queries as q  # noqa: E402

ROLE = "aa_app_user"
VARIANTS = {"whole": q.TENANT_TP_PLACES_WHOLE, "per_tour": q.TENANT_TP_PLACES_PER_TOUR}


@pytest.fixture
def seeded(cur):  # noqa: F811
    tid, grp = cur.seeded["served"]
    name = f"Luang Prabang {grp[:8]}"
    dest = t228._dest(cur, name, country="Laos", lat=19.89, lng=102.13)
    t228._atom(cur, tid, day=1, place=name)
    t228._component(cur, grp, tid, day=1, name=name, destination_id=dest)
    return cur


def _as_reader(c, sql, explain=False):
    c.execute("SAVEPOINT aa992_places")
    try:
        c.execute(f"SET LOCAL ROLE {ROLE}")
        c.execute("SELECT set_config('app.tenant_id', '', true)")
        c.execute(("EXPLAIN (FORMAT JSON, COSTS OFF) " if explain else "") + sql)
        if explain:
            raw = c.fetchone()[0]
            return (raw if isinstance(raw, list) else json.loads(raw))[0]["Plan"]
        cols = [d[0] for d in c.description]
        return dict(zip(cols, c.fetchone()))
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa992_places")


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


def test_both_forms_agree_and_count_the_located_tour(seeded):
    out = {k: _as_reader(seeded, sql) for k, sql in VARIANTS.items()}
    assert out["whole"] == out["per_tour"], out
    row = out["whole"]
    assert set(row) == {"served_tours", "with_located_places"}
    assert row["with_located_places"] >= 1                     # the seeded served tour has a located place
    assert row["served_tours"] > row["with_located_places"]    # the restored key is served without one


def test_both_forms_agree_when_the_place_loses_its_coordinates(seeded):
    seeded.execute("UPDATE shared.destinations SET geo_status = 'suspect' WHERE name LIKE 'Luang Prabang %'")
    out = {k: _as_reader(seeded, sql) for k, sql in VARIANTS.items()}
    assert out["whole"] == out["per_tour"], out


def test_the_per_tour_form_filters_the_view_by_tour_id(seeded):
    """Under the LIMIT (the per-tour subquery), every live-tour scan inside v_tour_place is keyed on the outer tour's
    tour_id: the itinerary is parsed and the places resolved for that tour only. The planner also drops the view's
    unused row_number(), so the nested loop streams and LIMIT 1 stops at the tour's first located place."""
    plan = _as_reader(seeded, q.TENANT_TP_PLACES_PER_TOUR, explain=True)
    limits = [n for n in _nodes(plan) if n["Node Type"] == "Limit"
              and any(m.get("Relation Name") == "tour_atoms" for m in _nodes(n))]
    assert len(limits) == 1, json.dumps(plan)[:3000]
    live_scans = [n for n in _nodes(limits[0]) if n.get("Relation Name") in ("published_tours", "raw_tours")]
    assert live_scans, json.dumps(limits[0])[:3000]
    for n in live_scans:
        assert re.search(r"\btour_id = \w+\.tour_id", f"{n.get('Index Cond', '')} {n.get('Filter', '')}"), \
            json.dumps(n)[:1500]
