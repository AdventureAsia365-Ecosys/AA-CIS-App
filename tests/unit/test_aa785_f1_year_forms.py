"""AA-785 — T9/T10 F1 reads year ranges ("1637–38") and decades ("the late 1630s") as years.

S234 (80 pieces vs the panel): every F1_grounding violation was one of these forms — '38' read out
of "1637–38", '1630' out of "the late 1630s", '1990' out of "since the early 1990s" — and none was a
real invented number. The fixtures below are the S234 sentences (final texts and discarded attempts,
.tmp-session/s234/final/joined2.json) with their real seed lines. `year_forms` is opt-in: T3, A1 and
source_facts keep the plain reading (tests/unit/test_aa298_aa325_grounding.py stays unchanged)."""
import json
from pathlib import Path

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_shared.grounding import find_novel_numeric_claims

# The seed lines the S234 Punakha / Khövsgöl pieces had (pieces*.json `seed`).
SEED_PUNAKHA = ("- It was built in 1637–1638 by Zhabdrung Ngawang Namgyal, the unifier of Bhutan.\n"
                "- It was damaged by a glacial lake outburst flood in 1955.")
SEED_1637 = "Punakha Dzong was founded in 1637 by Zhabdrung Ngawang Namgyal."
SEED_KHOVSGOL = "The surrounding area is protected as Khövsgöl Nuur National Park, established in 1992."

S234_RANGE_SENTENCES = [
    "Punakha Dzong was placed in 1637–38 by Zhabdrung Ngawang Namgyal on a narrow peninsula between the "
    "Pho Chhu and Mo Chhu — the Male River and the Female River — connected to land by a wooden cantilever bridge.",
    "Siting a religious and governmental centre at a river confluence is a decision that carries meaning — and "
    "Punakha Dzong was built precisely there in 1637–38 by Zhabdrung Ngawang Namgyal, the unifier of Bhutan.",
    "That choice, made in 1637–38, was not purely strategic.",
    "When Zhabdrung Ngawang Namgyal founded the dzong in 1637–38, he was establishing more than a building.",
    "When Zhabdrung Ngawang Namgyal founded the dzong in 1637–38, the confluence was not simply a dramatic backdrop.",
    "Zhabdrung Ngawang Namgyal founded the dzong in 1637–38 to serve as both the administrative and religious "
    "heart of Bhutan.",
    "The Pho Chhu and Mo Chhu converge at Punakha, and in 1637–38, Zhabdrung Ngawang Namgyal — the unifier of "
    "Bhutan — chose the meeting point.",
]
S234_DECADE_SENTENCES = [
    "Zhabdrung Ngawang Namgyal built it in the late 1630s not simply as a fortress or a monastery, but as both "
    "simultaneously.",
    "Zhabdrung Ngawang Namgyal built the dzong in the late 1630s at the point where the Pho Chhu and Mo Chhu converge.",
]
S234_1990S = ("It holds an estimated 70 percent of Mongolia's fresh water. The surrounding area has been "
              "protected as Khövsgöl Nuur National Park since the early 1990s.")


def _novel(sentence, seed):
    return find_novel_numeric_claims(sentence, [seed], year_forms=True)


@pytest.mark.parametrize("sentence", S234_RANGE_SENTENCES)
@pytest.mark.parametrize("seed", [SEED_PUNAKHA, SEED_1637, "It was built in 1637–38."])
def test_range_tail_accepted_when_start_in_seed(sentence, seed):
    assert _novel(sentence, seed) == []


@pytest.mark.parametrize("dash", ["–", "-", "—", "/", " – "])
def test_range_dash_forms(dash):
    assert _novel(f"It was built in 1637{dash}38.", SEED_1637) == []


def test_range_tail_accepted_when_full_end_year_in_seed():
    assert _novel("The work ran 1637–52.", "Begun in 1637 and finished in 1652.") == []
    assert _novel("The work ran 1637–52.", "Built 1637–52.") == []  # a seed range counts as its full end year


def test_range_far_end_flags_full_year():
    assert _novel("The work ran 1637–52.", SEED_1637) == ["1652"]


def test_range_start_not_in_seed_flags_start():
    assert _novel("It was built in 1702–03.", SEED_1637) == ["1702", "1703"]


