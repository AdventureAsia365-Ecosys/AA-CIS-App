"""AA-1049 W3 (root ADR 0020, T2 train) — the T2 linear orchestrator (opt-in), the D8 policy hold, the T2 dry store
and the offline T2 replay.

1. **Parity with main.** With ``T2_ORCHESTRATOR`` unset, ``graph`` or any unknown value, the full-chain harness
   (tests/unit/fixtures/aa1049_t2_harness.py: the real ``_rewrite_and_save``, LangGraph, T3 and save SQL, scripted
   model answers) gives a trace equal to the golden recorded on CIS main BEFORE the orchestrator existed: every model
   request (stage + sha of the exact prompt), every SQL statement with its args, the job diag. Regenerate the golden
   ONLY on a commit where main's T2 behaviour changes on purpose (``python -m tests.unit.fixtures.aa1049_t2_harness
   <out>`` on that commit), never to make this test pass.
2. **Opt-in ``linear``.** Every scripted path (clean, sentence repair, field regen, budget out, judge error, writer
   fallback, policy hold, job retry) reaches final_contract and the store step; writer calls <= 2 including a
   simulated job retry; the status is never more permissive than main's rule; bad repair outputs are reverted.
3. **D8 (elephant riding).** Already on main (S271 W0): the missing tests (both modes, the stored text verbatim, the
   contract verdict).
4. **T2 dry store + replay.** production_input / DryStore, and the declared-diff replay over a synthetic version set.

Invented tours and places only. No DB, no LLM, no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit.fixtures import aa1049_t2_harness as h

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "aa1049_t2_parity_golden.json").read_text(encoding="utf-8"))
LINEAR = {"T2_ORCHESTRATOR": "linear"}


def _save(trace: dict) -> list:
    [save] = [args for sql, args in trace["sql"] if "SET rewritten_content" in sql]
    return save


def _reviews(trace: dict) -> list[list[str]]:
    return [[d["check_id"] for d in args[4]] for sql, args in trace["sql"] if "review_queue" in sql]


def _stages(trace: dict) -> list[str]:
    return [c[0] for c in trace["llm"]]


# ── 1. parity with main ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "graph"])
def test_the_default_path_equals_main_byte_for_byte(value):
    env = {} if value is None else {"T2_ORCHESTRATOR": value}
    got = h.run_all(env=env)
    assert set(got) == set(GOLDEN)
    for name in GOLDEN:
        assert got[name] == GOLDEN[name], f"scenario {name} differs from main"


@pytest.mark.parametrize("value", ["", "GRAPH ", "on", "true", "1", "v2", "linear2", "lin ear"])
def test_an_unknown_value_falls_back_to_main(value):
    for name in ("clean", "grounding_unrepaired", "missing_summary"):
        assert h.run(name, env={"T2_ORCHESTRATOR": value}) == GOLDEN[name]


def test_linear_is_ignored_when_the_interim_profile_is_off():
    """T2_INTERIM=off (break glass) restores the judge-gated v1 path; the orchestrator does not run then."""
    off = {"T2_INTERIM": "off"}
    for name in ("clean", "missing_summary"):
        assert h.run(name, env={**off, **LINEAR}) == h.run(name, env=off)


def test_mode_reads_only_the_exact_value(monkeypatch):
    from services.content_generation import t2_orchestrator as o
    for raw, want in [(None, o.GRAPH), ("graph", o.GRAPH), ("linear", o.LINEAR), (" LINEAR ", o.LINEAR),
                      ("linear;", o.GRAPH), ("yes", o.GRAPH)]:
        if raw is None:
            monkeypatch.delenv(o.ORCHESTRATOR_ENV, raising=False)
        else:
            monkeypatch.setenv(o.ORCHESTRATOR_ENV, raw)
        assert o.mode() == want, raw


def test_the_switch_is_a_classified_mode_flag():
    from services.content_generation import t2_orchestrator as o
    from services.writing_memory import config_fp
    assert o.ORCHESTRATOR_ENV in config_fp.MODE_FLAGS["t2"]
    assert config_fp.mode_flags("t2", {o.ORCHESTRATOR_ENV: " Linear "}) == {o.ORCHESTRATOR_ENV: "linear"}


def test_the_first_writer_prompt_is_mains():
    """No prompt text change: the linear first write sends main's exact first writer request."""
    for name in ("clean", "grounding_repair", "missing_summary", "day_dropped"):
        lin = h.run(name, env=LINEAR)
        assert lin["llm"][0] == GOLDEN[name]["llm"][0]
        assert lin["unexpected"] == []


