"""AA-1127 (C1) — SEO intelligence: the Keywords tab and the demand totals read one "used" definition and the buyer
markets only. No DB (the SQL shape); the numbers on Postgres: tests/integration/test_aa1127_seo_linked_demand_db.py."""
from api.routers import admin_seo


def test_keywords_and_demand_share_the_used_definition_and_the_markets():
    for sql in (admin_seo._KEYWORDS_SQL, admin_seo._DEMAND_BY_MARKET_SQL):
        assert admin_seo._USED_CTE in sql
        assert "market = ANY($2::text[])" in sql
    assert "US-PAA2" not in admin_seo._MARKETS
    assert "linked_volume" in admin_seo._DEMAND_BY_MARKET_SQL
    assert "ORDER BY (u.keyword IS NOT NULL) DESC" in admin_seo._KEYWORDS_SQL
