"""AA-1000 — T10 tier 2: the LLM adjudicator (services/acp_content_writing/seed_align/adjudicate.py), root plan
docs/audits/2026-10-29-S257-aa789-design/PLAN.md ("Tier 2").

Fakes only: no live model call, no DB (the llm_call_log writers are replaced), no spaCy (tier 1's run record is a
stand-in). Pinned here:
- the trigger filter: only sentences a class trigger fires on AND tier 1 left alone, only after a clean tier-1 run;
- quote verification: a VIOLATION whose piece quote is not in the sentence, or whose seed quote is not in the seed
  excerpt and the seed, is dropped;
- fail open: a model error, an unusable reply or the deadline changes nothing;
- the never-hold invariant (a `sa:t2.` finding can never hold, whatever the env), mode off = zero calls, shadow =
  record only, repair = at most one finding (one sentence) per attempt, a $0 replay calls nothing;
- the calibration runner's $5 hard cost cap.
All copy is synthetic (an invented lake and village).
"""
from __future__ import annotations

import ast
import importlib.util
import json
import time
from pathlib import Path

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair
from services.acp_content_writing import trip_facts as tf
from services.acp_content_writing.hygiene import HygieneContext
from services.acp_content_writing.seed_align import adjudicate as adj
from services.acp_content_writing.seed_align import checks as sa
from services.acp_content_writing.seed_align import modes
from services.quality import registry as qr
from shared.llm_client.models import LLMResponse

ROOT = Path(__file__).resolve().parents[2]

SEED = """Lake Vashra — walk the northern shore
Day 2 of the trip
Source itinerary: Walk the northern shore of Lake Vashra and stop at the fishing village of Toren.
Naming: Use the name Vashra (the older spelling Vashira reads oddly to English speakers).
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- Lake Vashra is a glacial lake fed by three rivers.
- The fishing village of Toren keeps wooden smokehouses along the water."""

CTA = "Ask us about the northern shore walk."
NAMING_S = "Vashra is a name that sounds a little odd to English ears."
REPEAT_S = "It is a glacial lake that three rivers keep fed."
HYPE_S = "Few places can match the calm of its smokehouse mornings."
FAQ_S = "The village sits on the northern shore."
PIECE = f"""## The northern shore

Lake Vashra is a glacial lake fed by three rivers. {NAMING_S} {REPEAT_S}

Toren keeps wooden smokehouses along the water. {HYPE_S}

## FAQ

**Q: What makes Toren worth a stop?**
A: {FAQ_S} It keeps wooden smokehouses along the water.

**Q: How long is the walk?**
A: The source gives no distance. Ask the team.

{CTA}"""

ALL_ON = {adj.NAMING: "shadow", adj.HYPE: "shadow"}


def t1_ok(candidates=(), ran=ALL_ON) -> modes.SeedAlignRun:
    return modes.SeedAlignRun(record={"status": "ok", "modes": dict(ran),
                                      "candidates": [{"cls": c, "sentence": s} for c, s in candidates]})


def _sentences(trigs) -> dict:
    return {t.cls: t.sentence for t in trigs}


# ---------------------------------------------------------------- fakes

class FakeClient:
    """Answers every item with `verdict_for(item_number, prompt)`; records each request."""

    def __init__(self, verdict_for=None, raises=None, content=None, sleep=0.0, cost=0.002):
        self.requests = []
        self.verdict_for = verdict_for
        self.raises = raises
        self.content = content
        self.sleep = sleep
        self.cost = cost

    def generate(self, request):
        self.requests.append(request)
        if self.sleep:
            time.sleep(self.sleep)
        if self.raises:
            raise self.raises
        if self.content is not None:
            body = self.content
        else:
            n = request.user_prompt.count("   SENTENCE: ")
            body = json.dumps({"items": [self.verdict_for(k, request.user_prompt) for k in range(1, n + 1)]})
        return LLMResponse(content=body, model_used="gpt-6-luna", provider="bedrock", input_tokens=900,
                           output_tokens=200, cost_usd=self.cost, satellite_account="acc3")


