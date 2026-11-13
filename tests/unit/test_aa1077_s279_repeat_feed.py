"""AA-1077 (S279, lane f13-drop) — the repeat drop reads both F13 gates' repeat findings.

1. Where a repeat is reported: an exact or one-word-changed repeat is quoted by F13_repeat (blocking), never by the
   F13_hygiene warning; the hygiene repeat warning is the "two shared trigrams" arm (a shared place phrase).
2. `candidates`: F13_repeat quotes, then F13_hygiene repeat warnings (other hygiene warnings are not candidates); a
   sentence both quote counts as F13_repeat; `sources` narrows them (the replay's old arm).
3. The hygiene feed: a near-exact repeat quoted only by F13_hygiene is dropped under the same rule (the setting share,
   the floor, the invariants) and the log / trace name the gate; a hygiene warning that is not near-exact stays.
4. `trace`: one entry per candidate, outcome codes and numbers only, never text.

Invented words and places only (generated syllables, no stored text).
"""
from __future__ import annotations

import pytest

from services.acp_content_writing import hygiene, repeat_drop
from services.acp_content_writing import length_contract as lc
from services.quality import registry as qr
from tests.unit.test_aa1077_s279_repeat_drop import CTA, _blog, _first_sentence, _hyg, _with_twin

RIDGE = "The ridge above Varnholt rises to an elevation of roughly 1,645 metres above sea level over the pine slopes."
VALLEY = "Keswane lies lower, at an elevation of roughly 1,283 metres above sea level beside the slow brown river."


def _place_phrase_blog() -> str:
    """A long blog whose section 1 and section 4 share a place phrase ('elevation of roughly … metres above sea
    level'): the F13_hygiene repeat warning, not a strict repeat."""
    paras = _blog()
    paras[1] = paras[1] + " " + RIDGE
    paras[7] = paras[7] + " " + VALLEY
    return "\n\n".join(paras) + "\n\n" + CTA


def _hygiene_only(monkeypatch, sentence: str) -> None:
    """find_issues as the repeat drop sees it, with `sentence` quoted by F13_hygiene only (a detector change)."""
    real = hygiene.find_issues

    def fake(text, ctx, **kw):
        found = real(text, ctx, **kw)
        quoted = f"'{sentence[:200]}'"
        if not any(f.endswith(quoted) for f in found["repeat"]):
            return found
        return {**found, "repeat": [f for f in found["repeat"] if not f.endswith(quoted)],
                "hygiene": found["hygiene"] + [f"{repeat_drop.HYGIENE_REPEAT_PREFIX}{sentence[:200]}'"]}

    monkeypatch.setattr(repeat_drop.hygiene, "find_issues", fake)


# ── 1. where a repeat is reported ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("twin", ["{first}.", "{first} tovelin."])
def test_an_exact_or_near_exact_repeat_is_f13_repeat_never_the_hygiene_warning(twin):
    text, first = _with_twin(twin)
    later = twin.format(first=first[:-1])
    found = hygiene.find_issues(text, _hyg())
    assert any(f.endswith(f"'{later}'") for f in found["repeat"])
    assert not any(f.startswith(repeat_drop.HYGIENE_REPEAT_PREFIX) for f in found["hygiene"])
    assert repeat_drop.candidates(text, _hyg()) == {later: qr.F13_REPEAT}


def test_a_shared_place_phrase_is_the_hygiene_warning():
    text = _place_phrase_blog()
    found = hygiene.find_issues(text, _hyg())
    assert found["repeat"] == []
    assert found["hygiene"] == [f"{repeat_drop.HYGIENE_REPEAT_PREFIX}{VALLEY}'"]


# ── 2. candidates ────────────────────────────────────────────────────────────────────────────────────────────────

def test_candidates_read_the_hygiene_repeat_warning_and_sources_narrow_them():
    text = _place_phrase_blog()
    assert repeat_drop.candidates(text, _hyg()) == {VALLEY: qr.F13_HYGIENE}
    assert repeat_drop.candidates(text, _hyg(), sources=(qr.F13_REPEAT,)) == {}
    assert repeat_drop.SOURCES == (qr.F13_REPEAT, qr.F13_HYGIENE)


def test_other_hygiene_warnings_are_not_candidates(monkeypatch):
    text = _place_phrase_blog()
    real = hygiene.find_issues

    def fake(t, ctx, **kw):
        found = real(t, ctx, **kw)
        return {**found, "hygiene": found["hygiene"] + [f"social proof ('loved by') the source does not state: "
                                                        f"'{RIDGE}'"]}

    monkeypatch.setattr(repeat_drop.hygiene, "find_issues", fake)
    assert repeat_drop.candidates(text, _hyg()) == {VALLEY: qr.F13_HYGIENE}


