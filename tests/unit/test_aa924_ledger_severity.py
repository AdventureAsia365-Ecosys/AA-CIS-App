"""AA-924 PR1a — per-finding severity in the T10 ledger and the legacy routing view.

`merge_ledger` folds F2_banned_patterns + F2_brand_forbidden + F15_voice's stock findings into one F2_lexicon entry
and F15_voice + F15_voice_warn into one F15_voice entry; `expand_ledger` rebuilds the legacy entries. Every routing
decision reads the expanded view, so the invariant `expand(merge(L)) == L` is what keeps routing unchanged.
"""
import copy

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair
from services.acp_content_writing import service
from services.acp_content_writing.gate_eval import ledger_snapshot
from services.acp_shared.channel_spec import channel_spec
from services.quality import registry as qr
from tests.unit import aa791_t9_fault_set as fs

LINKEDIN_PACK = {**fs.PACK, "channel_specs": {}}

# LinkedIn texts that make every F15 kind fire: a STOCK_PATTERNS phrase, a template line, a contrast frame, a
# generic opener, broetry and the brand's audience range (F15_voice_warn).
VOICE_TEXT = (
    "There is a quiet camp opposite a mountain range near Lake Khövsgöl. "
    "The detail is worth sitting with when you plan the day with children aged 7–16. "
    "It's not a hotel. It's a ger camp with a stove and a view of the range at dusk. "
    "This is not a metaphor for slow travel, it is a real afternoon by the water. "
    "Short walk. Long dinner. "
    "The surrounding area is home to reindeer-herding Tsaatan communities who move with the seasons. "
    + fs.CTA + "."
)
BANNED_TEXT = "Sod Camp is a hidden gem worth sitting with on a long trip. " + fs.CLEAN


def _ledger(text: str, channel: str = "linkedin", stock_blocking: bool = True, pack: dict | None = None,
            atom_text: str = fs.SEED) -> dict:
    with fs.judges_pass():
        return qg.run_quality_gates(content_text=text, atom_text=atom_text, cta=fs.CTA, brand_rubric_text="rubric",
                                    channel=channel, brand_pack=pack or fs.PACK, stock_blocking=stock_blocking)


def _legacy_ledgers() -> list[tuple[str, list, str]]:
    """(id, legacy ledger, content_text): the 40 AA-791 cases plus voice-heavy LinkedIn / facebook texts."""
    out = []
    with fs.judges_pass():
        for case in fs.FAULT_CASES + fs.CLEAN_CASES:
            out.append((case.case_id, qg.expand_ledger(fs.run_case(case)["gate_ledger"]), case.content_text))
    for ch in ("linkedin", "facebook", "blog"):
        for sb in (True, False):
            for text in (VOICE_TEXT, BANNED_TEXT):
                out.append((f"{ch}|{sb}|{text[:12]}", qg.expand_ledger(_ledger(text, ch, sb)["gate_ledger"]), text))
    return out


LEDGERS = _legacy_ledgers()


@pytest.mark.parametrize("case_id,ledger,_text", LEDGERS, ids=[c[0] for c in LEDGERS])
def test_expand_merge_round_trip(case_id, ledger, _text):
    merged = qg.merge_ledger(ledger)
    assert qg.expand_ledger(merged) == ledger
    assert qg.merge_ledger(merged) == merged  # idempotent
    names = [g["gate"] for g in merged]
    if any(g["gate"] in qr.SOURCE_HOME for g in ledger):
        assert qr.F2_LEXICON in names and qr.F15_VOICE in names
        assert qr.F2_BANNED_PATTERNS not in names and qr.F15_VOICE_WARN not in names
        assert len(merged) == len(ledger) - 2


@pytest.mark.parametrize("case_id,ledger,_text", LEDGERS, ids=[c[0] for c in LEDGERS])
def test_flags_round_trip(case_id, ledger, _text):
    flags = [g for g in ledger if not g["passed"] and not g["blocking"]]
    merged = qg.merge_flags(flags)
    assert qg.expand_ledger(merged) == flags
    assert all(not g["passed"] for g in merged)