def _item_sentences(prompt: str) -> list[str]:
    return [ln.split("   SENTENCE: ", 1)[1] for ln in prompt.splitlines() if ln.startswith("   SENTENCE: ")]


def _item_seed(prompt: str, k: int) -> list[str]:
    blocks = prompt.split("\n")
    out, n = [], 0
    for ln in blocks:
        if ln.startswith(f"{k + 1}. CHECK:"):
            break
        if ln.startswith(f"{k}. CHECK:"):
            n = k
            continue
        if n == k and ln.startswith("   - "):
            out.append(ln[5:])
    return out


def honest_violation(k: int, prompt: str) -> dict:
    """A VIOLATION whose quotes are both real: the sentence's first 4 words and a seed line's first 4 words."""
    sent = _item_sentences(prompt)[k - 1]
    seed_line = _item_seed(prompt, k)[0]
    return {"n": k, "verdict": "VIOLATION", "piece_quote": " ".join(sent.split()[:4]),
            "seed_quote": " ".join(seed_line.split()[:4]), "action": "repair"}


@pytest.fixture
def env(monkeypatch):
    for c in adj.CLASSES:
        monkeypatch.delenv(f"{adj.MODE_ENV}_{c.upper()}", raising=False)
    monkeypatch.delenv(adj.MODE_ENV, raising=False)
    for c in sa.ALL_CLASSES:
        monkeypatch.delenv(f"{modes.MODE_ENV}_{c.upper()}", raising=False)
    monkeypatch.delenv(modes.MODE_ENV, raising=False)
    return monkeypatch


@pytest.fixture
def log_rows(monkeypatch):
    rows = {"ok": [], "unusable": []}
    monkeypatch.setattr(adj, "record_call_sync", lambda **kw: rows["ok"].append(kw))
    monkeypatch.setattr(adj, "record_unusable_sync", lambda resp, **kw: rows["unusable"].append(kw))
    return rows


# ---------------------------------------------------------------- modes

def test_every_class_off_by_default(env):
    assert adj.active_modes() == {}
    assert adj.run(PIECE, tier1=t1_ok()) is None


@pytest.mark.parametrize("value", ["hold", "HOLD", "on", "1", "warn", ""])
def test_anything_but_off_shadow_repair_reads_as_off(env, value):
    env.setenv(adj.MODE_ENV, value)
    assert adj.active_modes() == {}


def test_global_env_and_per_class_override(env):
    env.setenv(adj.MODE_ENV, "shadow")
    env.setenv(f"{adj.MODE_ENV}_SCOPELESS_SUPERLATIVE", "repair")
    env.setenv(f"{adj.MODE_ENV}_FAQ_NON_ANSWER", "off")
    assert adj.active_modes() == {adj.NAMING: "shadow", adj.REPEAT: "shadow", adj.HYPE: "repair"}


def test_classes_sit_under_existing_gates_and_tier1_env_does_not_switch_tier2(env):
    assert set(adj.CLASS_GATE.values()) <= {qr.F13_LEAK, qr.F13_REPEAT, qr.F18_RARITY}
    env.setenv(modes.MODE_ENV, "repair")
    assert adj.active_modes() == {}


def test_layer_imports_no_gate_builder():
    tree = ast.parse((Path(adj.__file__)).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and "quality_gates" in m for m in imported), imported


def test_stage_route_is_a_non_anthropic_judge_with_no_shadow():
    from shared.llm_client.role_config import SAFE_DEFAULTS

    cfg = SAFE_DEFAULTS[adj.STAGE]
    assert (cfg.role, cfg.provider, cfg.model_id, cfg.account_route) == ("judge", "openai", "gpt-6-luna", "acc3")
    assert cfg.fallback_model_ids[-1] == "gpt-6-luna-openai" and cfg.shadow_model_id is None
    sql = (ROOT / "api" / "migrations" / "309_t10_adjudicate_stage.sql").read_text(encoding="utf-8")
    assert "('t10_adjudicate', 'judge', 'openai', 'gpt-6-luna', 'acc3', true" in sql
    assert "ARRAY['gpt-5.6-luna', 'gpt-6-luna-openai']" in sql
    # an INSERT into an existing table is not lint-expand: no directive, a person applies it through DB Migrate
    assert not any(ln.strip().lower().startswith("-- migrate:") for ln in sql.splitlines())


