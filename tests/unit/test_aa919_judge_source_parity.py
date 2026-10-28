"""AA-919 S256 — the R2 judge reads what the AA-729 panel read (source parity), and only R2 calls the judge.

Run cc0f75cf (kappa 0.316): the judge failed 5 of 8 pass-labelled t9_social texts, mostly fact.embellish, while the
panel's verifier found the flagged facts in the FULL writer input (angle reasoning, goal, brand pack). The judge's
SOURCE is now `eval_judge.judge_source_text`: every section of the stored T9 input, each cut to a fixed size, facts
last, bounded by SOURCE_CHARS. `eval_writers.source_text` (the isolation probe's allowed text, used by the live
post-activation check and the shadow) stays as it was. Invented text only (public repo)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.writing_memory import eval_judge as ej
from services.writing_memory import eval_writers as ew
from services.writing_memory import r2

REPO = Path(__file__).resolve().parents[2]

ANGLE = {"name": "Zz invented angle", "formula_fit": "Problem then proof.", "why_it_works": "Zz the angle reasoning.",
         "best_final_style": "Calm; one Zzbrand sentence on how it plans trips.",
         "answers": ["Is there a zz invented retreat?"], "option_id": "zz-opt", "idx": 3, "violations": []}
BRAND = {"brand_name": "Zzbrand", "brand_type": "Invented quiet travel brand",
         "core_idea": "Zz private journeys with expert guiding.", "customer_segment": "Invented couples",
         "customer_mindset": "Zz they want depth.", "voice": ["Calm", "Precise"],
         "style_guide": "Zz use slow sentences.", "good_examples": "Zz an on-brand line.",
         "forbidden_words": ["zzepic"], "system_prompt": "ZZ SHOULD NOT APPEAR"}
GOAL = {"key": "trust_building", "name": "Zz Trust", "description": "Zz build trust in how the brand plans trips.",
        "logic": "Zz proof from the source."}


def _ctx(**over) -> dict:
    ctx = {"channel": "linkedin", "atom_text": "Zz invented seed line.\nZz place background fact.",
           "scope": "SCOPE: day 4 only.", "chosen": ANGLE, "goal": GOAL, "brand_audience": BRAND,
           "trip_places": ["Zz Pass", "Zz Lake"], "cta": "Ask Zzbrand.", "facts_text": "Zz fact block.",
           "channel_style": {"display_name": "LinkedIn", "structure": "Zz hook then one brand sentence.",
                             "channel": "linkedin"},
           "people_also_ask": [{"question": "Zz invented question?"}], "sibling_openers": ["ZZ SIBLING OPENER"]}
    ctx.update(over)
    return ctx


def test_t9_judge_source_holds_the_whole_writer_input_the_panel_read():
    src = ej.judge_source_text("t9_social", {"context": _ctx()})
    for needle in ("Zz place background fact.", "day 4 only.", "Zz the angle reasoning.", "zz invented retreat",
                   "Zz build trust in how the brand plans trips.", "Zz private journeys with expert guiding.",
                   "Invented couples", "Zz use slow sentences.", "Zz an on-brand line.", "Zz Pass; Zz Lake",
                   "Zz hook then one brand sentence.", "Ask Zzbrand.", "Zz invented question?", "Zz fact block."):
        assert needle in src, needle
    # writer-internal or out-of-scope keys stay out (the brand's own system prompt, other pieces' openers, ids)
    assert "ZZ SHOULD NOT APPEAR" not in src and "ZZ SIBLING OPENER" not in src and "zz-opt" not in src
    heads = [ln[:-1] for ln in src.splitlines() if ln.endswith(":") and ln[:-1] in dict(ej.T9_SOURCE_SECTIONS)]
    assert heads == ["SEED", "SCOPE", "TRIP", "ANGLE BRIEF", "GOAL", "BRAND PACK", "CHANNEL", "WRITER ASKS", "FACTS"]


def test_t9_judge_source_reads_the_live_writer_input_shape_too():
    live = {"writer_input": {"channel": "linkedin", "seed": "Zz invented seed.", "scope": "SCOPE: day 2 only.",
                             "angle": ANGLE, "goal": GOAL, "brand_pack": BRAND, "facts": "Zz live fact.",
                             "route_moments": [{"atom_id": "x", "seed": "Zz another moment."}]}}
    src = ej.judge_source_text("t9_social", live)
    assert "Zz the angle reasoning." in src and "Zz private journeys" in src and "Zz live fact." in src
    assert "ROUTE:\nZz another moment." in src


def test_route_moments_the_seed_already_holds_are_not_repeated():
    """A T9 blog's seed is its joined route moments: the old source carried them twice (40k chars for a 19k seed)."""
    ctx = _ctx(atom_text="Zz moment one.\n\nZz moment two.", route_segments=[["a", "Zz moment one."],
                                                                          ["b", "Zz moment two."]])
    src = ej.judge_source_text("t9_blog", {"context": ctx})
    assert src.count("Zz moment one.") == 1 and "ROUTE:" not in src