@pytest.mark.parametrize("case_id,ledger,text", LEDGERS, ids=[c[0] for c in LEDGERS])
def test_routing_identical_on_merged_ledger(case_id, ledger, text):
    merged = qg.merge_ledger(ledger)
    assert qg.hold_failure(merged) == qg.hold_failure(ledger)
    assert sentence_repair.plan(text, merged) == sentence_repair.plan(text, ledger)
    assert service._style_only_repair(merged) == service._style_only_repair(ledger)
    assert service._repair_feedback(merged, "blog") == service._repair_feedback(ledger, "blog")
    assert service._blocking_failures(merged) == service._blocking_failures(ledger)
    flags = [g for g in ledger if not g["passed"] and not g["blocking"]]
    st, reason, out_flags = service._hold_or_ship(merged, qg.merge_flags(flags))
    st0, reason0, out_flags0 = service._hold_or_ship(ledger, flags)
    hold = qg.hold_failure(ledger)
    # the held reason names the stored entry (F2_lexicon), not the retired source name
    assert (st, reason) == (st0, reason0 if hold is None else reason0.replace(hold["gate"], qg.ledger_gate(hold), 1))
    assert qg.expand_ledger(out_flags) == out_flags0
    assert qg.expand_ledger(service._waive_style_only(merged, "approved")) == service._waive_style_only(
        ledger, "approved")


def test_voice_text_fires_every_f15_kind():
    legacy = qg.expand_ledger(_ledger(VOICE_TEXT)["gate_ledger"])
    f15 = next(g for g in legacy if g["gate"] == qr.F15_VOICE)
    kinds = {qg._f15_kind(v) for v in f15["violations"]}
    assert {0, 1, 3} <= kinds  # stock phrase, contrast frame, stock line
    assert next(g for g in legacy if g["gate"] == qr.F15_VOICE_WARN)["violations"]


def test_merged_shape_and_worst_severity():
    legacy = qg.expand_ledger(_ledger(VOICE_TEXT)["gate_ledger"])
    merged = {g["gate"]: g for g in qg.merge_ledger(legacy)}
    lex, voice = merged[qr.F2_LEXICON], merged[qr.F15_VOICE]
    # attempt 1: F15_voice blocks (stock + frames), F15_voice_warn warns
    assert {f["source"] for f in lex["findings"]} == {qr.F15_VOICE}
    assert all(f["text"].startswith(qg.F15_STOCK_PREFIXES) and f["severity"] == qg.BLOCK for f in lex["findings"])
    assert lex["severity"] == qg.BLOCK and lex["blocking"] and not lex["passed"]
    assert {f["source"] for f in voice["findings"]} == {qr.F15_VOICE, qr.F15_VOICE_WARN}
    assert {f["severity"] for f in voice["findings"] if f["source"] == qr.F15_VOICE_WARN} == {qg.WARN}
    assert voice["severity"] == qg.BLOCK and voice["blocking"]
    assert voice["violations"] == [f["text"] for f in voice["findings"]]
    assert [p["gate"] for p in lex["parts"]] == [qr.F2_BANNED_PATTERNS, qr.F2_BRAND_FORBIDDEN, qr.F15_VOICE]
    assert [p["gate"] for p in voice["parts"]] == [qr.F15_VOICE, qr.F15_VOICE_WARN]
    assert all("violations" not in p for p in lex["parts"] + voice["parts"])
    # attempt 2: every F15 finding is a warn, so both entries are warn and non-blocking
    later = {g["gate"]: g for g in qg.merge_ledger(qg.expand_ledger(_ledger(VOICE_TEXT, stock_blocking=False)
                                                                    ["gate_ledger"]))}
    assert later[qr.F2_LEXICON]["severity"] == qg.WARN and not later[qr.F2_LEXICON]["blocking"]
    assert later[qr.F15_VOICE]["severity"] == qg.WARN and not later[qr.F15_VOICE]["blocking"]


