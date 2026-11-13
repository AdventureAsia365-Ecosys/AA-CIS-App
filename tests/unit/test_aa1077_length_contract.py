"""AA-1077 — the blog length contract (services/acp_content_writing/length_contract.py): one owner for the writer
target, the F4 band, the extend repair and the post-loop re-check; F4 typed `length_short` / `length_long`; F4
re-run on the text a blog stores (service._recheck_length); the FAQ answer fit never takes a blog under the floor.

Invented places only (Vellmar Gorge, Lake Orrin). No model call, no DB.
"""
import asyncio
import re
from unittest.mock import AsyncMock, patch

from services.acp_content_writing import gate_eval, length_contract as lc, length_trim, prompts
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service as svc
from services.quality import registry as qr

PARA = ("The trail climbs above Vellmar Gorge through pine and juniper, and the river noise fades as the path turns "
        "towards the ridge where the old shepherd huts stand in a loose row.")


def _blog(chars: int) -> str:
    out = "## Vellmar Gorge\n\n"
    while len(out) < chars:
        out += PARA + "\n\n"
    return out[:chars]


# ---------------------------------------------------------------- one owner

def test_the_gate_the_trim_and_the_truth_cut_read_the_contract():
    assert (qg.length_contract.floor_chars(), qg.length_contract.ceiling_chars()) == (lc.floor_chars(), lc.ceiling_chars())
    assert qg._length_bounds_for_channel("blog") == (lc.floor_chars(), lc.ceiling_chars())
    assert length_trim._length_bounds_for_channel is qg._length_bounds_for_channel


def test_the_writer_target_is_read_from_the_registered_prompt_symbols():
    assert lc.writer_words() == (prompts._blog_words()[0], prompts._blog_words()[1], prompts._BLOG_WORDS_MAX)


def test_the_writer_floor_sits_above_the_f4_floor_and_its_max_under_the_ceiling():
    lo, _hi, most = lc.writer_words()
    assert lo * lc.CHARS_PER_WORD >= lc.floor_chars() * (1 + lc.target_margin())
    assert most * lc.CHARS_PER_WORD <= lc.ceiling_chars()
    # the extend repair aims at the length target (S278), at or under the writer's floor, never past the ceiling
    assert lc.floor_chars() < lc.extend_target_chars() == lc.target_chars() <= lo * lc.CHARS_PER_WORD


def test_the_prompt_comment_no_longer_says_tags_included():
    src = open(prompts.__file__, encoding="utf-8").read()
    assert "4,700 characters, tags included" not in src
    assert "TAG-STRIPPED" in src


# ---------------------------------------------------------------- F4 messages and sides

def test_a_short_blog_names_its_side_words_and_target():
    r = qg.gate_extreme_length(_blog(3947), "blog")
    assert not r["passed"]
    (v,) = r["violations"]
    assert v.startswith("length_short: content is only 3947 chars")
    lo, hi, _ = lc.writer_words()
    assert "under the blog floor of 4,700 chars" in v and f"{lo:,}-{hi:,} words" in v
    assert "effectively empty" not in v
    # the audit scripts' reading of the number still works
    assert re.search(r"only (\d+) chars", v).group(1) == "3947"
    assert lc.violation_side(v) == lc.SHORT


def test_a_long_blog_keeps_far_beyond_for_the_rewrite_target():
    v = qg.gate_extreme_length(_blog(13500), "blog")["violations"][0]
    assert v.startswith("length_long: content is 13500 chars") and "far beyond" in v
    assert svc._F4_OVER_RE.search(v)
    assert lc.violation_side(v) == lc.LONG


def test_a_blog_in_band_passes_and_other_channels_keep_their_wording():
    assert qg.gate_extreme_length(_blog(5000), "blog")["passed"]
    assert qg.gate_extreme_length("Hi.", "facebook")["violations"] == ["content is only 3 chars — effectively empty"]
    assert qg.gate_extreme_length("x" * 6001, "facebook")["violations"] == [
        "content is 6001 chars — far beyond any real channel example"]


def test_old_wording_reads_its_side():
    assert lc.violation_side("content is only 3448 chars — effectively empty") == lc.SHORT
    assert lc.violation_side("content is 14576 chars — far beyond any real channel example") == lc.LONG
    assert lc.violation_side("blog is 812 chars") is None
    assert lc.entry_side({"gate": qr.F4_EXTREME_LENGTH, "passed": True, "violations": []}) is None


# ---------------------------------------------------------------- reason codes

def _f4(v, **kw):
    return {"gate": qr.F4_EXTREME_LENGTH, "passed": False, "blocking": True, "repairable": True,
            "violations": [v], **kw}


def test_hold_reason_codes_split_f4_by_side_new_and_old_ledgers():
    short_new = [_f4(qg.gate_extreme_length(_blog(4000), "blog")["violations"][0])]
    short_old = [_f4("content is only 4000 chars — effectively empty")]
    long_old = [_f4("content is 14576 chars — far beyond any real channel example")]
    unreadable = [_f4("F4_extreme_length violation")]
    assert gate_eval.hold_reason_codes(short_new) == ["length_short"]
    assert gate_eval.hold_reason_codes(short_old) == ["length_short"]
    assert gate_eval.hold_reason_codes(long_old) == ["length_long"]
    assert gate_eval.hold_reason_codes(unreadable) == ["length"]


