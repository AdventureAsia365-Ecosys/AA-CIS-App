"""AA-924 PR1b — the lexicon gate (F2_lexicon) and F15_voice absorbing F15_voice_warn.

F2_lexicon = F2_banned_patterns + F2_brand_forbidden + the F15 stock list (STOCK_PATTERNS on a channel with a spec,
hygiene.template_lines everywhere), one finding per word with its own severity; F15_voice keeps contrast frames,
sibling templates and pasted brief phrases (block on attempt 1) and holds F15_voice_warn's checks as warn findings.
The AA-800 word layers stay their own gates, and no word is judged in two gates.
"""
import re

import pytest

from services.acp_content_writing import hygiene
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair
from services.acp_shared.channel_spec import STOCK_PATTERNS, STOCK_PHRASES, channel_spec
from services.acp_shared.signature_vocabulary import PLATFORM_TRUTH, POSITIONING, STOCK_SUPERLATIVES
from services.quality import registry as qr
from tests.unit import aa791_t9_fault_set as fs

SPEC = channel_spec("linkedin", fs.PACK)
# Plain phrases each banned pattern stands for (the seed list, written out).
BANNED_PHRASES = ["nestled", "tapestry", "hidden gem", "must-visit", "must-see", "unforgettable", "bucket list",
                  "whether you're a parent or a child", "in conclusion", "embark on", "awaits you", "immerse yourself",
                  "look no further", "delve", "in today's fast-paced world", "game-changing", "revolutionary",
                  "unlock your potential", "take your trip to the next level", "whether you're a beginner or an expert"]


def _run(text: str, channel: str = "linkedin", pack: dict | None = None, stock_blocking: bool = True) -> dict:
    with fs.judges_pass():
        return qg.run_quality_gates(content_text=text, atom_text=fs.SEED, cta=fs.CTA, brand_rubric_text="rubric",
                                    channel=channel, brand_pack=pack or fs.PACK, stock_blocking=stock_blocking)


def _entry(out: dict, gate: str) -> dict:
    return next(g for g in out["gate_ledger"] if g["gate"] == gate)


# ------------------------------------------------------------------------------------------- word lists

def test_word_lists_do_not_overlap():
    banned = qg._BANNED_PATTERNS_COMPILED
    layers = {"STOCK_SUPERLATIVES": STOCK_SUPERLATIVES, "POSITIONING": POSITIONING, "PLATFORM_TRUTH": PLATFORM_TRUTH}
    for name, words in layers.items():
        for w in words:
            assert not any(p.search(w) for p in banned), (name, w)
            assert not any(p.search(w) for p in STOCK_PATTERNS), (name, w)
    for a, b in [("STOCK_SUPERLATIVES", "POSITIONING"), ("STOCK_SUPERLATIVES", "PLATFORM_TRUTH"),
                 ("POSITIONING", "PLATFORM_TRUTH")]:
        assert not set(layers[a]) & set(layers[b]), (a, b)
    for phrase in STOCK_PHRASES:
        assert not any(p.search(phrase) for p in banned), phrase
    for phrase in BANNED_PHRASES:
        assert any(p.search(phrase) for p in banned), phrase  # the list above is the seed list
        assert not any(p.search(phrase) for p in STOCK_PATTERNS), phrase
        assert not any(p.search(phrase) for p in hygiene.TEMPLATE_LINE_RES), phrase


# ------------------------------------------------------------------------------------------- the stack

@pytest.mark.parametrize("channel,count", [("blog", 23), ("linkedin", 22), ("facebook", 21), ("instagram", 21),
                                           ("tiktok", 21), ("email", 21), ("landing_page", 21), ("ads", 21)])
def test_gate_count_per_channel(channel, count):
    gates = [g["gate"] for g in _run(fs.CLEAN, channel)["gate_ledger"]]
    assert len(gates) == len(set(gates)) == count
    assert qr.F2_LEXICON in gates and qr.F15_VOICE in gates
    assert not {qr.F2_BANNED_PATTERNS, qr.F2_BRAND_FORBIDDEN, qr.F15_VOICE_WARN} & set(gates)
    assert gates.index(qr.F2_LEXICON) == gates.index(qr.F1_GROUNDING) + 1  # F2_banned_patterns' place


def test_banned_and_forbidden_findings_block():
    pack = {**fs.PACK, "forbidden_words": ["ger stay"]}
    out = _run("Sod Camp is a hidden gem. A ger stay opposite the range is the first night. " + fs.CLEAN,
               channel="facebook", pack=pack)
    lex = _entry(out, qr.F2_LEXICON)
    assert [(f["source"], f["severity"]) for f in lex["findings"]] == [(qr.F2_BANNED_PATTERNS, qg.BLOCK),
                                                                       (qr.F2_BRAND_FORBIDDEN, qg.BLOCK)]
    assert lex["blocking"] and lex["severity"] == qg.BLOCK and not out["passed"]
    assert out["first_failure"]["gate"] == qr.F2_BANNED_PATTERNS  # routing still reads the source