def test_worst_severity_mixed_entry():
    legacy = qg.expand_ledger(_ledger(BANNED_TEXT, stock_blocking=False)["gate_ledger"])
    lex = next(g for g in qg.merge_ledger(legacy) if g["gate"] == qr.F2_LEXICON)
    sev = {f["source"]: f["severity"] for f in lex["findings"]}
    assert sev == {qr.F2_BANNED_PATTERNS: qg.BLOCK, qr.F15_VOICE: qg.WARN}
    assert lex["severity"] == qg.BLOCK and lex["blocking"]
    assert not qg.never_holds(lex)  # the banned pattern may hold the piece
    warn_only = qg.merge_ledger([g for g in legacy if g["gate"] != qr.F2_BANNED_PATTERNS])
    lex2 = next(g for g in warn_only if g["gate"] == qr.F2_LEXICON)
    assert lex2["severity"] == qg.WARN and not lex2["blocking"]


def test_passed_entries_keep_their_sources_blocking():
    legacy = qg.expand_ledger(_ledger(fs.CLEAN, channel="facebook")["gate_ledger"])
    merged = {g["gate"]: g for g in qg.merge_ledger(legacy)}
    assert merged[qr.F2_LEXICON]["passed"] and merged[qr.F2_LEXICON]["blocking"]  # like F2_banned_patterns
    assert merged[qr.F15_VOICE]["passed"] and not merged[qr.F15_VOICE]["blocking"]  # like F15_voice
    assert merged[qr.F2_LEXICON]["severity"] is None and merged[qr.F2_LEXICON]["findings"] == []


def test_legacy_ledger_passes_through_unchanged():
    legacy = qg.expand_ledger(_ledger(VOICE_TEXT)["gate_ledger"])
    assert qg.expand_ledger(legacy) == legacy
    assert qg.expand_ledger(legacy) is not legacy  # a copy, never the caller's list
    old = [{"gate": "F8_framework", "passed": False, "violations": ["x"], "repairable": True, "blocking": True},
           {"gate": qr.F15_VOICE, "passed": False, "violations": ["stock phrase 'a' — b: 'c d e f'"],
            "repairable": True, "blocking": True}]
    assert qg.expand_ledger(old) == old  # a pre-AA-924 F15_voice entry has no parts: not merged
    assert not qg.ledger_is_merged(old)
    assert qg.reshape_like(old, old) == old


def test_entries_that_are_not_merged_never_move():
    """_final_integrity appends F13_repeat at the end: expanding must not sort it back."""
    legacy = qg.expand_ledger(_ledger(VOICE_TEXT)["gate_ledger"])
    tail = {"gate": qr.F13_REPEAT, "passed": False, "violations": ["v"], "repairable": True, "blocking": True}
    moved = [g for g in legacy if g["gate"] != qr.F13_REPEAT] + [tail]
    assert qg.expand_ledger(qg.merge_ledger(moved)) == moved
    assert qg.hold_failure(qg.merge_ledger(moved)) == qg.hold_failure(moved)


def test_regression_guard_counts_a_new_source_inside_a_failed_merged_entry():
    prev = qg.merge_ledger(qg.expand_ledger(_ledger(VOICE_TEXT)["gate_ledger"]))  # F2_lexicon fails on stock
    cur = qg.merge_ledger(qg.expand_ledger(_ledger(BANNED_TEXT, channel="linkedin")["gate_ledger"]))
    prev_gates = {g["gate"] for g in prev if g.get("blocking", True) and not g["passed"]}
    cur_gates = {g["gate"] for g in cur if g.get("blocking", True) and not g["passed"]}
    assert qr.F2_LEXICON in prev_gates and qr.F2_LEXICON in cur_gates  # merged names alone would hide it
    new = service._blocking_failures(cur) - service._blocking_failures(prev)
    assert qr.F2_BANNED_PATTERNS in new


