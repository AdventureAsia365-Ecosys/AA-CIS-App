"""AA-1169 — the Eval runs reads (services/writing_memory/eval_review.py) on real Postgres, as cis_app.

One transaction, rolled back. The rows are invented (no text from the DB, audits or prompts). Triggers are off while
the rows are inserted (session_replication_role = replica: the result guard and the FKs are not the point here); CHECK
constraints still apply. The reads then run as cis_app (the API's login), so the grants and RLS policies the API
relies on are exercised too.

What it proves:
  * RUNS_SQL filters by writer, run kind and pre-registration id, counts the result rows per verdict and pages;
  * RUN_ROWS_SQL groups a run's rows per golden item, with the judge claims typed and no output text;
  * ITEM_SQL / ITEM_ROWS_SQL / SCORED_SQL give one item's source, its rows and the scored output of a re-judged
    control row (which has no output of its own), side by side;
  * an item that is not in the run, and a run of another pipeline, read as absent.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.writing_memory import eval_review as er  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
PREREG = "zz-aa1169-proof"
OUTPUT = {"seo_title": "Walking the Lantern Hills of Norvale", "slug": "lantern-hills-norvale",
          "content_text": "Day 1 starts at the Velin market.\n\nIt is one of the rarest views in the whole valley."}
CONTROL_OUTPUT = {"seo_title": "Lantern Hills", "content_text": "By evening the group reaches the ridge camp."}
INPUT = {"context": {"channel": "blog", "trip_name": "Lantern Hills", "facts_text": "The walk is 12 km."}}
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000f1169")   # a T9 item is always tenant scoped (CIS 284)
CLAIMS = [{"class": "unsupported", "claim": "It is one of the rarest views in the whole valley.", "why": "invented"}]


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("SET LOCAL session_replication_role = replica")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _run(c, *, writer="t9_blog", kind="r2", prereg=PREREG, pipeline="writing_memory"):
    details = {"prereg": {"id": prereg, "arm": "B"}} if prereg else {}
    if pipeline != "writing_memory":
        return await c.fetchval(
            "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, regression_detected, "
            "triggered_by, run_kind) VALUES ($1, 'zz1169', 0, false, 'pytest', 'regression') RETURNING id", pipeline)
    return await c.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, fingerprint) VALUES ('writing_memory', 'zz1169', 1, 0.5, $1::jsonb, 'pytest', $2, $3, "
        "'{}'::jsonb) RETURNING id", json.dumps(details), kind, writer)


async def _result(c, run, item, *, arm, mode, verdict, output=None, claims=None, scored=None):
    details = {"judge_claims": claims} if claims else {}
    return await c.fetchval(
        "INSERT INTO shared.eval_item_result (run_id, item_id, writer, scope, arm, mode, phase, scored_result_id, "
        "output, output_sha256, verdict, failure_codes, hard_stop, judge_version, cost_usd, details, tenant_id) "
        "VALUES ($1, $2, 't9_blog', 'tenant', $3, $4, 'holdout', $5, $6::jsonb, $7, $8, $9::text[], false, "
        "'judge-v1', 0.01, $10::jsonb, $11) RETURNING result_id",
        run, item, arm, mode, scored, json.dumps(output) if output else None, "a" * 64 if output else None, verdict,
        ["claims"] if claims else [], json.dumps(details), TENANT)


async def _seed(c) -> dict:
    gset = await c.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, status, source_uri, source_sha256, seed, holdout_pct, "
        "tier_rules, shared_inputs, meta, label_history, item_count, imported_by) VALUES ('zz_aa1169', '1.0.0', "
        "'active', 'file:zz-aa1169.json', $1, 7, 0.300, '{}', '{}', '{}', '[]', 2, 'pytest') RETURNING set_id",
        "e" * 64)
    items = []
    for sample in ("zz1169-b", "zz1169-a"):
        items.append(await c.fetchval(
            "INSERT INTO shared.eval_golden_item (set_id, sample_id, writer, channel, scope, tenant_id, split, trap, "
            "kind, tiers, input, control_output, post_edit, label, label_verdict, label_source, refs, input_sha256) "
            "VALUES ($1, $2, 't9_blog', 'blog', 'tenant', $5, 'holdout', false, 'real', ARRAY['trek_hike'], "
            "$3::jsonb, NULL, false, '{\"verdict\": \"fail\"}', 'fail', 'agent_panel', '{}', $4) RETURNING item_id",
            gset, sample, json.dumps(INPUT), "f" * 64, TENANT))
    run = await _run(c)
    other_run = await _run(c, kind="r1", prereg=None)
    legacy = await _run(c, pipeline="s1_old")
    first, second = items
    source = await _result(c, other_run, first, arm="control", mode="regenerate", verdict="fail",
                           output=CONTROL_OUTPUT)
    cand = await _result(c, run, first, arm="candidate", mode="regenerate", verdict="fail", output=OUTPUT,
                         claims=CLAIMS)
    ctl = await _result(c, run, first, arm="control", mode="rejudge", verdict="pass", scored=source)
    await _result(c, run, second, arm="candidate", mode="regenerate", verdict="pass", output=OUTPUT)
    return {"run": str(run), "other_run": str(other_run), "legacy": str(legacy), "first": str(first),
            "second": str(second), "cand": str(cand), "ctl": str(ctl), "source": str(source)}


async def _as_cis_app(c):
    await c.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await c.execute("GRANT SELECT ON shared.prompt_eval_runs, shared.eval_item_result, shared.eval_golden_item "
                    "TO cis_app")
    await c.execute("SET LOCAL ROLE cis_app")


@pytest.mark.asyncio
async def test_runs_filter_count_and_page(conn):
    ids = await _seed(conn)
    await _as_cis_app(conn)
    page = await er.list_runs(conn, writer="t9_blog", run_kind=None, prereg=PREREG, limit=10, offset=0)
    (run,) = page["runs"]
    assert page["total"] == 1 and run["run_id"] == ids["run"] and run["prereg_id"] == PREREG
    assert (run["results"], run["passes"], run["fails"], run["errors"], run["outputs"]) == (3, 2, 1, 0, 2)
    assert run["cost_usd"] == 0.5 and isinstance(run["created_at"], str)
    both = await er.list_runs(conn, writer="t9_blog", run_kind=None, prereg=None, limit=50, offset=0)
    assert {ids["run"], ids["other_run"]} <= {r["run_id"] for r in both["runs"]}
    assert ids["legacy"] not in {r["run_id"] for r in both["runs"]}           # another pipeline is not listed
    r1 = await er.list_runs(conn, writer=None, run_kind="r1", prereg=None, limit=50, offset=0)
    assert ids["other_run"] in {r["run_id"] for r in r1["runs"]} and ids["run"] not in {r["run_id"] for r in r1["runs"]}
    paged = await er.list_runs(conn, writer="t9_blog", run_kind=None, prereg=None, limit=1, offset=1)
    assert len(paged["runs"]) == 1 and paged["total"] >= 2


@pytest.mark.asyncio
async def test_run_groups_rows_per_item_without_text(conn):
    ids = await _seed(conn)
    await _as_cis_app(conn)
    got = await er.read_run(conn, ids["run"])
    assert [i["sample_id"] for i in got["items"]] == ["zz1169-a", "zz1169-b"]
    b = got["items"][1]
    assert [(r["arm"], r["mode"]) for r in b["rows"]] == [("candidate", "regenerate"), ("control", "rejudge")]
    assert b["rows"][0]["claim_classes"] == {"rarity": 1} and b["rows"][1]["scored_result_id"] == ids["source"]
    assert "rarest" not in json.dumps(got)
    assert got["summary"]["candidate"]["judged"] == 2 and got["summary"]["candidate"]["passes"] == 1
    assert await er.read_run(conn, ids["legacy"]) is None
    assert await er.read_run(conn, str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_item_reads_its_rows_and_the_scored_output(conn):
    ids = await _seed(conn)
    await _as_cis_app(conn)
    got = await er.read_item(conn, ids["run"], ids["first"])
    assert got["item"]["sample_id"] == "zz1169-b" and got["item"]["label"] == {"verdict": "fail"}
    assert got["input"] == INPUT and got["item"]["tiers"] == ["trek_hike"]
    cand, ctl = got["results"]
    assert cand["output_source"] == "own" and cand["claims"][0]["policy_class"] == "rarity"
    assert cand["output_fields"][-1] == {"field": "content_text", "text": OUTPUT["content_text"]}
    assert ctl["output_source"] == "scored" and ctl["output_fields"][-1]["text"] == CONTROL_OUTPUT["content_text"]
    assert "gate_replay" in cand                                 # replayed or "could not be replayed", never fatal
    assert await er.read_item(conn, ids["other_run"], ids["second"]) is None   # not an item of that run
    assert await er.read_item(conn, ids["run"], str(uuid.uuid4())) is None
