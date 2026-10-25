"""AA-790 follow-up — hygiene.sentence_spans in linear time.

sentence_spans (and _split_first_line) found sentence ends with
    [.!?…]+["'”’)*]*(?:\\s?TAG)*(?=\\s+\\S|\\s*$)
and finditer re-tried every start inside a mark run that a non-space follows: O(n^2) on '!' * n + 'x'
(~2.5 s at 10k chars). hygiene._sentence_end_matches is the one-pass scan that replaced it.

Two kinds of test, like test_aa912_codeql.py: (1) the scan gives the SAME matches as the old pattern
(copied here verbatim) on every text in the unit-test JSON fixtures, on the hygiene samples and on 30k
random lines built from the characters the pattern cares about; (2) a runtime bound on the adversarial
inputs (old code: seconds at 10k chars, minutes at 100k).
"""
import json
import random
import re
import time
from pathlib import Path

import pytest

from services.acp_content_writing import hygiene
from services.acp_shared.citation_tags import TAG

_OLD_SENT_END_RE = re.compile(rf"[.!?…]+[\"'”’)*]*(?:\s?{TAG})*(?=\s+\S|\s*$)")
BOUND_S = 1.0  # at 100k chars; the scan runs these in milliseconds
FIXTURES = Path(__file__).parent


def _elapsed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def _old_matches(line: str, pos: int = 0) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _OLD_SENT_END_RE.finditer(line, pos)]


def _new_matches(line: str, pos: int = 0) -> list[tuple[int, int]]:
    return list(hygiene._sentence_end_matches(line, pos))


def _fixture_texts() -> list[str]:
    out: list[str] = []

    def walk(o):
        if isinstance(o, str):
            if any(c in o for c in ".!?…"):
                out.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for f in sorted(FIXTURES.glob("**/*.json")):
        try:
            walk(json.loads(f.read_text(encoding="utf-8")))
        except ValueError:  # also a UnicodeDecodeError
            continue
    return out


_SAMPLES = [
    "Climb Mt. Fuji early. It is 4ft. Then the 1st. try.",
    "We met Dr. Smith in the U.S. and e.g. Paris. St. Petersburg.",
    "No. 5 is open. Is it hard? No. It is 4,000 ft. above the valley.",
    "A run of dots... and 12.5 km. End.",
    "Wow!! Really?! \"Yes.\" (Indeed.) **Day one.** We drive.",
    "Tagged. [F:fact_1] Next. [R:atom_83c773a60a][F:f-2] Then.[F:x] y",
    "x. [F:1]y and z.[F:1] [R:2]q and w.[F:1] [R:2] e",
    "Unclosed [F:abc. Next sentence! [R:] empty. [F:a]] double.",
    "Ends on marks!!!",
    "Ends on a tag. [F:1]",
    "Trailing space.   ",
    "Ellipsis… then more…",
    "Non-breaking\u00a0space.\u00a0Next.\u2003Em space.",
    "Tabs.\tNext.\tLast.",
    "",
    "!",
    ".x",
    "! x",
]


def test_sentence_end_scan_same_as_old_pattern_on_fixtures_and_samples():
    texts = _fixture_texts() + _SAMPLES
    assert len(texts) > 300  # the fixture corpus is really read
    lines = 0
    for text in texts:
        for line in text.split("\n"):
            lines += 1
            assert _new_matches(line) == _old_matches(line), line
            for pos in (1, 3, len(line) // 2):
                assert _new_matches(line, pos) == _old_matches(line, pos), (line, pos)
    assert lines > 500


_TOKENS = [".", "!", "?", "…", "\"", "'", "”", "’", ")", "*", " ", " ", "\t", "\u00a0", "a", "B", "1",
           "[F:1]", "[R:atom_1]", "[F:a.b]", "[R:", "]", "[", "[F:]", "Mt", "No", "ft", "U.S", "#Tag", "\x1c", "\u2028"]


def test_sentence_end_scan_same_as_old_pattern_on_random_lines():
    rng = random.Random(790)
    for _ in range(30_000):
        line = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(0, 24)))
        pos = rng.randint(0, len(line))
        assert _new_matches(line) == _old_matches(line), line
        assert _new_matches(line, pos) == _old_matches(line, pos), (line, pos)


def _old_sentence_spans(text: str) -> list[dict]:
    """sentence_spans as it was before the scan (the old regex in place of _sentence_end_matches)."""
    real = hygiene._sentence_end_matches
    hygiene._sentence_end_matches = _old_matches
    try:
        return hygiene.sentence_spans(text)
    finally:
        hygiene._sentence_end_matches = real


def test_sentence_spans_unchanged_on_fixtures():
    for text in _fixture_texts() + _SAMPLES + ["\n".join(_SAMPLES)]:
        assert hygiene.sentence_spans(text) == _old_sentence_spans(text)


def _old_split_first_line(text: str, first_line_max):
    """_split_first_line before the scan (the old regex search)."""
    if not first_line_max:
        return text, []
    text = text.lstrip()
    head, sep, rest = text.partition("\n")
    if len(head) <= first_line_max:
        return text, []
    m = _OLD_SENT_END_RE.search(head)
    if not m or m.end() > first_line_max or m.end() >= len(head.rstrip()):
        return text, []
    first, remainder = head[:m.end()].rstrip(), head[m.end():].strip()
    return f"{first}\n\n{remainder}{sep}{rest}", ["split the first line after its first sentence"]


def test_split_first_line_same_as_old():
    for text in _fixture_texts() + _SAMPLES:
        for limit in (None, 20, 60, 150):
            assert hygiene._split_first_line(text, limit) == _old_split_first_line(text, limit), (text, limit)


@pytest.mark.parametrize("adv", [
    "!" * 100_000 + "x",
    "." * 100_000 + "x",
    "?!…" * 33_000 + "x",
    "!)" * 50_000 + "x",
    "x." + "[F:1]" * 20_000 + "y",
    "x. " + "[F:1] " * 16_000 + "y",
    ("!" * 50 + "x") * 2_000,
    "a!" * 50_000,
    "a. " * 33_000,
    "! " * 50_000,
])
def test_sentence_spans_is_linear(adv):
    assert _elapsed(lambda: hygiene.sentence_spans(adv)) < BOUND_S
    assert _elapsed(lambda: hygiene.sentence_spans("Intro line.\n- " + adv + "\nA: " + adv)) < 2 * BOUND_S


def test_split_first_line_is_linear():
    assert _elapsed(lambda: hygiene._split_first_line("!" * 100_000 + "x more text", 150)) < BOUND_S
