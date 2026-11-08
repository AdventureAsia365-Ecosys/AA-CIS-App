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
4. **T2 dry store + replay.** production_input / DryStore, and the declared-diff replay over a synthetic version set,
   diffed against the replay of the same set RECORDED ON CIS MAIN (aa1049_t2_replay_main_base.json).

Invented tours and places only. No DB, no LLM, no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
    """Status, stored score and qa_status: never looser than main's for the same scenario."""
    for name, t in linear_traces.items():
        status = "needs_review" if name == "elephant" else _save(t)[1]
        if status == "ai_generated":
            assert _MAIN_STATUS[name] == "ai_generated", name
        if name == "elephant":
            assert t == GOLDEN[name]
            continue
        lin, main = _save(t), _save(GOLDEN[name])
        assert lin[2] is None or (main[2] is not None and lin[2] <= main[2]), (name, lin[2], main[2])
        if main[3] == "escalated":
            assert lin[3] == "escalated", name


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


def test_the_voice_judge_runs_off_the_event_loop():
    import threading
    seen = []

    def spy(st):
        from services.content_generation import judge_node as jn
        real = jn.judge_node

        def wrapped(state):
            seen.append(threading.current_thread() is threading.main_thread())
            return real(state)
        st.enter_context(patch.object(jn, "judge_node", wrapped))
    t = h.run("clean", env=LINEAR, extra=spy)
    assert seen == [False] and _save(t)[7]["t2_orchestrator"]["budget"]["voice"] == 1


def test_the_meta_regenerate_counts_its_real_attempts_and_cost():
    """T3's meta regenerate may bill up to META_ATTEMPTS t2_flag_fix calls (flag_fix's loop) without reporting them:
    the budget counts the calls really billed and their cost, and never lets it run past the repair cap."""
    from services.content_generation import t2_orchestrator as o
    assert o.META_ATTEMPTS == 3
    short = "Velmora walks."                                  # never lands in band: every attempt is billed
    sc = {"script": {"t2_generate": [h.writer_draft(seo_meta="Too short.")], "t2_judge": [h.judge_reply()],
                     "t2_flag_fix": [{"seo_meta": short}]}}
    seen = []

    def spy(st):
        real = o.plan

        def p(codes):
            out = real(codes)
            seen.append(out)
            return out
        st.enter_context(patch.object(o, "plan", p))
        st.enter_context(patch.object(o, "normalize", lambda gen, bundle: 0))   # keep the short meta short
    with patch.dict(h.SCENARIOS, {"meta_short": sc}):
        t = h.run("meta_short", env=LINEAR, extra=spy)
    assert any("regenerate_section" in p for p in seen), seen
    rec = _save(t)[7]["t2_orchestrator"]
    flag_fix = _stages(t).count("t2_flag_fix")
    assert rec["meta_attempts"] == 3 and flag_fix >= 3
    assert rec["budget"]["repair"] == flag_fix <= 3          # the meta's 3 calls used the whole repair budget
    # writer + 3 meta attempts (+ the judge) were charged
    assert rec["budget"]["usd"] == pytest.approx(0.001 * (rec["budget"]["writer"] + flag_fix + rec["budget"]["voice"]))


def test_a_meta_regenerate_that_lands_first_time_refunds_the_unused_attempts():
    import asyncio

    from services.content_generation import t2_orchestrator as o
    from shared.llm_client.call_log import record_call_sync

    class TP:
        @staticmethod
        def _t3_meta_regenerate(*_a):
            record_call_sync(stage="t2_flag_fix", role="writer", model="m", cost_usd=0.002)
            return "new meta", None
    from types import SimpleNamespace
    run = o.Run(bundle=SimpleNamespace(tour_dict={}, brand_rules={}, tenant_id="t", source_texts=[]),
                budget=o.VersionBudget())
    with patch("shared.llm_client.call_log.record_call", AsyncMock(return_value=None)):
        out = asyncio.run(o._meta_regenerate(run, TP, {}))
    assert out == ("new meta", None)
    assert run.budget.repair == 1 and run.meta_attempts == 1 and run.budget.usd == pytest.approx(0.002)


