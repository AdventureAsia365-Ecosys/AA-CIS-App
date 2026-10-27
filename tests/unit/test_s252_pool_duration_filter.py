"""S252 — the Browse Pool duration filters use the bounded catalog-style expression, never the digit concatenation
that overflowed INTEGER (the real-SQL behaviour is in tests/integration/test_s252_pool_duration_filter.py)."""
import inspect

from api.routers import v1_tours


def test_pool_duration_filters_use_the_bounded_expression():
    src = inspect.getsource(v1_tours)
    assert "REGEXP_REPLACE(COALESCE(rt.duration,''),'[^0-9]'" not in src
    assert src.count("{_DURATION_DAYS_SQL} >=") == 1 and src.count("{_DURATION_DAYS_SQL} <=") == 1
    assert r"(\d{1,4})" in v1_tours._DURATION_DAYS_SQL
