"""AA-912 — CodeQL backlog: linear-time regexes (py/redos, py/polynomial-redos) and generic error
bodies (py/stack-trace-exposure).

Each rewritten regex has two tests: (1) the new code gives the SAME result as the old pattern
(copied here verbatim as `_OLD_*`) on representative inputs, and (2) a runtime bound on the
adversarial input built from the alert's message (old code: seconds to hours at these sizes).
Each stack-trace fix asserts the exception text (a fake ARN / SQL / token) never reaches the
response, the response carries a ref, the keys and status code are unchanged, and the ref is
logged server side with the traceback.
"""
import json
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from services.acp_content_writing import generate
from services.acp_content_writing import hygiene
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import text_integrity as ti
from services.acp_content_writing import trip_facts as tf
from services.acp_shared import citation_tags as ct
from services.acp_shared import grounding
from services.photos import match as photo_match

BOUND_S = 1.0  # worst case at >= 100k chars; the new code runs these in milliseconds
FAKE_ARN = "arn:aws:iam::123456789012:role/FAKE-Task-Role"


def _elapsed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


# ---------------------------------------------------------------- hygiene.py

_OLD_HASHTAG_TAIL_RE = re.compile(r"(?:\n[ \t]*(?:#[A-Za-z][A-Za-z0-9]+[\s,]*)+[ \t]*)+\s*$")
_OLD_WORD_BEFORE_DOT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z.]+$")
_OLD_DASH_RE = re.compile("[ \t]*\u2014[ \t]*|[ \t]+\u2013[ \t]+")
_OLD_TAG = r"\[(?:R|F):[^\]]+\]"

_HASHTAG_SAMPLES = [
    "Intro.\n#Japan #Travel,\n\n#Hiking \n",
    "Intro.\n#Japan\nBook now.",
    "Day one in Paro.\n\n#Bhutan #Paro #TigersNest",
    "Line\n  #Mongolia, #Gobi\n#Steppe\n\n",
    "No tags here at all.",
    "Ends with a tag #Inline",
    "Text\n#A\n#Bb",  # a one-letter tag is not a tag (needs 2+ chars)
    "Text\n#2024 #Trip",
    "Talk to us.\n\n#Vietnam #HaLongBay #Cruise\n#Asia",
    "",
    "\n#Only",
]


def test_hashtag_tail_same_as_old_pattern():
    for s in _HASHTAG_SAMPLES:
        old = _OLD_HASHTAG_TAIL_RE.search(s)
        assert hygiene._hashtag_tail_start(s) == (old.start() if old else None), s
    assert hygiene._hashtag_tail_start("Intro.\n#Japan #Travel,\n\n#Hiking \n") == 6
    assert hygiene._hashtag_tail_start("Intro.\n#Japan\nBook now.") is None


def test_hashtag_tail_is_linear():  # alert 26 (exponential: '\n#A0' x 24 + '!' took 3.4 s)
    assert _elapsed(lambda: hygiene.ensure_cta("Intro." + "\n#A0" * 40 + "!", "Talk to Atlas Travel.")) < BOUND_S
    assert _elapsed(lambda: hygiene._hashtag_tail_start("x" + "\n#A0" * 250_000 + "!")) < BOUND_S
    assert _elapsed(lambda: hygiene._hashtag_tail_start("x" + "\n#A0 " * 50_000 + "!")) < BOUND_S


_DOT_SAMPLES = [
    "Climb Mt. Fuji early. It is 4ft. Then the 1st. try.",
    "We met Dr. Smith in the U.S. and e.g. Paris.\nNext line. St. Petersburg.",
    "No. 5 is open. Is it hard? No. It is 4,000 ft. above the valley.",
    "A run of dots... and 12.5 km. End.\n.",
    "abc.def. 21st. x.y.z. 1a.b. ",
]


def test_word_before_dot_same_as_old_pattern():
    for s in _DOT_SAMPLES:
        for i in range(len(s) + 1):
            old = _OLD_WORD_BEFORE_DOT_RE.search(s, 0, i)
            assert hygiene._word_before_dot(s, i) == (old.group(0) if old else ""), (s, i)
    assert not hygiene.is_sentence_end("Climb Mt. Fuji", 8)
    assert hygiene.is_sentence_end("It is 4ft. Then", 9)


