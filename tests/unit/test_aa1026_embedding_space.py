"""AA-1026 D5 — embedding space registry (CIS 328/329), the space-bound embed path, the Titan V2 request shape and the
re-embed job (dry run, fill, probe). No DB, no provider: fakes only (invented texts)."""
from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services.embeddings import reembed, sources
from services.jobs import embedding_reembed_job as job
from shared.llm_client import call_log
from shared.llm_client import embed as embed_mod
from shared.llm_client import embedding_space as es
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.role_config import StageConfig

ROOT = Path(__file__).resolve().parents[2]
M328 = (ROOT / "api" / "migrations" / "328_embedding_space_registry.sql").read_text(encoding="utf-8")
M329 = (ROOT / "api" / "migrations" / "329_embedding_reembed_tools_titan_fallback.sql").read_text(encoding="utf-8")

COHERE = es.BUILTIN_SPACES[es.COHERE_V4_1536]
TITAN = es.BUILTIN_SPACES[es.TITAN_V2_1024]


def _cohere(key, account):
    return CatalogModel(model_key=key, label=key, vendor="cohere", provider="bedrock", api_style="embed",
                        bedrock_profile_ids={account: "global.cohere.embed-v4:0"}, callable_via=("embed",),
                        price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True)


TITAN_ROW = CatalogModel(model_key="titan-embed-text-v2", label="Titan V2", vendor="amazon", provider="bedrock",
                         api_style="embed", bedrock_profile_ids={"acc2-usw2": "amazon.titan-embed-text-v2:0"},
                         callable_via=("embed",), price_in_per_mtok=0.02, price_out_per_mtok=0.0, enabled=True)
CATALOG = {"cohere-embed-v4": _cohere("cohere-embed-v4", "acc2"),
           "cohere-embed-v4-acc3": _cohere("cohere-embed-v4-acc3", "acc3"),
           "titan-embed-text-v2": TITAN_ROW}


def _route():
    return StageConfig("f10_embed", "embed", "cohere", "cohere-embed-v4", None,
                       fallback_model_ids=("cohere-embed-v4-acc3",))


def _cohere_client(dim):
    client = MagicMock()

    def invoke(**kw):
        texts = json.loads(kw["body"])["texts"]
        return {"body": io.BytesIO(json.dumps({"embeddings": {"float": [[0.5] * dim for _ in texts]}}).encode()),
                "ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": str(10 * len(texts))}}}
    client.invoke_model.side_effect = invoke
    return client


def _titan_client(dim, tokens=7, vector=None):
    client = MagicMock()

    def invoke(**kw):
        body = json.loads(kw["body"])
        out = {"embedding": vector if vector is not None else [0.25] * body.get("dimensions", 1024),
               "inputTextTokenCount": tokens}
        return {"body": io.BytesIO(json.dumps(out).encode())}
    client.invoke_model.side_effect = invoke
    return client


# ── the 328 seed and its built-in mirror ─────────────────────────────────────────────────────────────────────────

def test_builtin_spaces_mirror_the_328_seed():
    for s in es.BUILTIN_SPACES.values():
        keys = "ARRAY[" + ", ".join(f"'{k}'" for k in s.model_keys) + "]"
        inputs = ", ".join("NULL" if v is None else f"'{v}'" for v in (s.document_input_type, s.query_input_type))
        row = (f"('{s.space_key}', '{s.model_family}', '{s.model_id}', {keys},\n     {s.dimension}, {inputs}, "
               f"{s.version}, '{s.status}',")
        assert row in M328, row
    assert M328.count("'migration-328-aa1026'") == 7       # 2 spaces + 5 columns


def test_builtin_columns_mirror_the_328_seed():
    for c in es.BUILTIN_COLUMNS.values():
        head = (f"('{c.table_schema}', '{c.table_name}', '{c.column_name}', '{c.space_key}', '{c.swap_group}',")
        assert head in M328, head
        keys = "ARRAY[" + ", ".join(f"'{k}'" for k in c.key_columns) + "]"
        writers = ("ARRAY[" + ", ".join(f"'{w}'" for w in c.writer_stages) + "]") if c.writer_stages \
            else "ARRAY[]::text[]"
        source = f"'{c.source_key}'" if c.source_key else "NULL"
        assert f"{keys}, {writers}, {source}," in M328, c.qualified


