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
import functools
import itertools
import threading
from pathlib import Path
from typing import Optional
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
    src = (ROOT / "api/routers/admin_pipeline/_engine.py").read_text(encoding="utf-8")
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

# The ledger writers of shared/llm_client/call_log.py and decide.py (decide's stage is its first argument), and the
# call_log row builders whose rows reach record_calls (their stage is a keyword too).
LEDGER_WRITERS = {"record_call", "record_call_sync", "record_call_with_pool", "record_unusable",
                  "record_unusable_sync", "record_billed_failure", "record_billed_failure_sync",
                  "billed_unusable_rows", "gateway_failure_row", "cut_reply_row"}
DECIDE_WRITERS = {"decide", "decide_sync"}
# record_calls takes prebuilt rows: in a module that calls it, every dict(stage=...) / {"stage": ...} is a row.
ROW_SINKS = {"record_calls", "record_calls_sync"}
SCANNED = ("api", "services", "shared")
WRITER_MODULES = ("shared/llm_client/call_log.py", "shared/llm_client/decide.py")
MAX_DEPTH = 6


def _module_constants(tree: ast.Module) -> dict[str, str]:
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _callee(call: ast.Call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


def _params(func) -> list[str]:
    a = func.args
    return [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]


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


@functools.lru_cache(maxsize=None)
def _constants_of(dotted: str) -> dict[str, str]:
    """The string constants of repo module ``dotted`` ({} when it is not a repo file)."""
    base = ROOT / dotted.replace(".", "/")
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return _module_constants(ast.parse(path.read_text(encoding="utf-8")))
    return {}


def _imports(tree: ast.Module, rel: str) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """(name -> (module, attribute) of every ``from m import a as name``, alias -> module of every module bound)."""
    names: dict[str, tuple[str, str]] = {}
    mods: dict[str, str] = {}
    package = rel[:-3].split("/")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            for a in node.names:
                bound = a.asname or a.name
                names[bound] = (mod, a.name)
                mods[bound] = f"{mod}.{a.name}"                # `from pkg import module` binds a module
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    mods[a.asname] = a.name
    return names, mods


class _Module:
    """One module's resolver: the stage names an expression can evaluate to (literals, module constants, constants
    imported from a repo module, local assignments, parameter defaults, and a parameter forwarded by its callers in
    the same module, through ``**dict`` keywords too), and whether a call passes a key (an explicit keyword, or a
    ``**name`` whose dict names the key)."""

    def __init__(self, src: str, rel: str = "m.py"):
        tree = ast.parse(src)
        self.consts = _module_constants(tree)
        self.imported, self.mod_alias = _imports(tree, rel)
        self.decide_names = {b for b, (m, a) in self.imported.items()
                             if m.endswith("llm_client.decide") and a in DECIDE_WRITERS}
        self.decide_mods = {b for b, m in self.mod_alias.items() if m.endswith("llm_client.decide")}
        self.funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.owner: dict[ast.AST, ast.AST] = {}      # node -> innermost enclosing function (BFS: inner overwrites)
        for f in self.funcs:
            for n in ast.walk(f):
                if n is not f:
                    self.owner[n] = f
        self.local = {f: _local_sources(f) for f in self.funcs}
        self.calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    # -- names and arguments
    def _imported_const(self, node: ast.AST, func) -> Optional[str]:
        """The value of an imported repo constant (``NAME`` from ``from m import NAME``, or ``mod.NAME``)."""
        if isinstance(node, ast.Name) and node.id in self.imported and self._scope_of(node.id, func) is None:
            mod, attr = self.imported[node.id]
            return _constants_of(mod).get(attr)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and \
                node.value.id in self.mod_alias and self._scope_of(node.value.id, func) is None:
            return _constants_of(self.mod_alias[node.value.id]).get(node.attr)
        return None

    def _scope_of(self, name: str, func):
        """The function (``func`` or an enclosing one) that defines ``name``, else None."""
        while func is not None:
            if name in self.local[func] or name in _params(func):
                return func
            func = self.owner.get(func)
        return None

    def _arg_sources(self, func, param: str) -> list[tuple[ast.AST, object]]:
        """(expression, its function) for every same-module call of ``func`` that passes ``param``."""
        out = []
        pos = [p.arg for p in func.args.posonlyargs + func.args.args]
        for call in self.calls:
            if _callee(call) != func.name:
                continue
            caller = self.owner.get(call)
            kw = next((k.value for k in call.keywords if k.arg == param), None)
            if kw is not None:
                out.append((kw, caller))
                continue
            if param in pos:
                i = pos.index(param) - (1 if pos and pos[0] in ("self", "cls")
                                        and isinstance(call.func, ast.Attribute) else 0)
                head = list(itertools.takewhile(lambda a: not isinstance(a, ast.Starred), call.args))
                if 0 <= i < len(head):
                    out.append((head[i], caller))
                    continue
            for k in call.keywords:
                if k.arg is None:
                    out += self._dict_entry(k.value, param, caller, set(), 0)
        return out

    def _sources(self, name: str, func, seen: set, depth: int) -> Optional[list[tuple[ast.AST, object]]]:
        """(expression, its function) ``name`` can take in ``func``; None when already followed or too deep."""
        scope = self._scope_of(name, func)
        if scope is None:
            return []
        if (id(scope), name) in seen or depth > MAX_DEPTH:
            return None
        seen.add((id(scope), name))
        out = [(e, scope) for e in self.local[scope].get(name, ())]
        if name in _params(scope):
            out += self._arg_sources(scope, name)
        return out

    def _dict_entry(self, expr: ast.AST, key: str, func, seen: set, depth: int) -> list[tuple[ast.AST, object]]:
        """(value expression, its function) for every value ``expr`` (a dict) can hold under ``key``."""
        if depth > MAX_DEPTH:
            return []
        if isinstance(expr, ast.Dict):
            out = [(v, func) for k, v in zip(expr.keys, expr.values) if isinstance(k, ast.Constant) and k.value == key]
            for k, v in zip(expr.keys, expr.values):
                if k is None:                                    # {**other}
                    out += self._dict_entry(v, key, func, seen, depth + 1)
            return out
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "dict":
            out = [(k.value, func) for k in expr.keywords if k.arg == key]
            for k in expr.keywords:
                if k.arg is None:
                    out += self._dict_entry(k.value, key, func, seen, depth + 1)
            return out
        if isinstance(expr, ast.Name):
            return [e for src, f in self._sources(expr.id, func, seen, depth + 1) or ()
                    for e in self._dict_entry(src, key, f, seen, depth + 1)]
        return []

    # -- the questions the guard asks
    def resolve(self, expr: ast.AST, func, seen: Optional[set] = None, depth: int = 0) -> tuple[set[str], bool]:
        """(every stage name ``expr`` can evaluate to that the module spells, open): open when a part of it comes
        from outside the module's sight (an attribute, a call's result, an import, a parameter no same-module caller
        sets and no default names)."""
        seen = set() if seen is None else seen
        out: set[str] = set()
        open_ = False
        stack = [expr]
        while stack:
            node = stack.pop()
            if isinstance(node, ast.Constant):
                if isinstance(node.value, str):
                    out.add(node.value)
            elif isinstance(node, (ast.Name, ast.Attribute)) and self._imported_const(node, func) is not None:
                out.add(self._imported_const(node, func))
            elif isinstance(node, ast.Name):
                if node.id in self.consts and self._scope_of(node.id, func) is None:
                    out.add(self.consts[node.id])
                    continue
                srcs = self._sources(node.id, func, seen, depth + 1)
                if srcs is None:
                    continue                                # already followed on this path
                open_ |= not srcs
                for src, f in srcs:
                    names, o = self.resolve(src, f, seen, depth + 1)
                    out |= names
                    open_ |= o
            elif isinstance(node, ast.IfExp):
                stack += [node.body, node.orelse]
            elif isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
                open_ = True                                # its literals still count: state.get("k", "s1_judge")
                out |= {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            else:
                stack.extend(ast.iter_child_nodes(node))
        return out, open_

    def passes(self, call: ast.Call, key: str) -> bool:
        if any(k.arg == key for k in call.keywords):
            return True
        func = self.owner.get(call)
        return any(self._dict_entry(k.value, key, func, set(), 0) for k in call.keywords if k.arg is None)

    def sites(self):
        """(line, stage expression or None, row node, passes_key callable) for every ledger write / row."""
        sink = any(_callee(c) in ROW_SINKS for c in self.calls)
        for node in self.calls:
            name = _callee(node)
            if name in LEDGER_WRITERS:
                expr = next((k.value for k in node.keywords if k.arg == "stage"), None)
                if expr is None and not any(k.arg is None for k in node.keywords):
                    continue                                # stage passed positionally to a writer: none today
                yield node.lineno, expr, node, (lambda key, n=node: self.passes(n, key))
            elif node.args and self._is_decide(node):
                yield node.lineno, node.args[0], node, (lambda key, n=node: self.passes(n, key))
            elif sink and name == "dict" and isinstance(node.func, ast.Name) and \
                    any(k.arg == "stage" for k in node.keywords):
                expr = next(k.value for k in node.keywords if k.arg == "stage")
                yield node.lineno, expr, node, (lambda key, n=node: self.passes(n, key))
        if sink:
            for node in self.funcs_dicts():
                expr = next(v for k, v in zip(node.keys, node.values)
                            if isinstance(k, ast.Constant) and k.value == "stage")
                yield node.lineno, expr, node, (lambda key, n=node: any(
                    isinstance(k, ast.Constant) and k.value == key for k in n.keys))

    def _is_decide(self, call: ast.Call) -> bool:
        """A call of the ledger's decide / decide_sync (other modules have their own ``decide``)."""
        f = call.func
        if isinstance(f, ast.Name):
            return f.id in self.decide_names
        return isinstance(f, ast.Attribute) and f.attr in DECIDE_WRITERS and isinstance(f.value, ast.Name) and \
            f.value.id in self.decide_mods

    def funcs_dicts(self):
        seen = set()
        for f in self.funcs:
            for n in ast.walk(f):
                if isinstance(n, ast.Dict) and id(n) not in seen and \
                        any(isinstance(k, ast.Constant) and k.value == "stage" for k in n.keys):
                    seen.add(id(n))
                    yield n


def _forwards_a_request_stage(expr: ast.AST) -> bool:
    """``request.stage`` / ``self.stage``: the stage of an object built elsewhere (the gateway's own rows)."""
    return any(isinstance(n, ast.Attribute) and n.attr == "stage" for n in ast.walk(expr))


def _scan(rel: str, src: str):
    """(sites, unresolved) of one module: sites = (rel, line, stage, key, passes_key) for every ruled stage a ledger
    write can carry; unresolved = (rel, line) of a write whose stage the guard cannot name: it resolves to no string
    spelled in the module or a repo constant it imports (a helper's ``stage`` parameter that no same-module caller
    sets), or it forwards an object's ``.stage``."""
    mod = _Module(src, rel)
    sites, unresolved = [], []
    for line, expr, node, passes in mod.sites():
        func = mod.owner.get(node)
        if expr is None:                                    # stage only inside a **row: resolve it like a key
            entries = [e for k in node.keywords if k.arg is None
                       for e in mod._dict_entry(k.value, "stage", func, set(), 0)]
            if not entries:
                continue                                    # a **kwargs pass-through with no stage in sight
            stages = set().union(*(mod.resolve(e, f)[0] for e, f in entries))
            forwarded = any(_forwards_a_request_stage(e) for e, _f in entries)
        else:
            stages, _open = mod.resolve(expr, func)
            forwarded = _forwards_a_request_stage(expr)
        if forwarded or not any(stages):
            unresolved.append((rel, line))
        for stage in sorted(s for s in stages if s):
            key = attribution.required_key(stage)
            if key is not None:
                sites.append((rel, line, stage, key, passes(key)))
    return sites, unresolved


@functools.lru_cache(maxsize=1)
def _scan_tree():
    sites, unresolved = [], []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in WRITER_MODULES:
                continue                     # the writers themselves (their stage is a parameter)
            s, u = _scan(rel, path.read_text(encoding="utf-8"))
            sites += s
            unresolved += u
    return sorted(set(sites)), sorted(set(unresolved))


def _ledger_sites():
    """(path, line, stage, key, passes_key) for every ledger write of a ruled stage under SCANNED, the stages of
    attribution.STAGE_FORWARDED counted at each write whose stage the module does not spell."""
    sites, unresolved = _scan_tree()
    for rel, line in unresolved:
        for stage in attribution.STAGE_FORWARDED.get(rel, ()):
            key = attribution.required_key(stage)
            if key is not None:
                sites.append((rel, line, stage, key, False))
    return sorted(set(sites))


def test_every_ruled_ledger_write_carries_its_key():
    """A ledger row of a ruled stage must name its object: the call passes the key, or its module is registered in
    attribution.BOUND_MODULES (it only runs below a binder of that key)."""
    missing = [(rel, line, stage, key) for rel, line, stage, key, passes in _ledger_sites()
               if not passes and rel not in attribution.BOUND_MODULES.get(key, ())]
    assert missing == [], (
        "ledger writes of a ruled stage without their key: pass tour_id= / content_piece_id= at the call, or bind "
        "it at the stage's entry point and list the module in shared/llm_client/attribution.py BOUND_MODULES")


def test_every_ledger_write_has_a_known_stage():
    """A write whose stage the guard cannot name (a helper's ``stage`` parameter no same-module caller sets) is
    invisible to the rules: its module must name the stages it carries in attribution.STAGE_FORWARDED, or say in
    attribution.STAGE_PASSTHROUGH why any stage is right (the gateway's own rows)."""
    _sites, unresolved = _scan_tree()
    unknown = [(rel, line) for rel, line in unresolved
               if rel not in attribution.STAGE_FORWARDED and rel not in attribution.STAGE_PASSTHROUGH]
    assert unknown == [], (
        "ledger writes whose stage the guard cannot resolve: pass the stage from a same-module caller or constant, "
        "or list the module and the stages it carries in shared/llm_client/attribution.py STAGE_FORWARDED")


def test_the_forwarded_and_passthrough_lists_are_not_stale():
    _sites, unresolved = _scan_tree()
    mods = {rel for rel, _l in unresolved}
    stale = sorted((set(attribution.STAGE_FORWARDED) | set(attribution.STAGE_PASSTHROUGH)) - mods)
    assert stale == []
    assert not set(attribution.STAGE_FORWARDED) & set(attribution.STAGE_PASSTHROUGH)


def test_a_forwarded_ruled_stage_is_checked_like_any_other(monkeypatch):
    """A STAGE_FORWARDED module that carries a ruled stage gets sites for it (and fails without its key)."""
    monkeypatch.setitem(attribution.STAGE_FORWARDED, "services/places/llm.py", ("t9_invented_stage",))
    sites = [(p, k) for r, _l, s, k, p in _ledger_sites() if (r, s) == ("services/places/llm.py", "t9_invented_stage")]
    assert sites and all(k == "content_piece_id" and not p for p, k in sites)


def test_the_guard_sees_the_call_sites():
    sites = _ledger_sites()
    stages = {s for _r, _l, s, _k, _p in sites}
    assert {"s1_generate", "s1_grounding", "a3_atomize", "t9_write", "t10_judge", "a3_demand",
            "a3_question_landing", "t9_faq_answer_fit", "t10_adjudicate", "s1_judge"} <= stages, stages
    by_mod = {(r, s) for r, _l, s, _k, _p in sites}
    # forwarded through a same-module caller's parameter default (adjudicate) and a **dict (faq_answer_fit)
    assert ("services/acp_content_writing/seed_align/adjudicate.py", "t10_adjudicate") in by_mod
    assert ("services/acp_content_writing/faq_answer_fit.py", "t9_faq_answer_fit") in by_mod
    fit = [p for r, _l, s, _k, p in sites if r == "services/acp_content_writing/faq_answer_fit.py"]
    assert fit and all(fit)                                  # content_piece_id reaches its rows through **ids
    assert len(sites) >= 25


# Invented module sources: what the resolver must see.
_FORWARDED_SRC = '''
STAGE = "t9_invented_check"

def _call(items, *, stage, ids):
    record_call_sync(stage=stage, model="m", **ids)
    record_unusable_sync(None, stage=stage, role="judge", error="x")

def run(items, piece_id=None, *, stage=STAGE):
    ids = {"content_piece_id": piece_id}
    call = dict(stage=stage, ids=ids)
    return _call(items, **call)
'''

_HIDDEN_SRC = '''
def _call(stage):
    record_call_sync(stage=stage, model="m")
'''

_ROWS_SRC = '''
def rows_of(stage="s1_invented"):
    rows = [dict(stage=stage, role="judge"), {"stage": "t10_invented", "content_piece_id": None}]
    rows += billed_unusable_rows(stage=stage, role="judge", billed=[], error="x")
    record_calls_sync(rows)
'''


def test_the_resolver_follows_a_forwarded_stage_and_a_dict_of_ids():
    sites, unresolved = _scan("m.py", _FORWARDED_SRC)
    assert unresolved == []
    assert {(s, k, p) for _r, _l, s, k, p in sites} == {
        ("t9_invented_check", "content_piece_id", True),     # record_call_sync: **ids names the key
        ("t9_invented_check", "content_piece_id", False),    # record_unusable_sync: no key
    }


def test_the_resolver_reports_a_stage_it_cannot_see():
    assert _scan("m.py", _HIDDEN_SRC) == ([], [("m.py", 3)])


def test_the_resolver_reads_rows_and_row_builders():
    sites, unresolved = _scan("m.py", _ROWS_SRC)
    assert unresolved == []
    assert sorted((s, p) for _r, _l, s, _k, p in sites) == [
        ("s1_invented", False), ("s1_invented", False), ("t10_invented", True)]


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
