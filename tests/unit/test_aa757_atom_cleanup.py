"""AA-757 (S224): prompt day labels stripped from evidence; pure-logistics atoms dropped.
Examples are real atoms from the S224 offline A/B (477 days, Haiku vs GPT-6 Luna)."""
import pytest

from services.acp_shared.atom_extraction import checkable_evidence, clean_evidence, is_logistics_atom

DAY = "A full day of road transfer by private vehicle, no cycling. The route travels along Route 9."


def test_clean_evidence_drops_prompt_day_labels():
    ev = ("DAY 4 (extract atoms only from this section):\nDay 4 — Khe Sanh to Xepon\n"
          "A full day of road transfer by private vehicle, no cycling.")
    assert clean_evidence(ev) == "A full day of road transfer by private vehicle, no cycling."


def test_checkable_evidence_accepts_a_quote_with_the_day_label():
    ev = "Day 4 — Khe Sanh to Xepon\nA full day of road transfer by private vehicle, no cycling."
    assert checkable_evidence(ev, DAY) == "A full day of road transfer by private vehicle, no cycling."


def test_clean_evidence_keeps_a_plain_quote():
    assert clean_evidence("The route travels along Route 9.") == "The route travels along Route 9."


@pytest.mark.parametrize("place,action", [
    ("the hotel", "check-in"), ("your hotel", "check in"), ("airport", "transfer to"),
    ("Dazhai", "stay overnight"), ("Paro International Airport", "arrive"), ("Walnut Grove", "rest"),
    ("your room", "settle into your room"),
])
def test_logistics_atoms_are_dropped(place, action):
    assert is_logistics_atom(place, action)


@pytest.mark.parametrize("place,action", [
    ("Tuol Sleng", "take a guided tour of"), ("Naminoue Shrine", "draw omikuji fortunes"),
    ("Tad Lo's waterfalls", "swimming"), ("Kyirong", "enjoy a Nepali dinner"),
    ("Jhola", "camp overnight"), ("Raikot Bridge to Tatto village", "drive by 4x4 jeeps"),
    ("Mount Phusalao", "cycle upward to viewpoint and watch sunset"),
])
def test_real_moments_are_kept(place, action):
    assert not is_logistics_atom(place, action)