def test_every_vector_column_of_the_schema_reference_is_registered():
    """The five base-table vector columns of db-schema-reference (01/11/2026); the views only read them."""
    assert set(es.BUILTIN_COLUMNS) == {es.ATOM_EMBEDDING, es.QUESTION_EMBEDDING, es.CONTENT_PIECE_EMBEDDING,
                                       es.TOUR_PLACE_EMBEDDING, es.PUBLISHED_TOUR_EMBEDDING}
    groups: dict = {}
    for c in es.BUILTIN_COLUMNS.values():
        groups.setdefault(c.swap_group, set()).add(c.space_key)
    assert all(len(v) == 1 for v in groups.values())
    assert {c.swap_group for c in es.BUILTIN_COLUMNS.values()
            if es.ATOM_EMBEDDING == c.qualified or es.QUESTION_EMBEDDING == c.qualified} == {"f10_embed"}


def test_328_and_329_are_contract_files():
    # The expand classify replay of the S263 preflight finds 328 changing an existing object: both are applied by
    # hand with DB Migrate (no migrate directive).
    assert not M328.startswith("-- migrate:")
    assert not M329.startswith("-- migrate: expand")


def test_329_seeds_the_fallback_disabled_and_never_routed():
    assert "'Bedrock, main account, us-west-2 (task role)', 'us-west-2', 'aws_only',\n     false, 5.00," in M329
    assert re.search(r"'amazon.titan-embed-text-v2:0', 20, true, 'bedrock_profile', 'embed'", M329)
    assert "'{\"acc2-usw2\": \"amazon.titan-embed-text-v2:0\"}'::jsonb" in M329
    assert "stages route to titan-embed-text-v2" in M329
    assert "GRANT SELECT ON catalog.v_embedding_space_v1 TO tripplanner;" in M329
    assert re.search(r"REVOKE ALL ON FUNCTION shared\._embedding_shadow_reset\(\) FROM PUBLIC, cis_app;", M329)
    assert "SECURITY DEFINER" not in M329