def test_the_contract_compares_the_stored_text_with_the_gated_text():
    """The contract's gated sha is the selected draft exactly as verify checked it: a code step that edits the text
    AFTER verify is a POST_GATE_EDIT finding (the clean scenario has none)."""
    from services.content_generation import t2_orchestrator as o

    def tamper(st):
        real = o.brand_report

        def edit_after_gate(chosen, bundle):
            chosen["generated"]["summary"] += " Edited after the gate."
            return real(chosen, bundle)
        st.enter_context(patch.object(o, "brand_report", edit_after_gate))
    clean = _save(h.run("clean", env=LINEAR))[8]["final_contract"]
    assert "POST_GATE_EDIT" not in json.dumps(clean) and clean["sha_match"] is True
    edited = _save(h.run("clean", env=LINEAR, extra=tamper))[8]["final_contract"]
    assert "POST_GATE_EDIT" in json.dumps(edited)


def test_the_store_step_stamps_the_sha_of_the_text_it_writes():
    """sha_match compares the text the contract checked with the text save_version writes, not a text with itself."""
    import asyncio

    from services.content_generation import t2_orchestrator as o
    from services.jobs import t2_rewrite_job as job
    td = job.source_tour_dict(h.MASTER_ROW)
    checked = o.stored_text(h.writer_draft(), td)
    qa = {"passed": True, "attempts": 0, "structural_issues": [], "grounding_issues": []}
    contract = job.final_contract_record(checked, td, h.MASTER_ROW, exit="done", actual="ai_generated", qa=qa,
                                         score=9.0, gated=checked)
    assert contract["sha_match"] is True
    for text, want in ((checked, True), ({**checked, "summary": "Changed after the contract."}, False)):
        conn = h.Conn()
        asyncio.run(o.save_version(h.pool(conn), h.VERSION, rewritten=text, status="ai_generated", quality_score=9.0,
                                   qa=qa, generation_meta=None, judge_breakdown={"final_contract": contract},
                                   final_contract=contract))
        [args] = [a for sql, a in conn.executed if "SET rewritten_content" in sql]
        assert json.loads(args[8])["final_contract"]["sha_match"] is want


def test_config_fp_records_the_switch_only_when_it_changes_the_path():
    from services.writing_memory import config_fp
    for env in ({"T2_ORCHESTRATOR": "graph"}, {"T2_ORCHESTRATOR": "on"}, {"T2_ORCHESTRATOR": " linear2"},
                {"T2_ORCHESTRATOR": "linear", "T2_INTERIM": "off"}):
        assert "T2_ORCHESTRATOR" not in config_fp.mode_flags("t2", env), env
    assert config_fp.mode_flags("t2", {"T2_ORCHESTRATOR": "linear"}) == {"T2_ORCHESTRATOR": "linear"}
    assert config_fp.mode_flags("t2", {"T2_ORCHESTRATOR": "graph", "T2_FACTS": "on"}) == {"T2_FACTS": "on"}


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
    meta = _last_checkpoint(first)
    # the voice call was counted before it was sent: the retry pays neither the writer nor the judge again
    assert meta["t2_orchestrator"]["checkpoint"]["budget"]["voice"] == 1

    second = h.run("clean", env=LINEAR, conn=_Resume(meta))
    assert "t2_generate" not in _stages(second) and "t2_judge" not in _stages(second)
    rec = _save(second)[7]["t2_orchestrator"]
    assert rec["resumed"] is True and rec["budget"]["writer"] == 1 and _save(second)[1] == "ai_generated"


