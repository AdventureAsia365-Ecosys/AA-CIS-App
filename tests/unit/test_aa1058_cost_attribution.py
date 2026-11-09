"""AA-1058 — cost attribution on every ledger row: tour_id (S1 / A1 / A3 atomize), content_piece_id (T9 / T10),
job_id (recompute and every job), a non-empty stage and app (DataForSEO rows too), and the S1 prompt version.

- ``call_log.bind_attribution`` / ``attributed``: the bound tour / piece fill a row whose caller passed none (all three
  writers: record_call, record_call_with_pool, record_calls), never override an explicit id, nest, survive the
  background write thread, and stay off eval rows.
- The job worker binds the payload's tour / piece; the shadow thread carries the binding.
- An empty stage is written as ``unattributed`` (logged), never lost; a ruled stage without its key is logged.
- ``billed_fields(resp, prompt_version=...)``: the caller's prompt hash only when the register gave none.
- S1: generated_content.prompt_version is written (the registered version, else the writer hash).
- DataForSEO: stage from the caller, else the bound job kind, else the endpoint's, else ``unattributed``; app ``cis``.
- The guard: an AST scan of every ledger write of a ruled stage (attribution.RULES) fails unless the call passes its
  key or its module is registered in attribution.BOUND_MODULES; each registered binder still binds its key.
No DB, no AWS, no provider.
"""
from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.dfs_client import call_log as dfs_log
from shared.llm_client import attribution, call_log, capacity

ROOT = Path(__file__).resolve().parents[2]
TOUR = "11111111-2222-3333-4444-555555555555"
TOUR2 = "66666666-7777-8888-9999-000000000000"
PIECE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
JOB = "12121212-3434-5656-7878-909090909090"


# ── the rules ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stage,key", [
    ("s1_generate", "tour_id"), ("s1_judge", "tour_id"), ("s1_grounding", "tour_id"), ("a1_seo", "tour_id"),
    ("t5_atomize", "tour_id"), ("a3_atomize", "tour_id"),
    ("t9_write", "content_piece_id"), ("t9_repair", "content_piece_id"), ("t10_judge", "content_piece_id"),
    ("a3_demand", "job_id"), ("a3_question_landing", "job_id"), ("a3_segment_type", "job_id"),
    ("a3_place_card", None), ("t2_generate", None), ("adhoc_aa1058", None), ("", None), (None, None),
    ("s1", None),
])
def test_required_key(stage, key):
    assert attribution.required_key(stage) == key


# ── the writers fill the bound ids ────────────────────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self):
        self.calls = []
        self.execute = AsyncMock(side_effect=lambda sql, *args: self.calls.append((sql, args)))
        self.executemany = AsyncMock(side_effect=lambda sql, rows: self.calls.extend((sql, r) for r in rows))
        self.close = AsyncMock()

    def is_in_transaction(self):
        return False

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Tx()


def _cols(args) -> dict:
    """The v2 INSERT arguments by column name."""
    from shared.aa_llm import ledger
    return dict(zip(ledger.COLUMNS, args))


