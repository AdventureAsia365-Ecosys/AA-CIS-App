"""AA-1077 S278 — the blog length margin: one length target above the F4 floor (registry threshold
t9.blog_length_target_margin) for the writer's word floor, the extend repair and the F4 short rewrite; the normalizer's
optional steps never take a blog under the floor or make one already under it shorter; a $0 replay of stored length
traces (scripts/aa1077/margin_replay.py).

The S278 numbers (12/11, 9 new blog pieces on 3 audit tenants, writer floor 900 words): 4,699 / 4,696 / 4,696 chars
at the end of the loop (1-4 under the 4,700 floor), one of them cut to 4,455 by the normalizer; two thin pieces
4,007 → 3,869 and 3,521 → 3,218. Invented places only (Vellmar Gorge). No model call, no DB.
"""
import asyncio
import inspect
from unittest.mock import AsyncMock, patch

from scripts.aa1077 import margin_replay as mr
from services.acp_content_writing import length_contract as lc, length_extend, prompts
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


# ---------------------------------------------------------------- one target, from the registry

def test_the_target_is_the_floor_plus_the_registry_margin_never_the_floor():
    assert lc.target_margin() == qr.threshold("t9.blog_length_target_margin") == 0.17
    assert lc.floor_chars() == 4700          # the floor is not lowered
    assert lc.target_chars() == round(4700 * 1.17) == 5499
    assert lc.target_words() == 950             # 5,499 / 5.8 = 948.1, rounded up to ten
    assert lc.target_chars() > lc.floor_chars()


def test_the_writer_word_floor_is_the_target_in_words():
    """The prompt's hashed literal is pinned to the threshold: a new margin fails here until the prompt (and its
    t9_write version) follows."""
    lo, hi, most = lc.writer_words()
    assert lo == prompts._blog_words()[0] == lc.target_words()
    assert lo * lc.CHARS_PER_WORD >= lc.target_chars()
    assert lo < hi < most and most * lc.CHARS_PER_WORD <= lc.ceiling_chars()
    assert f"never under {lo:,}" in prompts._blog_length_instructions()


def test_the_extend_repair_aims_at_the_target():
    assert lc.extend_target_chars() == lc.target_chars()
    # S278 7b80e4ca: 4,699 chars; the extend now adds ~800 chars, not 521 (floor + 520 = 5,220)
    assert lc.extend_target_chars() - 4699 == 800


# ---------------------------------------------------------------- the F4 short rewrite is told the target

def _f4_entry(chars: int) -> dict:
    return {**qg.gate_extreme_length(_blog(chars), "blog"), "blocking": True}


def test_a_short_blog_rewrite_is_told_the_target_not_the_floor():
    for chars in (4699, 4696, 3521):          # the S278 edge pieces and a thin one
        (line,) = svc._repair_feedback([_f4_entry(chars)], "blog")
        assert line.startswith(f"F4_extreme_length: length_short: content is only {chars} chars")
        assert "at least 5,499 characters (about 950 words" in line and "not just at the floor" in line
        assert "never invented facts" in line


def test_the_long_side_and_other_channels_keep_their_feedback():
    (long_line,) = svc._repair_feedback([_f4_entry(13500)], "blog")
    assert long_line.endswith(svc.F4_OVER_MAX_TARGET) and "5,499" not in long_line
    fb = {**qg.gate_extreme_length("Hi.", "facebook"), "blocking": True}
    assert svc._repair_feedback([fb], "facebook") == ["F4_extreme_length: content is only 3 chars — effectively empty"]
    # an under-floor blog F4 line read without a channel (no band known) gets no target
    assert svc._repair_feedback([_f4_entry(4699)])[0].endswith("words")


# ---------------------------------------------------------------- the normalizer's floor guard

def test_floor_guard_reads_the_s278_numbers():
    assert lc.floor_guard(_blog(4696), _blog(4455))      # e083d9c2: already under, made shorter → not made
    assert lc.floor_guard(_blog(4940), _blog(4699))      # in band → 1 char under → not made
    assert not lc.floor_guard(_blog(5499), _blog(5258))  # a 241-char cut from the target stays in band → made
    assert not lc.floor_guard(_blog(4455), _blog(4455))  # no cut
    assert not lc.floor_guard(_blog(4400), _blog(4500))  # longer is never stopped


def test_every_normalizer_step_is_classified():
    src = inspect.getsource(svc._normalize_final_text)
    for step in lc.NORMALIZER_STEPS:
        assert f'trace["{step}"]' in src
    assert set(lc.NORMALIZER_STEPS.values()) == {"floor_guarded", "mandatory"}
    assert lc.NORMALIZER_STEPS["authenticity_cut"] == lc.NORMALIZER_STEPS["final_integrity"] == "mandatory"


def _fit(after: str):
    rec = {"mode": "on", "dropped": ["Q?"], "verdicts": [], "cost_usd": 0.001}
    return patch.object(svc.faq_answer_fit, "apply_answer_fit", new=AsyncMock(return_value=(after, rec)))


def test_the_faq_fit_never_makes_an_under_floor_blog_shorter():
    before = _blog(4696)
    ctx: dict = {}
    with _fit(_blog(4455)):
        text, flags, _ = asyncio.run(svc._apply_faq_answer_fit(before, [], context=ctx, floor_aware=True))
    assert text == before
    assert ctx["faq_answer_fit"]["floor_kept"] is True and ctx["faq_answer_fit"]["floor_kept_chars"] == [4696, 4455]
    (line,) = [v for f in flags for v in f["violations"]]
    assert "4,696 → 4,455 chars" in line and "4,700-char length floor" in line