def _sql_code(sql: str) -> str:
    """The file without its -- comments (the header explains what moved where)."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_329_has_no_dynamic_sql_and_only_the_trigger_function():
    # The X1 role lint refuses a role switch in dynamic SQL (CIS PR #1225 preflight): the owner steps (prepare, swap,
    # rollback, drop shadow, view rebuild) run in the AA-CIS-Infra run-script, so 329 creates one function only and
    # executes no dynamic SQL; its role switches are the literal catalog recipe.
    code = _sql_code(M329)
    assert re.findall(r"CREATE OR REPLACE FUNCTION (\S+)\(", code) == ["shared._embedding_shadow_reset"]
    assert not re.search(r"\bEXECUTE\s+(format|'|\$|[a-z_]+\s*\()", code, re.IGNORECASE)
    assert re.findall(r"SET LOCAL ROLE (\w+);", code) == ["catalog_owner", "cis_owner"]
    for gone in ("embedding_prepare_shadow", "_embedding_switch", "embedding_swap", "embedding_rollback",
                 "embedding_drop_shadow", "embedding_drop_previous", "embedding_dependent_views"):
        assert gone not in code, gone


# ── registry read ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_registry():
    es.invalidate()
    yield
    es.invalidate()


@pytest.fixture(autouse=True)
def ledger_rows(monkeypatch):
    """AA-642 A9: embed() writes its own 0 USD row for a failed attempt on a background thread — captured, no DB."""
    seen: list = []

    async def record_calls(rows):
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    yield seen


def test_unreadable_registry_falls_back_to_the_builtin_mirror():
    with patch.object(es.connections, "run_sync", side_effect=RuntimeError("db down")):
        spaces, columns = es.registry_sync()
    assert spaces == es.BUILTIN_SPACES and columns == es.BUILTIN_COLUMNS
    assert es.space_for(es.QUESTION_EMBEDDING) == COHERE


def test_loaded_registry_wins_and_a_swapped_column_reads_its_new_space():
    swapped = dataclasses.replace(es.BUILTIN_COLUMNS[es.TOUR_PLACE_EMBEDDING], space_key=es.TITAN_V2_1024,
                                  previous_column="embedding__prev", previous_space_key=es.COHERE_V4_1536)
    columns = {**es.BUILTIN_COLUMNS, swapped.qualified: swapped}
    with patch.object(es.connections, "run_sync", return_value=(dict(es.BUILTIN_SPACES), columns)) as rs:
        assert es.space_for(es.TOUR_PLACE_EMBEDDING) == TITAN
        assert es.space_for(es.CONTENT_PIECE_EMBEDDING) == COHERE
        es.registry_sync()
    assert rs.call_count == 1                                   # cached


def test_unknown_column_and_space_raise_keyerror():
    with patch.object(es.connections, "run_sync", side_effect=RuntimeError("db down")):
        with pytest.raises(KeyError):
            es.column("acp_contract.atoms.nope")
        with pytest.raises(KeyError):
            es.space("nope/16/v1")


def test_input_type_per_role():
    assert COHERE.input_type("document") == "search_document" and COHERE.input_type("query") == "search_query"
    assert TITAN.input_type("document") is None
    with pytest.raises(ValueError):
        COHERE.input_type("other")


def test_ledger_signal():
    assert es.ledger_signal(SimpleNamespace(space_key=es.COHERE_V4_1536)) == {"embedding_space": es.COHERE_V4_1536}
    assert es.ledger_signal(MagicMock(space_key=None)) == {}
    assert es.ledger_signal(object()) == {}


# ── the space-bound embed path ───────────────────────────────────────────────────────────────────────────────────

def _plan(model, allowed_scopes=None):
    """One registry deployment per model (acc2-usw2 for Titan, as once the runbook enables it)."""
    account = next(iter(model.bedrock_profile_ids))
    return [SimpleNamespace(connection=None if account != "acc2-usw2" else SimpleNamespace(connection_key=account,
                                                                                            auth_mode="task_role"),
                            deployment=SimpleNamespace(wire_id=model.bedrock_profile_ids[account]))] \
        if account == "acc2-usw2" else None


def _gateway(runtimes):
    calls = []

    def runtime_for(account):
        calls.append(account)
        return runtimes[account]
    return [patch.object(embed_mod, "get_stage_config_sync", return_value=_route()),
            patch.object(embed_mod, "get_model_sync", side_effect=CATALOG.get),
            patch.object(embed_mod, "deployment_plan", side_effect=_plan),
            patch.object(embed_mod.breaker, "guarded", side_effect=lambda conn, key, fn: fn()),
            patch.object(embed_mod, "_runtime_for", side_effect=runtime_for)], calls


def _run(patches, fn):
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in reversed(patches):
            p.stop()


def test_space_chain_keeps_route_order_within_the_space_else_the_space_models():
    assert embed_mod._space_chain(["cohere-embed-v4", "cohere-embed-v4-acc3"], COHERE) == \
        ["cohere-embed-v4", "cohere-embed-v4-acc3"]
    assert embed_mod._space_chain(["cohere-embed-v4", "other"], COHERE) == ["cohere-embed-v4"]
    assert embed_mod._space_chain(["cohere-embed-v4", "cohere-embed-v4-acc3"], TITAN) == ["titan-embed-text-v2"]


def test_embed_in_the_cohere_space_uses_its_dimension_and_tags_the_space():
    client = _cohere_client(1536)
    patches, calls = _gateway({"acc2": client})
    resp = _run(patches, lambda: embed_mod.embed("f10_embed", ["a meadow walk"], input_type="search_document",
                                                 space=COHERE))
    assert calls == ["acc2"]
    got_space = resp.space_key
    assert got_space == es.COHERE_V4_1536
    assert json.loads(client.invoke_model.call_args.kwargs["body"])["output_dimension"] == 1536


def test_embed_in_the_titan_space_never_calls_cohere():
    titan = _titan_client(1024)
    patches, calls = _gateway({"acc2": _cohere_client(1536), "acc2-usw2": titan})
    resp = _run(patches, lambda: embed_mod.embed("f10_embed", ["a meadow walk", "an old bridge"],
                                                 input_type=None, space=TITAN))
    assert calls == ["acc2-usw2"]
    assert resp.model_used == "titan-embed-text-v2"
    got_space = resp.space_key
    assert got_space == es.TITAN_V2_1024
    assert resp.fallback_used is False and len(resp.vectors) == 2 and len(resp.vectors[0]) == 1024
    assert titan.invoke_model.call_count == 2                  # one text per request
    body = json.loads(titan.invoke_model.call_args_list[0].kwargs["body"])
    assert body == {"inputText": "a meadow walk", "normalize": True, "dimensions": 1024}
    assert titan.invoke_model.call_args.kwargs["modelId"] == "amazon.titan-embed-text-v2:0"
    assert resp.input_tokens == 14 and resp.tokens_estimated is False
    assert resp.cost_usd == pytest.approx(14 * 0.02 / 1_000_000, abs=1e-6)


def test_titan_failure_does_not_fall_back_into_the_cohere_space(ledger_rows):
    broken = MagicMock()
    broken.invoke_model.side_effect = RuntimeError("AccessDenied")
    patches, calls = _gateway({"acc2": _cohere_client(1536), "acc2-usw2": broken})
    with pytest.raises(RuntimeError, match="All models in the route failed"):
        _run(patches, lambda: embed_mod.embed("f10_embed", ["x"], input_type=None, space=TITAN))
    assert calls == ["acc2-usw2"]
    call_log.wait_for_pending_writes()
    # AA-642 A9 kept: the failed Titan attempt has its own ledger row; the Cohere space is never tried or logged.
    assert [(r["model"], r["connection_key"], r["attempt_no"]) for r in ledger_rows] == [
        ("titan-embed-text-v2", "acc2-usw2", 1)]


def test_titan_refuses_a_dimension_it_does_not_have_and_a_malformed_answer():
    with pytest.raises(RuntimeError, match="dimensions"):
        embed_mod._titan_call(TITAN_ROW, "acc2-usw2", ["x"], 1536, None)
    with patch.object(embed_mod, "_runtime_for", return_value=_titan_client(1024, vector=[0.1] * 3)):
        with pytest.raises(RuntimeError, match="malformed"):
            embed_mod._titan_call(TITAN_ROW, "acc2-usw2", ["x"], 1024, None)


def test_titan_estimates_tokens_when_the_count_is_missing():
    client = MagicMock()
    client.invoke_model.return_value = {"body": io.BytesIO(json.dumps({"embedding": [0.1] * 256}).encode())}
    with patch.object(embed_mod, "_runtime_for", return_value=client):
        resp = embed_mod._titan_call(TITAN_ROW, "acc2-usw2", ["abcdefgh"], 256, None)
    assert resp.tokens_estimated is True and resp.input_tokens == 2


def test_embed_without_space_is_unchanged():
    patches, calls = _gateway({"acc2": _cohere_client(4)})
    resp = _run(patches, lambda: embed_mod.embed("f10_embed", ["x"], dimensions=4))
    assert resp.space_key is None and calls == ["acc2"]


def test_content_embedding_writes_the_space_on_its_ledger_row():
    from services.acp_shared import content_embedding as ce_mod
    patches, _ = _gateway({"acc2": _cohere_client(1536)})
    patches += [patch.object(ce_mod, "_pace_calls"), patch.object(es.connections, "run_sync",
                                                                   side_effect=RuntimeError("db down"))]
    with patch.object(ce_mod, "record_call_sync") as log:
        out = _run(patches, lambda: ce_mod.compute_embeddings(["a ridge trail"]))
    assert len(out[0]) == 1536
    assert log.call_args.kwargs["quality_signal"]["embedding_space"] == es.COHERE_V4_1536


# ── sources ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_registered_source_reads_its_own_table():
    for c in es.BUILTIN_COLUMNS.values():
        if c.source_key:
            assert sources.source_for(c).table == c.table
    with pytest.raises(ValueError, match="no re-embed text source"):
        sources.source_for(es.BUILTIN_COLUMNS[es.PUBLISHED_TOUR_EMBEDDING])
    bad = dataclasses.replace(es.BUILTIN_COLUMNS[es.QUESTION_EMBEDDING], source_key="tour_place_text")
    with pytest.raises(ValueError, match="reads geo.tour_place"):
        sources.source_for(bad)


def test_select_sql_is_a_keyset_page_of_rows_without_a_shadow_vector():
    col = es.BUILTIN_COLUMNS[es.ATOM_EMBEDDING]
    first = sources.select_sql(col, "embedding__shadow", after=False)
    assert 't."embedding" IS NOT NULL' in first and 't."embedding__shadow" IS NULL' in first
    assert 'ORDER BY t."atom_id", t."view" LIMIT $1' in first and "> (" not in first
    nxt = sources.select_sql(col, "embedding__shadow", after=True)
    assert '(t."atom_id", t."view") > ($2, $3)' in nxt
    assert "LEFT JOIN acp_contract.tour_atoms a" in nxt


def test_update_and_estimate_sql():
    col = es.BUILTIN_COLUMNS[es.QUESTION_EMBEDDING]
    assert sources.update_sql(col, "embedding__shadow", "vector") == (
        'UPDATE "acp_contract"."question_embedding" SET "embedding__shadow" = $1::vector '
        'WHERE "question_hash" = $2 AND "embedding__shadow" IS NULL')
    est = sources.estimate_sql(col, None)
    assert "embedding__shadow" not in est and "n_chars" in est


def test_content_piece_text_strips_citation_tags():
    src = sources.SOURCES["content_piece_text"]
    out = src.text("A quiet lake [R:12] at dawn [F:3].")
    assert "[R:" not in out and "[F:" not in out and out.startswith("A quiet lake")
    assert sources.SOURCES["question_text"].text(None) == ""


# ── re-embed: dry run, fill, probe ───────────────────────────────────────────────────────────────────────────────

class FakeConn:
    def __init__(self, rows, shadow_type="vector(1024)", estimate=(3, 0, 120)):
        self.rows = rows                  # [(key, text)] in key order, the shadow still empty
        self.filled: dict = {}
        self.shadow_type = shadow_type
        self.estimate = estimate
        self.updates = []

    async def fetchval(self, sql, *args):
        return self.shadow_type

    async def fetchrow(self, sql, *args):
        n = sum(1 for k, _ in self.rows if k not in self.filled) if "embedding__shadow" in sql else len(self.rows)
        blank = sum(1 for k, t in self.rows if k not in self.filled and not t.strip())
        if self.estimate is not None and not self.filled:
            return {"n_rows": self.estimate[0], "n_blank": self.estimate[1], "n_chars": self.estimate[2]}
        return {"n_rows": n, "n_blank": blank, "n_chars": 0}

    async def fetch(self, sql, limit, *after):
        todo = [(k, t) for k, t in self.rows if k not in self.filled and (not after or k > after[0])]
        return [{"k0": k, "text": t} for k, t in todo[:limit]]

    async def executemany(self, sql, args):
        self.updates.append(sql)
        for literal, key in args:
            self.filled[key] = literal


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


PREPARED = dataclasses.replace(es.BUILTIN_COLUMNS[es.QUESTION_EMBEDDING], shadow_column="embedding__shadow",
                               shadow_space_key=es.TITAN_V2_1024)


@pytest.fixture
def registry_prepared():
    columns = {**es.BUILTIN_COLUMNS, PREPARED.qualified: PREPARED}
    registry = (dict(es.BUILTIN_SPACES), columns)
    with patch.object(es.connections, "run_sync", side_effect=lambda fn: registry if fn is es._fetch_all else {}), \
         patch.object(reembed, "price_in", return_value=0.02):
        yield


def _fake_embed(dim=1024, space_key=es.TITAN_V2_1024, cost=0.0001):
    calls = []

    def fn(stage, texts, *, input_type, dimensions, space):
        calls.append((stage, list(texts), input_type, dimensions, space.space_key))
        return SimpleNamespace(vectors=[[0.5] * dim for _ in texts], model_used="titan-embed-text-v2",
                               account="acc2-usw2", input_tokens=5 * len(texts), tokens_estimated=False,
                               cost_usd=cost, fallback_used=False, space_key=space_key)
    return fn, calls


def test_dry_run_before_prepare_counts_every_vector_row():
    conn = FakeConn([], estimate=(1006, 2, 60_000))
    with patch.object(reembed, "price_in", return_value=0.02):
        out = asyncio.run(reembed.dry_run(FakePool(conn), es.QUESTION_EMBEDDING, es.TITAN_V2_1024))
    assert out["prepared"] is False and out["rows"] == 1006 and out["blank_rows"] == 2
    assert out["estimated_tokens"] == 16500 and out["estimated_usd"] == pytest.approx(0.00033)
    assert out["requests"] == 1004 and out["target_space"] == es.TITAN_V2_1024
    steps = out["next_steps"]
    assert steps[0].startswith("prepare: AA-CIS-Infra db/scripts/aa1026_embedding_shadow.py")
    assert 'ACTION = "prepare", COLUMN = "acp_contract.question_embedding.embedding", ' \
           'SPACE = "titan-embed-text-v2/1024/v1"' in steps[0]
    assert 'ACTION = "swap"' in steps[2] and "swap group f10_embed" in steps[2]
    assert not any("shared.embedding_" in s for s in steps)


def test_dry_run_refusals():
    pool = FakePool(FakeConn([]))
    with pytest.raises(reembed.ReembedRefused, match="name the target space"):
        asyncio.run(reembed.dry_run(pool, es.QUESTION_EMBEDDING))
    with pytest.raises(reembed.ReembedRefused, match="already serves"):
        asyncio.run(reembed.dry_run(pool, es.QUESTION_EMBEDDING, es.COHERE_V4_1536))
    with pytest.raises(reembed.ReembedRefused, match="no re-embed text source"):
        asyncio.run(reembed.dry_run(pool, es.PUBLISHED_TOUR_EMBEDDING, es.TITAN_V2_1024))
    with pytest.raises(reembed.ReembedRefused, match="not a registered"):
        asyncio.run(reembed.dry_run(pool, "acp_contract.nope.embedding", es.TITAN_V2_1024))


def _record_patch():
    return patch.object(reembed, "record_call_with_pool")


def test_fill_writes_the_shadow_in_pages_skips_blank_and_logs_each_call(registry_prepared):
    rows = [("h1", "where to swim"), ("h2", "  "), ("h3", "best sunrise spot"), ("h4", "quiet cafes")]
    conn = FakeConn(rows, estimate=None)
    fn, calls = _fake_embed()
    progress = []

    async def prog(**kw):
        progress.append(kw)
    with _record_patch() as log:
        out = asyncio.run(reembed.fill(FakePool(conn), es.QUESTION_EMBEDDING, cost_cap_usd=1.0, batch_size=2,
                                       pace_seconds=0, embed_fn=fn, progress=prog))
    assert [c[1] for c in calls] == [["where to swim"], ["best sunrise spot", "quiet cafes"]]
    assert all(c[0] == "f10_embed" and c[3] == 1024 and c[4] == es.TITAN_V2_1024 and c[2] is None for c in calls)
    assert set(conn.filled) == {"h1", "h3", "h4"} and conn.filled["h1"].startswith("[0.5,")
    assert out["written"] == 3 and out["blank_rows"] == 1 and out["calls"] == 2 and out["stopped"] is None
    # a blank-text row keeps the column incomplete: the swap would refuse it too (runbook: purge or fix it)
    assert out["complete"] is False and out["remaining_rows"] == 1 and out["remaining_blank_rows"] == 1
    assert out["blank_keys"] == [["h2"]]
    sig = log.call_args.kwargs["quality_signal"]
    assert sig["op"] == "reembed" and sig["embedding_space"] == es.TITAN_V2_1024
    assert log.call_args.kwargs["role"] == "embed" and log.call_args.kwargs["stage"] == "f10_embed"
    assert "connection_key" in log.call_args.kwargs               # **billed_fields(resp)
    assert progress[-1]["written"] == 3 and progress[-1]["last_key"] == ["h4"]


def test_fill_stops_before_crossing_the_cost_cap(registry_prepared):
    conn = FakeConn([("h1", "x" * 4000), ("h2", "y" * 4000)], estimate=None)
    fn, calls = _fake_embed(cost=0.11)
    with _record_patch(), patch.object(reembed, "price_in", return_value=100.0):
        out = asyncio.run(reembed.fill(FakePool(conn), es.QUESTION_EMBEDDING, cost_cap_usd=0.15, batch_size=1,
                                       pace_seconds=0, embed_fn=fn))
    # one page = 1100 tokens x 100 USD / 1M = 0.11 USD estimated (0.11 spent): the second page would cross 0.15
    assert out["stopped"] == "cost_cap" and len(calls) == 1 and out["written"] == 1


def test_fill_writes_nothing_when_the_answer_is_in_another_space(registry_prepared):
    conn = FakeConn([("h1", "a"), ("h2", "b")], estimate=None)
    fn, _ = _fake_embed(space_key=es.COHERE_V4_1536)
    with _record_patch(), pytest.raises(reembed.ReembedWrongSpace, match="not in titan"):
        asyncio.run(reembed.fill(FakePool(conn), es.QUESTION_EMBEDDING, cost_cap_usd=1.0, pace_seconds=0,
                                 embed_fn=fn))
    assert conn.filled == {}
    assert issubclass(reembed.ReembedWrongSpace, reembed.ReembedRefused)     # the job makes it NonRetryable


def test_fill_cap_counts_what_earlier_attempts_of_the_job_spent(registry_prepared):
    conn = FakeConn([("h1", "x" * 4000), ("h2", "y" * 4000)], estimate=None)
    fn, calls = _fake_embed(cost=0.11)
    with _record_patch(), patch.object(reembed, "price_in", return_value=100.0):
        # an earlier attempt spent 0.11 of the 0.15 cap: the next 0.11 page would cross it, so nothing is called
        out = asyncio.run(reembed.fill(FakePool(conn), es.QUESTION_EMBEDDING, cost_cap_usd=0.15, batch_size=1,
                                       pace_seconds=0, embed_fn=fn, already_spent=0.11))
    assert out["stopped"] == "cost_cap" and calls == [] and out["written"] == 0
    assert out["spent_usd"] == 0 and out["job_spent_usd"] == pytest.approx(0.11)
    with _record_patch(), patch.object(reembed, "price_in", return_value=100.0):
        out = asyncio.run(reembed.fill(FakePool(conn), es.QUESTION_EMBEDDING, cost_cap_usd=0.25, batch_size=1,
                                       pace_seconds=0, embed_fn=fn, already_spent=0.11))
    assert out["stopped"] == "cost_cap" and len(calls) == 1 and out["job_spent_usd"] == pytest.approx(0.22)


def test_fill_resumes_where_it_stopped(registry_prepared):
    conn = FakeConn([("h1", "a"), ("h2", "b"), ("h3", "c")], estimate=None)
    fn, calls = _fake_embed()
    with _record_patch():
        first = asyncio.run(reembed.fill(FakePool(conn), es.QUESTION_EMBEDDING, cost_cap_usd=1.0, batch_size=1,
                                         max_rows=2, pace_seconds=0, embed_fn=fn))
        second = asyncio.run(reembed.fill(FakePool(conn), es.QUESTION_EMBEDDING, cost_cap_usd=1.0, batch_size=1,
                                          pace_seconds=0, embed_fn=fn))
    assert first["stopped"] == "max_rows" and first["written"] == 2
    assert second["written"] == 1 and [c[1] for c in calls][-1] == ["c"] and second["complete"] is True


def test_fill_refusals(registry_prepared):
    fn, _ = _fake_embed()
    pool = FakePool(FakeConn([], shadow_type="vector(1536)"))
    with pytest.raises(reembed.ReembedRefused, match="cost_cap_usd"):
        asyncio.run(reembed.fill(pool, es.QUESTION_EMBEDDING, cost_cap_usd=0, embed_fn=fn))
    with pytest.raises(reembed.ReembedRefused, match="cost_cap_usd"):
        asyncio.run(reembed.fill(pool, es.QUESTION_EMBEDDING, cost_cap_usd=26, embed_fn=fn))
    with pytest.raises(reembed.ReembedRefused, match="dimension 1024"):
        asyncio.run(reembed.fill(pool, es.QUESTION_EMBEDDING, cost_cap_usd=1, embed_fn=fn))
    with pytest.raises(reembed.ReembedRefused, match="no shadow is prepared"):
        asyncio.run(reembed.fill(pool, es.ATOM_EMBEDDING, cost_cap_usd=1, embed_fn=fn))
    with pytest.raises(reembed.ReembedRefused, match="does not exist"):
        asyncio.run(reembed.fill(FakePool(FakeConn([], shadow_type=None)), es.QUESTION_EMBEDDING,
                                 cost_cap_usd=1, embed_fn=fn))


def test_cast_and_literal():
    assert reembed.cast_for("vector(1024)", 1024) == "vector"
    assert reembed.cast_for("real[]", 1024) == "real[]"
    with pytest.raises(reembed.ReembedRefused):
        reembed.cast_for("text; drop table x", 1024)
    assert reembed.vector_literal([1, 0.5], "vector") == "[1.0,0.5]"
    assert reembed.vector_literal([1, 0.5], "real[]") == "{1.0,0.5}"


def test_probe_ok_and_refused():
    fn, calls = _fake_embed()
    with _record_patch() as log:
        ok = asyncio.run(reembed.probe(FakePool(FakeConn([])), es.TITAN_V2_1024, embed_fn=fn))
    assert ok["ok"] is True and ok["dimension"] == 1024 and calls[0][0] == "adhoc_conn_test"
    assert log.call_args.kwargs["stage"] == "adhoc_conn_test"

    def refused(*a, **k):
        raise RuntimeError("All models in the route failed: AccessDenied")
    with _record_patch() as log:
        bad = asyncio.run(reembed.probe(FakePool(FakeConn([])), es.TITAN_V2_1024, embed_fn=refused))
    assert bad["ok"] is False and "AccessDenied" in bad["error"] and not log.called


# ── job payload ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_job_payload_validation():
    assert job.check_payload({"column": es.QUESTION_EMBEDDING})["mode"] == "dry_run"
    with pytest.raises(ValueError, match="mode"):
        job.check_payload({"mode": "swap", "column": es.QUESTION_EMBEDDING})
    with pytest.raises(ValueError, match="column"):
        job.check_payload({"mode": "fill", "cost_cap_usd": 1})
    with pytest.raises(ValueError, match="cost_cap_usd"):
        job.check_payload({"mode": "fill", "column": es.QUESTION_EMBEDDING})
    with pytest.raises(ValueError, match="space_key"):
        job.check_payload({"mode": "probe"})
    with pytest.raises(ValueError, match="batch_size"):
        job.check_payload({"mode": "fill", "column": es.QUESTION_EMBEDDING, "cost_cap_usd": 1, "batch_size": 0})
    out = job.check_payload({"mode": "fill", "column": es.QUESTION_EMBEDDING, "cost_cap_usd": 1, "max_rows": 5})
    assert out == {"mode": "fill", "requested_by": None, "column": es.QUESTION_EMBEDDING, "cost_cap_usd": 1.0,
                   "max_rows": 5}


def test_job_turns_a_refusal_into_non_retryable():
    from shared.jobs.registry import NonRetryable
    ctx = SimpleNamespace(payload={"mode": "dry_run", "column": es.PUBLISHED_TOUR_EMBEDDING,
                                   "space_key": es.TITAN_V2_1024}, pool=FakePool(FakeConn([])))
    with pytest.raises(NonRetryable, match="no re-embed text source"):
        asyncio.run(job.run.__wrapped__(ctx) if hasattr(job.run, "__wrapped__") else job.run(ctx))


def _job_ctx(payload, conn, cost_usd=0.0):
    async def prog(**kw):
        return None
    ctx = SimpleNamespace(payload=payload, pool=FakePool(conn), cost_usd=cost_usd, progress=prog)
    ctx.add_cost = lambda usd: setattr(ctx, "cost_usd", ctx.cost_usd + float(usd or 0.0))
    return ctx


def _run_job(ctx):
    return asyncio.run(job.run.__wrapped__(ctx) if hasattr(job.run, "__wrapped__") else job.run(ctx))


def test_job_fill_cap_spans_retries_and_a_wrong_space_answer_is_not_retried(registry_prepared):
    from shared.jobs.registry import NonRetryable
    payload = {"mode": "fill", "column": es.QUESTION_EMBEDDING, "cost_cap_usd": 0.15, "batch_size": 1}
    fn, calls = _fake_embed(cost=0.11)
    with _record_patch(), patch.object(reembed, "price_in", return_value=100.0), \
         patch("shared.llm_client.embed.embed", fn), patch.object(reembed, "OTHER_DEFAULTS",
                                                                   {"batch_size": 16, "pace_seconds": 0}):
        # attempt 2 of a job whose attempt 1 already spent 0.11 (shared.job.cost_usd): no new paid call
        ctx = _job_ctx(payload, FakeConn([("h1", "x" * 4000)], estimate=None), cost_usd=0.11)
        out = _run_job(ctx)
    assert out["stopped"] == "cost_cap" and calls == [] and ctx.cost_usd == pytest.approx(0.11)

    wrong, _ = _fake_embed(space_key=es.COHERE_V4_1536)
    with _record_patch(), patch("shared.llm_client.embed.embed", wrong), \
         patch.object(reembed, "OTHER_DEFAULTS", {"batch_size": 16, "pace_seconds": 0}):
        ctx = _job_ctx({**payload, "cost_cap_usd": 1}, FakeConn([("h1", "a")], estimate=None))
        with pytest.raises(NonRetryable, match="not in titan"):
            _run_job(ctx)


def test_daily_probe_also_covers_the_titan_deployment():
    from services.jobs import llm_probe_job
    conn = SimpleNamespace(kind="bedrock")
    deps = [SimpleNamespace(model_key="titan-embed-text-v2"), SimpleNamespace(model_key="cohere-embed-v4")]
    other = dataclasses.replace(TITAN_ROW, model_key="other-embed", vendor="othervendor")
    with patch("shared.llm_client.catalog.get_model_sync", side_effect={**CATALOG, "other-embed": other}.get):
        pairs = llm_probe_job._embeddable(conn, deps + [SimpleNamespace(model_key="other-embed")])
    assert [m.model_key for _, m in pairs] == ["titan-embed-text-v2", "cohere-embed-v4"]