# ── 2. the opt-in linear orchestrator ───────────────────────────────────────────────────────────

_MAIN_STATUS = {name: (_save(t)[1] if name != "elephant" else "needs_review") for name, t in GOLDEN.items()}


@pytest.fixture(scope="module")
def linear_traces():
    return h.run_all(env=LINEAR)


def test_every_linear_path_reaches_the_contract_and_the_store(linear_traces):
    for name, t in linear_traces.items():
        save = _save(t)
        jb = save[1] if name == "elephant" else save[8]
        assert jb["final_contract"]["exit"] in ("done", "policy_hold"), name
        assert jb["final_contract"]["sha_match"] is True, name
        if name != "elephant":
            assert save[7]["t2_orchestrator"]["version"] == "t2-linear-1"


def test_linear_writer_calls_stay_within_the_budget(linear_traces):
    for name, t in linear_traces.items():
        stages = _stages(t)
        assert stages.count("t2_generate") <= 2, name
        assert stages.count("t2_judge") <= 1, name
        assert stages.count("t2_flag_fix") <= 3, name
        assert "t2_itinerary_nudge" not in stages and "t2_brand_audit" not in stages, name


def test_linear_is_never_more_permissive_than_main(linear_traces):
    for name, t in linear_traces.items():
        status = "needs_review" if name == "elephant" else _save(t)[1]
        if status == "ai_generated":
            assert _MAIN_STATUS[name] == "ai_generated", name


def test_linear_scripted_paths(linear_traces):
    t = linear_traces
    assert _stages(t["clean"]) == ["t2_generate", "t2_judge"] and _save(t["clean"])[1] == "ai_generated"
    # sentence repair clears the number: one t2_flag_fix call, a pass
    assert _stages(t["grounding_repair"]) == ["t2_generate", "t2_flag_fix", "t2_judge"]
    assert _save(t["grounding_repair"])[1] == "ai_generated" and _save(t["grounding_repair"])[3] == "passed"
    # the repair wrote another invented figure: refused, the finding stays -> needs_review (main: ai_generated)
    assert _save(t["grounding_unrepaired"])[1] == "needs_review"
    assert _reviews(t["grounding_unrepaired"]) == [["grounding:novel_numeric_claim"]]
    assert "9000" not in json.dumps(_save(t["grounding_unrepaired"])[0])
    # a dropped day: one field regen (the scripted writer drops it again) -> needs_review with the slot code
    assert _stages(t["day_dropped"]).count("t2_generate") == 2 and _save(t["day_dropped"])[1] == "needs_review"
    assert _reviews(t["day_dropped"]) == [["structural:T2_DAY_COUNT_VS_MASTER"]]
    # budget out: two writes, then stop
    assert _stages(t["missing_summary"]).count("t2_generate") == 2
    assert _save(t["missing_summary"])[7]["t2_orchestrator"]["budget"]["writer"] == 2
    # an unparseable first reply costs the second write (main's graph retries it too)
    assert _stages(t["writer_retry"]) == ["t2_generate", "t2_generate", "t2_judge"]
    # policy hold: 0 model calls, same as main
    assert t["elephant"]["llm"] == [] and t["elephant"] == GOLDEN["elephant"]


def test_the_voice_judge_is_report_only():
    """A judge error (or a low verdict) never changes the stored status: validate + the findings decide."""
    def boom(st):
        st.enter_context(patch("services.content_generation.judge_node.score_brand_fit",
                               side_effect=RuntimeError("judge down")))
    err = h.run("clean", env=LINEAR, extra=boom)
    assert _save(err)[1] == "ai_generated"
    assert "t2_judge" not in _stages(err)


def test_a_held_fallback_writer_is_needs_review_in_linear_only():
    def held(st):
        st.enter_context(patch("services.quality.route_guard.t2_mark", return_value={"held": True, "entries": []}))
    assert _save(h.run("clean", env=LINEAR, extra=held))[1] == "needs_review"
    assert _save(h.run("clean", extra=held))[1] == "ai_generated"      # main: marked only


