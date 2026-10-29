"""AA-789 tier 0 (S258) — prevention: the writer never sees the text the two leak classes copied.

- Naming-note leak (S234 r3 #14, the copy restated the note's reason for avoiding a spelling): the seed's Naming line
  is a use-this-name line built from the names in the card's note (`atom_seed.naming_line`), never the note's prose.
- Brief recipe leak (S234 r2 #19, the copy counted its own facts aloud; r1 #10 '## A Single Sentence on …'): the
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
    ("The town is correctly spelled Vélkar (also romanised as Velcar or Welkar); 'Velkar' is an outdated rendering "
     "without the accent, still found in old brochures. Guides should write Vélkar in every leaflet to avoid an "
     "awkward misreading.", "Vélkar",
     "Naming: use the name 'Vélkar', not 'Velkar'; also written 'Velcar', 'Welkar'."),
    ("Usually written 'Sarn Edda' or 'Sarn-Edda'; the full reserve name is Sarn Edda-Morvi Crater Nature Park. For "
     "visiting readers, describing it as an extinct crater in Dolmar Province is enough.",
     "Sarn Edda", "Naming: use the name 'Sarn Edda'; also written 'Sarn-Edda', "
                  "'Sarn Edda-Morvi Crater Nature Park'."),
    ("The hermitage is formally known as Ostra Vellin; 'Heron's Rest' is the popular English nickname and is fine "
     "for visiting readers. 'Vellin' means 'Heron's Perch' in the old valley tongue.",
     "Heron's Rest Hermitage", "Naming: use the name 'Heron's Rest Hermitage'; also written 'Ostra Vellin'."),
    ("Correctly spelled 'Mirelle Cascades' or 'Tor Mirelle'; also seen as 'Mirella' or 'Myrelle' in older guides. "
     "For visiting readers, 'Mirelle Cascades' near Port Ansel is the usual English form.",
     "Mirelle Cascades", "Naming: use the name 'Mirelle Cascades'; also written 'Tor Mirelle', 'Mirella', "
                         "'Myrelle'."),
    ("Officially known as Calder Vosk Aurenhall ('Hall of the Quiet Tide'); in English it is almost always "
     "referred to as Calder Keep. No other spelling variants.", "Calder Keep",
     "Naming: use the name 'Calder Keep'; also written 'Hall of the Quiet Tide', 'Calder Vosk Aurenhall'."),
    ("Also written Brenhal.", "Brennal Abbey", "Naming: use the name 'Brennal Abbey'; also written 'Brenhal'."),
])
def test_naming_line_keeps_the_names_and_drops_the_prose(note, display, expected):
    line = naming_line(note, display)
    assert line == expected
    assert not NOTE_PROSE_RE.search(line)


def test_a_place_the_note_only_mentions_is_not_a_name():
    # 'near Port Ansel', 'in Dolmar Province': other places, not other names of this one
    assert "Port Ansel" not in naming_line("For visiting readers, 'Mirelle Cascades' near Port Ansel is the usual "
                                           "English form.", "Mirelle Cascades")
    assert naming_line("Describing it as an extinct crater in Dolmar Province is enough.",
                       "Sarn Edda") == "Naming: use the name 'Sarn Edda'."


def test_accents_are_not_folded_when_names_are_compared():
    # 'Velkar' folds to the same letters as 'Vélkar'; it must still be listed, as the name to avoid
    assert "not 'Velkar'" in naming_line("'Velkar' is a common approximation.", "Vélkar")


@pytest.mark.parametrize("note, display, expected", [
    # a pronunciation is not a spelling: shown as 'also written', trip_facts / hygiene would read it as a name
    ("Pronounced 'KAL-der'.", "Calder Keep", "Naming: use the name 'Calder Keep'."),
    ("The pronunciation is roughly 'VEL-kar'; also written Velcar.", "Vélkar",
     "Naming: use the name 'Vélkar'; also written 'Velcar'."),
    ("Also written Calder, pronounced 'KAL-der'.", "Calder Keep", "Naming: use the name 'Calder Keep'."),
    # 'misspelled' carries the naming word inside it; the clause is one to avoid
    ("Often misspelled as Lorvenn.", "Lorven", "Naming: use the name 'Lorven', not 'Lorvenn'."),
    ("Locals call it the Old Lantern.", "Fenwick Light",
     "Naming: use the name 'Fenwick Light'; also written 'Old Lantern'."),
])
def test_pronunciations_misspellings_and_call(note, display, expected):
    assert naming_line(note, display) == expected


@pytest.mark.parametrize("note", [None, "", "   "])
def test_no_note_no_line(note):
    assert naming_line(note, "Vélkar") is None


def test_the_seed_renders_the_line_and_trip_facts_still_reads_the_other_names():
    card = {"display_name": "Vélkar", "locality": "", "region": "Dolmar", "country": "Mongolia",
            "naming_note": "The town is correctly spelled Vélkar (also romanised as Velcar or Welkar); 'Velkar' is an "
                           "outdated rendering without the accent, still found in old brochures. Guides should write "
                           "Vélkar in every leaflet to avoid an awkward misreading.",
            "facts": [], "status": "auto_checked"}
    seed = compose_seed("Velkar — stop for lunch", "Stop for lunch near Velkar.", None, None, [], place="Velkar",
                        country="Mongolia", day=4, card=card)
    naming = next(ln for ln in seed.splitlines() if ln.startswith("Naming:"))
    assert naming == "Naming: use the name 'Vélkar', not 'Velkar'; also written 'Velcar', 'Welkar'."
    assert not NOTE_PROSE_RE.search(seed)
    (moment,) = tf.parse_moments(seed)
    assert moment.naming == naming
    assert {"velcar", "welkar"} <= set(moment.tokens)  # the quoted spellings still identify the moment


# ---------------------------------------------------------------- the thin-moment wording

# a count of facts or sentences next to 'one' / 'single' / 'only N' — the shapes the writer copied
COPYABLE_RE = re.compile(r"\bone (?:plain )?sentence\b|\bsingle sentence\b|\bonly \d+ source fact|\bin one sentence\b",
                         re.I)


@pytest.mark.parametrize("n", [0, 1, 2])
def test_thin_note_has_no_count_and_no_shape(n):
    note = thin_note(n)
    assert note.startswith("Thin moment:")  # parsing contract (trip_facts, claim_rules)
    assert not COPYABLE_RE.search(note) and str(n) not in note
    assert "never make it the hook" in note and "the title" in note and "climax" in note  # the bans are kept


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
    moments = [{"day": 3, "label": "Sarn Edda — trek", "place": "Sarn Edda", "facts": ["a", "b", "c"],
                "fact_count": 3, "thin": False},
               {"day": 14, "label": "ruins of an ancient monastery — visit", "place": None, "facts": ["a"],
                "fact_count": 1, "thin": True}]
    line = next(ln for ln in render_digest(moments).splitlines() if ln.startswith("THIN MOMENTS"))
    assert "name them only in passing" in line and not COPYABLE_RE.search(line)


def test_module_keeps_its_contract_constants():
    assert seed_mod.THIN_FACTS == 3