def test_sentence_end_word_scan_is_linear():  # alert 10 (quadratic)
    assert _elapsed(lambda: hygiene.is_sentence_end("." * 200_000 + ",. ", 200_001)) < BOUND_S
    # 210k chars on one line: every period re-searched from offset 0 before (~10 s)
    assert _elapsed(lambda: hygiene.sentence_spans("The trail climbs to the pass. " * 7_000)) < BOUND_S


_DASH_SAMPLES = [
    "a \u2014 \t\u2014 b \u2013 c",
    "Paro\u2014Thimphu, 1637\u20131638, 3 \u2013 5 days",
    "The pass \u2014 at 4,000 m \u2014 is cold.\t\u2014\tYes.",
    "x  \u2013  y \u2013z a\u2013 b \u2014\u2014 c",
    "no dashes at all - just hyphens",
]


def test_dash_re_same_spans_as_old():
    for s in _DASH_SAMPLES:
        assert [m.span() for m in hygiene._DASH_RE.finditer(s)] == [m.span() for m in _OLD_DASH_RE.finditer(s)], s
    assert [m.span() for m in hygiene._DASH_RE.finditer("a \u2014 \t\u2014 b \u2013 c")] == [(1, 5), (5, 7), (8, 11)]


def test_dash_re_is_linear():  # alert 16 (quadratic on a long blank run)
    assert _elapsed(lambda: hygiene.fix_dashes("Start \u2014a" + "\t" * 200_000 + "b.")) < BOUND_S
    assert _elapsed(lambda: list(hygiene._DASH_RE.finditer(" " * 1_000_000 + "\u2013x"))) < BOUND_S


def test_tag_after_dot_same_on_real_tags():
    old = re.compile(_OLD_TAG)
    for s in ["[R:atom_83c773a60a] Next", "[F:fact-12][R:a]", "[R:]", "[R:a b] x", "[F:x", "no tag"]:
        o, n = old.match(s), hygiene._TAG_AT_RE.match(s)
        assert (o and o.span()) == (n and n.span()), s
    assert hygiene.is_sentence_end("It ends.[R:atom_1] Next", 7)


def test_sentence_end_and_strip_tags_agree_on_malformed_tags():  # review: one tag definition in hygiene
    # a "tag" spanning a newline or holding '[' is no tag for either reader
    for text, i in [("It ends here.[R:a\nb] Next one.", 12), ("It ends here.[R:a1 and [F:f2] goes on.", 12)]:
        assert not hygiene.is_sentence_end(text, i)
        assert "[R:" not in hygiene.strip_tags(text)
    assert hygiene.strip_tags("It ends here.[R:a1 and [F:f2] goes on.") == "It ends here. and goes on."
    assert hygiene._TAG == ct.TAG and hygiene._TAG_AT_RE.pattern == ct.TAG


def test_tag_after_dot_scan_is_bounded():  # alert 9 (quadratic through sentence_tail_end)
    assert _elapsed(lambda: hygiene.sentence_tail_end(".[F:" * 250_000, 0)) < BOUND_S


# ---------------------------------------------------------------- sentence_repair.py

_OLD_FAQ_MARKUP_RE = re.compile(r"^\**\s*Q:\s*|\*+$")
_OLD_SOURCE_LABEL_RE = re.compile(r"\s?\[(?:Moment|Fact) id=[^\]]*\]")
# well-formed tags only: every reader gives the pre-AA-912 result on these
_TAG_SAMPLES = [
    "Lake [R:3f2a-11] is cold [F:9].",
    "[R:a][F:b] tail",
    "Punakha. [R:atom_1] Ask [F:f2][R:a] us.",
    "x [R:a][F:b] y",
    "Stray [ bracket ] and [R:ok] and more",
    "We visit **Tiger's Nest** [R:abc] [Moment id=m1].",
    "Two ids [R:atom_1, atom_2] here.\nNext line [F:fact_9]",
]


def test_sentence_repair_tag_same_as_old():  # alert 20
    for s in _TAG_SAMPLES:
        assert re.sub(sr._TAG, " ", s) == re.sub(_OLD_TAG, " ", s), s
        assert " ".join(ct.strip_citation_tags(s, " ").split()) == " ".join(re.sub(_OLD_TAG, " ", s).split()), s
    assert sr._clean_quote("Lake [R:3f2a-11] is cold [F:9].") == "Lake is cold ."


