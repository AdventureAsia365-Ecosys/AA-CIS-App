"""AA-1055 PR 2 — migration 393 against real Postgres: acp_contract.place_keyword (the stored keyword map of a place),
search_demand related_searches / paa_retrieved_on / serp_location with its backfill, and the segment_research_log tier
column with its new primary key.

conftest applies every migration, 393 included. Each test works inside one transaction that rolls back; the backfill
test re-runs the file's body (it is re-runnable) over invented rows. Fixtures use invented place names only.
"""
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
M393 = ROOT / "api" / "migrations" / "393_place_keyword_demand_tiers.sql"


def _body() -> str:
    return M393.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("BEGIN")
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        conn.close()


def _columns(c, table: str) -> dict:
    c.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
              "WHERE table_schema = 'acp_contract' AND table_name = %s", (table,))
    return {r["column_name"]: r for r in c.fetchall()}


def _pk(c, table: str) -> list[str]:
    c.execute("""
        SELECT a.attname AS col FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)
    """, (f"acp_contract.{table}",))
    return [r["col"] for r in c.fetchall()]


def test_393_registered_and_shapes(cur):
    cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '393'")
    assert cur.fetchone()["ok"] == 1
    pk = _columns(cur, "place_keyword")
    assert {"subject_kind", "subject", "keyword", "rank", "country", "activity", "source", "prompt_version",
            "jev_zone", "jev_mode", "jev_probability", "proposed_at"} <= set(pk)
    assert _pk(cur, "place_keyword") == ["subject_kind", "subject", "keyword"]
    sd = _columns(cur, "search_demand")
    assert sd["related_searches"]["data_type"] == "jsonb"
    assert sd["paa_retrieved_on"]["data_type"] == "timestamp with time zone"
    assert sd["serp_location"]["is_nullable"] == "YES"
    log = _columns(cur, "segment_research_log")
    assert log["tier"]["is_nullable"] == "NO"
    assert _pk(cur, "segment_research_log") == ["canonical_place", "market", "tier"]


def test_place_keyword_checks(cur):
    cur.execute("INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, source, prompt_version, "
                "jev_zone) VALUES ('place', 'Zarvok Lake', 'zarvok lake', 'place_name', 'a3_propose@x', "
                "'not_asked')")
    cur.execute("INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, source, prompt_version, "
                "country, activity) VALUES ('country_activity', 'velmora|trek', 'velmora trek', "
                "'country_activity', 'v1', 'velmora', 'trek')")
    for sql in (
        # a country + activity row needs both parts
        "INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, source, prompt_version) "
        "VALUES ('country_activity', 'velmora|hike', 'velmora hike', 'country_activity', 'v1')",
        "INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, source, prompt_version) "
        "VALUES ('tour', 'x', 'x', 'llm_propose', 'v1')",
        "INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, source, prompt_version, jev_zone) "
        "VALUES ('place', 'Zarvok Lake', 'zarvok lake boat', 'llm_propose', 'v1', 'maybe')",
    ):
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(sql)
        cur.execute("ROLLBACK TO SAVEPOINT s")


def test_log_tiers_share_a_place_and_market(cur):
    cur.execute("INSERT INTO acp_contract.segment_research_log (canonical_place, market, researched_at) "
                "VALUES ('Zarvok Lake', 'US', now())")
    cur.execute("INSERT INTO acp_contract.segment_research_log (canonical_place, market, researched_at, tier) "
                "VALUES ('Zarvok Lake', 'US', now(), 'serp') "
                "ON CONFLICT (canonical_place, market, tier) DO UPDATE SET researched_at = now()")
    cur.execute("SELECT tier FROM acp_contract.segment_research_log WHERE canonical_place = 'Zarvok Lake' "
                "ORDER BY tier")
    assert [r["tier"] for r in cur.fetchall()] == ["serp", "volume"]
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier) "
                    "VALUES ('Zarvok Lake', 'UK', 'paa')")
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_backfill_marks_only_rows_holding_a_serp(cur):
    cur.execute("""
        INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, retrieved_on,
                                                serp_domains)
        VALUES ('zarvok lake', 'UK', 900, '["is zarvok lake deep?"]', '2026-09-01', NULL),
               ('zarvok lake trail', 'US', 40, '[]', '2026-09-02', '["example.test"]'),
               ('zarvok lake boat', 'US', 10, '[]', '2026-09-03', NULL)
    """)
    cur.execute(_body())     # re-runnable: the backfill reaches the rows inserted above
    cur.execute("SELECT keyword, paa_retrieved_on::date::text AS d, serp_location, related_searches "
                "FROM acp_contract.search_demand WHERE keyword LIKE 'zarvok lake%%' ORDER BY keyword")
    rows = {r["keyword"]: r for r in cur.fetchall()}
    assert rows["zarvok lake"]["d"] == "2026-09-01" and rows["zarvok lake"]["serp_location"] == "UK"
    assert rows["zarvok lake trail"]["d"] == "2026-09-02" and rows["zarvok lake trail"]["serp_location"] == "US"
    assert rows["zarvok lake boat"]["d"] is None and rows["zarvok lake boat"]["serp_location"] is None
    assert all(r["related_searches"] is None for r in rows.values())


def test_cis_app_can_upsert_the_map(cur):
    cur.execute("SELECT has_table_privilege('cis_app', 'acp_contract.place_keyword', 'INSERT') AS ins, "
                "has_table_privilege('cis_app', 'acp_contract.place_keyword', 'UPDATE') AS upd, "
                "has_table_privilege('cis_app', 'acp_contract.place_keyword', 'DELETE') AS del")
    row = cur.fetchone()
    assert row["ins"] and row["upd"] and row["del"]
