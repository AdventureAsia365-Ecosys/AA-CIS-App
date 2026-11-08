"""AA-1052 W6 — lineage of every writer output (services/quality/lineage.py), the stamp the final contract's store
step writes (final_contract.stamp_stored) and GET /admin/quality/lineage/* (api/routers/admin_quality_lineage.py).

No database: the SQL itself runs in tests/integration/test_aa1052_lineage_sql.py.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from services.quality import final_contract as fc
from services.quality import lineage, writing_policy
from shared.llm_client import route_settings

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
OBJ = "00000000-0000-0000-0000-0000000a1052"
FS = "00000000-0000-0000-0000-0000000f5052"
JOB = "00000000-0000-0000-0000-00000000b052"


@pytest.fixture
def route_version(monkeypatch):
    monkeypatch.setattr(route_settings, "_state", ("model_first", 41))
    return 41


# ── the stamp ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_stamp_names_the_policy_and_the_cached_route_version(route_version):
    st = lineage.stamp()
    assert st == {"v": lineage.LINEAGE_VERSION, "policy_sha": writing_policy.policy_sha(),
                  "route_config_version": route_version}


def test_stamp_never_reads_the_database(monkeypatch):
    """No settings row read yet in this process: the route version is None, and nothing is fetched."""
    monkeypatch.setattr(route_settings, "_state", None)
    monkeypatch.setattr(route_settings, "_load", MagicMock(side_effect=AssertionError("no read")))
    assert lineage.stamp()["route_config_version"] is None


def test_stamp_survives_a_broken_policy(monkeypatch, route_version):
    monkeypatch.setattr(writing_policy, "policy_sha", MagicMock(side_effect=RuntimeError("bad file")))
    st = lineage.stamp()
    assert st["policy_sha"] is None and st["route_config_version"] == route_version


def test_every_store_step_writes_the_lineage_with_the_contract(route_version):
    """final_contract.stamp_stored is the one store step of every S1 / T2 / T9 exit (the topology test pins that)."""
    rec = fc.Contract(workflow=fc.T9, exit="done", verdict=fc.APPROVED, text_sha=fc.text_sha("A text.")).record()
    out = fc.stamp_stored(rec, fc.text_sha("A text."), actual="approved")
    assert out["lineage"] == {"v": lineage.LINEAGE_VERSION, "policy_sha": writing_policy.policy_sha(),
                              "route_config_version": route_version}
    assert out["sha_match"] is True and out["verdict"] == fc.APPROVED     # the contract itself is unchanged
    assert fc.stamp_stored(None, "x") is None


@pytest.mark.parametrize("workflow,exit", [(fc.S1, "approve"), (fc.S1, "human_edit"), (fc.S1, "revalidation"),
                                           (fc.S1, "auto_publish"), (fc.T2, "admin_sentence_apply"),
                                           (fc.T9, "admin_sentence_apply")])
def test_a_later_exit_stamps_its_policy_but_no_route_version(route_version, workflow, exit):
    """A later exit often runs in the API process, where the route cache is None or stale: no number, not a wrong one."""
    rec = fc.Contract(workflow=workflow, exit=exit, verdict=fc.PASS_VERDICT[workflow], text_sha="s").record()
    out = fc.stamp_stored(rec, "s")
    assert out["lineage"] == {"v": lineage.LINEAGE_VERSION, "policy_sha": writing_policy.policy_sha(),
                              "route_config_version": None}


def test_every_writer_exit_is_a_contract_exit():
    """WRITER_EXITS names only exits the final contract records (a typo would silently drop the route version)."""
    for workflow, exits in lineage.WRITER_EXITS.items():
        known = {e for names in fc.EXITS[workflow].values() for e in names}
        assert exits <= known, (workflow, exits - known)


def test_writer_lineage_is_the_writer_exits_own_stamp(route_version, monkeypatch):
    rec = fc.stamp_stored(fc.Contract(workflow=fc.T2, exit="done", verdict=fc.AI_GENERATED, text_sha="s").record(),
                          "s")
    monkeypatch.setattr(writing_policy, "policy_sha", lambda: "later-policy")
    monkeypatch.setattr(route_settings, "_state", ("model_first", 99))
    assert lineage.writer_lineage(rec) == rec["lineage"]                # same moment as the contract, not "now"
    assert rec["lineage"]["route_config_version"] == route_version
    # no record (a graph that left none) or a later exit's record: a fresh stamp with the route version
    assert lineage.writer_lineage(None) == {"v": lineage.LINEAGE_VERSION, "policy_sha": "later-policy",
                                            "route_config_version": 99}
    later = {**rec, "exit": "admin_sentence_apply"}
    assert lineage.writer_lineage(later)["route_config_version"] == 99


def test_the_exit_log_keeps_each_exits_stamp(route_version):
    rec = fc.stamp_stored(fc.Contract(workflow=fc.S1, exit="approve", verdict=fc.PUBLISH, text_sha="s").record(),
                          "s")
    assert fc.compact(rec)["lineage"] == rec["lineage"]


def test_the_object_reads_take_the_writer_time_key_not_the_contract_record():
    """A later exit replaces final_contract (S1 append, T2 / T9 sentence apply): the lineage read must not look there."""
    for wf in (lineage.S1, lineage.T2, lineage.T9):
        assert "'final_contract'" not in lineage.OBJECT_SQL[wf] + lineage.COVERAGE_SQL[wf], wf
        assert f"->'{lineage.KEY}'" in lineage.OBJECT_SQL[wf], wf


# ── assembly ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _row(**kw):
    base = {"object_id": OBJ, "tenant_id": "t-1", "created_at": NOW, "status": "approved", "fs_id": FS,
            "writer_prompt_version": "a1b2c3d4", "job_id": JOB, "tour_id": None,
            "stamp": json.dumps({"v": lineage.LINEAGE_VERSION, "policy_sha": "739223d6cc24f1e5",
                                 "route_config_version": 41})}
    base.update(kw)
    return base


def _call(stage="t9_write", pv="v2", rv=41, model="sonnet-5-5", calls=1):
    return {"stage": stage, "prompt_version": pv, "route_config_version": rv, "model": model, "calls": calls,
            "first_at": NOW, "last_at": NOW}


def test_a_complete_t9_piece():
    out = lineage.assemble("t9", _row(), [_call(), _call("t10_judge", "v3", 41, "gpt-6-luna", 2)],
                           ledger_join="piece")
    assert out["complete"] is True and out["missing"] == []
    assert out["policy_sha"] == "739223d6cc24f1e5" and out["fs_id"] == FS
    assert out["prompt_version"] == {"writer": "a1b2c3d4", "stages": {"t9_write": ["v2"], "t10_judge": ["v3"]}}
    assert out["route_config_version"] == {"stamped": 41, "ledger": [41]}
    assert out["calls"][1]["calls"] == 2 and out["calls"][0]["first_at"] == NOW.isoformat()
    assert out["ledger_join"] == "piece" and out["lineage_version"] == lineage.LINEAGE_VERSION


def test_an_object_written_before_the_stamp_names_what_it_lacks():
    """A version stored before W6 has no lineage stamp and, here, no FactSheet: the ledger still gives the route."""
    out = lineage.assemble("s1", _row(stamp=None, fs_id=None, tour_id="tour-1"), [_call("s1_generate", "v1", 39)])
    assert out["missing"] == ["policy_sha", "fs_id"] and out["complete"] is False
    assert out["route_config_version"] == {"stamped": None, "ledger": [39]} and out["tour_id"] == "tour-1"


def test_nothing_known_at_all():
    out = lineage.assemble("t2", _row(stamp="{}", fs_id=None, writer_prompt_version=None), [])
    assert out["missing"] == list(lineage.FIELDS)


def test_the_eval_expects_no_factsheet_and_reads_its_run():
    row = _row(fs_id=None, run_id="run-1", item_id="item-1", run_prompt_version="wm-r2",
               stamp={"policy_sha": "739223d6cc24f1e5", "route_config_version": "40", "config_fp": "c" * 64})
    out = lineage.assemble("eval", row, [_call("writing_memory_eval", "v2", 40, "gpt-6-luna")])
    assert out["complete"] is True and "fs_id" not in lineage.EXPECTED["eval"]
    assert out["run"] == {"run_id": "run-1", "item_id": "item-1", "prompt_version": "wm-r2",
                          "config_fp": "c" * 64}
    assert out["route_config_version"]["stamped"] == 40


def test_a_repeated_stage_version_is_listed_once():
    out = lineage.assemble("t9", _row(), [_call(), _call(model="other"), _call(pv=None)])
    assert out["prompt_version"]["stages"] == {"t9_write": ["v2"]}


def test_unknown_workflow():
    with pytest.raises(ValueError):
        lineage.assemble("t7", _row())
    with pytest.raises(ValueError):
        lineage.ledger_args("t7", _row())


def test_ledger_arguments_per_workflow():
    r = _row(tour_id="tour-1", run_id="run-1", item_id="item-1", run_created_at=NOW, run_finished_at=NOW)
    assert lineage.ledger_args("s1", r) == (JOB, "tour-1", NOW)
    assert lineage.ledger_args("t2", r) == (JOB,)
    assert lineage.ledger_args("t9", r) == (OBJ, JOB)
    assert lineage.ledger_args("eval", r) == ("run-1", "item-1", NOW, NOW)
    assert not lineage.can_read_ledger("t2", _row(job_id=None))
    assert lineage.can_read_ledger("s1", _row(job_id=None, tour_id="tour-1"))
    assert not lineage.can_read_ledger("s1", _row(job_id=None, tour_id=None))


def test_every_workflow_has_its_sql():
    for wf in lineage.WORKFLOWS:
        assert wf in lineage.OBJECT_SQL and wf in lineage.LEDGER_SQL and wf in lineage.COVERAGE_SQL
        for col in ("object_id", "fs_id", "writer_prompt_version", "stamp", "created_at"):
            assert f"AS {col}" in lineage.OBJECT_SQL[wf] or f".{col}" in lineage.OBJECT_SQL[wf], (wf, col)
        for f in ("objects",) + lineage.FIELDS:
            assert f"AS {f}" in lineage.COVERAGE_SQL[wf], (wf, f)


def test_the_sql_reads_where_the_writers_write():
    """The keys the readers use are the ones the store steps write (the writer-time lineage key, writer stamps)."""
    assert f"gc.metadata->'{lineage.KEY}'" in lineage.OBJECT_SQL["s1"]
    assert f"judge_breakdown->'{lineage.KEY}'" in lineage.OBJECT_SQL["t2"]
    assert f"writer_input->'{lineage.KEY}'" in lineage.OBJECT_SQL["t9"]
    from services.content_generation.s1_checkpoint import JOB_MARKER_KEY
    assert f"metadata->>'{JOB_MARKER_KEY}'" in lineage.OBJECT_SQL["s1"]


# Every writer store step of S1 / T2 / T9 (the function that writes the writer's text with its contract record).
WRITER_STORE_STEPS = {
    "api/routers/admin_pipeline.py": "_execute_run_tour",
    "services/jobs/t2_rewrite_job.py": ("_rewrite_and_save", "_save_policy_hold"),
    "services/content_generation/t2_orchestrator.py": "save_version",
    "services/acp_content_writing/service.py": "_finalize_piece",
}


def test_every_writer_store_step_writes_the_writer_time_lineage():
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    for path, names in WRITER_STORE_STEPS.items():
        tree = ast.parse((root / path).read_text())
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in (names if isinstance(names, tuple) else (names,)):
            assert name in funcs, (path, name)
            calls = {c.func.attr for c in ast.walk(funcs[name])
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
            assert "writer_lineage" in calls, f"{path}::{name} stores writer text without the writer-time lineage"


# ── coverage ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_coverage_shares():
    row = {"objects": 4, "policy_sha": 2, "fs_id": 4, "prompt_version": 4, "route_config_version": 3}
    cov = lineage.coverage_from_row("t9", row)
    assert cov["objects"] == 4
    assert cov["fields"]["policy_sha"] == {"count": 2, "share": 0.5}
    assert cov["fields"]["route_config_version"]["share"] == 0.75
    assert cov["complete_share_floor"] == 0.25


def test_coverage_of_the_eval_and_of_nothing():
    cov = lineage.coverage_from_row("eval", {"objects": 2, "fs_id": 0, "policy_sha": 2, "prompt_version": 2,
                                             "route_config_version": 2})
    assert cov["fields"]["fs_id"] is None and cov["complete_share_floor"] == 1.0
    empty = lineage.coverage_from_row("s1", {"objects": 0})
    assert empty["fields"]["fs_id"] == {"count": 0, "share": None} and empty["complete_share_floor"] is None


# ── the endpoints ─────────────────────────────────────────────────────────────────────────────────────────────────

def _app(fetchrow, fetch=None):
    from api.core.staff_authz import staff_policy
    from api.routers import admin_quality_lineage as r

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    pool = MagicMock()

    @asynccontextmanager
    async def acq():
        yield conn
    pool.acquire = MagicMock(side_effect=lambda: acq())
    app = FastAPI()
    app.include_router(r.router)
    app.state.pool = pool
    app.dependency_overrides[staff_policy] = lambda: None
    return app, conn


def test_coverage_endpoint_counts_every_workflow():
    from fastapi.testclient import TestClient

    async def fetchrow(sql, days):
        return {"objects": 2, "policy_sha": 1, "fs_id": 2, "prompt_version": 2, "route_config_version": 2}
    app, conn = _app(fetchrow)
    body = TestClient(app).get("/admin/quality/lineage/coverage?days=14").json()
    assert body["days"] == 14 and [w["workflow"] for w in body["workflows"]] == list(lineage.WORKFLOWS)
    assert body["workflows"][0]["fields"]["policy_sha"] == {"count": 1, "share": 0.5}
    assert [c.args[1] for c in conn.fetchrow.await_args_list] == [14] * 4
    assert TestClient(app).get("/admin/quality/lineage/coverage?days=0").status_code == 422
    assert TestClient(app).get("/admin/quality/lineage/coverage?days=91").status_code == 422


def test_object_endpoint_reads_the_row_then_its_ledger():
    from fastapi.testclient import TestClient

    async def fetchrow(sql, oid):
        assert sql is lineage.OBJECT_SQL["t9"] and oid == OBJ
        return _row()
    app, conn = _app(fetchrow, [_call()])
    body = TestClient(app).get(f"/admin/quality/lineage/t9/{OBJ}").json()
    assert body["complete"] is True and body["ledger_join"] == "piece"
    assert conn.fetch.await_args.args == (lineage.LEDGER_SQL["t9"], OBJ, JOB)


def test_object_endpoint_t2_without_a_job_skips_the_ledger():
    from fastapi.testclient import TestClient

    async def fetchrow(sql, oid):
        return _row(job_id=None)
    app, conn = _app(fetchrow)
    body = TestClient(app).get(f"/admin/quality/lineage/t2/{OBJ}").json()
    assert body["ledger_join"] is None and conn.fetch.await_count == 0


def test_object_endpoint_s1_joins_by_job_or_by_tour_window():
    from fastapi.testclient import TestClient

    async def by_job(sql, oid):
        return _row(tour_id="tour-1")
    app, _ = _app(by_job)
    assert TestClient(app).get(f"/admin/quality/lineage/s1/{OBJ}").json()["ledger_join"] == "job"

    async def by_tour(sql, oid):
        return _row(job_id=None, tour_id="tour-1")
    app, _ = _app(by_tour)
    assert TestClient(app).get(f"/admin/quality/lineage/s1/{OBJ}").json()["ledger_join"] == "tour_window"


def test_object_endpoint_refusals():
    from fastapi.testclient import TestClient

    async def none(sql, oid):
        return None
    app, _ = _app(none)
    c = TestClient(app)
    assert c.get(f"/admin/quality/lineage/t9/{OBJ}").status_code == 404
    assert c.get(f"/admin/quality/lineage/t7/{OBJ}").status_code == 422
    assert c.get("/admin/quality/lineage/t9/not-a-uuid").status_code == 422


def test_without_a_pool_the_endpoints_say_so():
    from fastapi.testclient import TestClient

    from api.core.staff_authz import staff_policy
    from api.routers import admin_quality_lineage as r
    app = FastAPI()
    app.include_router(r.router)
    app.dependency_overrides[staff_policy] = lambda: None
    assert TestClient(app).get("/admin/quality/lineage/coverage").status_code == 503