def test_sentence_repair_tag_is_linear():
    s = "[R:" + "[F:" * 100_000
    assert _elapsed(lambda: re.sub(sr._TAG, " ", s)) < BOUND_S
    assert _elapsed(lambda: sr._clean_quote(s)) < BOUND_S


def test_faq_markup_same_as_old():  # alert 21
    for s in ["**Q: Is it busy?**", "Q: Why go?", "Mid **bold** end**", "***", "**Q:**", "plain", "a*b*"]:
        assert sr._FAQ_MARKUP_RE.sub("", s) == _OLD_FAQ_MARKUP_RE.sub("", s), s
    assert sr._FAQ_MARKUP_RE.sub("", "**Q: Is it busy?**") == "Is it busy?"


def test_faq_markup_is_linear():
    s = "x" + "*" * 100_000 + "x"  # was ~16 s
    assert _elapsed(lambda: sr._FAQ_MARKUP_RE.sub("", s)) < BOUND_S


def test_source_label_same_as_old():  # alert 22
    for s in ["A [Fact id=f12] and [Moment id=m-3] label.", "[Fact id=]", *_TAG_SAMPLES]:
        assert sr._SOURCE_LABEL_RE.sub("", s) == _OLD_SOURCE_LABEL_RE.sub("", s), s
    assert sr._SOURCE_LABEL_RE.sub("", "A [Fact id=f12] and [Moment id=m-3] label.") == "A and label."
    # malformed (review): an unclosed label loses only itself and its id, never the prose after it
    assert sr._SOURCE_LABEL_RE.sub("", "No label [Fact id=open") == "No label"
    assert sr._SOURCE_LABEL_RE.sub("", "Camp [Moment id=m1 by the lake [Fact id=f2].") == "Camp by the lake."


def test_source_label_is_linear():
    assert _elapsed(lambda: sr._SOURCE_LABEL_RE.sub("", " [Fact id=" * 20_000)) < BOUND_S
    assert _elapsed(lambda: sr.sanitize_replacement("[Fact id=" * 20_000, None)) < BOUND_S


# ---------------------------------------------------------------- quality_gates / text_integrity / grounding

_OLD_STRIP_TAG_RE = re.compile(r"\s?\[(?:R|F):[^\]]+\]")


def test_strip_citation_tags_same_as_old():  # alert 17
    for s in _TAG_SAMPLES:
        assert qg.strip_citation_tags(s) == _OLD_STRIP_TAG_RE.sub("", s), s
        assert hygiene.strip_tags(s) == _OLD_STRIP_TAG_RE.sub("", s), s
    assert qg.strip_citation_tags("Punakha. [R:atom_1] Ask [F:f2][R:a] us.") == "Punakha. Ask us."


def test_strip_citation_tags_is_linear():
    for s in ("[R:" + "[F:" * 100_000, "[R:" + "a" * 1_000_000, "[R:a " * 200_000, " [R:" * 300_000):
        assert _elapsed(lambda s=s: qg.strip_citation_tags(s)) < BOUND_S


# Malformed tags (review finding, medium). Pre-AA-912 the strip deleted everything from an unclosed
# '[R:' to the next ']' (real prose); the first AA-912 cut left '[R:abc' visible. Now: only the opener and
# the id right after it go, the prose stays, and no '[R:' / '[F:' reaches the tenant.
_MALFORMED = [
    ("The lake freezes in winter [R:a1. Visit in July [F:f2] for festivals.",
     "The lake freezes in winter. Visit in July for festivals."),
    ("Lake views [R:a1 all day [F:f2] from camp.", "Lake views all day from camp."),
    ("Walk to the lake [R:atom_1 [F:fact_2] then rest.", "Walk to the lake then rest."),
    ("Ends on an open tag [F:fact_9", "Ends on an open tag"),
    ("An empty tag [R:] stays out.", "An empty tag stays out."),
    ("Stray [ bracket ] and [R:ok] and [F:open", "Stray [ bracket ] and and"),
    # a tag never spans a line: the opener goes, the next line is kept
    ("Paro sits high [R:a1\nThe dzong is old.]", "Paro sits high\nThe dzong is old.]"),
]