def test_a_job_retry_resumes_from_the_stored_best_candidate():
    """The save fails after the write (the job would retry); the retry reads the checkpoint the first run left on the
    pending version and makes NO writer call: writer calls over both attempts = 1."""
    class FailingSave(h.Conn):
        async def execute(self, sql, *args):
            if "SET rewritten_content" in sql:
                raise RuntimeError("db gone")
            return await super().execute(sql, *args)

    first = FailingSave()
    with pytest.raises(RuntimeError, match="db gone"):
        h.run("clean", env=LINEAR, conn=first)
    [cp] = [args for sql, args in first.executed if "SET generation_meta" in sql]
    meta = cp[0]

    class Resume(h.Conn):
        async def fetchval(self, sql, *args):
            self.executed.append((sql, args))
            return meta if "generation_meta" in sql else None

    second = h.run("clean", env=LINEAR, conn=Resume())
    assert "t2_generate" not in _stages(second)
    rec = _save(second)[7]["t2_orchestrator"]
    assert rec["resumed"] is True and rec["budget"]["writer"] == 1 and _save(second)[1] == "ai_generated"


def test_a_resumed_run_with_the_writer_budget_spent_never_writes_again():
    from services.content_generation import t2_orchestrator as o
    b = o.VersionBudget.from_dict({"writer": 2, "repair": 1, "usd": 0.01})
    assert not b.can("writer") and b.can("repair") and b.can("voice")
    assert o.VersionBudget.from_dict({"writer": "x"}).writer == 0
    capped = o.VersionBudget(usd=0.25)
    assert not capped.can("repair") and not capped.can("voice")


def test_linear_status_is_a_subset_of_mains_rule():
    from services.content_generation import t2_orchestrator as o
    from services.jobs.t2_rewrite_job import _t2_status
    for score in (0.0, 5.0, 6.99, 7.0, 9.9):
        for codes in ([], ["T3_grounding"], ["MISSING_FIELD"], ["T2_DAY_COUNT_VS_MASTER"]):
            for held in (False, True):
                lin = o.linear_status(score, codes, held)
                main = _t2_status(score, [c for c in codes if c not in ("T3_grounding", "T2_DAY_COUNT_VS_MASTER")])
                if lin == "ai_generated":
                    assert main == "ai_generated"


def test_select_best_prefers_fewer_findings_then_score():
    from services.content_generation import t2_orchestrator as o
    a = {"codes": ["T3_grounding"], "score": 9.9, "attempt": 0}
    b = {"codes": [], "score": 7.5, "attempt": 1}
    c = {"codes": ["T3_grounding", "MISSING_FIELD"], "score": 9.9, "attempt": 2}
    assert max([a, b, c], key=o.best_key) is b
    assert max([a, c], key=o.best_key) is a      # a field regen that adds a finding never replaces a better draft


def test_a_repair_that_writes_a_forbidden_word_back_is_refused():
    """Recorded bad repair output: the brand-forbidden word comes back in the replacement -> the sentence keeps its
    original text (grounding.apply_replacements), the finding stays."""
    sc = dict(h.SCENARIOS["grounding_unrepaired"])
    sc["script"] = {**sc["script"], "t2_flag_fix": [{"1": "Spend three breathtaking days in Velmora."}]}
    with patch.dict(h.SCENARIOS, {"bad_word": sc}):
        t = h.run("bad_word", env=LINEAR)
    assert "breathtaking" not in json.dumps(_save(t)[0]).lower()
    assert _save(t)[1] == "needs_review"


def test_the_slot_reader_reads_both_day_forms():
    from services.content_generation import t2_orchestrator as o
    assert o.day_slots(h._ITIN) == [1, 2, 3]
    assert o.day_slots("Day 1: A\nwalk 3 days\n\nDay 2: B\nx\n\nDay 3: C\ny") == [1, 2, 3]
    assert o.day_slots("Day 1: A\n\nDay 3: C") == [1, 3]
    assert o.day_slots("Days 1-2: A\n\nDay 3: C") == [1, 2, 3]


# ── route table (t2 rows) ───────────────────────────────────────────────────────────────────────


def test_every_t2_enforce_code_has_a_route_row():
    from services.acp_content_writing import route_table as rt
    from services.content_generation.graph import _HARD_BLOCK_CODES
    for code in set(_HARD_BLOCK_CODES) | {"T3_grounding", "T2_DAY_COUNT_VS_MASTER"}:
        assert code in rt.T2_TABLE, code
    assert rt.t2_schema_errors() == []
    assert rt.t2_route("SOMETHING_NEW").action == rt.T2_DEFAULT == "rewrite"     # fail closed: still enforced
    assert rt.t2_route("T3_grounding").action == "repair_sentence"
    assert rt.t2_route("META_TOO_SHORT").section == "seo_fields"
    # the T9 table is untouched by the t2 section
    assert rt.schema_errors() == [] and rt.MECHANICS_VERSION == "t9-route-table-v1"


