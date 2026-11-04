"""AA-1039 R6 slice 3 — Writing Lab stage read (services/writing_memory/lab.py, GET /admin/writing-memory/lab/{writer}).
No DB: the pure helpers, the per-section isolation (a missing table empties only its section), the no-text rule of the
SQL, the route (404 for an unknown writer, admin only) and that the lab does not learn (no write, no LLM, no job).
"""
from __future__ import annotations

import ast
import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from services.writing_memory import lab

NOW = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_tally_sums_per_value_largest_first_and_names_null():
    rows = [{"k": "pass", "n": 2}, {"k": "fail", "n": 5}, {"k": None, "n": 1}, {"k": "pass", "n": 4}]
    assert lab.tally(rows, "k") == {"pass": 6, "fail": 5, "none": 1}
    assert list(lab.tally(rows, "k")) == ["pass", "fail", "none"]
    assert lab.tally([], "k") == {}


def test_row_out_and_prompt_row():
    assert lab.row_out({"a": NOW, "b": 1}) == {"a": NOW.isoformat(), "b": 1}
    r = lab.prompt_row({"stage": "t9_write", "version": "v1", "prompt_sha256": "ab" * 32, "created_at": NOW})
    assert r["sha_short"] == "ab" * 6 and r["prompt_sha256"] == "ab" * 32 and r["created_at"] == NOW.isoformat()


@pytest.mark.parametrize("switch,active,last,code,ok", [
    (None, None, None, "no_release", False),
    ({"memory_enabled": True}, None, None, "no_release", False),
    ({"memory_enabled": False}, {"reverify": False}, None, "memory_off", False),
    (None, {"reverify": False}, None, "memory_off", False),
    ({"memory_enabled": True}, {"reverify": True, "reverify_reasons": ["model_route"]}, None, "reverify", False),
    ({"memory_enabled": True}, {"reverify": False}, {"verdict": "fail"}, "regression", False),
    ({"memory_enabled": True}, {"reverify": False}, {"verdict": "pass", "regression_detected": True},
     "regression", False),
    ({"memory_enabled": True}, {"reverify": False}, {"verdict": "pass"}, "passing", True),
    ({"memory_enabled": True}, {"reverify": False}, None, "passing", True),
])
def test_gate_state(switch, active, last, code, ok):
    s = lab.gate_state(switch=switch, active=active, active_eval=last)
    assert s["code"] == code and s["ok"] is ok
    if code == "reverify":
        assert s["reasons"] == ["model_route"]


def test_evidence_run_ids_are_the_releases_own_runs_without_r0():
    assert lab.evidence_run_ids(None) == []
    rel = {"r0_run_id": "r0", "eval_run_id": "ev", "shadow_run_id": None, "reverify_run_id": "rv"}
    assert lab.evidence_run_ids(rel) == ["ev", "rv"]


def test_every_writer_has_prompt_stages_from_the_register():
    import json
    manifest = json.loads((ROOT / "shared" / "llm_client" / "prompt_versions.json").read_text())
    registered = {e["stage"] for e in manifest["prompts"]}
    assert set(lab.PROMPT_STAGES) == set(lab.WRITERS)
    for writer, stages in lab.PROMPT_STAGES.items():
        assert stages and set(stages) <= registered, writer


# ── the SQL never reads text ────────────────────────────────────────────────────────────────────────────────────────

# Columns that hold tenant or prompt text (db-schema-reference `pii:free_text`, prompt bodies). The Lab shows ids,
# labels, counts and hashes only; the logged ai_ops read of tenant text is R2's.
TEXT_COLUMNS = ("input", "control_output", "pipeline_verdict", "label", "output", "before_text", "after_text",
                "problem", "rule_text", "replacement", "match_phrases", "masked_text", "primary_output",
                "shadow_output", "notes", "retired_reason", "rejected_reason", "details")


def _sql_constants() -> dict[str, str]:
    return {k: v for k, v in vars(lab).items() if k.endswith("_SQL") and isinstance(v, str)}