def test_malformed_tags_keep_prose_and_leave_no_fragment():
    for raw, want in _MALFORMED:
        for out in (qg.strip_citation_tags(raw), hygiene.strip_tags(raw), ct.strip_citation_tags(raw)):
            assert out == want, raw
            assert "[R:" not in out and "[F:" not in out, raw
        assert sr.sanitize_replacement(raw, None) == " ".join(want.split()), raw
        assert qg.deep_strip_citation_tags({"v": [raw]}) == {"v": [want]}


def test_dangling_tag_is_not_an_unbalanced_bracket():
    # text_integrity reads the paragraph as the tenant will see it (after the final strip); a real stray
    # bracket of the prose is still reported
    assert not ti._unbalanced("Lake views [R:a1 all day [F:f2] from camp.")
    assert not ti._unbalanced("Ends on an open tag [F:fact_9")
    assert ti._unbalanced("Lake views [all day from camp.")


def test_drop_tags_is_the_shared_strip():  # alert 24
    for s in ["Visit Erdene Zuu [R:f-01]. Then [F:atom 7] rest.", "no tags here", "[R:]", "[R:a [F:b]",
              "a] [R:x", "[R:x] tail [F:", "", *_TAG_SAMPLES, *(r for r, _ in _MALFORMED)]:
        assert ti._drop_tags(s) == ct.strip_citation_tags(s), s


def test_unbalanced_is_linear_on_unclosed_tags():
    adv = "[R:" + "[F:" * 100_000  # ~300k chars; the old sub took ~9 s
    assert _elapsed(lambda: ti._unbalanced(adv)) < BOUND_S


_OLD_CITE_RE = re.compile(r"\[(?:R|F):[^\]]*\]")


def test_cite_strip_same_as_old():  # alert 8
    for s in [*_TAG_SAMPLES, "Walk 12 km [R:atom_1][F:fact_9].", "Day 3: 4,460 m [F:]", "From 1637 [R:a]-38 on"]:
        for yf in (False, True):
            want = grounding.find_novel_numeric_claims(_OLD_CITE_RE.sub("", s), ["2"], year_forms=yf)
            assert grounding.find_novel_numeric_claims(s, ["2"], year_forms=yf) == want, s
    assert grounding.find_novel_numeric_claims("Walk 12 km [R:atom_1][F:fact_9].", ["12 km"]) == []


def test_cite_strip_on_dangling_tag_checks_the_tenant_text():
    # the number after an unclosed tag is prose the tenant sees: F1 must check it (the old strip hid it)
    assert grounding.find_novel_numeric_claims("The trek is [R:a1 12 km long [F:f2].", ["the trek"]) == ["12"]
    # an id that looks like a number is never read as a claim
    assert grounding.find_novel_numeric_claims("The trek is long [R:12", ["the trek"]) == []


def test_cite_strip_is_linear():
    s = "[R:" + "[F:" * 100_000
    assert _elapsed(lambda: grounding.find_novel_numeric_claims(s, [])) < BOUND_S


# ---------------------------------------------------------------- one tag definition, trailing tags

_OLD_TRAILING_TAGS_RE = re.compile(rf"(?:\s?{_OLD_TAG})+\s*$")
_TRAILING_SAMPLES = [
    "Visit Paro. [R:atom_1]", "Visit Paro.[R:a][F:b] \n", "Visit Paro. [R:a]  [F:b]", "No tags.", "",
    "[R:a]", "x\n\n[R:a]", "Mid [R:a] text.", "Ends ] [R:a]", "Bad [R:a]b]", "Two [R:a1, a2]\t",
]


def test_trailing_tags_same_as_old_regex():
    for s in _TRAILING_SAMPLES:
        old = _OLD_TRAILING_TAGS_RE.search(s)
        assert ct.trailing_tags(s) == (old.group(0) if old else ""), s
    assert ct.trailing_tags("Visit Paro. [R:a]  [F:b]") == " [F:b]"