# ---------------------------------------------------------------- trigger filter

def test_triggers_fire_on_the_four_residual_forms():
    trig = adj.find_triggers(PIECE, SEED, t1_ok().record)
    assert _sentences(trig) == {adj.NAMING: NAMING_S, adj.REPEAT: REPEAT_S, adj.HYPE: HYPE_S, adj.FAQ: FAQ_S}
    assert [t.start for t in trig] == sorted(t.start for t in trig)
    by = {t.cls: t for t in trig}
    assert by[adj.REPEAT].context == "Lake Vashra is a glacial lake fed by three rivers."
    assert by[adj.REPEAT].seed_lines == ("Lake Vashra is a glacial lake fed by three rivers.",)
    assert by[adj.FAQ].context == "What makes Toren worth a stop?"
    assert by[adj.NAMING].seed_lines[0].startswith("Naming:")
    # writer instructions never reach the model
    assert not any("writer instruction" in ln for t in trig for ln in t.seed_lines)


def test_a_sentence_tier1_already_flagged_is_not_sent():
    trig = adj.find_triggers(PIECE, SEED, t1_ok(candidates=[(sa.ORPHAN_OPENER, REPEAT_S), (sa.NAMING_NOTE_LEAK,
                                                                                            NAMING_S)]).record)
    assert set(_sentences(trig)) == {adj.HYPE, adj.FAQ}


@pytest.mark.parametrize("record", [None, {"status": "nlp_unavailable", "candidates": []},
                                    {"status": "error", "candidates": []}, {"status": "no_seed", "candidates": []}])
def test_no_clean_tier1_run_means_no_trigger(record):
    assert adj.find_triggers(PIECE, SEED, record) == []


def test_a_class_tier1_has_a_check_for_needs_tier1_to_have_run_it():
    trig = adj.find_triggers(PIECE, SEED, t1_ok(ran={}).record)
    assert set(_sentences(trig)) == {adj.REPEAT, adj.FAQ}  # tier 1 has no check for these two


def test_clean_copy_and_seed_backed_forms_do_not_trigger():
    clean = ("## The northern shore\n\nLake Vashra is a glacial lake fed by three rivers. The walk follows the "
             "northern shore to Toren. Toren keeps wooden smokehouses along the water.\n\n## FAQ\n\n"
             "**Q: Is Toren on the walk?**\nA: Yes, the walk stops there.\n\n"
             "**Q: How far is the walk?**\nA: The source gives no distance. Ask the team.\n\n" + CTA)
    assert adj.find_triggers(clean, SEED, t1_ok().record) == []
    # the seed makes the same claim: the hype trigger stands down
    seed = SEED + "\n- Few places can match the calm of its smokehouse mornings."
    assert adj.HYPE not in _sentences(adj.find_triggers(PIECE, seed, t1_ok().record))
    # no Naming note in the seed: nothing to leak
    no_naming = "\n".join(ln for ln in SEED.splitlines() if not ln.startswith("Naming"))
    assert adj.NAMING not in _sentences(adj.find_triggers(PIECE, no_naming, t1_ok().record))


def test_the_only_sentence_of_an_faq_answer_is_adjudicated_but_never_goes_live(env, log_rows):
    lone = PIECE.replace(f"A: {FAQ_S} It keeps wooden smokehouses along the water.", f"A: {FAQ_S}")
    trig = {t.cls: t for t in adj.find_triggers(lone, SEED, t1_ok().record)}
    assert trig[adj.FAQ].sentence == FAQ_S and trig[adj.FAQ].cuttable is False
    assert all(t.cuttable for c, t in trig.items() if c != adj.FAQ)
    env.setenv(adj.MODE_ENV, "shadow")
    env.setenv(f"{adj.MODE_ENV}_FAQ_NON_ANSWER", "repair")  # only the lone FAQ sentence is in repair mode
    out = adj.run(lone, tier1=t1_ok(), trip_ctx=tf.TripFactContext(seed=SEED), client=FakeClient(honest_violation))
    faq = next(f for f in out.record["findings"] if f["cls"] == adj.FAQ)
    assert faq["mode"] == "repair" and faq["live_blocked"] == "only_sentence_of_faq_answer"
    assert out.live == {} and "live" not in out.record


