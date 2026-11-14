"""AA-1077 (S279) — the blog repeat drop (services/acp_content_writing/repeat_drop.py).

1. `near_exact`: exact and one-word-changed copies match; a paraphrase and a short line do not.
2. `drop_repeats`: a near-exact repeat the final pass keeps (it adds one word) is dropped before F13 when the blog
   stays at or above the floor; kept (F13 holds as today) when the drop would leave it under the floor, when the
   changed word is a fact (a proper name told nowhere else), or when it is a paraphrase.
3. The switch (`T9_REPEAT_DROP`) and the T9 hook (blog only, after the length trim, before the gates).

Invented words and places only (generated syllables, no stored text).
"""
from __future__ import annotations

import inspect
import itertools

import pytest

from services.acp_content_writing import hygiene, repeat_drop
from services.acp_content_writing import length_contract as lc

BRAND = "Brennick Trails"
CTA = "Plan your trip with Brennick Trails today."
_SYL = ("ka", "lo", "mi", "re", "su", "ta", "ve", "zo", "ni", "pa", "ru", "de")


def _words():
    for a, b, c in itertools.product(_SYL, _SYL, _SYL):
        yield f"{a}{b}{c}n"


def _blog(sections: int = 6, per: int = 7) -> list[str]:
    """Paragraphs of a long invented blog: '## …' headings and prose whose sentences share no content word."""
    gen = _words()
    names = ("one", "two", "three", "four", "five", "six", "seven", "eight")
    paras = []
    for h in range(sections):
        paras.append(f"## Section {names[h]}")
        sents = []
        for _ in range(per):
            w = [next(gen) for _ in range(10)]
            sents.append(f"The {w[0]} {w[1]} rises over the {w[2]} {w[3]} where walkers {w[4]} the {w[5]} {w[6]} "
                         f"near {w[7]} {w[8]} {w[9]}.")
        paras.append(" ".join(sents))
    return paras


def _hyg() -> hygiene.HygieneContext:
    return hygiene.HygieneContext(seed="A quiet valley walk.", brand_name=BRAND, cta=CTA)


def _first_sentence(paras: list[str]) -> str:
    return paras[1].split(". ")[0] + "."


def _with_twin(twin: str, paras=None) -> tuple[str, str]:
    paras = list(paras or _blog())
    first = _first_sentence(paras)
    paras[7] = paras[7] + " " + twin.format(first=first[:-1])
    return "\n\n".join(paras) + "\n\n" + CTA, first


# ── 1. near_exact ────────────────────────────────────────────────────────────────────────────────────────────────

def test_near_exact_matches_exact_and_one_changed_word():
    first = _first_sentence(_blog())
    assert repeat_drop.near_exact(first, first)
    one_changed = first.rsplit(" ", 1)[0] + " tovelin."
    assert repeat_drop.near_exact(one_changed, first)


def test_near_exact_refuses_a_paraphrase_and_a_short_line():
    first = _first_sentence(_blog())
    w = first[:-1].split()
    paraphrase = " ".join(w[:6] + ["tovelin", "marrows", "quillet", "brasken"] + w[10:]) + "."
    assert not repeat_drop.near_exact(paraphrase, first)
    assert not repeat_drop.near_exact("Lake walk here.", "Lake walk here.")      # under 4 copy words


# ── 2. drop_repeats ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_near_exact_repeat_dedupe_keeps_is_dropped_above_the_floor():
    text, first = _with_twin("{first} tovelin.")
    twin = first[:-1] + " tovelin."
    hyg = _hyg()
    assert any(twin[:60] in f for f in hygiene.find_issues(text, hyg)["repeat"])        # F13 would hold it
    assert hygiene.dedupe(text, hygiene.brand_words(BRAND), CTA, hyg.source_text)[1] == []   # the final pass keeps it
    out, log = repeat_drop.drop_repeats(text, hyg)
    assert twin not in out and first in out
    assert hygiene.find_issues(out, hyg)["repeat"] == []
    assert lc.measure(out) >= lc.floor_chars()
    assert len(log) == 1 and log[0].startswith("dropped a near-exact repeat")