def test_range_across_a_century():
    assert _novel("Restored in 1999–00.", "Restored from 1999.") == []
    assert _novel("Restored in 1998–02.", "Restored from 1998.") == ["2002"]


@pytest.mark.parametrize("sentence", S234_DECADE_SENTENCES)
def test_decade_accepted_when_seed_has_year_in_decade(sentence):
    assert _novel(sentence, SEED_1637) == []
    assert _novel(sentence, SEED_PUNAKHA) == []


def test_early_1990s_accepted_with_seed_1992():
    assert _novel(S234_1990S, SEED_KHOVSGOL + " It holds an estimated 70 percent of Mongolia's fresh water.") == []


def test_decade_apostrophe_and_century_forms():
    assert _novel("Built in the 1630's.", SEED_1637) == []
    assert _novel("Built in the 1600s.", SEED_1637) == []  # "the 1600s" read as the century


@pytest.mark.parametrize("sentence, seed, flagged", [
    ("A trading post since the 1880s.", SEED_1637, ["1880s"]),
    ("Protected since the early 1990s.", "Established in 1985.", ["1990s"]),
    ("Protected since the early 1990s.", "No year here.", ["1990s"]),
])
def test_decade_flags_without_seed_year(sentence, seed, flagged):
    assert _novel(sentence, seed) == flagged


@pytest.mark.parametrize("sentence, flagged", [
    ("It is a 38 km walk from the dzong.", ["38"]),
    ("Founded in 1637, it sits 38 metres above the river.", ["38"]),
    ("Founded in 1637–38, it sits 52 metres above the river.", ["52"]),
    ("It was founded in 1639.", ["1639"]),
])
def test_non_year_numbers_still_flag(sentence, flagged):
    assert _novel(sentence, SEED_1637) == flagged


def test_range_does_not_eat_longer_numbers():
    # "1637-385" is not a year range; the plain reading applies to both digit runs.
    assert _novel("Code 1637-385 applies.", SEED_1637) == ["385"]
    assert _novel("Between 1500-20.5 m.", "1500") == ["20.5"]
    assert _novel("At 1500–20 m.", "1500") == ["1520"]  # read as a range: still flagged, never a new pass


def test_default_off_keeps_the_plain_reading():
    """year_forms=False (T3, A1, source_facts) is byte-for-byte the old behaviour."""
    assert find_novel_numeric_claims(S234_RANGE_SENTENCES[2], [SEED_1637]) == ["38"]
    assert find_novel_numeric_claims(S234_DECADE_SENTENCES[0], [SEED_1637]) == ["1630"]
    assert find_novel_numeric_claims(S234_1990S, [SEED_KHOVSGOL]) == ["1990", "70"]


def test_default_off_matches_the_aa325_fixture_both_ways():
    """The AA-325 real-unit fixture gives the same result with year_forms on — it has no year forms."""
    units = json.loads((Path(__file__).parent / "fixtures" / "aa325_grounding_units.json").read_text())
    for u in units:
        assert find_novel_numeric_claims(u["sentence"], u["cited_atom_texts"], year_forms=True) == \
            find_novel_numeric_claims(u["sentence"], u["cited_atom_texts"])


def test_t9_gate_grounding_uses_year_forms():
    text = " ".join(S234_RANGE_SENTENCES + S234_DECADE_SENTENCES)
    assert qg.gate_grounding(text, SEED_PUNAKHA)["passed"] is True
    assert qg.gate_grounding(S234_1990S, SEED_KHOVSGOL)["violations"] == [
        "sentence states ['70'] not present in the content seed: "
        "'It holds an estimated 70 percent of Mongolia's fresh water.'"]


def test_f1_message_still_parsed_by_sentence_repair():
    text = "The dzong was founded in 1637–52 by the Zhabdrung. Book now."
    gate = qg.gate_grounding(text, SEED_1637)
    assert gate["violations"] == [
        "sentence states ['1652'] not present in the content seed: "
        "'The dzong was founded in 1637–52 by the Zhabdrung.'"]
    m = sr._F1_QUOTE_RE.search(gate["violations"][0])
    assert m and m.group(1) == "The dzong was founded in 1637–52 by the Zhabdrung."
    items = sr.plan(text, [gate])
    assert [i["sentence"] for i in items] == ["The dzong was founded in 1637–52 by the Zhabdrung."]
    assert "states ['1652']" in items[0]["problem"]