def test_judge_source_is_bounded_deterministic_and_never_cut_by_the_prompt():
    big = _ctx(atom_text="Zz seed sentence. " * 3000, facts_text="Zz fact sentence.\n" * 5000,
               chosen={**ANGLE, "best_final_style": "Zz style words " * 1000})
    src = ej.judge_source_text("t9_blog", {"context": big})
    assert len(src) <= ej.SOURCE_CHARS
    assert src == ej.judge_source_text("t9_blog", {"context": big})
    assert src.count(ej.CUT_MARK.strip()) == 3                       # seed, brief and facts each cut once
    # the brand pack after the long seed and brief is kept whole; the facts come last with the space left
    assert "Zz private journeys with expert guiding." in src and src.rindex("FACTS:") > src.rindex("BRAND PACK:")
    seed_cap = dict(ej.T9_SOURCE_SECTIONS)["SEED"]
    seed = src.split("\n\n", 1)[0]
    assert len(seed) <= len("SEED:\n") + seed_cap
    prompt = ej.build_user_prompt(writer="t9_blog", source_text=src, output_text="x")
    assert f"<SOURCE>\n{src}\n</SOURCE>" in prompt


def test_clip_cuts_at_a_line_then_a_word_boundary():
    text = "first line here\nsecond line is longer than the cap allows"
    out = ej.clip(text, 40)
    assert out == "first line here" + ej.CUT_MARK and len(out) <= 40
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
    out = ej.clip(words, 45)
    assert out.endswith(ej.CUT_MARK) and len(out) <= 45 and not out[:-len(ej.CUT_MARK)].endswith(("alp", "et"))
    assert ej.clip("short", 45) == "short"


def test_isolation_source_text_is_unchanged_and_a1_t2_judge_source_is_it():
    """The live post-activation check and the shadow read ew.source_text (allowed text): no brief or brand pack."""
    inp = {"context": _ctx()}
    old = ew.source_text("t9_social", inp)
    assert "Zz the angle reasoning." not in old and "Zz build trust" not in old and "Zz private journeys" not in old
    assert ej.judge_source_text("t9_social", inp) != old
    tour = {"tour": {"name": "Zz loop", "summary": "Zz walkers cross a meadow."}, "brand_rules_ref": "s1:x"}
    shared = {"brand_rules": {"s1:x": {"brand_name": "Zzbrand"}}}
    for w in ("a1", "t2"):
        assert ej.judge_source_text(w, tour, shared) == ew.source_text(w, tour, shared)


def test_judge_version_covers_the_source_layout(monkeypatch):
    v = ej.judge_version()
    assert len(v) == 12
    monkeypatch.setattr(ej, "SOURCE_CHARS", ej.SOURCE_CHARS + 1)
    assert ej.judge_version() != v