def test_an_exact_repeat_is_dropped_too():
    text, first = _with_twin("{first}.")
    out, log = repeat_drop.drop_repeats(text, _hyg())
    assert out.count(first) == 1 and len(log) == 1


def test_a_drop_that_leaves_the_blog_under_the_floor_is_not_made():
    text, first = _with_twin("{first} tovelin.")
    floor = lc.measure(text) - 10                 # the twin is ~120 chars: dropping it goes under this floor
    out, log = repeat_drop.drop_repeats(text, _hyg(), floor=floor)
    assert out == text
    assert len(log) == 1 and log[0].startswith("kept a near-exact repeat") and f"{floor:,}" in log[0]


def test_the_real_floor_holds_a_short_blog_as_today():
    paras = _blog(sections=5, per=6)              # ~3,800 chars: under the F4 floor already
    text, _first = _with_twin("{first} tovelin.", paras)
    assert lc.measure(text) < lc.floor_chars()
    out, log = repeat_drop.drop_repeats(text, _hyg())
    assert out == text and log and log[0].startswith("kept a near-exact repeat")


def test_a_changed_word_that_is_a_fact_keeps_the_sentence():
    text, first = _with_twin("{first} beside Velmora Quay.")   # a proper name told nowhere else: a fact token
    hyg = _hyg()
    assert hygiene.find_issues(text, hyg)["repeat"]
    out, log = repeat_drop.drop_repeats(text, hyg)
    assert out == text and log == []


def test_a_paraphrase_stays_for_f13():
    paras = _blog()
    w = _first_sentence(paras)[:-1].split()
    paraphrase = " ".join(w[:6] + ["tovelin", "marrows", "quillet", "brasken"] + w[10:]) + "."
    paras[7] = paras[7] + " " + paraphrase
    text = "\n\n".join(paras) + "\n\n" + CTA
    out, log = repeat_drop.drop_repeats(text, _hyg())
    assert out == text and log == []


def test_a_clean_blog_is_left_alone():
    text = "\n\n".join(_blog()) + "\n\n" + CTA
    assert repeat_drop.drop_repeats(text, _hyg()) == (text, [])
    assert repeat_drop.drop_repeats("", _hyg()) == ("", [])


# ── 3. switch and hook ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [(None, True), ("", True), ("on", True), ("off", False), ("0", False),
                                          ("FALSE", False), ("no", False)])
def test_switch(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(repeat_drop.MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(repeat_drop.MODE_ENV, raw)
    assert repeat_drop.enabled() is expected


def test_t9_runs_it_on_blogs_after_the_trim_and_before_the_gates():
    from services.acp_content_writing import service
    src = inspect.getsource(service.run_write_background)
    trim, drop, gates = (src.index('step_guard("length_trim"'), src.index("repeat_drop.drop_repeats("),
                         src.index("gates, content_text=content_text"))   # S280: the piece's run_quality_gates partial
    assert trim < drop < gates
    assert 'if channel == "blog" and repeat_drop.enabled():' in src[trim:drop]
    assert 'step_guard("repeat_drop"' in src[drop:gates]


def test_the_share_is_the_setting(monkeypatch):
    # S279 thresholds L1: moved from thresholds.yaml to the setting cis.t9.repeat_drop_near_exact (evidence required)
    from shared import settings
    d = settings.definition(repeat_drop.NEAR_EXACT_SETTING)
    assert repeat_drop.NEAR_EXACT_SHARE == d.default_value == repeat_drop.near_exact_share() == 0.9
    assert d.evidence_required and d.min_value == 0.8 and d.max_value == 1.0
    monkeypatch.setattr(repeat_drop.settings, "get", lambda key, tenant_id=None: 1.0)
    assert repeat_drop.near_exact_share() == 1.0
