"""AA-789 tier 0 (S258) — prevention: the writer never sees the text the two leak classes copied.

- Naming-note leak (S234 r3 #14 'one phonetic approximation in English that is best avoided'): the seed's Naming line
  is a use-this-name line built from the names in the card's note (`atom_seed.naming_line`), never the note's prose.
- Brief recipe leak (S234 r2 #19 'One ruined site, one date, one fact.', r1 #10 '## A Single Sentence on …'): the
  thin-moment note, the route digest and the writer / angle rules carry no count and no 'one sentence' shape.

The notes below are synthetic, in the shape of real place-card naming notes. No DB, no model call.
"""
from __future__ import annotations

import re

import pytest

from services.acp_angle_gate import prompts as angle_prompts
from services.acp_angle_gate.route_digest import render_digest
from services.acp_content_writing import prompts as writer_prompts
from services.acp_content_writing import trip_facts as tf
from services.acp_shared import atom_seed as seed_mod
from services.acp_shared.atom_seed import compose_seed, naming_line, thin_note

# words of a naming note's explanation that came back as copy; none may reach the writer
NOTE_PROSE_RE = re.compile(r"approximation|connotation|international|audience|acceptable|romanis|variants?\b"
                           r"|phonetic|standard English|sufficient|nickname", re.I)


@pytest.mark.parametrize("note, display, expected", [
    ("The city is correctly spelled Mörön (also romanised as Murun or Muren); 'Moron' is a common English phonetic "
     "approximation used in itineraries. It should be referred to as Mörön for an international audience to avoid "
     "unintended connotations.", "Mörön",
     "Naming: use the name 'Mörön', not 'Moron'; also written 'Murun', 'Muren'."),
    ("Commonly written 'Uran Togoo' or 'Uran-Togoo'; the full protected-area name is Uran Togoo-Tulga Uul Natural "
     "Monument. For an international audience, identifying it as a dormant volcano in Bulgan Province is sufficient.",
     "Uran Togoo", "Naming: use the name 'Uran Togoo'; also written 'Uran-Togoo', "
                   "'Uran Togoo-Tulga Uul Natural Monument'."),
    ("The monastery is formally known as Paro Taktsang; 'Tiger's Nest' is the widely used English nickname and is "
     "acceptable for international travel audiences. 'Taktsang' means 'Tiger's Lair' in Dzongkha.",
     "Tiger's Nest Monastery", "Naming: use the name 'Tiger's Nest Monastery'; also written 'Paro Taktsang'."),
    ("Correctly spelled 'Kuang Si Falls' or 'Tat Kuang Si'; also seen as 'Kwang Si' or 'Kuang Xi' in older sources. "
     "For an international travel audience, 'Kuang Si Falls' near Luang Prabang is the standard English form.",
     "Kuang Si Falls", "Naming: use the name 'Kuang Si Falls'; also written 'Tat Kuang Si', 'Kwang Si', 'Kuang Xi'."),
    ("Officially known as Pungtang Dechen Photrang Dzong ('Palace of Great Happiness'); internationally it is "
     "universally referred to as Punakha Dzong. No significant spelling variants.", "Punakha Dzong",
     "Naming: use the name 'Punakha Dzong'; also written 'Palace of Great Happiness', "
     "'Pungtang Dechen Photrang Dzong'."),
    ("Also written Pawangka.", "Pabongka Monastery", "Naming: use the name 'Pabongka Monastery'; also written "
                                                     "'Pawangka'."),
])
def test_naming_line_keeps_the_names_and_drops_the_prose(note, display, expected):
    line = naming_line(note, display)
    assert line == expected
    assert not NOTE_PROSE_RE.search(line)


def test_a_place_the_note_only_mentions_is_not_a_name():
    # 'near Luang Prabang', 'in Bulgan Province': other places, not other names of this one
    assert "Luang Prabang" not in naming_line("For an international travel audience, 'Kuang Si Falls' near Luang "
                                              "Prabang is the standard English form.", "Kuang Si Falls")
    assert naming_line("Identifying it as a dormant volcano in Bulgan Province is sufficient.",
                       "Uran Togoo") == "Naming: use the name 'Uran Togoo'."