def _last_checkpoint(conn) -> dict:
    cps = [args for sql, args in conn.executed if "SET generation_meta" in sql and "rewritten_content" not in sql]
    assert cps, "no checkpoint was persisted"
    return json.loads(cps[-1][0])


class _Resume(h.Conn):
    def __init__(self, meta: dict):
        super().__init__()
        self.meta = json.dumps(meta)

    async def fetchval(self, sql, *args):
        self.executed.append((sql, args))
        return self.meta if "generation_meta" in sql else None


def test_a_billed_call_is_persisted_before_it_is_sent():
    """Every billed call is counted on the pending version BEFORE the request: the first checkpoint precedes the
    first writer request (no candidate yet, so it carries the budget only)."""
    class Spy(h.Conn):
        pass
    conn = Spy()
    calls_at_first_cp = []

    def spy(st):
        from services.content_generation import t2_orchestrator as o
        real = o.save_checkpoint

        async def wrapped(pool, vid, budget, best):
            if not calls_at_first_cp:
                calls_at_first_cp.append((budget.writer, best))
            return await real(pool, vid, budget, best)
        st.enter_context(patch.object(o, "save_checkpoint", wrapped))
    t = h.run("clean", env=LINEAR, conn=conn, extra=spy)
    assert calls_at_first_cp == [(1, None)]
    assert _stages(t) == ["t2_generate", "t2_judge"]


def test_two_empty_writes_then_a_job_retry_make_no_new_writer_call():
    """The first write returns nothing usable twice: the run raises (the job retries). The budget was persisted before
    each call, so the retry starts with writer=2 and makes NO writer call: at most 2 t2_generate over both attempts."""
    sc = {"script": {"t2_generate": ["not json at all"], "t2_judge": [h.judge_reply()]}}
    from services.content_generation import t2_orchestrator as o
    writes: list[int] = []

    def count(st):
        real = o.write

        def spy(*a, **k):
            writes.append(1)
            return real(*a, **k)
        st.enter_context(patch.object(o, "write", spy))
    first = h.Conn()
    with patch.dict(h.SCENARIOS, {"empty_twice": sc}):
        with pytest.raises(RuntimeError, match="no usable draft"):
            h.run("empty_twice", env=LINEAR, conn=first, extra=count)
        assert len(writes) == 2
        meta = _last_checkpoint(first)
        cp = meta["t2_orchestrator"]["checkpoint"]
        assert cp["budget"]["writer"] == 2 and "best" not in cp
        retry = _Resume(meta)
        with pytest.raises(RuntimeError, match="no usable draft"):
            h.run("empty_twice", env=LINEAR, conn=retry, extra=count)
    assert len(writes) == 2                       # the retry made no writer call
    assert not [s for s, _ in retry.executed if "SET rewritten_content" in s]


def test_the_writer_call_is_checkpointed_before_verify():
    """A crash in verify (Jev, validate, T3 checks) right after the writer returned: the checkpoint already holds the
    spent call AND its cost, so the retry neither writes again nor under-counts the spend."""
    from services.content_generation import t2_orchestrator as o

    def crash(st):
        st.enter_context(patch.object(o, "verify", side_effect=RuntimeError("verify crashed")))
    conn = h.Conn()
    with pytest.raises(RuntimeError, match="verify crashed"):
        h.run("clean", env=LINEAR, conn=conn, extra=crash)
    budget = _last_checkpoint(conn)["t2_orchestrator"]["checkpoint"]["budget"]
    assert budget["writer"] == 1 and budget["usd"] == pytest.approx(0.001)


def test_a_sentence_repair_that_changes_nothing_still_counts_its_cost():
    """grounding.repair bills its t2_flag_fix call and still returns no usable field: _t3_sentence_repair returns
    None. The stored budget counts that call's cost: usd equals every billed call (t2_generate, t2_flag_fix,
    t2_judge), not only the ones whose result was kept."""
    t = h.run("grounding_unrepaired", env=LINEAR)
    assert _stages(t) == ["t2_generate", "t2_flag_fix", "t2_judge"]
    budget = _save(t)[7]["t2_orchestrator"]["budget"]
    assert budget["repair"] == 1
    assert budget["usd"] == pytest.approx(0.001 * len(_stages(t)))