def test_trailing_tags_is_linear():  # sentence_repair.splice / trip_facts thin coda (was ~11 s at 100k chars)
    for s in ("[R:a]" * 20_000 + "x", "[R:a] " * 20_000, "x]" * 100_000, "[R:" + "a" * 200_000 + "]"):
        assert _elapsed(lambda s=s: ct.trailing_tags(s)) < BOUND_S


def test_tag_patterns_have_one_definition():
    """Every reader of a citation tag uses services/acp_shared/citation_tags.py (review: four bodies drifted)."""
    from pathlib import Path
    assert sr._TAG == ct.TAG and qg.TAG_RE is ct.TAG_RE
    root = Path(__file__).resolve().parents[2]
    files = [*sorted((root / "services/acp_content_writing").glob("*.py")), root / "services/acp_shared/grounding.py"]
    for f in files:
        assert not re.search(r"\(\?:R\|F\):|\[RF\]:", f.read_text()), f"{f.name} defines its own citation-tag regex"


# ---------------------------------------------------------------- generate.py

_OLD_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def test_md_link_same_as_old():  # alert 7
    for s in ["See [Paro](https://x.com/a) **now**", "[a](http://b.c/d?e=f) and [g](https://h.i)",
              "[no url](ftp://x)", "[x](https://a b)", "[[nested]](https://x.com)", "plain",
              "Book here: [Bhutan tours](https://example.com/tours?country[]=bt)"]:
        assert generate._MD_LINK_RE.sub(r"\1 \2", s) == _OLD_MD_LINK_RE.sub(r"\1 \2", s), s
    assert generate.strip_markdown("See [Paro](https://x.com/a) **now**") == "See Paro https://x.com/a now"


def test_md_link_accepted_differences():
    # review (low): a URL with a non-empty "[...]" stays raw markdown (old: converted) — rare in writer output,
    # and allowing it would bring back the O(n^2) scan; "[]" query arrays still convert
    s = "Map: [Paro](https://x.com/m?a[0]=1)"
    assert generate.strip_markdown(s) == s
    # an unclosed '[' before a link: the stray bracket stays, the link converts (old: the link text kept the '[')
    assert (generate.strip_markdown("From $500 [subject to change. See [our site](https://example.com/x)")
            == "From $500 [subject to change. See our site https://example.com/x")


def test_md_link_is_linear():
    for s in ("[" + "[\\" * 100_000, "[\\](http://" + "[!](http://" * 20_000, "[a](http://[]" * 60_000):
        assert _elapsed(lambda s=s: generate.strip_markdown(s)) < BOUND_S


# ---------------------------------------------------------------- trip_facts.py

_M = tf.Moment(name="Erdene Zuu", tokens=frozenset({"erdene"}))


def test_names_moment_province_reads_stripped_offsets():  # alert 25
    assert tf.names_moment("Erdene Zuu is old.", _M)
    assert not tf.names_moment("Erdene Province is wide.", _M)
    assert not tf.names_moment("Erdene  aimag is wide.", _M)
    assert tf.names_moment("Erdene provinces differ.", _M)
    # the look-ahead read the raw sentence at stripped offsets: a leading tag shifted it
    assert not tf.names_moment("[R:fact-001] Erdene province is wide.", _M)


def test_names_moment_is_linear_on_long_tag():
    # 400k: the pre-fix code takes ~13 s here (1 s at 100k, too close to the bound to guard); new: ms
    L = 400_000
    s = "[R:" + " " * L + "province]" + " erdene" * (L // 7)
    assert _elapsed(lambda: tf.names_moment(s, _M)) < BOUND_S


# ---------------------------------------------------------------- text_integrity.py structure checks (PR #992)

_OLD_LAST_WORD_RE = re.compile(r"([A-Za-zÀ-ÖØ-öø-ÿ']+)\W*$")
_OLD_LINK_TAIL_RE = re.compile(
    r"(?:\S*://\S+|www\.\S+|\S+@\S+\.\w+|\S+\.(?:com|travel|net|org|io|co|asia)\S*)$", re.I)
_OLD_HASHTAG_LINE_RE = re.compile(r"(?:#[A-Za-z][A-Za-z0-9]+[\s,]*)+")


def _random_texts(n: int, alphabet: list[str], max_len: int = 14) -> list[str]:
    import random
    rnd = random.Random(992)
    return ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, max_len))) for _ in range(n)]