@pytest.fixture
def conn(monkeypatch):
    c = _Conn()
    monkeypatch.setattr(call_log.asyncpg, "connect", AsyncMock(return_value=c))
    monkeypatch.setattr(call_log, "get_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(call_log, "_v2_missing_at", 0.0)
    return c


def _row(**kw):
    base = dict(stage="s1_generate", role="writer", model="haiku-4-5", tokens_in=10, tokens_out=5, cost_usd=0.01,
                quality_signal={"json_parsed": True})
    base.update(kw)
    return base


def test_record_call_takes_the_bound_tour_and_piece(conn):
    with call_log.bind_attribution(tour_id=TOUR), call_log.bind_attribution(content_piece_id=PIECE):
        asyncio.run(call_log.record_call(**_row()))
    sql, args = conn.calls[0]
    assert sql == call_log._INSERT_V2_SQL
    row = _cols(args)
    assert (row["tour_id"], row["content_piece_id"]) == (TOUR, PIECE)


def test_an_explicit_id_wins_over_the_bound_one(conn):
    with call_log.bind_attribution(tour_id=TOUR, content_piece_id=PIECE):
        asyncio.run(call_log.record_call(**_row(tour_id=TOUR2, content_piece_id=TOUR2)))
    row = _cols(conn.calls[0][1])
    assert (row["tour_id"], row["content_piece_id"]) == (TOUR2, TOUR2)


def test_nothing_bound_writes_null(conn):
    asyncio.run(call_log.record_call(**_row(stage="t2_generate")))
    row = _cols(conn.calls[0][1])
    assert row["tour_id"] is None and row["content_piece_id"] is None


def test_record_call_with_pool_takes_the_bound_ids(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with call_log.bind_attribution(tour_id=TOUR):
        asyncio.run(call_log.record_call_with_pool(pool, **_row(stage="a3_atomize")))
    assert _cols(conn.calls[0][1])["tour_id"] == TOUR


def test_record_calls_rows_take_the_bound_ids(conn):
    with call_log.bind_attribution(content_piece_id=PIECE):
        asyncio.run(call_log.record_calls([_row(stage="t10_judge"), _row(stage="t10_judge", content_piece_id=TOUR2)]))
    pieces = [_cols(a)["content_piece_id"] for _sql, a in conn.calls]
    assert pieces == [PIECE, TOUR2]


def test_the_background_write_keeps_the_binding(conn):
    """record_call_sync under a running loop writes on its own thread (AA-927): the copied context carries the ids."""
    async def caller():
        with call_log.bind_attribution(tour_id=TOUR):
            call_log.record_call_sync(**_row())
    asyncio.run(caller())
    call_log.wait_for_pending_writes(5)
    assert _cols(conn.calls[0][1])["tour_id"] == TOUR


def test_an_eval_run_gets_no_production_attribution(conn):
    with call_log.bind_attribution(tour_id=TOUR, content_piece_id=PIECE), \
            call_log.bind_eval_stage("writing_memory_eval", "run-1"):
        asyncio.run(call_log.record_call(**_row()))
    row = _cols(conn.calls[0][1])
    assert row["tour_id"] is None and row["content_piece_id"] is None and row["stage"] == "writing_memory_eval"


def test_bind_attribution_nests_and_drops_a_non_uuid():
    with call_log.bind_attribution(tour_id=TOUR):
        with call_log.bind_attribution(content_piece_id="not-a-uuid", tour_id=None):
            assert call_log.current_attribution() == {"tour_id": TOUR}
        with call_log.bind_attribution(tour_id=TOUR2, content_piece_id=PIECE):
            assert call_log.current_attribution() == {"tour_id": TOUR2, "content_piece_id": PIECE}
        assert call_log.current_tour_id() == TOUR and call_log.current_content_piece_id() is None
    assert call_log.current_attribution() == {}


def test_attributed_decorator_binds_from_the_arguments():
    seen = []

    @call_log.attributed(tour_id=lambda *a, **k: k["tour_id"] if "tour_id" in k else a[0])
    async def run(tour_id, other=None):
        seen.append(call_log.current_tour_id())

    asyncio.run(run(TOUR))
    asyncio.run(run(tour_id=TOUR2))
    assert seen == [TOUR, TOUR2] and call_log.current_tour_id() is None
    with pytest.raises(TypeError):
        call_log.attributed(job_id=lambda *a, **k: None)


def test_a_failing_getter_binds_nothing_and_never_breaks_the_call():
    @call_log.attributed(content_piece_id=lambda *a, **k: a[5])
    async def run(x):
        return call_log.current_content_piece_id(), x

    assert asyncio.run(run(1)) == (None, 1)


def test_an_empty_stage_is_written_unattributed(conn):
    with patch.object(call_log.logger, "warning") as warn:
        asyncio.run(call_log.record_call(**_row(stage="")))
    assert _cols(conn.calls[0][1])["stage"] == call_log.UNATTRIBUTED_STAGE
    assert any(c.args[0] == "llm_call_log_stage_missing" for c in warn.call_args_list)


def test_every_cis_row_names_its_app(conn):
    asyncio.run(call_log.record_call(**_row(app="")))
    assert _cols(conn.calls[0][1])["app"] == "cis"


@pytest.mark.parametrize("stage,bind,warned", [
    ("s1_generate", {}, True),
    ("s1_generate", {"tour_id": TOUR}, False),
    ("t9_write", {"tour_id": TOUR}, True),
    ("t9_write", {"content_piece_id": PIECE}, False),
    ("a3_demand", {}, False),          # job_id: checked by the guard only (sync endpoints run without a job)
    ("t2_generate", {}, False),
])
def test_a_ruled_row_without_its_key_is_logged(conn, stage, bind, warned):
    with patch.object(call_log.logger, "warning") as warn, call_log.bind_attribution(**bind):
        asyncio.run(call_log.record_call(**_row(stage=stage)))
    assert any(c.args[0] == "llm_call_log_attribution_missing" for c in warn.call_args_list) is warned
    assert len(conn.calls) == 1          # the billed row is always written


# ── billed_fields: the caller's prompt id as a fallback ───────────────────────────────────────────────────────────

def _resp(**kw):
    from shared.llm_client.models import LLMResponse
    base = dict(content="x", model_used="haiku-4-5", provider="bedrock-satellite", input_tokens=1, output_tokens=1,
                cost_usd=0.0)
    base.update(kw)
    return LLMResponse(**base)


def test_billed_fields_prompt_version_fallback():
    assert call_log.billed_fields(_resp())["prompt_version"] is None
    assert call_log.billed_fields(_resp(), prompt_version="ab12cd34")["prompt_version"] == "ab12cd34"
    assert call_log.billed_fields(_resp(prompt_version="v2"), prompt_version="ab12cd34")["prompt_version"] == "v2"
    assert call_log.billed_fields(_resp(), prompt_version="  ")["prompt_version"] is None


def test_the_s1_writers_pass_their_prompt_hash():
    for path, n in (("services/content_generation/graph.py", 2),
                    ("services/content_generation/s1_writer_v2_live.py", 1)):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert src.count("billed_fields(resp, prompt_version=") == n, path


# ── the worker and the shadow thread ──────────────────────────────────────────────────────────────────────────────

def test_job_attribution_from_the_payload():
    from shared.jobs.worker import job_attribution
    assert job_attribution({"tour_id": TOUR, "version_id": "x"}) == {"tour_id": TOUR}
    assert job_attribution({"piece_id": PIECE, "request_id": "r"}) == {"content_piece_id": PIECE}
    assert job_attribution({"content_piece_id": PIECE}) == {"content_piece_id": PIECE}
    assert job_attribution({"scope": "platform"}) == {} and job_attribution(None) == {}


def test_the_worker_binds_the_payload_ids_around_the_handler():
    src = (ROOT / "shared/jobs/worker.py").read_text(encoding="utf-8")
    assert "call_log.bind_attribution(**job_attribution(job.payload))" in src


def test_the_shadow_thread_carries_the_binding():
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    seen, done = {}, threading.Event()

    def _bound(*_a, **_k):
        seen.update(call_log.current_attribution())
        done.set()

    with patch.object(c, "_run_shadow_bound", side_effect=_bound), \
            call_log.bind_attribution(tour_id=TOUR, content_piece_id=PIECE):
        c._start_shadow(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_judge"), MagicMock(),
                        _resp(), "acc3")
    assert done.wait(5)
    assert seen == {"tour_id": TOUR, "content_piece_id": PIECE}


def test_the_cassette_row_falls_back_to_the_bound_ids():
    src = (ROOT / "shared/llm_client/cassette.py").read_text(encoding="utf-8")
    assert "call_log.current_tour_id()" in src and "call_log.current_content_piece_id()" in src


# ── S1: generated_content.prompt_version ──────────────────────────────────────────────────────────────────────────

def test_s1_prompt_version_prefers_the_registered_version(monkeypatch):
    from api.routers import admin_pipeline
    from shared.llm_client import prompt_registry
    monkeypatch.setattr(prompt_registry, "version_for", lambda stage: "v2" if stage == "s1_generate" else None)
    assert admin_pipeline._s1_prompt_version({"prompt_version": "ab12cd34"}) == "v2"
    assert admin_pipeline._s1_prompt_version({"prompt_version": "ab12cd34", "generate_stage": "t2_generate"}) \
        == "ab12cd34"
    monkeypatch.setattr(prompt_registry, "version_for", lambda stage: None)
    assert admin_pipeline._s1_prompt_version({"prompt_version": "ab12cd34"}) == "ab12cd34"
    assert admin_pipeline._s1_prompt_version({}) is None


def test_the_s1_insert_writes_the_prompt_version_column():
    src = (ROOT / "api/routers/admin_pipeline.py").read_text(encoding="utf-8")
    assert "satellite_account, revalidate_passed, prompt_version" in src
    assert "_s1_prompt_version(result)," in src


# ── DataForSEO rows ───────────────────────────────────────────────────────────────────────────────────────────────

def test_dfs_stage_resolution():
    assert dfs_log.row_stage("s1_seo", "fetch_all") == "s1_seo"
    with capacity.bind_kind("segment_research"):
        assert dfs_log.row_stage(None, "serp_advanced") == "segment_research"
        assert dfs_log.row_stage("a3_search_demand", "serp_advanced") == "a3_search_demand"
    assert dfs_log.row_stage(None, "appendix_user_data") == "dfs_balance"
    with patch.object(dfs_log.logger, "warning") as warn:
        assert dfs_log.row_stage("", "serp_advanced") == dfs_log.UNATTRIBUTED_STAGE
    assert warn.call_args.args[0] == "dfs_call_log_stage_missing"


def test_dfs_rows_always_name_stage_and_app(monkeypatch):
    c = _Conn()
    monkeypatch.setattr(dfs_log.asyncpg, "connect", AsyncMock(return_value=c))
    monkeypatch.setattr(dfs_log, "get_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(dfs_log, "_v2_missing_at", 0.0)
    with capacity.bind_kind("s1_seo_prefetch"):
        dfs_log.record_dfs_call_sync(endpoint="serp_advanced", fetched_live=True, cost_usd=0.002, app="")
    asyncio.run(dfs_log.record_dfs_call(endpoint="appendix_user_data", fetched_live=True, cost_usd=0.0))
    stages_apps = [(args[-2], args[-1]) for _sql, args in c.calls]
    assert stages_apps == [("s1_seo_prefetch", "cis"), ("dfs_balance", "cis")]


def test_the_dfs_sites_this_issue_touched_name_their_stage():
    for path, needle in (("services/seo_intelligence/handler.py", "stage=DFS_STAGE"),
                         ("services/jobs/s1_seo_prefetch_job.py", 'stage="s1_seo_prefetch"'),
                         ("services/acp_shared/question_topup.py", "stage=DFS_STAGE")):
        assert needle in (ROOT / path).read_text(encoding="utf-8"), path


# ── the guard ─────────────────────────────────────────────────────────────────────────────────────────────────────

# The ledger writers of shared/llm_client/call_log.py and decide.py (decide's stage is its first argument).
LEDGER_WRITERS = {"record_call", "record_call_sync", "record_call_with_pool", "record_unusable",
                  "record_unusable_sync", "record_billed_failure", "record_billed_failure_sync"}
DECIDE_WRITERS = {"decide", "decide_sync"}
SCANNED = ("api", "services", "shared")


def _module_constants(tree: ast.Module) -> dict[str, str]:
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _strings(expr: ast.AST, consts: dict, local: dict, depth: int = 0) -> set[str]:
    """Every stage name ``expr`` can evaluate to that is spelled in the module: literals, module constants, and (one
    level) the expressions a local name or a parameter default was set from."""
    out: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
        elif isinstance(node, ast.Name):
            if node.id in consts:
                out.add(consts[node.id])
            elif depth == 0:
                for src in local.get(node.id, ()):
                    out |= _strings(src, consts, {}, depth + 1)
    return out


def _local_sources(func: ast.AST) -> dict[str, list[ast.AST]]:
    """name -> the expressions it is assigned from inside ``func`` (assignments and parameter defaults)."""
    out: dict[str, list[ast.AST]] = {}
    args = func.args
    pos = args.posonlyargs + args.args
    for a, d in zip(pos[len(pos) - len(args.defaults):], args.defaults):
        out.setdefault(a.arg, []).append(d)
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if d is not None:
            out.setdefault(a.arg, []).append(d)
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, []).append(node.value)
    return out


def _ledger_sites():
    """(path, line, stage, key, passes_key) for every ledger write of a ruled stage under SCANNED."""
    sites = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ("shared/llm_client/call_log.py", "shared/llm_client/decide.py"):
                continue                     # the writers themselves (their stage is a parameter)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            consts = _module_constants(tree)
            funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for func in funcs:
                local = _local_sources(func)
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    f = node.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if name in LEDGER_WRITERS:
                        expr = next((k.value for k in node.keywords if k.arg == "stage"), None)
                    elif name in DECIDE_WRITERS and node.args:
                        expr = node.args[0]
                    else:
                        continue
                    if expr is None:
                        continue
                    passed = {k.arg for k in node.keywords if k.arg}
                    for stage in sorted(_strings(expr, consts, local)):
                        key = attribution.required_key(stage)
                        if key is not None:
                            sites.append((rel, node.lineno, stage, key, key in passed))
    return sorted(set(sites))


def test_every_ruled_ledger_write_carries_its_key():
    """A ledger row of a ruled stage must name its object: the call passes the key, or its module is registered in
    attribution.BOUND_MODULES (it only runs below a binder of that key)."""
    missing = [(rel, line, stage, key) for rel, line, stage, key, passes in _ledger_sites()
               if not passes and rel not in attribution.BOUND_MODULES.get(key, ())]
    assert missing == [], (
        "ledger writes of a ruled stage without their key: pass tour_id= / content_piece_id= at the call, or bind "
        "it at the stage's entry point and list the module in shared/llm_client/attribution.py BOUND_MODULES")


def test_the_guard_sees_the_call_sites():
    sites = _ledger_sites()
    stages = {s for _r, _l, s, _k, _p in sites}
    assert {"s1_generate", "s1_grounding", "a3_atomize", "t9_write", "t10_judge", "a3_demand",
            "a3_question_landing"} <= stages, stages
    assert len(sites) >= 25


def test_every_registered_module_still_writes_a_ruled_stage():
    """A stale BOUND_MODULES entry would hide a future site in that module: every entry must have a ruled write."""
    seen = {(rel, key) for rel, _l, _s, key, _p in _ledger_sites()}
    stale = [(rel, key) for key, mods in attribution.BOUND_MODULES.items() for rel in mods if (rel, key) not in seen]
    assert stale == []


def _function(tree: ast.Module, dotted: str):
    parts = dotted.split(".")
    scope = tree.body
    found = None
    for part in parts:
        found = next((n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and n.name == part), None)
        assert found is not None, dotted
        scope = found.body
    return found


@pytest.mark.parametrize("key,binder", [(k, b) for k, binders in attribution.BINDERS.items() for b in binders])
def test_every_binder_still_binds_its_key(key, binder):
    rel, name = binder.split("::")
    func = _function(ast.parse((ROOT / rel).read_text(encoding="utf-8")), name)
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)]

    def called(fn_name: str) -> list[ast.Call]:
        return [c for c in calls if (c.func.attr if isinstance(c.func, ast.Attribute)
                                     else getattr(c.func, "id", None)) == fn_name]
    if key == attribution.JOB:
        assert called("bind_job"), binder
        return
    binds = called("bind_attribution") + called("attributed")
    ok = any(any(k.arg == key for k in c.keywords) for c in binds) or \
        any(any(k.arg is None and "job_attribution" in ast.unparse(k.value) for k in c.keywords) for c in binds)
    assert ok, binder