def test_accents_are_not_folded_when_names_are_compared():
    # 'Moron' folds to the same letters as 'Mörön'; it must still be listed, as the name to avoid
    assert "not 'Moron'" in naming_line("'Moron' is a common approximation.", "Mörön")


@pytest.mark.parametrize("note", [None, "", "   "])
def test_no_note_no_line(note):
    assert naming_line(note, "Mörön") is None


def test_the_seed_renders_the_line_and_trip_facts_still_reads_the_other_names():
    card = {"display_name": "Mörön", "locality": "", "region": "Khövsgöl", "country": "Mongolia",
            "naming_note": "The city is correctly spelled Mörön (also romanised as Murun or Muren); 'Moron' is a "
                           "common English phonetic approximation. It should be referred to as Mörön for an "
                           "international audience to avoid unintended connotations.",
            "facts": [], "status": "auto_checked"}
    seed = compose_seed("Moron — stop for lunch", "Stop for lunch near Moron.", None, None, [], place="Moron",
                        country="Mongolia", day=4, card=card)
    naming = next(ln for ln in seed.splitlines() if ln.startswith("Naming:"))
    assert naming == "Naming: use the name 'Mörön', not 'Moron'; also written 'Murun', 'Muren'."
    assert not NOTE_PROSE_RE.search(seed)
    (moment,) = tf.parse_moments(seed)
    assert moment.naming == naming
    assert {"murun", "muren"} <= set(moment.tokens)  # the quoted spellings still identify the moment


# ---------------------------------------------------------------- the thin-moment wording

# a count of facts or sentences next to 'one' / 'single' / 'only N' — the shapes the writer copied
COPYABLE_RE = re.compile(r"\bone (?:plain )?sentence\b|\bsingle sentence\b|\bonly \d+ source fact|\bin one sentence\b",
                         re.I)


@pytest.mark.parametrize("n", [0, 1, 2])
def test_thin_note_has_no_count_and_no_shape(n):
    note = thin_note(n)
    assert note.startswith("Thin moment:")  # parsing contract (trip_facts, claim_rules)
    assert not COPYABLE_RE.search(note) and str(n) not in note
    assert "never make it the hook, the title or the climax" in note


def test_a_thin_block_still_parses_as_thin():
    block = compose_seed("ruins of an ancient monastery — visit", "Visit the ruins.", None, None, [], day=14)
    seed = block + "\n" + thin_note(1)
    (moment,) = tf.parse_moments(seed)
    assert moment.thin


def test_writer_and_angle_rules_carry_no_copyable_shape():
    route_format = writer_prompts._BLOG_FORMAT_INSTRUCTIONS_ROUTE
    rule = next(ln for ln in route_format.split("\n- ") if "Thin moment" in ln)
    assert "named in passing" in rule and not COPYABLE_RE.search(rule)
    assert "only be named in passing" in angle_prompts.SYSTEM_PROMPT
    thin_para = angle_prompts.SYSTEM_PROMPT.split("A thin moment has almost no source facts", 1)[1][:200]
    assert not COPYABLE_RE.search(thin_para)


def test_route_digest_thin_line():
    moments = [{"day": 3, "label": "Uran Togoo — trek", "place": "Uran Togoo", "facts": ["a", "b", "c"],
                "fact_count": 3, "thin": False},
               {"day": 14, "label": "ruins of an ancient monastery — visit", "place": None, "facts": ["a"],
                "fact_count": 1, "thin": True}]
    line = next(ln for ln in render_digest(moments).splitlines() if ln.startswith("THIN MOMENTS"))
    assert "name them only in passing" in line and not COPYABLE_RE.search(line)


def test_module_keeps_its_contract_constants():
    assert seed_mod.THIN_FACTS == 3