def test_mode_off_classes_are_not_triggered():
    trig = adj.find_triggers(PIECE, SEED, t1_ok().record, classes=[adj.HYPE])
    assert _sentences(trig) == {adj.HYPE: HYPE_S}


# ---------------------------------------------------------------- quote verification

def _one(cls=adj.HYPE):
    return next(t for t in adj.find_triggers(PIECE, SEED, t1_ok().record) if t.cls == cls)


@pytest.mark.parametrize("piece_quote,seed_quote,kept,why", [
    ("Few places can match", "The fishing village of Toren", True, None),
    ("few  places CAN match", "the fishing village of toren", True, None),       # case / spacing normalised
    ("Few places rival the", "The fishing village of Toren", False, "piece_quote_not_in_sentence"),
    ("Few places can match", "Toren is the calmest village", False, "seed_quote_not_in_seed"),
    ("Few places", "The fishing village of Toren", False, "piece_quote_not_in_sentence"),  # under 3 words
    ("Few places can match", "", False, "seed_quote_not_in_seed"),
])
def test_a_violation_counts_only_when_both_quotes_check_out(log_rows, piece_quote, seed_quote, kept, why):
    item = _one()
    client = FakeClient(lambda k, p: {"n": k, "verdict": "VIOLATION", "piece_quote": piece_quote,
                                      "seed_quote": seed_quote, "action": "cut"})
    res = adj.adjudicate([item], SEED, client=client)
    assert bool(res.findings) is kept
    assert res.dropped == ([] if kept else [{"cls": adj.HYPE, "why": why}])
    assert len(log_rows["ok"]) == 1 and log_rows["ok"][0]["stage"] == "t10_adjudicate"
    assert log_rows["ok"][0]["role"] == "judge" and log_rows["ok"][0]["quality_signal"]["json_parsed"] is True


def test_a_seed_quote_from_outside_the_excerpt_is_dropped(log_rows):
    item = _one(adj.REPEAT)  # its excerpt is the one anchor line
    client = FakeClient(lambda k, p: {"n": k, "verdict": "VIOLATION", "piece_quote": "a glacial lake that",
                                      "seed_quote": "The fishing village of Toren", "action": "repair"})
    assert adj.adjudicate([item], SEED, client=client).dropped == [{"cls": adj.REPEAT, "why": "seed_quote_not_in_seed"}]


def test_ok_verdicts_make_no_finding(log_rows):
    client = FakeClient(lambda k, p: {"n": k, "verdict": "OK", "piece_quote": "", "seed_quote": "", "action": "repair"})
    res = adj.adjudicate(adj.find_triggers(PIECE, SEED, t1_ok().record), SEED, client=client)
    assert res.findings == [] and res.dropped == []


def test_worst_case_model_row_on_clean_sentences_never_becomes_a_finding(log_rows):
    """Plan §3.5 for tier 2: a model that says VIOLATION on everything and quotes a seed line it was not shown (here:
    the sentence itself as the seed quote) is stopped by the anchor check."""
    client = FakeClient(lambda k, p: {"n": k, "verdict": "VIOLATION",
                                      "piece_quote": " ".join(_item_sentences(p)[k - 1].split()[:4]),
                                      "seed_quote": " ".join(_item_sentences(p)[k - 1].split()[:4]),
                                      "action": "cut"})
    res = adj.adjudicate(adj.find_triggers(PIECE, SEED, t1_ok().record), SEED, client=client)
    assert res.findings == [] and len(res.dropped) == 4


# ---------------------------------------------------------------- fail open