# ── the read-only report (scripts/aa1058/ledger_attribution.py) ──────────────────────────────────────────────────

def test_the_report_rules_match_the_registry():
    from scripts.aa1058 import ledger_attribution as rep
    assert rep.FALLBACK_RULES == attribution.RULES
    assert rep.rules() == (attribution.RULES, "image")


def test_the_report_stage_lines():
    from scripts.aa1058 import ledger_attribution as rep
    rows = [dict(stage="s1_generate", n=4, cost=0.2, with_tour=3, with_piece=0, with_job=4, with_prompt=4, no_app=0),
            dict(stage="t2_generate", n=2, cost=0.1, with_tour=0, with_piece=0, with_job=2, with_prompt=0, no_app=1)]
    lines, ruled, carried = rep.stage_lines(rows, attribution.RULES)
    assert (ruled, carried) == (4, 3)
    assert lines[0] == ("stage.s1_generate rows=4 cost=0.2000 tour=0.750 piece=0.000 job=1.000 prompt_version=1.000 "
                        "no_app=0 rule=tour_id rule_ok=0.750")
    assert lines[1].endswith("rule=- rule_ok=-")


def test_the_report_hours_are_bounded(monkeypatch):
    from scripts.aa1058 import ledger_attribution as rep
    for raw, want in (("48", 48), ("0", 1), ("99999", rep.MAX_HOURS), ("x", 24)):
        monkeypatch.setenv("HOURS", raw)
        assert rep.hours_from_env() == want


def test_the_report_is_read_only():
    src = (ROOT / "scripts/aa1058/ledger_attribution.py").read_text(encoding="utf-8").upper()
    for word in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE", "ALTER ", "DROP "):
        assert word not in src, word