def test_the_t2_schema_lint_refuses_a_broken_section():
    from services.acp_content_writing import route_table as rt
    bad = {"t2": {"version": "t2-route-table-v1", "default": "rewrite",
                  "rows": [{"code": "X", "action": "repair_sentence"}, {"code": "X", "action": "cut"},
                           {"code": "Y", "action": "regenerate_section", "section": "faq"}]}}
    errs = " ".join(rt.t2_schema_errors(bad))
    assert "exactly one row per code" in errs and "parse is required" in errs and "action must be" in errs
    assert "section must be" in errs
    assert rt.t2_schema_errors({"t2": {"version": "v0", "default": "pass", "rows": []}})


# ── 3. D8 (elephant riding) — already on main; the missing tests ─────────────────────────────────


def test_d8_policy_hold_is_the_same_in_both_modes_and_makes_no_lookup():
    """The policy route runs before either path: no SEO lookup, no writing-memory read, no model call; exactly the
    version UPDATE and the review row (both modes, byte-equal)."""
    lin = h.run("elephant", env=LINEAR)
    assert lin == GOLDEN["elephant"]
    assert [s.split(" ")[0] for s, _ in lin["sql"]] == ["UPDATE", "INSERT"]
    assert lin["llm"] == [] and lin["diag"]["policy_hold"]["llm_calls"] == 0


def test_d8_keeps_the_master_prose_verbatim_even_with_a_tenant_forbidden_word():
    """Master text verbatim (D8): a tenant's forbidden word in the Master prose is NOT stripped or rewritten (no LLM
    edit); only the SEO title / meta are composed by code."""
    from services.jobs import t2_rewrite_job as job
    row = {**h.MASTER_ROW, "aa_summary": "A breathtaking ride on an elephant at Corran Ridge."}
    content, _ = job.policy_hold_content(job.source_tour_dict(row), {"forbidden_words": ["breathtaking"]})
    assert content["summary"] == row["aa_summary"]
    assert content["highlights"] == ["Velmora old market", "Corran Ridge chapel walk", "Farm kitchen lunch"]


def test_d8_a_negated_ride_is_not_a_hold():
    from services.jobs import t2_rewrite_job as job
    row = {**h.MASTER_ROW, "aa_summary": "We never ride elephants; watch them bathe at the river."}
    assert job.policy_hold_hits(row, job.source_tour_dict(row)) == []


@pytest.mark.xfail(strict=True, reason="final_contract.check_t2 reads days with output_days (dash form only), so the "
                   "dash-fixed Master text ('Day N:') of a D8 version records T2_DAY_COUNT_VS_MASTER and the verdict "
                   "needs_review instead of policy_hold (report mode only; follow-up in AA-1049-l15-t2.md)")
def test_d8_contract_verdict_is_policy_hold():
    jb = _save(GOLDEN["elephant"])[1]
    assert jb["final_contract"]["verdict"] == "policy_hold"


# ── 4. T2 dry store + offline replay ────────────────────────────────────────────────────────────


def test_the_dry_store_records_the_linear_save_and_touches_no_table():
    from services.content_generation import t2_store
    store = t2_store.DryStore()

    def dry(st):
        st.enter_context(t2_store.use_store(store))
    t = h.run("grounding_unrepaired", env=LINEAR, extra=dry)
    assert not [s for s, _ in t["sql"] if "SET rewritten_content" in s or "review_queue" in s]
    [row] = store.rows
    assert row["status"] == "needs_review" and row["final_contract"]["sha_match"] is True
    assert row["stored_sha"] == row["final_contract"]["stored_sha"]
    assert store.reviews and t2_store.active() is None


def test_production_input_uses_the_production_builders():
    from services.content_generation import t2_store
    from services.jobs import t2_rewrite_job as job
    inp = t2_store.production_input(h.MASTER_ROW)
    assert inp["tour_dict"] == job.source_tour_dict(h.MASTER_ROW)
    assert inp["policy_hold_hits"] == [] and inp["fs_sha"] and len(inp["source_sha"]) == 16
    ele = t2_store.production_input({**h.MASTER_ROW, "src_itineraries": "Day 2: elephant riding"})
    assert ele["policy_hold_hits"][0]["where"] == "source"


def _stored(d: dict) -> dict:
    d = dict(d)
    d["itineraries"] = "\n\n".join(f"Day {x['day']}: {x['title']}\n{x['body']}" for x in d["itineraries"])
    d.pop("seo_keywords_used", None)
    return {**d, "status": "done"}