def test_reason_family_groups_every_length_code_and_leaves_the_rest():
    assert {gate_eval.reason_family(c) for c in ("length", "length_short", "length_long")} == {"length"}
    assert gate_eval.reason_family("unsupported_trip_fact") == "unsupported_trip_fact"
    assert gate_eval.LENGTH_CODES == {"length", "length_short", "length_long"}


# ---------------------------------------------------------------- re-check after the normalizer

def test_recheck_holds_an_approved_blog_stored_under_the_floor():
    ledger = [{"gate": qr.F4_EXTREME_LENGTH, "passed": True, "violations": [], "repairable": True,
               "blocking": True}, {"gate": qr.F6_CTA_PRESENT, "passed": True, "violations": []}]
    led, status, reason, entry = svc._recheck_length(_blog(4426), ledger, "approved", None,
                                                     trace={"loop": 4801, "faq_answer_fit": 4426})
    assert status == "held" and reason.startswith("F4_extreme_length: length_short: content is only 4426 chars")
    f4 = next(g for g in led if g["gate"] == qr.F4_EXTREME_LENGTH)
    assert not f4["passed"] and "4426" in f4["violations"][0]
    assert entry["step"] == svc.LENGTH_TRACE_STEP and entry["verdict"] == "length_short" and entry["held"]
    assert entry["chars"] == {"loop": 4801, "faq_answer_fit": 4426} and entry["floor"] == 4700
    assert gate_eval.hold_reason_codes(led) == ["length_short"]
    assert ledger[0]["passed"]  # the caller's ledger is not changed in place


def test_recheck_of_a_blog_in_band_changes_nothing_but_records_the_trace():
    ledger = [{"gate": qr.F4_EXTREME_LENGTH, "passed": True, "violations": []}]
    led, status, reason, entry = svc._recheck_length(_blog(5200), ledger, "approved", None, trace={"loop": 5200})
    assert (led, status, reason) == (ledger, "approved", None)
    assert entry["verdict"] == "in" and not entry["held"]


def test_recheck_never_approves_a_held_piece_and_refreshes_its_number():
    ledger = [_f4("content is only 4586 chars — effectively empty")]
    led, status, reason, entry = svc._recheck_length(_blog(4289), ledger, "held", "F4_extreme_length: old", trace={})
    assert status == "held" and reason == "F4_extreme_length: old" and not entry["held"]
    assert "4289" in led[0]["violations"][0]
    # a stored text back in band leaves a held piece as it is
    led2, status2, _, _ = svc._recheck_length(_blog(5000), ledger, "held", "x", trace={})
    assert status2 == "held" and led2 == ledger


def test_the_normalizer_runs_the_recheck_last_for_a_blog():
    import inspect
    src = inspect.getsource(svc._normalize_final_text)
    assert src.index("_final_integrity(") < src.index("_recheck_length(")
    assert "floor_aware=True" in src


# ---------------------------------------------------------------- the FAQ answer fit is floor-aware

def _fit(after: str, record=None):
    rec = record if record is not None else {"mode": "on", "dropped": ["Q?"], "verdicts": [], "cost_usd": 0.001}
    return patch.object(svc.faq_answer_fit, "apply_answer_fit", new=AsyncMock(return_value=(after, rec)))


def test_a_drop_that_takes_the_blog_under_the_floor_is_not_made():
    before, after = _blog(4800), _blog(4600)
    ctx: dict = {}
    with _fit(after):
        text, flags, cost = asyncio.run(svc._apply_faq_answer_fit(before, [], context=ctx, floor_aware=True))
    assert text == before and ctx["faq_answer_fit"]["floor_kept"] is True and cost == 0.001
    assert any("length floor" in v for f in flags for v in f["violations"])


def test_a_drop_that_stays_over_the_floor_or_a_call_without_the_flag_is_made():
    with _fit(_blog(5000)):
        text, _, _ = asyncio.run(svc._apply_faq_answer_fit(_blog(5300), [], context={}, floor_aware=True))
    assert text == _blog(5000)
    with _fit(_blog(4600)):
        text, _, _ = asyncio.run(svc._apply_faq_answer_fit(_blog(4800), [], context={}))
    assert text == _blog(4600)


def test_writer_input_carries_the_length_trace_only_when_present():
    base = {"channel": "blog", "real_questions": [], "search_questions": []}
    assert "length_trace" not in svc.writer_input_snapshot(base)
    rec = {"step": svc.LENGTH_TRACE_STEP, "chars": {"loop": 5100}, "verdict": "in"}
    assert svc.writer_input_snapshot(dict(base, length_trace=rec))["length_trace"] == rec
    import inspect
    src = inspect.getsource(svc._normalize_final_text)
    assert 'context["length_trace"]' in src and "repair_log.append({\"attempt\": attempt, **entry})" not in src
