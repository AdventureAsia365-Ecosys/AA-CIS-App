"""AA-1058 — cost attribution against real Postgres (every migration applied by conftest).

- A row written inside bind_attribution lands with the bound tour_id / content_piece_id (record_call, record_calls);
  an empty stage lands as 'unattributed'.
- A DataForSEO row written inside a job (bound kind) lands with that stage and app 'cis'.
- The read-only report (scripts/aa1058/ledger_attribution.py) runs every query against the real columns and prints
  the shares and the cost per tour of the rows above.
Every test removes its own rows.
"""
import asyncio
import io
import os
import sys
from contextlib import redirect_stdout

import asyncpg
import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from scripts.aa1058 import ledger_attribution as report_mod  # noqa: E402
from shared.dfs_client import call_log as dfs_log  # noqa: E402
from shared.llm_client import call_log, capacity  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TOUR = "51515151-5151-4151-8151-515151515151"
PIECE = "52525252-5252-4252-8252-525252525252"
DFS_ENDPOINT = "zz_aa1058_endpoint"


@pytest.fixture
def cur(monkeypatch):
    real_connect = asyncpg.connect

    async def connect(*a, **k):                 # CI Postgres has no SSL; the code asks for it
        return await real_connect(DSN, **{key: v for key, v in k.items() if key == "timeout"})
    for mod in (call_log, dfs_log):
        monkeypatch.setattr(mod.asyncpg, "connect", connect)
        monkeypatch.setattr(mod, "get_database_url", lambda: DSN)
        monkeypatch.setattr(mod, "_v2_missing_at", 0.0)
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield c
    finally:
        c.execute("DELETE FROM shared.llm_call_log WHERE tour_id = %s OR content_piece_id = %s "
                  "OR stage = %s", (TOUR, PIECE, call_log.UNATTRIBUTED_STAGE))
        c.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = %s", (DFS_ENDPOINT,))
        conn.close()


def _row(stage, **kw):
    return dict(stage=stage, role="writer", model="haiku-4-5", tokens_in=10, tokens_out=5,
                quality_signal={"json_parsed": True}, **kw)


def _write_fixture_rows():
    with call_log.bind_attribution(tour_id=TOUR):
        asyncio.run(call_log.record_call(**_row("s1_generate", cost_usd=0.04)))
        asyncio.run(call_log.record_call(**_row("s1_judge", cost_usd=0.01)))
        with call_log.bind_attribution(content_piece_id=PIECE):
            asyncio.run(call_log.record_calls([_row("t10_judge", cost_usd=0.002)]))
    asyncio.run(call_log.record_call(**_row("", cost_usd=0.0)))


def test_bound_rows_land_with_their_ids(cur):
    _write_fixture_rows()
    cur.execute("SELECT stage, tour_id::text AS tour, content_piece_id::text AS piece, app "
                "FROM shared.llm_call_log WHERE tour_id = %s OR stage = %s ORDER BY stage",
                (TOUR, call_log.UNATTRIBUTED_STAGE))
    got = [(r["stage"], r["tour"], r["piece"], r["app"]) for r in cur.fetchall()]
    assert got == [("s1_generate", TOUR, None, "cis"), ("s1_judge", TOUR, None, "cis"),
                   ("t10_judge", TOUR, PIECE, "cis"), ("unattributed", None, None, "cis")]


def test_a_dfs_row_in_a_job_names_the_job_kind(cur):
    with capacity.bind_kind("s1_seo_prefetch"):
        asyncio.run(dfs_log.record_dfs_call(endpoint=DFS_ENDPOINT, fetched_live=True, cost_usd=0.002))
    asyncio.run(dfs_log.record_dfs_call(endpoint=DFS_ENDPOINT, cache_hit=True, cost_usd=0.0, stage="s1_seo"))
    cur.execute("SELECT stage, app FROM shared.dfs_call_log WHERE endpoint = %s ORDER BY stage", (DFS_ENDPOINT,))
    assert [(r["stage"], r["app"]) for r in cur.fetchall()] == [("s1_seo", "cis"), ("s1_seo_prefetch", "cis")]


def test_the_report_runs_on_the_real_columns(cur):
    _write_fixture_rows()
    with capacity.bind_kind("s1_seo_prefetch"):
        asyncio.run(dfs_log.record_dfs_call(endpoint=DFS_ENDPOINT, fetched_live=True, cost_usd=0.002))

    async def run():
        conn = await asyncpg.connect(DSN)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                await report_mod.report(conn, 1)
            return buf.getvalue()
        finally:
            await conn.close()

    text = asyncio.run(run())
    assert "ERR " not in text, text
    lines = dict(line.split("=", 1) for line in text.splitlines() if line.startswith("ATTRIB ") and "=" in line)
    assert lines["ATTRIB rules"] == "image"
    assert lines["ATTRIB tours.count"] == "1" and lines["ATTRIB tours.cost"] == "0.0520"
    assert lines["ATTRIB tours.cost_per_tour"] == "0.0520"
    assert lines["ATTRIB tours.prefix.s1"].startswith("tours=1 cost=0.0500")
    assert lines["ATTRIB pieces.prefix.t10"].startswith("pieces=1 cost=0.0020")
    assert any(k.startswith("ATTRIB stage.s1_generate rows") for k in lines)
    s1 = next(line for line in text.splitlines() if line.startswith("ATTRIB stage.s1_generate "))
    assert "tour=1.000" in s1 and "rule=tour_id" in s1 and "rule_ok=1.000" in s1
    assert int(lines["ATTRIB ledger.unattributed_stage"]) >= 1
    assert lines["ATTRIB dfs.s1_seo_prefetch.cis"].startswith("rows=")
    assert "ATTRIB s1.versions" in lines