def test_stock_findings_block_on_attempt_one_then_warn():
    text = ("Sod Camp sits opposite a mountain range near Lake Khövsgöl. That view is worth paying attention to "
            "on the first evening. " + fs.CLEAN)
    first = _entry(_run(text), qr.F2_LEXICON)
    assert [(f["source"], f["severity"]) for f in first["findings"]] == [(qr.F15_VOICE, qg.BLOCK)]
    assert first["findings"][0]["text"].startswith("stock phrase 'worth paying attention to'")
    later = _entry(_run(text, stock_blocking=False), qr.F2_LEXICON)
    assert [(f["source"], f["severity"]) for f in later["findings"]] == [(qr.F15_VOICE, qg.WARN)]
    assert not later["blocking"]
    # STOCK_PATTERNS are read on a channel with a spec only; a template line on every channel
    assert not _entry(_run(text, channel="facebook"), qr.F2_LEXICON)["findings"]
    tl = _entry(_run("The ger is not just filler on this route, it is the first night. " + fs.CLEAN,
                     channel="facebook"), qr.F2_LEXICON)
    assert [f["text"].split(" '")[0] for f in tl["findings"]] == ["stock line"]


def test_f15_voice_holds_frames_and_the_warn_checks():
    text = ("There is a quiet camp opposite a mountain range near Lake Khövsgöl. It's not a hotel. It's a ger camp "
            "with a stove and a view of the range at dusk. This is not a metaphor for slow travel, it is a real "
            "afternoon by the water. " + fs.CLEAN)
    voice = _entry(_run(text), qr.F15_VOICE)
    by_source = {}
    for f in voice["findings"]:
        by_source.setdefault(f["source"], set()).add(f["severity"])
    assert by_source == {qr.F15_VOICE: {qg.BLOCK}, qr.F15_VOICE_WARN: {qg.WARN}}
    assert voice["blocking"]  # a contrast frame on attempt 1
    # the warn checks alone never block the F15_voice entry
    only_warn = _entry(_run("There is a quiet camp opposite a mountain range near Lake Khövsgöl. " + fs.CLEAN),
                       qr.F15_VOICE)
    assert {f["source"] for f in only_warn["findings"]} == {qr.F15_VOICE_WARN}
    assert not only_warn["blocking"] and only_warn["severity"] == qg.WARN


# ------------------------------------------------------------------------------------------- one judgement per word

def test_forbidden_word_that_is_a_banned_pattern_is_judged_once():
    pack = {**fs.PACK, "forbidden_words": ["hidden gem"]}
    lex = _entry(_run("Sod Camp is a hidden gem. " + fs.CLEAN, channel="facebook", pack=pack), qr.F2_LEXICON)
    assert [f["source"] for f in lex["findings"]] == [qr.F2_BANNED_PATTERNS]


def test_stock_phrase_on_the_tenant_list_is_judged_once_at_block():
    pack = {**fs.PACK, "forbidden_words": ["worth paying attention to"]}
    text = ("Sod Camp sits opposite a mountain range near Lake Khövsgöl. That view is worth paying attention to "
            "on the first evening. " + fs.CLEAN)
    for sb in (True, False):
        lex = _entry(_run(text, pack=pack, stock_blocking=sb), qr.F2_LEXICON)
        assert [(f["source"], f["severity"]) for f in lex["findings"]] == [(qr.F2_BRAND_FORBIDDEN, qg.BLOCK)], sb


def test_platform_truth_skips_the_tenants_own_word():
    pack = {**fs.PACK, "forbidden_words": ["cheap"]}
    out = _run("Cheap flights are not part of this. " + fs.CLEAN, channel="facebook", pack=pack)
    assert _entry(out, qr.F2_PLATFORM_TRUTH)["passed"]
    assert [f["source"] for f in _entry(out, qr.F2_LEXICON)["findings"]] == [qr.F2_BRAND_FORBIDDEN]
    # without the tenant's word, platform truth still owns it
    out = _run("Cheap flights are not part of this. " + fs.CLEAN, channel="facebook")
    assert not _entry(out, qr.F2_PLATFORM_TRUTH)["passed"] and _entry(out, qr.F2_LEXICON)["passed"]


_WORD_GATES = (qr.F2_LEXICON, qr.F2_PLATFORM_TRUTH, qr.F2_SUPERLATIVES, qr.F2_STYLE_WARN)
_QUOTED_WORD_RE = re.compile(r"'([^']{2,60})'")