def test_the_faq_fit_still_drops_from_a_blog_written_at_the_target():
    with _fit(_blog(5258)):
        text, _, _ = asyncio.run(svc._apply_faq_answer_fit(_blog(5499), [], context={}, floor_aware=True))
    assert text == _blog(5258)


def test_the_length_trace_names_the_target_and_the_guarded_steps():
    ledger = [{"gate": qr.F4_EXTREME_LENGTH, "passed": False, "blocking": True,
               "violations": [lc.short_message(4696, 809)]}]
    _led, status, _r, entry = svc._recheck_length(
        _blog(4696), ledger, "held", "F4_extreme_length: x",
        trace={"loop": 4696, "faq_answer_fit": 4696}, floor_kept=["faq_answer_fit"])
    assert status == "held" and entry["target"] == 5499 and entry["floor_kept"] == ["faq_answer_fit"]
    _led, _s, _r, entry2 = svc._recheck_length(_blog(5499), [], "approved", None, trace={"loop": 5499})
    assert "floor_kept" not in entry2 and entry2["verdict"] == "in"
    src = inspect.getsource(svc._normalize_final_text)
    assert "floor_kept=kept" in src


# ---------------------------------------------------------------- $0 replay of the S278 traces

def _trace(loop, **after):
    chars = {"loop": loop}
    for step in mr.STEPS[1:]:
        chars[step] = after.get(step, chars[list(chars)[-1]])
    return chars


def test_replay_s278_edge_pieces_clear_the_floor_at_the_new_target():
    # 7b80e4ca / 3612fd6a: no normalizer cut; written 1-4 chars under the floor with the 900-word floor
    for loop in (4699, 4696):
        r = mr.replay_trace(_trace(loop))
        assert r["stored_verdict"] == "length_short" and r["guarded"] == loop
        assert r["projected"] == round(loop * 950 / 900) and r["projected_verdict"] == "in"
        assert r["change"] == "target"


def test_replay_s278_normalizer_cut_kept_by_the_guard_or_cleared_by_the_target():
    # e083d9c2: 4,696 → 4,455. When the FAQ answer fit made the cut, the guard keeps the text (stored 4,696)
    r = mr.replay_trace(_trace(4696, faq_answer_fit=4455))
    assert r["stored"] == 4455 and r["guarded"] == 4696 and r["floor_kept"] == ["faq_answer_fit"]
    assert r["change"] == "guard" and r["projected"] == 4957 and r["projected_verdict"] == "in"
    # when a mandatory step made it (final integrity), the cut stays and the target still clears the floor
    r2 = mr.replay_trace(_trace(4696, final_integrity=4455))
    assert r2["guarded"] == 4455 and r2["floor_kept"] == [] and r2["mandatory_cut"] == 241
    assert r2["projected"] == 4957 - 241 == 4716 and r2["projected_verdict"] == "in" and r2["change"] == "target"


def test_replay_thin_pieces_stay_short_the_margin_is_no_seed_fix():
    # fbc75fe7 4,007 → 3,869, f97aeb18 3,521 → 3,218 (also held on other gates): the margin does not reach them
    for loop, after in ((4007, 3869), (3521, 3218)):
        r = mr.replay_trace(_trace(loop, final_integrity=after))
        assert r["projected_verdict"] == "length_short" and r["change"] == "none"


def test_replay_piece_and_summary():
    rows = [
        mr.replay_piece({"piece_id": "7b80e4ca-0", "tenant": "audit-x", "status": "held",
                         "content_text": _blog(4699),
                         "writer_input": {"length_trace": {"attempt": 2, "chars": _trace(4699)}}}),
        mr.replay_piece({"piece_id": "e083d9c2-0", "tenant": "audit-x", "status": "held",
                         "content_text": _blog(4455),
                         "writer_input": '{"length_trace": {"attempt": 1, "chars": {"loop": 4696, '
                                         '"faq_answer_fit": 4455, "final_integrity": 4455}}}'}),
        mr.replay_piece({"piece_id": "89ec7b6c-0", "tenant": "audit-x", "status": "approved",
                         "content_text": _blog(5590), "writer_input": {}}),
    ]
    assert [r["change"] for r in rows] == ["target", "guard", "no_trace"]
    s = mr.summarize(rows)
    assert s["traced"] == 2 and s["target_chars"] == 5499 and s["target_words"] == 950
    assert s["short_stored"] == ["7b80e4ca", "e083d9c2"] and s["short_projected"] == []
    assert s["changed_by_guard"] == ["e083d9c2"] and s["cleared_by_target"] == ["7b80e4ca"]
    assert s["largest_cut"] == 241


def test_replay_reads_no_trace_and_old_words():
    assert mr.replay_trace({}) is None and mr.replay_trace(None) is None
    assert mr.replay_trace(_trace(4699), old_words=950)["projected"] == 4699


def test_extend_plan_deficit_for_the_s278_edge_piece():
    # the plan's deficit is the target minus the text (the extend module reads the contract, no own number)
    assert "extend_target_chars()" in inspect.getsource(length_extend.plan_for)
