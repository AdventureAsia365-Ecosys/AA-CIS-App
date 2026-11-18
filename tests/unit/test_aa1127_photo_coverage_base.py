"""AA-1127 (C1, S283 UI audit) — Photos › "Tours with photos": the base is the served tours, like Places.

The tile read "162 of 917 active tours": 917 was every active raw row, while Places counted 844 live tours and Master
Content 892. The base and the covered count now read one set (admin_photos.SERVED_TOURS_SQL: live Master Content of the
master tenant + live source, shared/tour_liveness), the rule Places' coverage uses. No DB."""
import inspect
import re

from api.routers import admin_photos, admin_places
from shared import tour_liveness as tl


def _n(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def test_served_set_is_the_shared_liveness_rule():
    sql = _n(admin_photos.SERVED_TOURS_SQL)
    assert tl.live_master_sql("pt") in sql and tl.live_source_sql("rt") in sql
    assert "pt.tenant_id = $1::uuid" in sql
    from services.places import store
    assert admin_photos.MASTER_TENANT_ID == store.MASTER_TENANT_ID == "00000000-0000-0000-0000-000000000001"


def test_places_coverage_counts_the_same_live_rule():
    places = _n(admin_places.COVERAGE_SQL)
    assert "pt.master_status = 'active' AND pt.deleted_at IS NULL" in places
    assert "rt.source_status = 'active' AND rt.deleted_at IS NULL" in places
    assert "pt.tenant_id = $1::uuid" in places


def test_base_and_covered_both_read_the_served_set():
    src = _n(inspect.getsource(admin_photos.summary))
    assert '"WITH served AS (" + SERVED_TOURS_SQL + ")"' in src
    assert "tour_id IN (SELECT tour_id FROM served)" in src
    assert '"SELECT country, count(*) AS n FROM (" + SERVED_TOURS_SQL + ") s GROUP BY country", MASTER_TENANT_ID' in src
    assert "FROM silver_aa_internal.raw_tours WHERE source_status = 'active'" not in src