def test_a_sentence_both_gates_quote_counts_as_f13_repeat(monkeypatch):
    text, first = _with_twin("{first} tovelin.")
    later = first[:-1] + " tovelin."
    real = hygiene.find_issues

    def fake(t, ctx, **kw):
        found = real(t, ctx, **kw)
        return {**found, "hygiene": found["hygiene"] + [f"{repeat_drop.HYGIENE_REPEAT_PREFIX}{later}'"]}

    monkeypatch.setattr(repeat_drop.hygiene, "find_issues", fake)
    assert repeat_drop.candidates(text, _hyg()) == {later: qr.F13_REPEAT}


# ── 3. the hygiene feed ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_near_exact_repeat_quoted_only_by_f13_hygiene_is_dropped(monkeypatch):
    text, first = _with_twin("{first} tovelin.")
    later = first[:-1] + " tovelin."
    _hygiene_only(monkeypatch, later)
    assert repeat_drop.candidates(text, _hyg()) == {later: qr.F13_HYGIENE}
    trace: list = []
    out, log = repeat_drop.drop_repeats(text, _hyg(), trace=trace)
    assert later not in out and first in out
    assert lc.measure(out) >= lc.floor_chars()
    assert len(log) == 1 and log[0].startswith("dropped a near-exact repeat") and f"({qr.F13_HYGIENE};" in log[0]
    assert trace == [{"source": qr.F13_HYGIENE, "outcome": repeat_drop.DROPPED, "dice": trace[0]["dice"],
                      "chars_after": lc.measure(out)}]
    assert trace[0]["dice"] >= repeat_drop.near_exact_share()


def test_the_old_arm_leaves_a_hygiene_only_repeat(monkeypatch):
    text, first = _with_twin("{first} tovelin.")
    _hygiene_only(monkeypatch, first[:-1] + " tovelin.")
    assert repeat_drop.drop_repeats(text, _hyg(), sources=(qr.F13_REPEAT,)) == (text, [])


def test_a_hygiene_only_repeat_keeps_the_floor(monkeypatch):
    text, first = _with_twin("{first} tovelin.")
    _hygiene_only(monkeypatch, first[:-1] + " tovelin.")
    floor = lc.measure(text) - 10
    trace: list = []
    out, log = repeat_drop.drop_repeats(text, _hyg(), floor=floor, trace=trace)
    assert out == text and log[0].startswith("kept a near-exact repeat")
    assert [t["outcome"] for t in trace] == [repeat_drop.FLOOR]


def test_a_hygiene_only_repeat_obeys_the_setting_share(monkeypatch):
    text, first = _with_twin("{first} tovelin.")
    _hygiene_only(monkeypatch, first[:-1] + " tovelin.")
    monkeypatch.setattr(repeat_drop.settings, "get", lambda key, tenant_id=None: 1.0)   # exact copies only
    trace: list = []
    assert repeat_drop.drop_repeats(text, _hyg(), trace=trace) == (text, [])
    assert [t["outcome"] for t in trace] == [repeat_drop.NOT_NEAR_EXACT]


def test_a_shared_place_phrase_is_not_dropped_and_the_gate_still_warns():
    text = _place_phrase_blog()
    trace: list = []
    out, log = repeat_drop.drop_repeats(text, _hyg(), trace=trace)
    assert (out, log) == (text, [])
    assert trace == [{"source": qr.F13_HYGIENE, "outcome": repeat_drop.NOT_NEAR_EXACT, "dice": trace[0]["dice"],
                      "chars_after": None}]
    assert trace[0]["dice"] < repeat_drop.near_exact_share()
    assert hygiene.find_issues(out, _hyg())["hygiene"]          # the backstop: F13_hygiene still quotes it


# ── 4. trace ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_trace_holds_codes_and_numbers_never_text():
    text, first = _with_twin("{first} by Velmora.")     # a fact token (a name told nowhere else): refused
    trace: list = []
    out, _log = repeat_drop.drop_repeats(text, _hyg(), trace=trace)
    assert out == text
    assert [t["outcome"] for t in trace] == [repeat_drop.FACTS]
    for t in trace:
        assert set(t) == {"source", "outcome", "dice", "chars_after"}
        assert t["outcome"] in repeat_drop.OUTCOMES
        assert not any(isinstance(v, str) and len(v) > 20 for v in t.values())


def test_dice_is_none_under_four_copy_words_and_one_for_a_copy():
    first = _first_sentence(_blog())
    assert repeat_drop.dice(first, first) == 1.0
    assert repeat_drop.dice("Lake walk here.", "Lake walk here.") is None