_TI_ALPHABET = list("aZ'_1.é×ÿ@:/#, \t\n!?-wWcomCOMioasiaK\u212a\u017fİ") + [
    "www.", "://", ".com", ".io", ".Asia", ".co", "#ab", "@x"]
_TI_SAMPLES = [
    "", "'", "a", "the trail climbs to the.", "time to swim and", "a place worth coming back to.",
    "It's", "rock 'n'", "rock 'n' ''", "Day 2", "café ×", "on Day 2_ !", "'' 1", "word''!!",
    "https://trailpulse.travel/laos", "www.x.com", "hello@wild.travel", "x@y.", "@x.io", "a.COM",
    "WWW.", "://x", "x://", "a@b.c_d", "a@.c", "#Bhutan #Paro, #TigersNest ", "#A", "#2024 #Trip", "#ab\t#cd",
    "#ab #c", "#ab,", " #ab",
]


def test_last_word_same_as_old_pattern():  # alert 44
    for s in _TI_SAMPLES + _random_texts(30_000, _TI_ALPHABET):
        m = _OLD_LAST_WORD_RE.search(s)
        assert ti._last_word(s) == (m.group(1) if m else None), repr(s)


def test_link_token_same_as_old_pattern():  # alert 41
    for s in _TI_SAMPLES + _random_texts(30_000, _TI_ALPHABET):
        tok = "".join(s.split())  # the caller passes clean.split()[-1]: never any whitespace
        assert ti._is_link_token(tok) == (_OLD_LINK_TAIL_RE.search(tok) is not None), repr(tok)


def test_hashtag_line_same_as_old_pattern():  # alerts 42, 43
    assert hygiene._HASHTAG_LINE_RE.pattern == _OLD_HASHTAG_LINE_RE.pattern
    for s in _TI_SAMPLES + _random_texts(30_000, _TI_ALPHABET):
        assert ti._is_hashtag_line(s) == (_OLD_HASHTAG_LINE_RE.fullmatch(s) is not None), repr(s)


@pytest.mark.parametrize("fn,adv", [
    # the old last-word search took ~1.2 s on 1k apostrophes (cubic); the old link search grew with '!' runs
    (lambda s: ti._last_word(s), "'" * 100_000 + "1"),
    (lambda s: ti._last_word(s), "a" * 100_000 + "!"),
    (lambda s: ti._is_link_token(s), "!" * 100_000),
    (lambda s: ti._is_link_token(s), "://" * 50_000),
    (lambda s: ti._is_link_token(s), "www." * 50_000),
    (lambda s: ti._is_link_token(s), "!@" * 50_000),
    (lambda s: ti._is_link_token(s), "!.io" + "!.co" * 50_000),
    (lambda s: ti._is_hashtag_line(s), "#ab" + "\t" * 100_000 + "x"),
    (lambda s: ti.dangling_tails(s), "One two three " + "'" * 100_000 + "1"),
    (lambda s: ti.structure_findings(s), "Some prose about the trail and\n\n#ab" + "\t" * 100_000 + "x and more"),
    (lambda s: ti._ends_open(s), "Some prose about the trail ends at " + "!" * 100_000 + "a"),
], ids=["last_word_apostrophes", "last_word_letters", "link_bang", "link_scheme", "link_www", "link_at", "link_tld",
        "hashtag_tabs", "dangling_tails", "structure_findings_tabs", "ends_open_bang"])
def test_text_integrity_structure_checks_are_linear(fn, adv):
    assert _elapsed(lambda: fn(adv)) < BOUND_S


# ---------------------------------------------------------------- photos/match.py

_OLD_TRAILING = re.compile(r"(?:[\s_\-]*(?:\(\d+\)|copy|\d+))+$", re.IGNORECASE)