def test_system_prompt_names_the_sections_and_asks_for_the_quoted_phrase():
    sp = ej.system_prompt()
    assert "BRAND PACK" in sp and "ANGLE BRIEF" in sp and "unsupported_claims" in sp
    assert ej.CUT_MARK.strip() in sp


def test_reply_schema_is_strict_at_every_level():
    def check(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert sorted(node["required"]) == sorted(node["properties"])
            for p in node["properties"].values():
                check(p)
        if node.get("type") == "array":
            check(node["items"])
    check(ej.JUDGE_SCHEMA["schema"])


def test_parse_verdict_keeps_known_claims_capped_and_trimmed():
    claims = [{"class": "fact.embellish", "claim": "Zz  phrase " + "x" * 400, "why": "not in source"},
              {"class": "zz.unknown", "claim": "dropped", "why": "unknown class"},
              "not an object"] + [{"class": "fact.number", "claim": f"n{i}", "why": "w"} for i in range(20)]
    v = ej.parse_verdict(json.dumps({"verdict": "pass", "score": 7, "failure_classes": ["fact.embellish"],
                                     "unsupported_claims": claims}))
    assert len(v["claims"]) == ej.MAX_CLAIMS and v["claims"][0]["class"] == "fact.embellish"
    assert v["claims"][0]["claim"].startswith("Zz phrase") and len(v["claims"][0]["claim"]) == ej.CLAIM_CHARS
    assert all(c["class"] in ej.JUDGE_CLASSES for c in v["claims"])
    no_claims = ej.parse_verdict(json.dumps({"verdict": "fail", "score": 3, "failure_classes": []}))
    assert no_claims["claims"] == []


# ── only R2 calls the judge ──────────────────────────────────────────────────────────────────────────────────────

_IMPORT = re.compile(r"^\s*(from\s+services\.writing_memory\.eval_judge\s+import|import\s+services\.writing_memory"
                     r"\.eval_judge|from\s+services\.writing_memory\s+import\s+[^\n]*\beval_judge\b)", re.M)


def test_only_the_r2_runner_imports_the_eval_judge():
    """Live pipelines never call the eval judge: the only non-test module that imports it is the R2 runner (the
    writing_memory_r2 job / CLI). A new importer must be a deliberate change of this test."""
    importers = set()
    for top in ("api", "services", "shared", "worker", "scripts"):
        for f in (REPO / top).rglob("*.py"):
            if _IMPORT.search(f.read_text(encoding="utf-8", errors="ignore")):
                importers.add(f.relative_to(REPO).as_posix())
    assert importers == {"services/writing_memory/r2.py"}


def test_r2_judges_with_the_parity_source_and_stores_the_claims(monkeypatch):
    from tests.unit.test_aa919_r2_runner import FakeStore, _rows, golden, make_judge, run

    monkeypatch.setattr(ej, "judge_source_text", lambda writer, inp, shared=None: f"PARITY {writer}")
    base = make_judge()
    seen = []

    async def judge(pool, *, writer, source_text, output_text, run_id, item_id, arm):
        seen.append(source_text)
        v = await base(pool, writer=writer, source_text=source_text, output_text=output_text, run_id=run_id,
                       item_id=item_id, arm=arm)
        claims = ({"class": "fact.inclusion", "claim": "BADFACT", "why": "zz not in source"},) \
            if v.failure_codes else ()
        return SimpleNamespace(**vars(v), claims=claims)

    store = FakeStore(golden())
    run(store, r2.R2Config(writer="a1"), judge=judge)
    assert seen and set(seen) == {"PARITY a1"}
    failed = [r for r in _rows(store, arm="control") if r["failure_codes"]]
    assert failed and all(r["details"]["judge_claims"][0]["claim"] == "BADFACT" for r in failed)
    assert all("judge_claims" not in r["details"] for r in _rows(store, arm="control") if not r["failure_codes"])