@pytest.mark.parametrize("client,status", [
    (FakeClient(raises=RuntimeError("route exhausted")), "error"),
    (FakeClient(content="I think sentence 2 is a problem."), "error"),
    (FakeClient(content='{"items": [{"n": 1, "verdict": "MAYBE"}]}'), "error"),
    (FakeClient(content='{"items": [{"n": 1, "verdict": "OK", "piece_quote": "", "seed_quote": "", '
                        '"action": "cut"}]}'), "error"),  # a missing item is an unusable reply
])
def test_a_model_error_or_unusable_reply_changes_nothing(env, log_rows, client, status):
    env.setenv(adj.MODE_ENV, "repair")
    out = adj.run(PIECE, tier1=t1_ok(), trip_ctx=tf.TripFactContext(seed=SEED), client=client)
    assert out.record["status"] == status and out.live == {} and out.record["findings"] == []
    if isinstance(client.raises, Exception):
        assert log_rows["unusable"] == [] and log_rows["ok"] == []  # no reply, nothing billed to log
    else:
        assert len(log_rows["unusable"]) == 1 and log_rows["ok"] == []  # billed reply: one billed_unusable row


def test_the_deadline_changes_nothing(env, log_rows, monkeypatch):
    env.setenv(adj.MODE_ENV, "repair")
    monkeypatch.setattr(adj, "TIMEOUT_S", 0.05)
    out = adj.run(PIECE, tier1=t1_ok(), trip_ctx=tf.TripFactContext(seed=SEED),
                  client=FakeClient(raises=RuntimeError("late"), sleep=0.5))  # finishes after the test, logs nothing
    assert out.record["status"] == "timeout" and out.live == {}


def test_tier1_not_ok_means_no_call(env, log_rows):
    env.setenv(adj.MODE_ENV, "repair")
    client = FakeClient(honest_violation)
    for tier1 in (None, modes.SeedAlignRun(record={"status": "nlp_unavailable", "candidates": []})):
        out = adj.run(PIECE, tier1=tier1, trip_ctx=tf.TripFactContext(seed=SEED), client=client)
        assert out.record["status"] == "tier1_not_ok" and out.live == {}
    assert client.requests == []


# ---------------------------------------------------------------- shadow / repair (module level)

def test_shadow_records_findings_and_never_goes_live(env, log_rows):
    env.setenv(adj.MODE_ENV, "shadow")
    client = FakeClient(honest_violation)
    out = adj.run(PIECE, tier1=t1_ok(), trip_ctx=tf.TripFactContext(seed=SEED), client=client)
    assert out.live == {} and "live" not in out.record
    assert out.record["status"] == "ok" and out.record["sent"] == 4 and len(client.requests) == 1
    assert {f["cls"] for f in out.record["findings"]} == set(adj.CLASSES)
    assert all(f["mode"] == "shadow" for f in out.record["findings"])


def test_repair_puts_at_most_one_sentence_live_the_first_in_text_order(env, log_rows):
    env.setenv(adj.MODE_ENV, "repair")
    out = adj.run(PIECE, tier1=t1_ok(), trip_ctx=tf.TripFactContext(seed=SEED), client=FakeClient(honest_violation))
    live = [v for vs in out.live.values() for v in vs]
    assert len(live) == 1 and len(out.record["findings"]) == 4
    assert out.live == {qr.F13_LEAK: [adj.finding_text(adj.NAMING, NAMING_S)]}  # NAMING_S is the first in the text
    # the gate's sentence repair reads the finding as "<problem>: '<sentence>'"
    m = sentence_repair._BRAND_QUOTE_RE.search(live[0])
    assert m.group(2) == NAMING_S and m.group(1).startswith("sa:t2.naming_note_leak: ")


def test_repair_only_for_classes_in_repair_mode(env, log_rows):
    env.setenv(adj.MODE_ENV, "shadow")
    env.setenv(f"{adj.MODE_ENV}_SCOPELESS_SUPERLATIVE", "repair")
    out = adj.run(PIECE, tier1=t1_ok(), trip_ctx=tf.TripFactContext(seed=SEED), client=FakeClient(honest_violation))
    assert out.live == {qr.F18_RARITY: [adj.finding_text(adj.HYPE, HYPE_S)]}


