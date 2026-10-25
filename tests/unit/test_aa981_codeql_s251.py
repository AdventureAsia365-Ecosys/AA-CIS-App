"""AA-981 — CodeQL alerts opened by S251 on main: five py/polynomial-redos and one py/stack-trace-exposure.

Like test_aa912_codeql.py / test_aa790_sentence_spans_linear.py, each regex rewrite has two tests: (1) the new scan
gives the SAME result as the old pattern (copied here verbatim as `_OLD_*`) on every text in the unit-test JSON
fixtures, on hand-written samples and on random strings built from the characters the pattern cares about; (2) a
runtime bound on the adversarial input from the alert's message. The 404 fix asserts the exception text never
reaches the response while the code, status and fixed message stay.

  hygiene.is_sentence_end         \\s*[a-z0-9] / \\s*\\d after a unit / "No." abbreviation  -> _next_non_space
  repair_contract._sentence_initial   (?:^|[.!?:;]\\s+|\\n\\s*(?:[-*•]\\s*)?|…)$ search       -> backward scan
  masking._is_sentence_start          (?:^|[.!?:;]\\s+|\\n\\s*|["“(]\\s*)$ search             -> backward scan
  masking.mask (URLs)                 https?://\\S+|www\\.\\S+|\\S+@\\S+\\.\\w+ sub                -> _url_spans
  lint.constraint_only                re.split on …|\\s+(?:and\\s+then|then)\\s+              -> _clauses
"""
import json
import random
import re
import time
from pathlib import Path

import pytest

from services.acp_content_writing import hygiene
from services.quality import repair_contract as rc
from services.writing_memory import lint, masking

BOUND_S = 1.0  # at >= 100k chars; the new code runs these in milliseconds
FIXTURES = Path(__file__).parent

_OLD_NEXT_RUNS_ON_RE = re.compile(r"\s*[a-z0-9]")
_OLD_NEXT_IS_NUMBER_RE = re.compile(r"\s*\d")
_OLD_SENT_START_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*(?:[-*•]\s*)?|\*\*\s*|#+\s*|\"\s*|“\s*)$")
_OLD_SENT_END_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*|[\"“(]\s*)$")
_OLD_URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\w+")
_OLD_CLAUSE_SPLIT_RE = re.compile(r"[.;:!?]|,\s*(?:and|but|or)\s+|\s+(?:and\s+then|then)\s+", re.I)


def _elapsed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def _fixture_texts() -> list[str]:
    out: list[str] = []

    def walk(o):
        if isinstance(o, str):
            if len(o) > 2:
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
    "Climb Mt. Fuji early. It is 4 ft. above the valley. The pass is 4 ft. The trail is steep.",
    "Is it hard? No. 5 is open. No. 7 too. No. Really.",
    "# Day 1\n\n- Hanoi: Old Quarter\n* Ha Long Bay\n• Sapa\n**Highlights** Hoi An\n## Notes\n\"Hue\" and “Da Nang”",
    "Visit https://example.com/a?b=c or www.example.org today; mail info@example.com, a@b.c!d, x@@y..z.",
    "Lunch: Pho. (Kyoto) Then: Osaka; then Nara!  \n\n  Tokyo\n-   Fuji\n\t*\tKobe",
    "Cut the intro, and then trim the list. Do not mention lunch then add a map; never promise, but include views.",
    "  Leading spaces, then words.\n\n",
    "",
    "\n",
    "@",
    "a@b",
    "http://",
    "https://x",
    "wwww.x",
]


def _texts(max_len: int = 3_000) -> list[str]:
    texts = [t for t in _fixture_texts() if len(t) <= max_len] + _SAMPLES
    assert len(texts) > 300  # the fixture corpus is really read
    return texts


# ---------------------------------------------------------------- hygiene.is_sentence_end (AA-790)

def test_next_non_space_same_as_old_patterns():
    rng = random.Random(981)
    alphabet = [" ", "\t", "\n", " ", " ", "\x1c", "a", "Z", "5", "٥", "²", ".", "x", "é"]
    cases = [t for t in _texts()] + ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
                                     for _ in range(20_000)]
    for text in cases:
        for j in range(len(text) + 1):
            nxt = hygiene._next_non_space(text, j)
            assert (nxt in hygiene._RUNS_ON_CHARS) == bool(_OLD_NEXT_RUNS_ON_RE.match(text, j)), (text, j)
            assert nxt.isdecimal() == bool(_OLD_NEXT_IS_NUMBER_RE.match(text, j)), (text, j)


@pytest.mark.parametrize("adv", [
    "It is 4 ft." + " " * 100_000 + "a",
    "Gate No." + " " * 100_000 + "5",
    "ft. " * 33_000,
    ("4 km." + " " * 50) * 2_000 + "x",
])
def test_is_sentence_end_after_units_is_linear(adv):
    marks = [i for i, c in enumerate(adv) if c == "."]
    assert _elapsed(lambda: [hygiene.is_sentence_end(adv, i) for i in marks]) < BOUND_S
    assert _elapsed(lambda: hygiene.sentence_spans(adv)) < BOUND_S


# ---------------------------------------------------------------- repair_contract._sentence_initial