def test_trailing_counter_same_as_old():  # alert 27
    for s in ["Olkhon Island1", "Gyeongbokgung Palace (2)", "Bukchon_Hanok-Village (2)", "Temple copy 3",
              "Mt Fuji (1) (2)", "IMG_20240101_123456", "x(12", "Copy", "Lake COPY", "plain", "", "12",
              "Olkhon Island 2\n", "Lake copy\n", "\n", "12\n", "x\n\n"]:
        assert photo_match._strip_trailing_counter(s) == _OLD_TRAILING.sub("", s), s
    assert photo_match.place_label("Olkhon Island 2\n.jpg") == "Olkhon Island"  # review: '$' before a final '\n'
    assert photo_match.place_label("Gyeongbokgung Palace (2).jpg") == "Gyeongbokgung Palace"
    assert photo_match.place_label("Temple copy 3.jpg") == "Temple"
    assert photo_match.place_label("Olkhon Island1.jpeg") == "Olkhon Island"


def test_trailing_counter_is_linear():
    for name in ("IMG_" + "0" * 100_000 + "x.jpg", "copy" * 25_000 + "x.jpg", "(1)" * 34_000 + "x.jpg"):
        assert _elapsed(lambda name=name: photo_match.place_label(name)) < BOUND_S
    # exponential before: 30 chars would hang the Drive sync worker
    assert _elapsed(lambda: photo_match.place_label("IMG_" + "0" * 40 + "x.jpg")) < BOUND_S


# ---------------------------------------------------------------- stack-trace exposure

def _assert_ref(text: str) -> str:
    m = re.search(r"\(ref ([0-9a-f]{12})\)", text)
    assert m, text
    return m.group(1)


def _logged_refs(logs: list[dict]) -> set:
    return {e.get("error_ref") for e in logs if e.get("error_ref")}


@pytest.mark.asyncio
async def test_cost_explorer_check_hides_exception_text():  # alert 32
    from api.routers import admin_llm_ops

    request = MagicMock()
    result = {"rows": [], "errors": {"acc1": f"AccessDenied {FAKE_ARN}"}}
    with patch.object(admin_llm_ops, "verify_admin_secret"), \
            patch.object(admin_llm_ops, "fetch_all_accounts_cost", return_value=result), \
            patch.object(admin_llm_ops, "record_snapshot", AsyncMock(return_value=0)), \
            patch("api.services.platform_cost.refresh_app_snapshot",
                  AsyncMock(side_effect=Exception(f"boom {FAKE_ARN}"))), \
            capture_logs() as logs:
        resp = await admin_llm_ops.check_cost_explorer(request, x_admin_secret="x", days=7, start=None, end=None)

    assert set(resp) == {"row_count", "app_tag_row_count", "errors", "period_start", "period_end_exclusive"}
    assert set(resp["errors"]) == {"acc1", "acc2_app_tag"}
    assert "arn:" not in json.dumps(resp)
    refs = {_assert_ref(v) for v in resp["errors"].values()}
    assert refs <= _logged_refs(logs)


@pytest.mark.asyncio
async def test_cost_explorer_check_502_hides_exception_text():
    from api.routers import admin_llm_ops

    with patch.object(admin_llm_ops, "verify_admin_secret"), \
            patch.object(admin_llm_ops, "fetch_all_accounts_cost",
                         side_effect=admin_llm_ops.CostExplorerUnavailable(f"all failed: {FAKE_ARN}")), \
            capture_logs() as logs:
        with pytest.raises(HTTPException) as exc:
            await admin_llm_ops.check_cost_explorer(MagicMock(), x_admin_secret="x", days=7, start=None, end=None)

    assert exc.value.status_code == 502
    assert "arn:" not in exc.value.detail
    assert _assert_ref(exc.value.detail) in _logged_refs(logs)
    assert any(e.get("exc_info") for e in logs)


@pytest.mark.asyncio
async def test_infra_status_hides_exception_text():  # alert 5
    from api.routers import admin_pipeline

    request = MagicMock()
    request.app.state.redis = AsyncMock()
    request.app.state.redis.get = AsyncMock(return_value=None)
    request.app.state.redis.set = AsyncMock()
    with patch.object(admin_pipeline, "verify_admin_secret"), \
            patch.object(admin_pipeline, "_boto3") as mock_boto3, capture_logs() as logs:
        mock_boto3.client.side_effect = Exception(f"AccessDeniedException: {FAKE_ARN}")
        result = await admin_pipeline.get_infra_status(request, x_admin_secret="x")

    assert "arn:" not in json.dumps(result)
    for section in ("ecs", "rds", "nat", "redis"):
        assert _assert_ref(result[section]["error"]) in _logged_refs(logs)
    assert "instance_id" in result["nat"]
    cached = request.app.state.redis.set.call_args
    assert cached is not None and "arn:" not in json.dumps([str(a) for a in cached.args])