def test_the_sentence_repair_cost_is_checkpointed_before_verify():
    """verify crashes on the repaired candidate (the second one): the last checkpoint already holds the repair call
    AND its cost, so the job retry starts with both counted."""
    from services.content_generation import t2_orchestrator as o
    real = o.verify
    seen = {"n": 0}

    async def crash_second(*a, **k):
        seen["n"] += 1
        if seen["n"] == 2:
            raise RuntimeError("verify crashed on the repaired candidate")
        return await real(*a, **k)

    def crash(st):
        st.enter_context(patch.object(o, "verify", crash_second))
    conn = h.Conn()
    with pytest.raises(RuntimeError, match="repaired candidate"):
        h.run("grounding_repair", env=LINEAR, conn=conn, extra=crash)
    budget = _last_checkpoint(conn)["t2_orchestrator"]["checkpoint"]["budget"]
    assert budget["writer"] == 1 and budget["repair"] == 1
    assert budget["usd"] == pytest.approx(0.002)


def test_a_budget_only_checkpoint_loads_and_resumes_without_a_best():
    import asyncio

    from services.content_generation import t2_orchestrator as o
    conn = _Resume({"t2_orchestrator": {"version": o.RECORD_VERSION,
                                        "checkpoint": {"budget": {"writer": 1, "repair": 0, "voice": 0}}}})
    cp = asyncio.run(o.load_checkpoint(h.pool(conn), "00000000-0000-0000-0000-000000000001"))
    assert cp == {"budget": {"writer": 1, "repair": 0, "voice": 0}}
    bad = _Resume({"t2_orchestrator": {"checkpoint": {"best": "x"}}})
    assert asyncio.run(o.load_checkpoint(h.pool(bad), "00000000-0000-0000-0000-000000000001")) is None


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


def test_d8_contract_verdict_is_policy_hold():
    """S273 (lane l17): the contract reads the dash-fixed Master text's days with day_parse.written_day_numbers, so a
    D8 version records policy_hold, not needs_review for a phantom T2_DAY_COUNT_VS_MASTER (was xfail)."""
    jb = _save(GOLDEN["elephant"])[1]
    assert jb["final_contract"]["verdict"] == "policy_hold"


# ── 4. T2 dry store + offline replay ────────────────────────────────────────────────────────────


def test_the_dry_store_records_the_linear_save_and_touches_no_table():
    from services.content_generation import t2_store
    store = t2_store.DryStore()

    def dry(st):
        st.enter_context(t2_store.use_store(store))
    t = h.run("grounding_unrepaired", env=LINEAR, extra=dry)
    # nothing is written and no checkpoint is read or written on the pool (only the input lookups read it)
    assert not [s for s, _ in t["sql"] if s.split(" ")[0] in ("UPDATE", "INSERT", "DELETE")]
    assert not [s for s, _ in t["sql"] if "generation_meta" in s or "review_queue" in s]
    [row] = store.rows
    assert row["status"] == "needs_review" and row["final_contract"]["sha_match"] is True
    assert row["stored_sha"] == row["final_contract"]["stored_sha"]
    assert store.reviews and t2_store.active() is None
    # the checkpoint went to the dry store
    [cp] = store.checkpoints.values()
    assert cp["budget"]["writer"] == 1 and cp["best"]["via"] in ("initial", "sentence_repair")


def test_a_dry_checkpoint_never_reaches_a_real_pending_version():
    """A dry run against a pool must not leave a checkpoint the next real job would resume from."""
    from services.content_generation import t2_store
    store = t2_store.DryStore()
    real = h.Conn()

    def dry(st):
        st.enter_context(t2_store.use_store(store))
    h.run("clean", env=LINEAR, extra=dry, conn=real)
    assert not [s for s, _ in real.executed if "generation_meta" in s]
    after = h.run("clean", env=LINEAR)
    assert _stages(after)[0] == "t2_generate"        # the next real job writes; nothing to resume from


