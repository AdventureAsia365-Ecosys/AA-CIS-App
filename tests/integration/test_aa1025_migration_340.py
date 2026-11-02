"""AA-1025 A7 — migration 340 (central LLM budgets in shared.spend_budget: provider 'llm', scopes app / stage / job /
eval / adhoc, per_month_usd, the T-8 alert-only app:cis seed) against real Postgres.

conftest applies every migration, 340 included. The tests re-run the file's body inside one transaction (it is
re-runnable) and roll back what they wrote.
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
M340 = ROOT / "api" / "migrations" / "340_spend_budget_central_llm_scopes.sql"
ACTOR = "migration-340-aa1025"


def _body() -> str:
    return M340.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("BEGIN")
    # Another module may leave an older CHECK behind (test_aa1002_migration_304 ends in the post-304 state, whose
    # spend_budget_provider_check has no 'llm'), so every test starts from 340's state: re-run its body (re-runnable)
    # inside this test's transaction; the ROLLBACK below undoes it with the rest.
    c.execute(_body())
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        conn.close()


def _insert(c, provider: str, scope: str, **cols) -> None:
    names = ["provider", "scope", *cols]
    c.execute(f"INSERT INTO shared.spend_budget ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))})",
              (provider, scope, *cols.values()))


def _spend(c, days_ago: int, usd: float, app: str = "cis", n: int = 1) -> None:
    """``n`` ledger rows of ``usd`` each, at noon UTC ``days_ago`` days before today."""
    for _ in range(n):
        c.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, cost_usd, app, created_at) "
                  "VALUES ('t9_write', 'writer', 'sonnet-5-5', '{}'::jsonb, %s, %s, "
                  "date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc' - make_interval(days => %s) "
                  "+ interval '12 hours')", (usd, app, days_ago))


def _seed(c):
    c.execute("SELECT per_run_usd::float AS run, per_day_usd::float AS day, per_month_usd::float AS month, "
              "hard_stop AS hard, alert_pct AS pct, updated_by AS who FROM shared.spend_budget "
              "WHERE provider = 'llm' AND scope = 'app:cis'")
    return c.fetchone()


def test_registered(cur):
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '340'")
    assert cur.fetchone()["ok"] == "340"


@pytest.mark.parametrize("scope", ["app:cis", "stage:t9_write", "job:place_dedupe", "eval", "adhoc",
                                   "stage:a3_place_dedupe_check"])
def test_an_llm_row_takes_the_central_scopes(cur, scope):
    _insert(cur, "llm", scope, per_day_usd=5, per_month_usd=50, hard_stop=False)
    cur.execute("SELECT per_month_usd::float AS m FROM shared.spend_budget WHERE provider = 'llm' AND scope = %s",
                (scope,))
    assert cur.fetchone()["m"] == 50.0


@pytest.mark.parametrize("scope", ["global", "stage:", "app:CIS", "tenant:x", "eval:x", "stage:t9 write"])
def test_an_llm_row_refuses_other_scopes(cur, scope):
    with pytest.raises(psycopg2.errors.CheckViolation, match="spend_budget_scope_check"):
        _insert(cur, "llm", scope)


@pytest.mark.parametrize("scope", ["stage:t9_write", "app:cis", "eval", "adhoc"])
def test_a_provider_row_keeps_global_and_job_scopes_only(cur, scope):
    _insert(cur, "bedrock", "job:zz_kind", per_run_usd=1)                    # still allowed
    with pytest.raises(psycopg2.errors.CheckViolation, match="spend_budget_scope_check"):
        _insert(cur, "bedrock", scope)


def test_a_negative_month_cap_is_refused(cur):
    with pytest.raises(psycopg2.errors.CheckViolation, match="spend_budget_per_month_usd_check"):
        _insert(cur, "llm", "adhoc", per_month_usd=-1)


def test_the_seed_is_p95_of_30_days_times_1_5_alert_only(cur):
    cur.execute("DELETE FROM shared.spend_budget WHERE provider = 'llm'")
    cur.execute("DELETE FROM shared.llm_call_log")
    for day in range(1, 31):                                                 # 30 days: 1, 2, ..., 30 USD
        _spend(cur, day, float(day))
    _spend(cur, 3, 1000.0, app="tripplanner")                                 # another app's spend counts not
    _spend(cur, 0, 500.0)                                                     # today is not a full day: left out
    _spend(cur, 40, 900.0)                                                    # older than 30 days: left out
    cur.execute(_body())
    # percentile_cont(0.95) of 1..30 = 28.55; x 1.5 = 42.825 -> 42.83 (numeric round half away from zero)
    assert _seed(cur) == {"run": None, "day": 42.83, "month": None, "hard": False, "pct": 80, "who": ACTOR}


def test_days_without_spend_count_as_zero(cur):
    cur.execute("DELETE FROM shared.spend_budget WHERE provider = 'llm'")
    cur.execute("DELETE FROM shared.llm_call_log")
    _spend(cur, 2, 10.0)                                                      # one day of 30 spent: p95 = 0 -> none
    cur.execute(_body())
    assert _seed(cur) is None
    for day in range(1, 4):                                                   # days 1..3 spent: 10, 20, 10 USD
        _spend(cur, day, 10.0)
    cur.execute(_body())
    assert _seed(cur)["day"] == 15.0                                          # sorted: 27 x 0, 10, 10, 20 -> p95 10


def test_no_cis_spend_means_no_seed(cur):
    cur.execute("DELETE FROM shared.spend_budget WHERE provider = 'llm'")
    cur.execute("DELETE FROM shared.llm_call_log")
    cur.execute(_body())
    assert _seed(cur) is None


def test_an_admin_row_is_never_overwritten(cur):
    cur.execute("DELETE FROM shared.spend_budget WHERE provider = 'llm'")
    _insert(cur, "llm", "app:cis", per_day_usd=7, hard_stop=True, alert_pct=90, updated_by="admin:zz-340")
    for day in range(1, 31):
        _spend(cur, day, 3.0)
    cur.execute(_body())
    assert _seed(cur) == {"run": None, "day": 7.0, "month": None, "hard": True, "pct": 90, "who": "admin:zz-340"}


def test_existing_rows_are_unchanged_by_a_rerun(cur):
    cur.execute("SELECT provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct, updated_by "
                "FROM shared.spend_budget WHERE provider <> 'llm' ORDER BY 1, 2")
    before = cur.fetchall()
    cur.execute(_body())
    cur.execute("SELECT provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct, updated_by "
                "FROM shared.spend_budget WHERE provider <> 'llm' ORDER BY 1, 2")
    assert cur.fetchall() == before


def test_guard_refuses_without_304(cur):
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '304'")
    with pytest.raises(psycopg2.errors.RaiseException, match="apply 304 first"):
        cur.execute(_body())