@pytest.mark.parametrize("words", [["cheap"], ["stunning"], ["curated"], ["hidden gem"], ["worth sitting with"],
                                   ["cheap", "stunning", "curated"]])
def test_one_text_never_gives_the_same_word_in_two_gates(words):
    pack = {**fs.PACK, "forbidden_words": words}
    text = ("Sod Camp is a hidden gem with cheap flights. The stunning, curated camp is worth sitting with. "
            "A stunning view and a magical night follow. " + fs.CLEAN)
    out = _run(text, pack=pack)
    seen: dict[str, str] = {}
    for g in qg.expand_ledger(out["gate_ledger"]) + qg.expand_ledger(out["flags"]):
        if g["passed"] or g["gate"] not in {qr.F2_BANNED_PATTERNS, qr.F2_BRAND_FORBIDDEN, qr.F15_VOICE,
                                            *_WORD_GATES}:
            continue
        for v in g["violations"]:
            m = _QUOTED_WORD_RE.search(v.split("/ -> ")[-1])
            word = (m.group(1) if m else v).lower()
            for w in words:
                if w in word:
                    assert seen.setdefault(w, g["gate"]) == g["gate"], (w, seen[w], g["gate"])


# ------------------------------------------------------------------------------------------- admin Apply

def test_check_replacement_reports_the_lexicon_without_the_stock_list():
    pack = {**fs.PACK, "forbidden_words": ["ger stay"]}
    res = sentence_repair.check_replacement("A ger stay that is worth sitting with and a hidden gem.", fs.SEED, pack)
    lex = [r for r in res if r["gate"] == qr.F2_LEXICON]
    assert len(lex) == 1 and lex[0]["blocking"]
    assert any("hidden gem" in v for v in lex[0]["violations"])
    assert any("ger stay" in v for v in lex[0]["violations"])
    assert not any("stock" in v for v in lex[0]["violations"])
    assert not sentence_repair.check_replacement("Sod Camp sits opposite a mountain range.", fs.SEED, pack)


def test_gate_lexicon_matches_the_stack_entry():
    text = ("Sod Camp is a hidden gem. That view is worth paying attention to on the first evening. " + fs.CLEAN)
    stack = _entry(_run(text), qr.F2_LEXICON)
    alone = qg.gate_lexicon(text, "\n".join([fs.SEED, fs.CTA]), fs.PACK, SPEC, True, atom_text=fs.SEED)
    assert alone["violations"] == stack["violations"]
    assert [(f["source"], f["severity"]) for f in alone["findings"]] == [(f["source"], f["severity"])
                                                                         for f in stack["findings"]]
    assert qg.gate_lexicon(text, fs.SEED, fs.PACK, SPEC, include_stock=False)["findings"][0]["source"] \
        == qr.F2_BANNED_PATTERNS
    assert [p["gate"] for p in qg.gate_lexicon(text, fs.SEED, fs.PACK, include_stock=False)["parts"]] == [
        qr.F2_BANNED_PATTERNS, qr.F2_BRAND_FORBIDDEN]


# ------------------------------------------------------------------------------------------- admin highlights

def test_highlights_read_each_findings_own_severity():
    from api.routers import admin_content_quality as cq

    text = ("Sod Camp is a hidden gem. That view is worth paying attention to on the first evening. " + fs.CLEAN)
    out = _run(text, stock_blocking=False)
    hs = cq.highlights(text, out["gate_ledger"], out["flags"])
    lex = [h for h in hs if h["gate"] == qr.F2_LEXICON]
    banned = next(h for h in lex if h["source"] == qr.F2_BANNED_PATTERNS)
    stock = next(h for h in lex if h["source"] == qr.F15_VOICE)
    assert banned["text"].lower() == "hidden gem" and banned["blocking"] and banned["severity"] == qg.BLOCK
    assert stock["sentence"].startswith("That view is worth paying attention to")
    assert not stock["blocking"] and stock["severity"] == qg.WARN  # warn inside a blocking entry
    items = cq.flagged_sentences(text, hs)
    assert any(p.get("severity") == qg.WARN and p.get("source") == qr.F15_VOICE
               for it in items for p in it["problems"])
    # an older piece's F2_brand_forbidden entry is still parsed, with the gate's own blocking
    old = {"gate": qr.F2_BRAND_FORBIDDEN, "passed": False, "repairable": True, "blocking": True,
           "violations": ["uses 'hidden gem', which the brand forbids: 'Sod Camp is a hidden gem.'"]}
    [h] = [h for h in cq.highlights(text, [old], []) if h["gate"] == qr.F2_BRAND_FORBIDDEN]
    assert h["blocking"] and h["severity"] == qg.BLOCK and "source" not in h