@pytest.mark.parametrize("col", TEXT_COLUMNS)
def test_no_text_column_is_selected(col):
    for name, sql in _sql_constants().items():
        select = sql.split("FROM", 1)[0]
        cols = re.findall(r"[a-z_.]+", select.lower())
        if col == "notes":
            # the prompt register's notes are an engineer's note on a prompt version (no tenant text): allowed there
            if name == "PROMPTS_SQL":
                continue
        assert col not in [c.split(".")[-1] for c in cols if not c.startswith("count")], (name, col)


def test_the_lab_reads_only():
    for name, sql in _sql_constants().items():
        assert re.match(r"\s*SELECT\b", sql), name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP)\b", sql.upper()), name


def test_the_lab_imports_no_llm_and_queues_no_job():
    tree = ast.parse((ROOT / "services" / "writing_memory" / "lab.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and ("llm" in m or "jobs" in m or "boto" in m) for m in mods), mods


# ── stage_view: one section at a time ───────────────────────────────────────────────────────────────────────────────

class _Conn:
    """Answers fetch / fetchrow by the SQL constant; a constant mapped to an exception raises it."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.calls: list[tuple[str, tuple]] = []

    def _answer(self, sql, args):
        name = next(k for k, v in _sql_constants().items() if v == sql)
        self.calls.append((name, args))
        a = self.answers.get(name, [])
        if isinstance(a, Exception):
            raise a
        return a

    async def fetch(self, sql, *args):
        return self._answer(sql, args)

    async def fetchrow(self, sql, *args):
        a = self._answer(sql, args)
        return a[0] if isinstance(a, list) and a else (a or None)


def _pool(conn):
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool


GOLDEN_COUNTS = [
    {"split": "tune", "kind": "real", "label_verdict": "pass", "label_source": "agent_panel", "n": 6},
    {"split": "holdout", "kind": "real", "label_verdict": "fail", "label_source": "human_label", "n": 3},
    {"split": "trap", "kind": "synthetic", "label_verdict": "fail", "label_source": "synthetic", "n": 1},
]
SIGNALS = [
    {"fact_status": "pass", "reason_code": "ai_phrasing", "action": "sentence_cut", "actor_type": "admin",
     "scope": "platform", "n": 4, "n30": 2},
    {"fact_status": "grey", "reason_code": None, "action": "tenant_edit", "actor_type": "tenant", "scope": "tenant",
     "n": 2, "n30": 2},
]
ACTIVE = {"release_id": "r-1", "release_no": 3, "scope": "platform", "status": "active", "reverify": False,
          "reverify_reasons": [], "activated_at": NOW, "r0_run_id": "e-0", "eval_run_id": "e-1",
          "shadow_run_id": "sh-1", "reverify_run_id": None}


def _answers(**over):
    a = {
        "GOLDEN_SETS_SQL": [{"set_id": "s-1", "name": "zz_lab", "version": "1.0.0", "created_at": NOW, "items": 10}],
        "GOLDEN_COUNTS_SQL": GOLDEN_COUNTS,
        "GOLDEN_ITEMS_SQL": [{"item_id": "i-1", "sample_id": "zz-1", "split": "tune", "created_at": NOW}],
        "SIGNAL_COUNTS_SQL": SIGNALS,
        "EVAL_RUNS_SQL": [{"id": "e-1", "run_kind": "r2", "verdict": "pass", "created_at": NOW}],
        "SHADOW_RUNS_SQL": [{"run_id": "sh-1", "items": 5, "pass": 4, "fail": 1, "errors": 0, "probes": 1,
                             "cost_usd": 0.12, "last_at": NOW}],
        "MODEL_SHADOW_SQL": [{"stage": "t9_write", "calls": 7, "errors": 1, "last_at": NOW}],
        "PROMPTS_SQL": [{"stage": "t9_write", "version": "v1", "prompt_sha256": "c" * 64, "created_at": NOW}],
        "SWITCH_SQL": [{"memory_enabled": True, "train_closed_month": None, "updated_at": NOW}],
        "ACTIVE_RELEASE_SQL": [ACTIVE],
        "LATEST_RELEASE_SQL": [ACTIVE],
        "RELEASE_COUNTS_SQL": [{"scope": "platform", "status": "active", "n": 1},
                               {"scope": "platform", "status": "retired", "n": 2},
                               {"scope": "tenant", "status": "active", "n": 3}],
        "RULE_COUNTS_SQL": [{"status": "candidate", "n": 4}, {"status": "active", "n": 1}],
        "LAST_EVAL_SQL": [{"id": "e-1", "run_kind": "r2", "verdict": "pass", "regression_detected": False,
                           "release_id": "r-1", "finished_at": NOW}],
        "ACTIVE_EVAL_SQL": [{"id": "e-1", "run_kind": "r2", "verdict": "pass", "regression_detected": False,
                             "release_id": "r-1", "finished_at": NOW}],
    }
    a.update(over)
    return a


def test_stage_view_end_to_end_shape():
    conn = _Conn(_answers())
    out = asyncio.run(lab.stage_view(_pool(conn), "t9_social"))
    assert out["writer"] == "t9_social" and out["prompt_stages"] == list(lab.PROMPT_STAGES["t9_social"])
    assert all(out["sections"][s] == {"ready": True, "error": None} for s in lab.SECTIONS)
    s = out["samples"]
    assert s["total"] == 10 and s["by_split"] == {"tune": 6, "holdout": 3, "trap": 1}
    assert s["by_kind"] == {"real": 9, "synthetic": 1} and s["golden_sets"][0]["items"] == 10
    lb = out["labels"]
    assert lb["golden"]["by_verdict"] == {"pass": 6, "fail": 4}
    assert lb["signals"]["total"] == 6 and lb["signals"]["last_30d"] == 4
    assert lb["signals"]["by_reason_code"] == {"ai_phrasing": 4, "none": 2}
    ex = out["experiments"]
    assert ex["eval_runs"][0]["created_at"] == NOW.isoformat() and ex["shadow_runs"][0]["pass"] == 4
    assert ex["model_shadow"][0]["calls"] == 7
    assert out["prompts"]["versions"][0]["sha_short"] == "c" * 12
    g = out["gate"]
    assert g["state"] == {"code": "passing", "ok": True}
    assert g["releases"] == {"retired": 2, "active": 1} and g["tenant_releases_active"] == 3
    assert g["rules"] == {"candidate": 4, "active": 1}
    # every read is filtered by the asked writer; the model shadow by the writer's prompt stages
    for name, args in conn.calls:
        if name == "MODEL_SHADOW_SQL":
            assert args[0] == list(lab.PROMPT_STAGES["t9_social"]) and args[1] == lab.MODEL_SHADOW_DAYS
        elif name == "PROMPTS_SQL":
            assert args[0] == list(lab.PROMPT_STAGES["t9_social"])
        else:
            assert args[0] == "t9_social", name


def test_the_gate_reads_the_active_releases_evaluation_not_a_newer_candidates():
    # The live release r-1 passed its r2; a newer candidate release r-2 then failed its r1 (the gate rejecting it).
    cand = {"id": "e-9", "run_kind": "r1", "verdict": "fail", "regression_detected": True, "release_id": "r-2",
            "finished_at": NOW}
    conn = _Conn(_answers(LAST_EVAL_SQL=[cand]))
    g = asyncio.run(lab.stage_view(_pool(conn), "t2"))["gate"]
    assert g["state"] == {"code": "passing", "ok": True}
    assert g["active_eval"]["id"] == "e-1" and g["active_eval"]["release_id"] == "r-1"
    assert g["last_eval"]["id"] == "e-9" and g["last_eval"]["for_active"] is False
    # ACTIVE_EVAL_SQL is scoped to the active release and its stored evidence runs (not the r0 replay)
    args = next(a for n, a in conn.calls if n == "ACTIVE_EVAL_SQL")
    assert args == ("t2", "r-1", ["e-1", "sh-1"])


def test_a_failed_evaluation_of_the_active_release_is_a_regression():
    bad = {"id": "e-5", "run_kind": "reverify", "verdict": "fail", "regression_detected": False, "release_id": "r-1",
           "finished_at": NOW}
    g = asyncio.run(lab.stage_view(_pool(_Conn(_answers(ACTIVE_EVAL_SQL=[bad], LAST_EVAL_SQL=[bad]))), "a1"))["gate"]
    assert g["state"]["code"] == "regression" and g["last_eval"]["for_active"] is True


def test_no_active_release_skips_the_active_evaluation_read():
    conn = _Conn(_answers(ACTIVE_RELEASE_SQL=[]))
    g = asyncio.run(lab.stage_view(_pool(conn), "t9_blog"))["gate"]
    assert g["state"]["code"] == "no_release" and g["active_eval"] is None
    assert g["last_eval"]["for_active"] is False
    assert "ACTIVE_EVAL_SQL" not in [n for n, _ in conn.calls]


def test_a1_reads_the_legacy_s1_regression_runs_too():
    conn = _Conn(_answers())
    asyncio.run(lab.stage_view(_pool(conn), "a1"))
    args = next(a for n, a in conn.calls if n == "EVAL_RUNS_SQL")
    assert args[0] == "a1" and args[1] == ["s1_old", "s1_from_atom"]
    conn = _Conn(_answers())
    asyncio.run(lab.stage_view(_pool(conn), "t2"))
    assert next(a for n, a in conn.calls if n == "EVAL_RUNS_SQL")[1] == []


def test_a_missing_table_empties_only_its_section():
    conn = _Conn(_answers(SHADOW_RUNS_SQL=asyncpg.exceptions.UndefinedTableError("no table")))
    out = asyncio.run(lab.stage_view(_pool(conn), "t2"))
    assert out["sections"]["experiments"] == {"ready": False, "error": None} and out["experiments"] is None
    assert out["sections"]["gate"]["ready"] and out["gate"]["state"]["code"] == "passing"


def test_a_failing_read_says_so_without_detail():
    conn = _Conn(_answers(SWITCH_SQL=RuntimeError("password=secret in the message")))
    out = asyncio.run(lab.stage_view(_pool(conn), "t2"))
    assert out["sections"]["gate"] == {"ready": False, "error": "gate could not be read"}
    assert out["gate"] is None and out["sections"]["samples"]["ready"]


def test_empty_stage_has_no_release():
    a = {k: [] for k in _sql_constants()}
    out = asyncio.run(lab.stage_view(_pool(_Conn(a)), "a1"))
    assert out["samples"]["total"] == 0 and out["gate"]["state"]["code"] == "no_release"
    assert out["gate"]["switch"] is None and out["gate"]["active_release"] is None


def test_unknown_writer_is_refused():
    with pytest.raises(ValueError):
        asyncio.run(lab.stage_view(_pool(_Conn({})), "zz"))


# ── the route ───────────────────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "aa1039-lab-test-admin-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", _pool(_Conn(_answers())), raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _h(role="admin"):
    return {"X-Admin-Secret": _SECRET, "x-admin-role": role, "x-admin-user-id": "11111111-1111-1111-1111-111111111111"}


def test_route_answers_the_stage(client):
    r = client.get("/admin/writing-memory/lab/t9_blog", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["writer"] == "t9_blog" and body["gate"]["state"]["code"] == "passing"


def test_route_unknown_writer_404(client):
    assert client.get("/admin/writing-memory/lab/zz", headers=_h()).status_code == 404


@pytest.mark.parametrize("role", ["viewer", "content"])
def test_route_is_admin_only(client, role):
    assert client.get("/admin/writing-memory/lab/t2", headers=_h(role)).status_code == 403