def test_waive_style_only_on_a_mixed_entry():
    legacy = qg.expand_ledger(_ledger(BANNED_TEXT, channel="linkedin")["gate_ledger"])
    merged = qg.merge_ledger(legacy)
    waived = service._waive_style_only(merged, "approved")
    lex = next(g for g in waived if g["gate"] == qr.F2_LEXICON)
    sev = {f["source"]: f["severity"] for f in lex["findings"]}
    assert sev[qr.F2_BANNED_PATTERNS] == qg.BLOCK and sev[qr.F15_VOICE] == qg.WARN  # only the style source
    assert lex["blocking"] and lex.get("waived")
    f15 = next(g for g in qg.expand_ledger(waived) if g["gate"] == qr.F15_VOICE)
    assert f15["waived"] and not f15["blocking"]
    banned = next(g for g in qg.expand_ledger(waived) if g["gate"] == qr.F2_BANNED_PATTERNS)
    assert banned["blocking"] and "waived" not in banned
    # a style-only F2_lexicon (stock phrase only) is waived as a whole
    voice = service._waive_style_only(qg.merge_ledger(qg.expand_ledger(_ledger(VOICE_TEXT)["gate_ledger"])),
                                      "approved")
    lex = next(g for g in voice if g["gate"] == qr.F2_LEXICON)
    assert lex["severity"] == qg.WARN and not lex["blocking"] and lex["waived"]


def test_never_holds_on_merged_entries():
    merged = {g["gate"]: g for g in qg.merge_ledger(qg.expand_ledger(_ledger(VOICE_TEXT)["gate_ledger"]))}
    assert qg.never_holds(merged[qr.F2_LEXICON]) and qg.never_holds(merged[qr.F15_VOICE])


def test_cut_stock_lines_trims_the_lexicon_entry():
    text = ("Sod Camp sits opposite a mountain range near Lake Khövsgöl, a quiet first night in a ger. "
            "The ger camp has a stove, and the range turns pink at dusk over the water. "
            "That view is worth paying attention to on the first evening of the trip. "
            "The surrounding area is home to reindeer-herding Tsaatan communities who move with the seasons. "
            "Families eat dinner together in the main ger before an early night. " * 3 + fs.CTA + ".")
    spec = channel_spec("linkedin", fs.PACK)
    legacy = qg.expand_ledger(_ledger(text, stock_blocking=False)["gate_ledger"])
    flags = [g for g in legacy if not g["passed"] and not g["blocking"]]
    merged, merged_flags = qg.merge_ledger(legacy), qg.merge_flags(flags)
    out0 = service._cut_stock_lines(text, copy.deepcopy(legacy), copy.deepcopy(flags), channel="linkedin", spec=spec,
                                    cta=fs.CTA)
    out1 = service._cut_stock_lines(text, merged, merged_flags, channel="linkedin", spec=spec, cta=fs.CTA)
    assert out1[0] == out0[0] and out1[3] == out0[3]
    assert qg.expand_ledger(out1[1]) == out0[1]
    assert qg.expand_ledger(out1[2]) == out0[2]
    assert qg.ledger_is_merged(out1[1])


def test_ledger_snapshot_keeps_finding_severity():
    merged = qg.merge_ledger(qg.expand_ledger(_ledger(BANNED_TEXT, channel="linkedin")["gate_ledger"]))
    snap = {g["gate"]: g for g in ledger_snapshot(merged)}
    lex = snap[qr.F2_LEXICON]
    assert lex["severity"] == qg.BLOCK
    assert {f["source"] for f in lex["findings"]} == {qr.F2_BANNED_PATTERNS, qr.F15_VOICE}
    assert all(len(f["text"]) <= 300 for f in lex["findings"]) and len(lex["findings"]) <= 5
    assert "findings" not in snap[qr.F1_GROUNDING]  # a legacy-shaped entry is snapshotted as before
    many = {"gate": qr.F2_LEXICON, "passed": False, "violations": ["x" * 400] * 7, "repairable": True,
            "blocking": True, "severity": qg.BLOCK, "parts": [],
            "findings": [{"text": "x" * 400, "severity": qg.BLOCK, "source": qr.F2_BANNED_PATTERNS}] * 7}
    out = ledger_snapshot([many])[0]
    assert len(out["findings"]) == 5 and len(out["findings"][0]["text"]) == 300