_START_TOKENS = [" ", "  ", "\t", "\n", " ", ".", "!", "?", ":", ";", "-", "*", "**", "•", "#", "##", "\"", "“",
                 "(", "a", "Kyoto", "x"]


def _random_texts(tokens, n, seed):
    rng = random.Random(seed)
    return ["".join(rng.choice(tokens) for _ in range(rng.randint(0, 14))) for _ in range(n)]


def test_sentence_initial_same_as_old_pattern():
    for text in _texts() + _random_texts(_START_TOKENS, 20_000, 9811):
        step = max(1, len(text) // 200)
        for idx in list(range(0, len(text) + 1, step)) + [len(text)]:
            assert rc._sentence_initial(text, idx) == bool(_OLD_SENT_START_RE.search(text[:idx])), (text, idx)


def test_new_names_unchanged_on_fixtures(monkeypatch):
    texts = _texts(max_len=1_500)
    samples = [rc.new_names(texts[i - 1], t) for i, t in enumerate(texts)]
    monkeypatch.setattr(rc, "_sentence_initial", lambda text, idx: bool(_OLD_SENT_START_RE.search(text[:idx])))
    assert samples == [rc.new_names(texts[i - 1], t) for i, t in enumerate(texts)]
    assert any(samples)


@pytest.mark.parametrize("adv", [
    "\n" * 100_000 + "Kyoto",
    "#" * 100_000 + " Kyoto",
    "a" + " " * 100_000 + "Kyoto",
    ("\n" * 20 + "x Osaka ") * 2_000,
])
def test_sentence_initial_is_linear(adv):
    assert _elapsed(lambda: rc._sentence_initial(adv, len(adv) - 1)) < BOUND_S
    small = adv[:20_000]
    assert _elapsed(lambda: rc.new_names("", small)) < BOUND_S


# ---------------------------------------------------------------- masking (AA-917)

def test_is_sentence_start_same_as_old_pattern():
    for text in _texts() + _random_texts(_START_TOKENS, 20_000, 9812):
        step = max(1, len(text) // 200)
        for idx in list(range(0, len(text) + 1, step)) + [len(text)]:
            assert masking._is_sentence_start(text, idx) == bool(_OLD_SENT_END_RE.search(text[:idx])), (text, idx)


_URL_TOKENS = ["http", "https", "://", "www", ".", "@", "!", "_", "a", "Z", "9", "é", "-", "/", " ", "\n", " ",
               "w", "h", "s:", "co", "x@y.z", ".w", "Z@"]


def test_url_spans_same_as_old_pattern():
    for text in _texts(max_len=20_000) + _random_texts(_URL_TOKENS, 40_000, 9813):
        assert masking._url_spans(text) == [(m.start(), m.end()) for m in _OLD_URL_RE.finditer(text)], text
        assert masking._mask_urls(text) == _OLD_URL_RE.sub("[URL]", text)


def test_mask_unchanged_on_fixtures(monkeypatch):
    texts = _texts(max_len=3_000)
    masked = [masking.mask(t) for t in texts]
    monkeypatch.setattr(masking, "_mask_urls", lambda text: _OLD_URL_RE.sub("[URL]", text))
    monkeypatch.setattr(masking, "_is_sentence_start", lambda text, idx: bool(_OLD_SENT_END_RE.search(text[:idx])))
    assert masked == [masking.mask(t) for t in texts]
    assert masking.mask("Book at https://aa.example/x or mail a.b@c.co today.") == "Book at [URL] or mail [URL] today."


@pytest.mark.parametrize("adv", [
    "!" * 100_000,
    "!@" * 50_000,
    "!" * 100_000 + "@x.y",
    "a@" * 50_000 + "b.c",
    "\n" * 100_000 + "Kyoto",
    ("x@y.z " + "www.a " + "!@" * 20) * 1_000,
])
def test_mask_is_linear(adv):
    assert _elapsed(lambda: masking.mask(adv)) < BOUND_S


# ---------------------------------------------------------------- lint (AA-917)

_CLAUSE_TOKENS = [" ", "   ", "\t", "\n", ",", ".", ";", ":", "!", "?", "and", "AND", "then", "Then", "but", "or",
                  "x", "do not", "mention"]


def test_clauses_same_as_old_split():
    for text in _texts(max_len=20_000) + _random_texts(_CLAUSE_TOKENS, 40_000, 9814):
        assert lint._clauses(text) == _OLD_CLAUSE_SPLIT_RE.split(text), text


def test_constraint_only_unchanged_on_samples():
    assert lint.constraint_only("Cut the intro, and then trim it.")["ok"]
    assert lint.constraint_only("Do not mention lunch then never promise views.")["ok"]
    bad = lint.constraint_only("Trim the intro   then   mention the free pick-up.")
    assert not bad["ok"] and "mention" in bad["detail"]


@pytest.mark.parametrize("adv", [
    "x" + " " * 100_000 + "y",
    " " * 100_000,
    "and" + " " * 100_000 + "x",
    (" " * 50 + "then") * 2_000,
])
def test_constraint_only_is_linear(adv):
    assert _elapsed(lambda: lint.constraint_only(adv)) < BOUND_S