def test_at_most_max_items_are_sent_in_one_call(env, log_rows):
    env.setenv(adj.MODE_ENV, "shadow")
    many = PIECE.replace(HYPE_S, " ".join(f"Few places can match the calm of bay {i}." for i in range(9)))
    client = FakeClient(honest_violation)
    out = adj.run(many, tier1=t1_ok(), trip_ctx=tf.TripFactContext(seed=SEED), client=client)
    assert len(client.requests) == 1 and out.record["sent"] == adj.MAX_ITEMS < out.record["triggers"]


# ---------------------------------------------------------------- never holds

@pytest.mark.parametrize("cls", adj.CLASSES)
def test_a_tier2_finding_never_holds_whatever_the_env(env, monkeypatch, cls):
    env.setenv(modes.MODE_ENV, "hold")
    env.setenv(adj.MODE_ENV, "hold")
    monkeypatch.setattr(modes, "HOLD_PROMOTED", frozenset(sa.ALL_CLASSES))  # even with every tier-1 class promoted
    g = {"gate": adj.CLASS_GATE[cls], "passed": False, "blocking": True, "repairable": True,
         "violations": [adj.finding_text(cls, "Some sentence.")]}
    assert not modes.finding_may_hold(g["violations"][0])
    assert qg.never_holds(g) and qg.hold_failure([g]) is None


# ---------------------------------------------------------------- run_quality_gates

def _gates(text: str = PIECE, f9_results=None):
    hctx = HygieneContext(seed=SEED, brand_name="Pretend Brand", cta=CTA, audience="adults")
    tctx = tf.TripFactContext(seed=SEED, audience="adults", cta=CTA, channel="blog")
    return qg.run_quality_gates(content_text=text, atom_text=SEED, cta=CTA, brand_rubric_text="", channel="blog",
                                hygiene_ctx=hctx, trip_ctx=tctx, f9_results=f9_results)


@pytest.fixture
def gate_env(env, log_rows, monkeypatch):
    """Tier 1 stands in as a clean run with no candidate (no spaCy here); no F9 judge call; a counting model."""
    monkeypatch.setattr(qg.seed_align, "run", lambda *a, **k: t1_ok())
    monkeypatch.setattr(qg, "gate_brand_voice", lambda *a, **k: [])
    client = FakeClient(honest_violation)
    monkeypatch.setattr(adj, "_default_client", lambda: client)
    return env, client


def _t2(ledger) -> list[str]:
    return [v for g in qg.expand_ledger(ledger) for v in g.get("violations") or [] if str(v).startswith("sa:t2.")]


def test_off_makes_zero_calls_and_leaves_the_outcome_as_tier1_left_it(gate_env):
    _env, client = gate_env
    out = _gates()
    assert client.requests == [] and "tier2" not in out["seed_align"] and _t2(out["gate_ledger"]) == []


def test_shadow_records_under_seed_align_and_never_changes_the_ledger(gate_env):
    env, client = gate_env
    off = _gates()
    env.setenv(adj.MODE_ENV, "shadow")
    shadow = _gates()
    assert len(client.requests) == 1
    assert shadow["gate_ledger"] == off["gate_ledger"] and shadow["flags"] == off["flags"]
    assert shadow["passed"] == off["passed"] and shadow["first_failure"] == off["first_failure"]
    t2 = shadow["seed_align"]["tier2"]
    assert t2["status"] == "ok" and t2["findings"] and all(f["mode"] == "shadow" for f in t2["findings"])
    assert shadow["seed_align"]["status"] == "ok"  # tier 1's own record is kept beside it


def test_repair_adds_one_finding_and_never_changes_what_holds(gate_env):
    env, _client = gate_env
    off = _gates()
    env.setenv(adj.MODE_ENV, "repair")
    rep = _gates()
    found = _t2(rep["gate_ledger"])
    assert found == [adj.finding_text(adj.NAMING, NAMING_S)]
    f13 = next(g for g in qg.expand_ledger(rep["gate_ledger"]) if g["gate"] == qr.F13_LEAK)
    assert not f13["passed"] and f13["blocking"] and f13["repairable"]
    # what holds (if anything) is what held without tier 2
    held_off, held_rep = qg.hold_failure(off["gate_ledger"]), qg.hold_failure(rep["gate_ledger"])
    assert (held_off or {}).get("gate") == (held_rep or {}).get("gate")