@pytest.mark.asyncio
async def test_infra_status_ecr_error_hides_exception_text():
    from api.routers import admin_pipeline

    request = MagicMock()
    request.app.state.redis = AsyncMock()
    request.app.state.redis.get = AsyncMock(return_value=None)
    ecs = MagicMock()
    ecs.describe_services.return_value = {"services": [{"status": "ACTIVE", "desiredCount": 1,
                                                        "runningCount": 1, "pendingCount": 0}]}
    ecs.list_tasks.return_value = {"taskArns": []}
    ecr = MagicMock()
    ecr.describe_images.side_effect = Exception(f"AccessDenied {FAKE_ARN}")
    other = MagicMock()
    with patch.object(admin_pipeline, "verify_admin_secret"), \
            patch.object(admin_pipeline, "_boto3") as mock_boto3:
        mock_boto3.client.side_effect = lambda svc, **kw: {"ecs": ecs, "ecr": ecr}.get(svc, other)
        result = await admin_pipeline.get_infra_status(request, x_admin_secret="x")

    assert "arn:" not in result["ecs"]["ecr_error"]
    _assert_ref(result["ecs"]["ecr_error"])


@pytest.mark.asyncio
async def test_spot_workers_hides_exception_text():  # alert 4
    from api.routers import admin_pipeline

    ecs = MagicMock()
    ecs.list_tasks.side_effect = Exception(f"AccessDeniedException {FAKE_ARN}")
    with patch.object(admin_pipeline, "verify_admin_secret"), \
            patch.object(admin_pipeline, "_boto3") as mock_boto3, capture_logs() as logs:
        mock_boto3.client.return_value = ecs
        result = await admin_pipeline.get_spot_workers(MagicMock(), x_admin_secret="x")

    assert set(result) == {"total_tasks", "spot_tasks", "on_demand_tasks", "spot_pct", "saving_per_hr",
                           "tasks", "error"}
    assert "arn:" not in result["error"]
    assert _assert_ref(result["error"]) in _logged_refs(logs)


@pytest.mark.asyncio
async def test_waves_check_hides_exception_text():  # alert 3
    from api.routers import admin_waves

    request = MagicMock()
    request.app.state.pool.fetch = AsyncMock(return_value=[{"id": "w-bad"}, {"id": "w-ok"}])
    request.app.state.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    request.app.state.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    async def fake_check(conn, wave_id):
        if wave_id == "w-bad":
            raise Exception('relation "shared.secret_thing" does not exist')
        return {}

    with patch.object(admin_waves, "verify_admin_secret"), \
            patch.object(admin_waves.wave_report, "check", side_effect=fake_check), capture_logs() as logs:
        resp = await admin_waves.check_waves(request, x_admin_secret="x")

    assert resp["checked"] == 2
    bad, ok = resp["waves"]
    assert bad["wave_id"] == "w-bad" and "relation" not in bad["error"]
    assert _assert_ref(bad["error"]) in _logged_refs(logs)
    assert ok == {"wave_id": "w-ok"}


@pytest.mark.asyncio
async def test_jev_canary_hides_exception_text():  # alert 2
    from api.routers import admin_llm_ops
    from shared.llm_client import decide as decide_mod

    with patch.object(admin_llm_ops, "verify_admin_secret"), \
            patch.object(decide_mod, "_call_jev",
                         AsyncMock(side_effect=RuntimeError("upstream said: secret-token-xyz"))), \
            patch.object(decide_mod, "reset_jev_breaker"), \
            patch.object(decide_mod, "jev_breaker_open", return_value=False), capture_logs() as logs:
        resp = await admin_llm_ops.check_jev_canary(MagicMock(), x_admin_secret="x")

    assert set(resp) == {"credit_ok", "billing_error", "alerted", "breaker_open", "detail", "checked_at"}
    assert resp["credit_ok"] is True and resp["billing_error"] is False
    assert "secret-token" not in resp["detail"]
    assert _assert_ref(resp["detail"]) in _logged_refs(logs)