def test_production_input_uses_the_production_builders():
    from services.content_generation import t2_store
    from services.jobs import t2_rewrite_job as job
    inp = t2_store.production_input(h.MASTER_ROW)
    assert inp["tour_dict"] == job.source_tour_dict(h.MASTER_ROW)
    assert inp["policy_hold_hits"] == [] and inp["fs_sha"] and len(inp["source_sha"]) == 16
    ele = t2_store.production_input({**h.MASTER_ROW, "src_itineraries": "Day 2: elephant riding"})
    assert ele["policy_hold_hits"][0]["where"] == "source"


def _versions() -> list[dict]:
    return h.replay_versions()


MAIN_BASE = json.loads((Path(__file__).parent / "fixtures" / "aa1049_t2_replay_main_base.json")
                       .read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def replay_runs():
    """The branch's replays (defaults and linear) over the synthetic version set, and the replay of the same set
    RECORDED ON CIS MAIN (aa1049_t2_replay_main_base.json: the script run with PYTHONPATH=<a git-archive export of
    origin/main>, which has no orchestrator and no T2 store seam). Re-record it, like the parity golden, only on a
    commit where main's T2 behaviour changes on purpose:
    ``PYTHONPATH=<main export> python <branch>/scripts/quality/replay_aa1049_t2.py --versions <replay_versions() as
    aa1049-t2-versions/1 JSON> --out <base>``."""
    from scripts.quality import replay_aa1049_t2 as rp
    vs = _versions()
    return rp, MAIN_BASE, rp.run(vs), rp.run(vs, {"T2_ORCHESTRATOR": "linear"})


def test_the_main_base_was_recorded_without_the_branch_code():
    assert MAIN_BASE["recorded_on"].startswith("CIS origin/main")
    assert MAIN_BASE["flags"] == {}
    # main has no T2 store seam: the replay's input builder fell back to the job's builders (no FactSheet sha)
    assert all(v["input"]["fs_sha"] is None for v in MAIN_BASE["versions"].values())
    assert set(MAIN_BASE["versions"]) == {v["id"] for v in _versions()}


def test_replay_defaults_equal_main(replay_runs):
    """The branch with every switch at its default against the replay recorded on main: 0 declared, 0 undeclared,
    0 MISS, 0 missing (the misses are the same cassette gaps on both sides, counted, never a pass)."""
    rp, base, branch, _ = replay_runs
    d = rp.diff(base, branch, opt_in=False)
    assert d["declared"] == {} and d["undeclared"] == [] and d["missing"] == [] and d["miss"] == []
    assert rp.exit_code(d) == 0
    assert d["misses"] == d["base_misses"] == {"t2_flag_fix": 1, "t2_judge": 3, "t2_brand_audit": 1}
    assert d["cassette_hits"] == d["base_cassette_hits"] > 0
    # the same defaults with --opt-in still allow no declared id (the switch is off)
    assert rp.diff(base, branch, opt_in=True)["declared"] == {}


def test_replay_classifies_opt_in_diffs_only_under_opt_in(replay_runs):
    rp, base, _, linear = replay_runs
    d = rp.diff(base, linear, opt_in=True)
    assert d["undeclared"] == [], d["undeclared"]
    assert d["declared"]["D-t2-status"] == 1          # the dropped day: ai_generated -> needs_review
    assert set(d["declared"]) <= set(rp.DECLARED)
    # a version whose difference may come from a cassette miss is MISS (re-record), never declared: the number
    # (linear's sentence repair missed) and the missing summary (main's brand-audit fix missed)
    assert {m["id"] for m in d["miss"]} == {"v-number", "v-missing"}
    assert rp.exit_code(d) == 3
    no = rp.diff(base, linear, opt_in=False)
    assert no["declared"] == {} and no["undeclared"]
    assert all(u.get("why") == "opt-in diff without --opt-in" for u in no["undeclared"])


def test_replay_never_declares_a_more_permissive_outcome(replay_runs):
    """A looser status, a score above main's or where main stored none, a QA pass main did not give, a review row
    with fewer findings: never declared, and never hidden in the MISS bucket."""
    rp, base, _, linear = replay_runs
    bad = json.loads(json.dumps(linear))
    bad["versions"]["v-missing"].update(status="ai_generated", qa_status="passed")     # a MISS version
    bad["versions"]["v-number"].update(reviews=[])                                    # a MISS version
    bad["versions"]["v-days"].update(quality_score=99.0)
    assert base["versions"]["v-number"]["reviews"]
    d = rp.diff(base, bad, opt_in=True)
    got = {(u["id"], u["field"]) for u in d["undeclared"]}
    assert {("v-missing", "status"), ("v-missing", "qa_status"), ("v-days", "quality_score"),
            ("v-number", "reviews")} <= got
    assert rp.exit_code(d) == 1


def test_replay_never_declares_a_score_where_main_stored_none():
    from scripts.quality import replay_aa1049_t2 as rp
    assert rp.more_permissive("quality_score", {"quality_score": None}, {"quality_score": 7.5})
    assert not rp.more_permissive("quality_score", {"quality_score": 9.0}, {"quality_score": None})
    assert rp._declared("quality_score", {"quality_score": None}, {"quality_score": 7.5}) is None
    assert rp._declared("qa_status", {"qa_status": "escalated"}, {"qa_status": "passed"}) is None
    assert rp._declared("reviews", {"reviews": ["a"]}, {"reviews": ["a", "b"]}) == "D-t2-review"
    assert rp._declared("reviews", {"reviews": ["a", "b"]}, {"reviews": ["a"]}) is None


def test_a_miss_on_a_report_only_judge_does_not_make_a_miss_row():
    from scripts.quality import replay_aa1049_t2 as rp
    v = {"status": "ai_generated", "misses": {"t2_judge": 1}}
    base = {"flags": {}, "versions": {"x": {**v, "llm_stages": {"t2_judge": 3}}}}
    branch = {"flags": {"T2_ORCHESTRATOR": "linear"}, "versions": {"x": {**v, "llm_stages": {"t2_judge": 1}}}}
    assert rp.diff(base, branch, opt_in=True)["miss"] == []
    off = {"flags": {"T2_INTERIM": "off"}, "versions": base["versions"]}
    assert rp.diff(off, branch, opt_in=True)["miss"]      # the judge gates the status then: a miss row


def test_replay_cli_exit_codes(tmp_path):
    from scripts.quality import replay_aa1049_t2 as rp
    vf = tmp_path / "v.json"
    vf.write_text(json.dumps({"schema": rp.VERSIONS_SCHEMA, "versions": _versions()[:3]}, default=str))
    base, br, rep = tmp_path / "b.json", tmp_path / "r.json", tmp_path / "r.md"
    assert rp.main(["--versions", str(vf), "--out", str(base)]) == 0
    assert rp.main(["--versions", str(vf), "--out", str(br), "--base", str(base), "--report", str(rep)]) == 0
    assert "undeclared: 0" in rep.read_text()
    assert rp.main(["--versions", str(vf), "--out", str(br), "--base", str(base), "--linear"]) == 1
    # with --opt-in the dropped day is declared and the only row left is a MISS (v-number: a sentence-repair cassette
    # miss): exit 3
    assert rp.main(["--versions", str(vf), "--out", str(br), "--base", str(base), "--linear", "--opt-in",
                    "--report", str(rep)]) == 3
    assert "MISS v-number" in rep.read_text()


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