def test_model_error_inside_the_gates_leaves_the_ledger_unchanged(gate_env, monkeypatch):
    env, _client = gate_env
    off = _gates()
    env.setenv(adj.MODE_ENV, "repair")
    monkeypatch.setattr(adj, "_default_client", lambda: FakeClient(raises=RuntimeError("boom")))
    out = _gates()
    assert out["gate_ledger"] == off["gate_ledger"] and out["seed_align"]["tier2"]["status"] == "error"


def test_a_replay_calls_nothing(gate_env):
    env, client = gate_env
    env.setenv(adj.MODE_ENV, "repair")
    out = _gates(f9_results=[])
    assert client.requests == [] and out["seed_align"]["tier2"]["status"] == "replay" and _t2(out["gate_ledger"]) == []


# ---------------------------------------------------------------- calibration runner: the $5 hard cap

def _runner():
    spec = importlib.util.spec_from_file_location("aa1000_cal", ROOT / "scripts" / "aa1000_adjudicate_calibrate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _eval_items(n: int) -> list[dict]:
    return [{"id": f"x-{i}", "text": PIECE, "seed_excerpt": SEED} for i in range(n)]


def test_budget_refuses_a_cap_over_five_dollars():
    cal = _runner()
    assert cal.HARD_CAP_USD == 5.0
    with pytest.raises(ValueError):
        cal.Budget(5.01)
    with pytest.raises(ValueError):
        cal.Budget(0)
    b = cal.Budget(5.0)
    assert b.allows(5.0) and not b.allows(5.000001)
    b.charge(0.0, 0.5)   # a reply without a cost still counts its estimate
    b.charge(0.7, 0.5)   # the larger of the two
    assert b.spent == pytest.approx(1.2) and b.calls == 2


def test_cli_refuses_a_cap_over_five_dollars(tmp_path):
    cal = _runner()
    ev = tmp_path / "e.jsonl"
    ev.write_text("")
    with pytest.raises(SystemExit):
        cal.main(["run", str(ev), str(tmp_path / "o.jsonl"), "--cap", "6"])


def test_runner_stops_before_the_call_that_could_pass_the_cap(log_rows, monkeypatch, tmp_path):
    cal = _runner()
    monkeypatch.setattr(cal, "worst_case_cost", lambda items, model: 1.0)
    client = FakeClient(honest_violation, cost=0.4)
    out = tmp_path / "o.r1.jsonl"
    summary = cal.calibrate(_eval_items(5), [str(out)], client=client, cap_usd=2.5,
                            t1=lambda text, seed: t1_ok().record)
    assert len(client.requests) == 2 and summary["calls"] == 2
    assert summary["spent_usd"] == pytest.approx(2.0) and summary["stopped"] == "cost_cap"
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert [r["status"] for r in rows] == ["ok", "ok", "not_run_cost_cap", "not_run_cost_cap", "not_run_cost_cap"]
    assert rows[0]["flagged"] and rows[0]["class"] == adj.NAMING and not rows[2]["flagged"]
    assert all(r["stage"] == "adhoc_aa1000" for r in log_rows["ok"])
    assert all(req.model_tier == "gpt-6-luna" and req.stage == "adhoc_aa1000" for req in client.requests)


def test_runner_counts_a_failed_call_against_the_cap_and_reports_test_retest(log_rows, monkeypatch, tmp_path):
    cal = _runner()
    monkeypatch.setattr(cal, "worst_case_cost", lambda items, model: 0.1)
    summary = cal.calibrate(_eval_items(2), [str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")],
                            client=FakeClient(raises=RuntimeError("down")), cap_usd=5.0,
                            t1=lambda text, seed: t1_ok().record)
    assert summary["calls"] == 4 and summary["spent_usd"] == pytest.approx(0.4)
    assert summary["statuses"] == {"error": 4} and summary["test_retest"] == 1.0


def test_runner_refuses_an_unpriced_model(tmp_path):
    cal = _runner()
    with pytest.raises(ValueError):
        cal.calibrate([], [str(tmp_path / "o.jsonl")], client=FakeClient(), model="sonnet")
