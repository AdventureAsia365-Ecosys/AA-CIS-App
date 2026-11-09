"""AA-642 (S263 lane L2, B2) — the daily probe also covers the embedding deployments and one Geoapify search
(services/jobs/llm_probe_job.py, shared/llm_client/embed.probe). No provider is called: every call is mocked."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.jobs import llm_probe_job as probe
from shared.llm_client import embed
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.connections import BUILTIN_CONNECTIONS
from shared.llm_client.deployments import ModelDeployment


def _model(key, style="converse", vendor="cohere", p_in=0.12, enabled=True):
    return CatalogModel(model_key=key, label=key, vendor=vendor, provider="bedrock", api_style=style,
                        callable_via=("embed",), price_in_per_mtok=p_in, price_out_per_mtok=0.0, enabled=enabled)


def _dep(model, conn, enabled=True, **kw):
    return ModelDeployment(model, conn, f"wire-{model}", 10, enabled, "seed", **kw)


CONNS = {"acc2": BUILTIN_CONNECTIONS["acc2"], "acc3": BUILTIN_CONNECTIONS["acc3"]}
EMB = _model("embed-x", style="embed")
DEPS = [_dep("embed-x", "acc2"), _dep("embed-x", "acc3"), _dep("text-y", "acc3")]


def _no_text(conn, deployments):
    return []


def _embeddable(conn, deployments):
    return [(d, EMB) for d in deployments if d.model_key == "embed-x"]


# ── the plan ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_plan_has_one_embedding_probe_per_enabled_deployment_and_the_geoapify_search():
    plan = probe.build_plan(CONNS, DEPS, _no_text, _embeddable, {"credits_left": 100})
    got = [(p["connection_key"], p["model_key"], p["probe"]) for p in plan["probes"]]
    assert got == [("acc2", "embed-x", "embed"), ("acc3", "embed-x", "embed"),
                   ("geoapify", probe.GEOAPIFY_PROBE_LABEL, "geocode")]
    # a few tokens at the catalog price: far below a cent a month for the two embedding probes
    assert plan["estimated_usd"] * 30 < 0.001 and plan["unpriced"] == 0


def test_a_disabled_embedding_deployment_and_a_skipped_geoapify_say_why():
    deps = [_dep("embed-x", "acc2", enabled=False), _dep("embed-x", "acc3")]
    plan = probe.build_plan(CONNS, deps, _no_text, _embeddable, {"skip": "disabled"})
    assert [(p["connection_key"], p["probe"]) for p in plan["probes"]] == [("acc3", "embed")]
    why = {(s["connection_key"], s["model_key"]): s["why"] for s in plan["skipped"]}
    assert why[("acc2", "embed-x")] == "deployment disabled"
    assert why[("geoapify", probe.GEOAPIFY_PROBE_LABEL)] == "disabled"


def test_without_the_new_filters_the_plan_is_text_only_as_before():
    plan = probe.build_plan(CONNS, DEPS, _no_text)
    assert plan["probes"] == []


def test_embeddable_takes_enabled_cohere_embedding_rows_on_bedrock_only(monkeypatch):
    from shared.llm_client import catalog
    rows = {"embed-x": EMB, "embed-off": _model("embed-off", style="embed", enabled=False),
            "embed-other": _model("embed-other", style="embed", vendor="othervendor"), "text-y": _model("text-y")}
    monkeypatch.setattr(catalog, "get_model_sync", lambda key: rows.get(key))
    deps = [_dep(k, "acc2") for k in rows]
    assert [d.model_key for d, _ in probe._embeddable(CONNS["acc2"], deps)] == ["embed-x"]
    assert probe._embeddable(SimpleNamespace(kind="openai"), deps) == []


# ── Geoapify state ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_geoapify_state_skips_when_off_not_configured_or_out_of_credit(monkeypatch):
    from services.places import geocoder
    from services.places import store as place_store

    monkeypatch.setattr(geocoder, "provider_name", lambda value=None: "model_only")
    assert (await probe.load_geoapify_state(MagicMock()))["skip"].startswith("not the configured geocoder")

    monkeypatch.setattr(geocoder, "provider_name", lambda value=None: "geoapify")
    monkeypatch.setattr(geocoder, "refresh_daily_credit_limit", AsyncMock(return_value=10))
    monkeypatch.setattr(place_store, "geocoder_requests_today", AsyncMock(return_value=3))
    monkeypatch.setattr(geocoder, "connection_switched_off", lambda: True)
    assert await probe.load_geoapify_state(MagicMock()) == {"skip": "disabled"}

    monkeypatch.setattr(geocoder, "connection_switched_off", lambda: False)
    assert (await probe.load_geoapify_state(MagicMock()))["credits_left"] == 7

    monkeypatch.setattr(place_store, "geocoder_requests_today", AsyncMock(return_value=10))
    assert (await probe.load_geoapify_state(MagicMock()))["skip"].startswith("daily credit limit reached")

    monkeypatch.setattr(place_store, "geocoder_requests_today", AsyncMock(side_effect=OSError("down")))
    assert (await probe.load_geoapify_state(MagicMock()))["skip"] == "credit count unreadable (OSError)"


# ── embed.probe ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_embed_probe_is_the_half_open_probe_of_one_deployment(monkeypatch):
    seen = {}

    def fake_guarded(conn, model_key, call, *, admit_call=True, answered=()):
        seen.update(conn=conn.connection_key, model=model_key, admit=admit_call)
        return call()

    def fake_call(model, account, texts, input_type, dimensions, wire_id=None):
        seen.update(account=account, wire=wire_id, texts=texts, input_type=input_type)
        return "resp"

    monkeypatch.setattr(embed.breaker, "guarded", fake_guarded)
    monkeypatch.setattr(embed, "_call", fake_call)
    assert embed.probe(EMB, CONNS["acc3"], "wire-z", ["a few words"]) == "resp"
    assert seen == {"conn": "acc3", "model": "embed-x", "admit": False, "account": "acc3", "wire": "wire-z",
                    "texts": ["a few words"], "input_type": "search_query"}
    with pytest.raises(RuntimeError, match="not a Bedrock embedding deployment"):
        embed.probe(_model("text-y"), CONNS["acc3"], "wire-z", ["x"])


# ── probe_embedding: ledger + audit ───────────────────────────────────────────────────────────────────────────────

class _Pool:
    def __init__(self):
        self.db = MagicMock()
        self.db.execute = AsyncMock()

    @asynccontextmanager
    async def acquire(self):
        yield self.db


def _patch_embedding(monkeypatch, outcome):
    from shared.llm_client import call_log, catalog
    from shared.llm_client import connections as conn_registry

    monkeypatch.setattr(conn_registry, "fetch_connections", AsyncMock(return_value=CONNS))
    monkeypatch.setattr(catalog, "get_model_sync", lambda key: EMB if key == "embed-x" else None)

    def fake_probe(model, conn, wire_id, texts):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(embed, "probe", fake_probe)
    rec = AsyncMock()
    monkeypatch.setattr(call_log, "record_call_with_pool", rec)
    return rec


@pytest.mark.asyncio
async def test_an_answered_embedding_probe_logs_its_cost_on_the_connection(monkeypatch):
    resp = embed.EmbedResponse(vectors=[[0.1]], model_used="embed-x", account="acc2", input_tokens=3,
                               tokens_estimated=False, cost_usd=0.000001)
    rec = _patch_embedding(monkeypatch, resp)
    pool = _Pool()
    out = await probe.probe_embedding(pool, "acc2", "embed-x", "wire-embed-x")
    assert out["ok"] is True and out["probe"] == "embed" and out["cost_usd"] == 0.000001
    kw = rec.await_args.kwargs
    assert kw["stage"] == "adhoc_conn_test" and kw["role"] == "embed" and kw["account"] == "acc2"
    assert kw["provider"] == "bedrock-native" and kw["cost_usd"] == 0.000001 and kw["tokens_out"] == 0
    assert "pricing_mode" in kw                                       # billed_fields(resp) passed on
    audit = pool.db.execute.await_args.args
    assert "acp_shared.audit_log" in audit[0] and audit[4] == "acc2"


@pytest.mark.asyncio
async def test_a_refused_embedding_probe_is_a_refused_row_at_zero_usd(monkeypatch):
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "InvokeModel")
    rec = _patch_embedding(monkeypatch, err)
    out = await probe.probe_embedding(_Pool(), "acc3", "embed-x", "wire-embed-x")
    assert out["ok"] is False and out["error_class"] == "access_denied" and out["cost_usd"] == 0.0
    kw = rec.await_args.kwargs
    assert kw["status"] == "refused" and kw["cost_usd"] == 0 and kw["role"] == "embed"
    assert kw["account"] == "acc3" and kw["provider"] == "bedrock-satellite"


@pytest.mark.asyncio
async def test_an_unknown_embedding_deployment_raises(monkeypatch):
    _patch_embedding(monkeypatch, None)
    with pytest.raises(LookupError):
        await probe.probe_embedding(_Pool(), "acc2", "nope", "w")


# ── probe_geoapify ────────────────────────────────────────────────────────────────────────────────────────────────

class _FakeGeocoder:
    cost_per_request = 0.0

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.requests = 0
        self.failed_requests = 0
        self.closed = False

    async def search(self, text, iso2):
        if self.behaviour == "off":
            raise self._exc_off
        self.requests += 1
        if self.behaviour == "ok":
            return {"results": []}
        self.failed_requests += 1
        raise self._exc_err

    async def aclose(self):
        self.closed = True


def _patch_geo(monkeypatch, behaviour, held=True):
    from services.places import geocoder, geolock
    from services.places import store as place_store
    from shared.llm_client import connections as conn_registry

    @asynccontextmanager
    async def fake_lock(pool):
        yield held

    g = _FakeGeocoder(behaviour)
    g._exc_off = geocoder.GeocoderUnavailable(f"geoapify: {conn_registry.switched_off_reason('geoapify')}")
    g._exc_err = (geocoder.GeocoderUnavailable("geoapify: HTTP 401 (key rejected)") if behaviour == "denied"
                  else geocoder.GeocoderError("geoapify: HTTP 429 after 1 attempts"))
    made = {}

    def fake_get(value=None, **kw):
        made.update(kw, value=value)
        return g

    monkeypatch.setattr(geolock, "geoapify_lock", fake_lock)
    monkeypatch.setattr(geocoder, "get_geocoder", fake_get)
    monkeypatch.setattr(geocoder, "refresh_daily_credit_limit", AsyncMock(return_value=2500))
    monkeypatch.setattr(place_store, "geocoder_requests_today", AsyncMock(return_value=4))
    logged = AsyncMock()
    monkeypatch.setattr(place_store, "log_geocode_calls", logged)
    note = AsyncMock()
    monkeypatch.setattr(conn_registry, "note_provider_access", note)
    return g, made, logged, note


@pytest.mark.asyncio
async def test_a_geoapify_probe_is_one_search_logged_in_the_credit_ledger(monkeypatch):
    g, made, logged, note = _patch_geo(monkeypatch, "ok")
    out = await probe.probe_geoapify(_Pool(), "job-1")
    assert out["ok"] is True and out["requests"] == 1 and out["cost_usd"] == 0.0
    assert made["value"] == "geoapify" and made["max_retries"] == 0 and made["credits_used_today"] == 4
    assert logged.await_args.args[1:5] == ("geoapify", 1, 0.0, "job-1") and g.closed
    note.assert_awaited_once_with("geoapify", "ok", probed=True)


@pytest.mark.asyncio
async def test_a_rejected_key_writes_access_denied(monkeypatch):
    _, _, logged, note = _patch_geo(monkeypatch, "denied")
    out = await probe.probe_geoapify(_Pool())
    assert out["ok"] is False and out["error_class"] == "access_denied"
    assert logged.await_args.kwargs["failed_requests"] == 1
    note.assert_awaited_once_with("geoapify", "access_denied", probed=True)


@pytest.mark.asyncio
async def test_a_throttled_search_stamps_the_probe_time_only(monkeypatch):
    _, _, _, note = _patch_geo(monkeypatch, "throttled")
    out = await probe.probe_geoapify(_Pool())
    assert out["ok"] is False and out["error_class"] == "other"
    note.assert_awaited_once_with("geoapify", None, probed=True)


@pytest.mark.asyncio
async def test_a_switched_off_geoapify_sends_nothing_and_writes_no_state(monkeypatch):
    _, _, logged, note = _patch_geo(monkeypatch, "off")
    out = await probe.probe_geoapify(_Pool())
    assert out["ok"] is False and out["requests"] == 0
    assert logged.await_args.args[2] == 0                             # log_geocode_calls skips a 0-request row
    note.assert_awaited_once_with("geoapify", None, probed=True)


@pytest.mark.asyncio
async def test_a_place_job_holding_the_lock_skips_the_probe(monkeypatch):
    _, made, logged, note = _patch_geo(monkeypatch, "ok", held=False)
    out = await probe.probe_geoapify(_Pool())
    assert out["skipped"] and out["ok"] is None
    assert made == {} and logged.await_count == 0 and note.await_count == 0


# ── the run: dispatch and counts ──────────────────────────────────────────────────────────────────────────────────

def _ctx(payload):
    ctx = MagicMock()
    ctx.payload = payload
    ctx.job_id = "j2"
    ctx.pool = MagicMock()
    return ctx


@pytest.mark.asyncio
async def test_a_live_run_sends_each_probe_to_its_own_prober_and_does_not_count_a_skipped_one(monkeypatch):
    monkeypatch.setenv(probe.SWITCH_ENV, "on")
    plan = probe.build_plan(CONNS, DEPS, _no_text, _embeddable, {"credits_left": 5})
    monkeypatch.setattr(probe, "load_plan", AsyncMock(return_value=plan))
    stamp = AsyncMock()
    monkeypatch.setattr(probe, "stamp_probed", stamp)

    async def fake_embed(pool, key, model_key, wire_id):
        if key == "acc3":
            raise LookupError("gone")
        return {"connection_key": key, "model_key": model_key, "probe": "embed", "ok": True, "cost_usd": 0.000001}

    async def fake_geo(pool, job_id):
        assert job_id == "j2"
        return {"connection_key": "geoapify", "model_key": "geocode-search", "probe": "geocode", "ok": None,
                "skipped": "a place job holds the Geoapify lock", "cost_usd": 0.0}

    monkeypatch.setattr(probe, "probe_embedding", fake_embed)
    monkeypatch.setattr(probe, "probe_geoapify", fake_geo)
    out = await probe.run(_ctx({"trigger": "schedule"}))
    assert out["probed"] == 2 and out["ok"] == 1 and out["failed"] == 1 and out["calls_made"] == 2
    assert out["cost_usd"] == 0.000001
    assert {r["connection_key"]: r.get("error_class") for r in out["results"]}["acc3"] == "unavailable"
    assert stamp.await_args.args[1] == ["acc2"]


@pytest.mark.asyncio
async def test_a_dry_run_lists_the_new_probes_and_calls_nothing(monkeypatch):
    monkeypatch.delenv(probe.SWITCH_ENV, raising=False)
    plan = probe.build_plan(CONNS, DEPS, _no_text, _embeddable, {"credits_left": 5})
    monkeypatch.setattr(probe, "load_plan", AsyncMock(return_value=plan))
    geo = AsyncMock()
    monkeypatch.setattr(probe, "probe_geoapify", geo)
    out = await probe.run(_ctx({"trigger": "manual"}))
    assert out["dry_run"] is True and out["calls_made"] == 0
    assert sorted(p["probe"] for p in out["planned"]) == ["embed", "embed", "geocode"]
    geo.assert_not_awaited()
