"""AA-900 W0 (root ADR 0020; S271 critic fixes F4 and "Change control") — no DB, no model call:

1. the widened release config fingerprint (services/writing_memory/config_fp.py): a migration-381-style effort row,
   a route reorder, a mode switch, a prompt edit, an L0 activation, a superseded platform release and a house-style
   edit each move `config_fp` and flip reverify; an unrelated stage does not; a pre-W0 fingerprint is not a change;
2. the env-switch guard: every `*_ENV = "..."` constant of a writer module is classified (output-changing or not);
3. the R1 freeze (r1_gate.py): mining refuses at $0, a release with content and an L0 lexicon rule do not activate,
   an empty release and the break-glass switch still work;
4. the L0 lexicon is warn-only by default (quality_gates.gating_forbidden / learned_hits);
5. the answered writer model on the T2 version record (insertion.writer_answer / generation_meta).
Every fixture name below is invented.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.acp_content_writing import quality_gates as qg
from services.writing_memory import config_fp as cf
from services.writing_memory import insertion, l0, mining, r1_gate
from services.writing_memory import releases as rel

ROOT = Path(__file__).resolve().parents[2]


def _route(stage, model="model-a", fallbacks=("model-b", "model-c"), **kw):
    return {"stage": stage, "role": kw.get("role", "writer"), "provider": "claude", "model_id": model,
            "fallback_model_ids": list(fallbacks), "account_route": None, "is_active": True,
            "allowed_scopes": None, "route_deadline_s": None, "degrade_mode": kw.get("degrade_mode"),
            "pii_class": "content"}


def _param(stage, model="model-a", effort=None, max_tokens=None):
    return {"stage": stage, "model_key": model, "max_tokens": max_tokens, "min_output_tokens": None,
            "temperature": None, "effort": effort, "reasoning_mode": None}


ROUTES = [_route("t9_write"), _route("t9_repair", model="model-d", fallbacks=()), _route("t10_judge", role="judge"),
          _route("writing_memory_eval", role="judge"), _route("a3_place_card"), _route("s1_generate"),
          _route("t2_generate")]
PARAMS = [_param("t9_faq_filter", "model-e", effort="low")]


def _fp(writer="t9_blog", *, routes=None, params=None, env=None, prompts=None, l0_rules=(), memory_items=None,
        brand=None, settings=None, service=cf.WRITER_SERVICE, policies=()):
    return cf.build(writer, routes=ROUTES if routes is None else routes, params=PARAMS if params is None else params,
                    catalog=[{"model_key": "model-a", "wire_model": "wire-a", "api_style": "converse",
                              "provider": "bedrock", "enabled": True, "max_output_tokens": None,
                              "supports_temperature": True}],
                    deployments=[], route_settings=settings or {"order_mode": "model_first", "current_version": 7},
                    env=env or {}, prompts=prompts or {"t9_write": "aaa"}, memory_items=memory_items,
                    l0_rules=l0_rules, brand_rows=brand, service=service, policies=policies)


def _wrap(c):
    return {"config_fp": c}


# ── 1. the fingerprint ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_migration_381_style_effort_row_moves_config_fp_and_flips_reverify():
    before = _fp()
    after = _fp(params=PARAMS + [_param("t9_write", "model-a", effort="medium")])
    assert before["sha"] != after["sha"]
    assert cf.reverify_components(_wrap(before), _wrap(after)) == ["stage_params"]
    assert cf.changed_stages(_wrap(before), _wrap(after)) == ["t9_write"]
    assert rel.reverify_reasons(_wrap(before), _wrap(after)) == ["model_route"]


def test_a_route_reorder_moves_config_fp():
    reordered = [_route("t9_write", fallbacks=("model-c", "model-b"))] + ROUTES[1:]
    before, after = _fp(), _fp(routes=reordered)
    assert cf.reverify_components(_wrap(before), _wrap(after)) == ["routes"]
    assert cf.changed_stages(_wrap(before), _wrap(after)) == ["t9_write"]


def test_a_repair_and_a_check_stage_are_on_the_path_too():
    for stage in ("t9_repair", "t9_faq_answer_fit", "t10_claims", "t8_angle_gen", "writing_memory_eval"):
        assert cf.on_path("t9_blog", stage), stage
    assert cf.on_path("a1", "s1_flag_fix") and cf.on_path("t2", "t2_itinerary_nudge")
    assert not cf.on_path("t9_blog", "s1_generate") and not cf.on_any_path("a3_place_card")


def test_an_unrelated_stage_does_not_move_config_fp():
    base = _fp()
    for changed in ([_route(r["stage"], model="model-z") if r["stage"] in ("a3_place_card", "s1_generate",
                                                                             "t2_generate") else r for r in ROUTES],):
        assert _fp(routes=changed)["sha"] == base["sha"]
    assert _fp(params=PARAMS + [_param("a3_place_days", effort="low")])["sha"] == base["sha"]
    # the route-control version alone is context, not config: every save of any stage bumps it
    assert _fp(settings={"order_mode": "model_first", "current_version": 8})["sha"] == base["sha"]
    assert _fp(settings={"order_mode": "aws_first", "current_version": 8})["sha"] != base["sha"]


def test_a_mode_switch_moves_config_fp_and_only_names_are_stored():
    base = _fp()
    on = _fp(env={"T9_READER_CHECKLIST_MODE": "R10", "T10_SA_MODE_DATES": "block", "UNRELATED_SWITCH": "1"})
    assert cf.reverify_components(_wrap(base), _wrap(on)) == ["mode_flags"]
    assert on["mode_flags_set"] == ["T10_SA_MODE_DATES", "T9_READER_CHECKLIST_MODE"]
    assert "r10" not in str(on) and "block" not in str(on["mode_flags_set"])
    assert _fp(env={"UNRELATED_SWITCH": "1"})["sha"] == base["sha"]
    spaced = {"T9_READER_CHECKLIST_MODE": "  r10 ", "T10_SA_MODE_DATES": "BLOCK"}
    assert _fp(env=spaced)["sha"] == on["sha"]                       # values are normalised (strip, lower)


def test_prompt_l0_memory_and_house_style_move_config_fp():
    base = _fp()
    assert cf.reverify_components(_wrap(base), _wrap(_fp(prompts={"t9_write": "bbb"}))) == ["prompts"]
    lex = [{"rule_id": "r-1", "scope": "platform", "rule_text": "quietly bramblewise", "replacement": None,
            "applies_to": ["linkedin"]}]
    assert cf.reverify_components(_wrap(base), _wrap(_fp(l0_rules=lex))) == ["l0_lexicon"]
    t1, t2 = _fp(memory_items={"platform": ["a" * 64]}), _fp(memory_items={"platform": ["b" * 64]})
    assert cf.reverify_components(_wrap(t1), _wrap(t2)) == ["memory_items"]
    b1 = _fp(brand={"brand_rules": [{"style_guide": "Plain."}]})
    b2 = _fp(brand={"brand_rules": [{"style_guide": "Warm."}]})
    assert cf.reverify_components(_wrap(b1), _wrap(b2)) == ["brand_config"]
    assert rel.reverify_reasons(_wrap(b1), _wrap(b2)) == ["prompt_version_mismatch"]


def test_a_pre_w0_fingerprint_is_not_a_change_and_legacy_keys_still_count():
    old = {"golden_set": "aa729@1", "writer_route": {"model_id": "model-a"}, "gate_taxonomy": "v3"}
    now = {**old, "config_fp": _fp()}
    assert rel.reverify_reasons(old, now) == []
    assert rel.reverify_reasons({**old, "config_fp": _fp()}, {**now, "golden_set": "aa729@2"}) == ["golden_set"]
    assert rel.config_drift(old, now) == {"components": [], "stages": [], "mode_flags_compared": False}


# worker-only switches of the Dev task definitions (infra modules/ecs: the api task has none of them)
WORKER_ENV = {"T2_JUDGE_GATE": "on", "T2_JUDGE_ANCHORS": "on", "T3_BACKGROUND_FACTS": "on", "T9_FAQ_LLM_FILTER": "on",
              "S1_WRITER_V2": "on"}


@pytest.mark.parametrize("writer", ["a1", "t2", "t9_blog"])
def test_an_api_draft_is_not_flagged_by_the_worker_env_alone(writer):
    drafted = _wrap(_fp(writer, env={}, service="api"))                 # admin drafts in the api
    scanned = _wrap(_fp(writer, env=WORKER_ENV, service="worker"))      # the worker's start scan
    assert cf.reverify_components(drafted, scanned) == []
    assert rel.reverify_reasons(drafted, scanned) == []
    assert rel.config_drift(drafted, scanned)["mode_flags_compared"] is False
    # an api scan (route-control save) never compares mode_flags, whatever the api env is
    assert cf.reverify_components(_wrap(_fp(writer, env=WORKER_ENV, service="worker")),
                                  _wrap(_fp(writer, env={}, service="api"))) == []


def test_the_worker_compares_mode_flags_against_the_releases_worker_evidence():
    drafted = _wrap(_fp("t2", env={}, service="api"))
    r2_same = _wrap(_fp("t2", env=WORKER_ENV, service="worker"))        # the R2 run of the release, in the worker
    scanned = _wrap(_fp("t2", env=WORKER_ENV, service="worker"))
    assert cf.reverify_components(drafted, scanned, [r2_same]) == []
    changed = _wrap(_fp("t2", env={**WORKER_ENV, "T2_JUDGE_GATE": "off"}, service="worker"))
    assert cf.reverify_components(drafted, changed, [r2_same]) == ["mode_flags"]
    assert rel.reverify_reasons(drafted, changed, (r2_same,)) == ["model_route"]
    assert rel.config_drift(drafted, changed, (r2_same,))["mode_flags_compared"] is True
    # a release the worker drafted compares against its own fingerprint
    assert cf.reverify_components(r2_same, changed) == ["mode_flags"]
    # an evidence run from the api, or one with no service recorded, is no reference
    api_run = _wrap(_fp("t2", env={}, service="api"))
    no_service = {"config_fp": {k: v for k, v in r2_same["config_fp"].items() if k != "service"}}
    assert cf.reverify_components(drafted, changed, [api_run, no_service]) == []


def test_a_component_unavailable_on_either_side_flags_nothing():
    real = _wrap(_fp())
    for broken in (_fp(params=cf.UNAVAILABLE), _fp(prompts=cf.UNAVAILABLE), _fp(prompts={"t9_write": cf.UNAVAILABLE}),
                   _fp(l0_rules=cf.UNAVAILABLE), _fp(policies=cf.UNAVAILABLE)):
        assert cf.reverify_components(real, _wrap(broken)) == [], broken["parts"]
        assert cf.reverify_components(_wrap(broken), real) == [], broken["parts"]
    # a failed routes read takes the catalog with it (its model list comes from the routes), and names no stage
    no_routes = _fp(routes=cf.UNAVAILABLE)
    assert no_routes["parts"]["routes"] == cf.UNAVAILABLE and no_routes["parts"]["catalog"] == cf.UNAVAILABLE
    assert cf.reverify_components(real, _wrap(no_routes)) == []
    assert cf.changed_stages(real, _wrap(no_routes)) == []
    # a real change elsewhere is still seen next to an unavailable part
    moved = _fp(routes=cf.UNAVAILABLE, prompts={"t9_write": "bbb"})
    assert cf.reverify_components(real, _wrap(moved)) == ["prompts"]


def test_collect_marks_the_catalog_unavailable_when_routes_fail():
    import asyncio
    fp = asyncio.run(cf.collect(_FakeConn(missing={"llm_role_config"}), "t9_blog", env={}))
    assert fp["parts"]["routes"] == cf.UNAVAILABLE and fp["parts"]["catalog"] == cf.UNAVAILABLE


def test_the_sibling_w0_switches_are_classified_ahead():
    assert "S1_JUDGE_LIVE" in cf.MODE_FLAGS["a1"] and "T2_INTERIM" in cf.MODE_FLAGS["t2"]
    for w in ("t9_blog", "t9_social"):
        assert {"T10_F9_POLICY_MODE", "T9_LAST_ATTEMPT_MODE"} <= set(cf.MODE_FLAGS[w])
    assert "LLM_CASSETTE" in cf.NOT_OUTPUT_FLAGS                         # record-only: never changes an output


def test_the_fingerprint_stays_small():
    import json
    big = _fp(routes=ROUTES + [_route(f"t9_extra_{i}") for i in range(30)],
              env={k: "on" for k in cf.GATEWAY_FLAGS})
    assert len(json.dumps(big)) < 4096          # CIS 287: pg_column_size(fingerprint) <= 8192


def test_the_path_prompts_come_from_the_manifest():
    shas = cf.prompt_shas("t9_blog")
    assert "t9_write" in shas and "t10_judge" in shas and "s1_generate" not in shas
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in shas.values())


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    """Answers the config_fp reads by table; a table in `missing` raises (as a missing table would)."""

    def __init__(self, missing=()):
        self.missing = set(missing)

    def transaction(self):
        return _Tx()

    async def fetch(self, sql, *args):
        for table in self.missing:
            if table in sql:
                raise RuntimeError(f"relation {table} does not exist")
        if "llm_role_config" in sql:
            return ROUTES
        if "stage_model_params" in sql:
            return PARAMS
        if "llm_route_settings" in sql:
            return [{"order_mode": "model_first", "current_version": 3}]
        return []


def test_collect_reads_every_component_and_a_missing_table_is_unavailable():
    import asyncio
    full = asyncio.run(cf.collect(_FakeConn(), "t9_blog", env={}))
    assert full["parts"]["routes"] != cf.UNAVAILABLE and full["route_config_version"] == 3
    gone = asyncio.run(cf.collect(_FakeConn(missing={"stage_model_params"}), "t9_blog", env={}))
    assert gone["parts"]["stage_params"] == cf.UNAVAILABLE and gone["parts"]["routes"] == full["parts"]["routes"]
    a1 = asyncio.run(cf.collect(_FakeConn(), "a1", env={}))
    assert a1["parts"]["brand_config"] is not None and a1["parts"]["l0_lexicon"] is None


# ── 2. the env-switch guard ────────────────────────────────────────────────────────────────────────────────────────

WRITER_MODULE_DIRS = ("services/acp_content_writing", "services/content_generation", "services/writing_memory",
                      "shared/llm_client")


def _env_constants():
    out = {}
    for d in WRITER_MODULE_DIRS:
        for path in (ROOT / d).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id.endswith("_ENV") and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    out[node.value.value] = str(path.relative_to(ROOT))
    return out


def test_every_env_switch_of_a_writer_module_is_classified():
    known = set(cf.GATEWAY_FLAGS) | set(cf.MEMORY_FLAGS) | set(cf.NOT_OUTPUT_FLAGS)
    for names in cf.MODE_FLAGS.values():
        known |= set(names)
    found = _env_constants()
    assert "T9_READER_CHECKLIST_MODE" in found and "LLM_STAGE_PARAMS" in found      # the scan sees them
    unclassified = {k: v for k, v in found.items() if k not in known}
    assert unclassified == {}, ("classify each new env switch in services/writing_memory/config_fp.py "
                                f"(MODE_FLAGS / GATEWAY_FLAGS if it changes an output, else NOT_OUTPUT_FLAGS): "
                                f"{unclassified}")


# ── 3. the R1 freeze ───────────────────────────────────────────────────────────────────────────────────────────────

def test_mining_is_frozen_at_zero_dollars_until_an_r1_record():
    kw = {"runs_this_month": 0, "auto_runs_this_month": 0, "new_pass": 50, "open_waves": 0, "auto_enabled": True}
    why = mining.decide_run("manual", r1_writers=0, **kw)
    assert why and "R1" in why
    assert mining.decide_run("auto", r1_writers=0, **kw) == why          # before every other reason
    assert mining.decide_run("manual", r1_writers=1, **kw) is None
    assert mining.decide_run("manual", r1_writers=None, **kw) is None    # the gate is off


def test_r1_problem_and_the_break_glass_switch(monkeypatch):
    monkeypatch.delenv(r1_gate.R1_GATE_ENV, raising=False)
    assert r1_gate.problem("t9_blog", {})[0] == "r1_missing"
    assert r1_gate.problem("t9_blog", None)[0] == "r1_missing"           # an unreadable table fails closed
    assert r1_gate.problem("t9_blog", {"t9_blog": {"run_id": "x"}}) is None
    assert r1_gate.summary({"t9_blog": {}})["frozen"] == ["a1", "t2", "t9_social"]
    monkeypatch.setenv(r1_gate.R1_GATE_ENV, "off")
    assert r1_gate.problem("t9_blog", {}) is None and r1_gate.summary(None)["frozen"] == []


class _R1Conn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def transaction(self):
        return _Tx()

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return self.rows if "run_kind = 'r1'" in sql else []

    async def fetchval(self, sql, *args):
        return None


@pytest.mark.asyncio
async def test_a_release_with_content_waits_for_r1_and_an_empty_one_does_not(monkeypatch):
    monkeypatch.delenv(r1_gate.R1_GATE_ENV, raising=False)
    monkeypatch.setattr(rel, "evidence_problem", lambda *a, **k: None)
    monkeypatch.setattr(rel, "runs_of", AsyncMock(return_value={}))
    monkeypatch.setattr(rel, "_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr(rel, "_closed_month", AsyncMock(return_value=None))
    from services.writing_memory import exemplars
    monkeypatch.setattr(exemplars, "never_in_release", AsyncMock(return_value=[]))
    content = {"release_id": "r", "writer": "t9_social", "content_class": "rules"}
    p = await rel.check(_R1Conn([]), content, first_activation=True)
    assert p and p[0] == "r1_missing" and "t9_social" in p[1]
    assert await rel.check(_R1Conn([]), {**content, "content_class": "empty"}, first_activation=True) is None
    assert await rel.check(_R1Conn([]), content, first_activation=False) is None     # a rollback is never frozen
    rec = [{"writer": "t9_social", "run_id": "run-1", "finished_at": None}]
    assert await rel.check(_R1Conn(rec), content, first_activation=True) is None


@pytest.mark.asyncio
async def test_an_l0_activation_waits_for_r1(monkeypatch):
    monkeypatch.delenv(r1_gate.R1_GATE_ENV, raising=False)
    from api.routers import admin_writing_memory as router
    lex = {"kind": "lexicon", "writer": "t9_social"}
    assert (await router._l0_r1_problem(_R1Conn([]), lex))[0] == "r1_missing"
    assert await router._l0_r1_problem(_R1Conn([]), {**lex, "kind": "writer_instruction"}) is None
    rec = [{"writer": "t9_social", "run_id": "run-1", "finished_at": None}]
    assert await router._l0_r1_problem(_R1Conn(rec), lex) is None


@pytest.mark.asyncio
async def test_run_mine_drafts_only_for_writers_with_r1(monkeypatch):
    monkeypatch.delenv(r1_gate.R1_GATE_ENV, raising=False)
    sigs = [{"signal_id": f"s{i}", "writer": "t9_social", "scope": "platform", "tenant_id": None,
             "source_id": f"p{i}", "actor_type": "admin", "actor_id": f"rev{i % 2}", "gate": None,
             "reason_code": "ai_phrasing", "before_text": "x", "after_text": "y", "channel": "linkedin",
             "created_at": None, "action": "sentence_rewrite", "fact_status": "pass"} for i in range(6)]
    monkeypatch.setattr(mining.rules_mod, "expire_stale", AsyncMock(return_value=0))
    monkeypatch.setattr(mining.rules_mod, "open_cluster_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(mining.rules_mod, "lint_rejected_signal_sets", AsyncMock(return_value={}))
    monkeypatch.setattr(mining, "a1_piece_count", AsyncMock(return_value=0))
    monkeypatch.setattr(mining, "_corpora", AsyncMock(return_value=({}, {}, [])))
    monkeypatch.setattr(mining, "build_clusters", lambda s, **k: [mining.Cluster(
        writer="t9_social", scope="platform", tenant_id=None, label="reason:ai_phrasing",
        basis="platform_reviewers", signals=sigs)])
    month = SimpleNamespace(check=lambda e: None, charge=lambda u: None, summary=lambda: {})
    import services.writing_memory.budget as budget
    import shared.cost_guard as cg
    monkeypatch.setattr(budget, "load_month_budget", AsyncMock(return_value=month))
    monkeypatch.setattr(cg, "load_run_budget", AsyncMock(return_value=month))
    import services.writing_memory.drafter as drafter
    draft = AsyncMock()
    monkeypatch.setattr(drafter, "draft_rule", draft)

    class _Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            pool = self

            class _A:
                async def __aenter__(self):
                    return pool.conn

                async def __aexit__(self, *a):
                    return False
            return _A()

    out = await mining.run_mine(_Pool(_R1Conn([])), None, trigger="manual")
    draft.assert_not_awaited()
    assert out["skipped_no_r1"] == 1 and out["calls"] == 0 and out["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_the_preference_drafter_is_frozen_too(monkeypatch):
    monkeypatch.delenv(r1_gate.R1_GATE_ENV, raising=False)
    from services.writing_memory import preference
    monkeypatch.setattr(preference, "enabled", lambda: True)

    class _Pool:
        def acquire(self):
            class _A:
                async def __aenter__(self):
                    return _R1Conn([])

                async def __aexit__(self, *a):
                    return False
            return _A()

    out = await preference.run_preference(_Pool(), None)
    assert out["ran"] is False and out["state"] == "r1_missing" and out["calls"] == 0


# ── 4. L0 lexicon warn-only ────────────────────────────────────────────────────────────────────────────────────────

SEED = "A short walk to the Velmora ridge with a picnic stop."
LEARNED_PACK = {"forbidden_words": ["glimmerland", "quietly bramblewise"],
                "learned_lexicon": [{"rule_id": "r-1", "scope": "platform", "phrase": "quietly bramblewise",
                                     "replacement": None, "added": True}]}
TEXT = "The Velmora ridge walk is quietly bramblewise and never glimmerland."


def test_a_learned_phrase_warns_and_the_tenant_word_still_blocks(monkeypatch):
    monkeypatch.delenv(l0.L0_ENFORCE_ENV, raising=False)
    assert qg.gating_forbidden(LEARNED_PACK) == ["glimmerland"]
    lex = qg.gate_lexicon(TEXT, SEED, LEARNED_PACK, include_stock=False)
    assert not lex["passed"] and all("uses 'quietly bramblewise'" not in v for v in lex["violations"])
    assert any("uses 'glimmerland'" in v for v in lex["violations"])
    warn = qg.gate_signature_words(TEXT, SEED, LEARNED_PACK)[1]
    assert warn["blocking"] is False and any("quietly bramblewise" in v for v in warn["violations"])
    clean = "The Velmora ridge walk is quietly bramblewise."
    assert qg.gate_lexicon(clean, SEED, LEARNED_PACK, include_stock=False)["passed"]


def test_the_enforce_switch_restores_the_blocking_finding(monkeypatch):
    monkeypatch.setenv(l0.L0_ENFORCE_ENV, "on")
    clean = "The Velmora ridge walk is quietly bramblewise."
    lex = qg.gate_lexicon(clean, SEED, LEARNED_PACK, include_stock=False)
    assert not lex["passed"] and any("quietly bramblewise" in v for v in lex["violations"])
    assert not any("learned" in v for v in qg.gate_signature_words(clean, SEED, LEARNED_PACK)[1]["violations"])


def test_a_learned_phrase_the_tenant_also_lists_keeps_blocking(monkeypatch):
    monkeypatch.delenv(l0.L0_ENFORCE_ENV, raising=False)
    pack = {**LEARNED_PACK, "learned_lexicon": [{**LEARNED_PACK["learned_lexicon"][0], "added": False}]}
    assert qg.gating_forbidden(pack) == ["glimmerland", "quietly bramblewise"]


def test_a_learned_platform_truth_word_still_blocks_on_its_own_layer(monkeypatch):
    monkeypatch.delenv(l0.L0_ENFORCE_ENV, raising=False)
    from services.acp_shared.signature_vocabulary import PLATFORM_TRUTH
    word = sorted(PLATFORM_TRUTH)[0]
    pack = {"forbidden_words": [word], "learned_lexicon": [{"rule_id": "r-2", "phrase": word, "added": True}]}
    text = f"The Velmora ridge walk, {word}."
    truth = qg.gate_platform_truth(text, SEED, qg.gating_forbidden(pack))
    assert not truth["passed"]
    assert not qg.learned_hits(text, SEED, pack)               # judged once, by the platform-truth layer


# ── 5. the answered writer model on a T2 version ───────────────────────────────────────────────────────────────────

def test_t2_generation_meta_records_the_answer():
    ans = insertion.writer_answer({"model_used": "model-a", "fallback_used": True})
    assert ans == {"answered_model": "model-a", "fallback_used": True}
    meta = insertion.generation_meta(None, prompt_version="abcd1234", answer=ans)
    assert meta == {"writer_answer": ans, "writer_prompt_version": "abcd1234"}
    assert insertion.writer_answer({"model_used": ""}) is None
    assert insertion.generation_meta(None) is None                           # unchanged without anything


# ── 6. the worker's hourly reverify tick ───────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_worker_tick_scans_and_never_fails_the_loop(monkeypatch):
    from shared.jobs import worker as worker_mod
    calls = []

    async def scan(pool, *, actor):
        calls.append(actor)
        raise RuntimeError("scan down")

    monkeypatch.setattr(rel, "reverify_scan_safe", scan)
    w = worker_mod.Worker(pool=object())
    assert w.reverify_every_seconds == worker_mod.REVERIFY_EVERY_S == 3600.0
    await w._reverify_scan()                                             # the error is logged, not raised
    assert calls == ["system:worker_tick"]


def test_the_worker_names_itself_the_writer_service():
    src = (ROOT / "shared/jobs/worker.py").read_text(encoding="utf-8")
    start = src.index("async def _main")
    assert src.index("config_fp.set_service(config_fp.WRITER_SERVICE)", start) < src.index(
        'reverify_scan_safe(pool, actor="system:worker_start")', start)