def _versions() -> list[dict]:
    jb = {"brand_fit": 8.0, "cross_brand_distinct": 8.0, "mission_present": True, "feedback": "ok"}
    out = []
    for name, over in (("clean", {}), ("number", {"summary": h._SUMMARY_412}),
                       ("days", {"itineraries": h.writer_draft()["itineraries"][:2]}), ("missing", {"summary": ""})):
        out.append({"id": f"v-{name}", "tenant_id": h.TENANT, "status": "ai_generated", "quality_score": 9.0,
                    "qa_status": "passed", "qa_repair_count": 0, "rewritten_content": _stored(h.writer_draft(**over)),
                    "judge_breakdown": jb if name != "missing" else None, "pt": dict(h.MASTER_ROW),
                    "brand_rules": h.BRAND_RULES})
    out.append({"id": "v-elephant", "tenant_id": h.TENANT, "rewritten_content": {}, "brand_rules": h.BRAND_RULES,
                "pt": {**h.MASTER_ROW, "src_itineraries": "Day 2: elephant riding"}})
    return out


@pytest.fixture(scope="module")
def replay_runs():
    from scripts.quality import replay_aa1049_t2 as rp
    vs = _versions()
    return rp, rp.run(vs), rp.run(vs), rp.run(vs, {"T2_ORCHESTRATOR": "linear"})


def test_replay_defaults_show_zero_diffs(replay_runs):
    rp, base, branch, _ = replay_runs
    d = rp.diff(base, branch, opt_in=False)
    assert d["declared"] == {} and d["undeclared"] == [] and d["missing"] == []
    assert d["misses"] == {"t2_flag_fix": 1, "t2_judge": 3, "t2_brand_audit": 1}   # counted, never a pass
    # the same defaults with --opt-in still allow no declared id (the switch is off)
    assert rp.diff(base, branch, opt_in=True)["declared"] == {}


def test_replay_classifies_opt_in_diffs_only_under_opt_in(replay_runs):
    rp, base, _, linear = replay_runs
    d = rp.diff(base, linear, opt_in=True)
    assert d["undeclared"] == [], d["undeclared"]
    assert d["declared"]["D-t2-status"] == 2          # the number and the dropped day: ai_generated -> needs_review
    assert set(d["declared"]) <= set(rp.DECLARED)
    no = rp.diff(base, linear, opt_in=False)
    assert no["declared"] == {} and no["undeclared"]
    assert all(u.get("why") == "opt-in diff without --opt-in" for u in no["undeclared"])


def test_replay_never_declares_a_more_permissive_status(replay_runs):
    rp, base, _, linear = replay_runs
    bad = json.loads(json.dumps(linear))
    bad["versions"]["v-missing"]["status"] = "ai_generated"
    bad["versions"]["v-missing"]["qa_status"] = "passed"
    d = rp.diff(base, bad, opt_in=True)
    assert {(u["id"], u["field"]) for u in d["undeclared"]} == {("v-missing", "status"), ("v-missing", "qa_status")}


def test_replay_cli_exit_codes(tmp_path):
    from scripts.quality import replay_aa1049_t2 as rp
    vf = tmp_path / "v.json"
    vf.write_text(json.dumps({"schema": rp.VERSIONS_SCHEMA, "versions": _versions()[:2]}, default=str))
    base, br, rep = tmp_path / "b.json", tmp_path / "r.json", tmp_path / "r.md"
    assert rp.main(["--versions", str(vf), "--out", str(base)]) == 0
    assert rp.main(["--versions", str(vf), "--out", str(br), "--base", str(base), "--report", str(rep)]) == 0
    assert "undeclared: 0" in rep.read_text()
    assert rp.main(["--versions", str(vf), "--out", str(br), "--base", str(base), "--linear"]) == 1
    assert rp.main(["--versions", str(vf), "--out", str(br), "--base", str(base), "--linear", "--opt-in"]) == 0


def test_replay_runs_without_the_seam_on_a_main_checkout(monkeypatch):
    """The script's input builder falls back to the job's builders when t2_store is not importable (main)."""
    import builtins

    from scripts.quality import replay_aa1049_t2 as rp
    real = builtins.__import__

    def no_seam(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "services.content_generation" and fromlist and "t2_store" in fromlist:
            raise ImportError("no seam on main")
        return real(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", no_seam)
    inp = rp.production_input(h.MASTER_ROW)
    assert inp["fs_sha"] is None and inp["policy_hold"] is False and len(inp["source_sha"]) == 16


def test_no_model_call_escapes_the_harness():
    for name, t in GOLDEN.items():
        assert t["unexpected"] == [], name
