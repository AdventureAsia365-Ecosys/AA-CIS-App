"""AA-610 (Sub 1): services.acp_shared.atom_extraction.checkable_evidence() — validates a
model-supplied `evidence` string is actually a verbatim quote of the day's source text before
trusting it as a `said` signal (acp_contract.atom_ranking.py, migration 160), falling back to
the whole day body when it is not. Pure-function tests only — no DB, no LLM, no HTTP."""

from services.acp_shared.atom_extraction import checkable_evidence


DAY_BODY = (
    "Depart Kathmandu early morning and drive to Besisahar. Trek gently along the Marsyangdi "
    "River to Bahundanda, passing terraced rice fields and small Gurung villages."
)


def test_verbatim_quote_is_returned_unchanged():
    evidence = "Trek gently along the Marsyangdi River to Bahundanda"
    assert checkable_evidence(evidence, DAY_BODY) == evidence


def test_verbatim_quote_with_extra_whitespace_still_checks_out():
    # Models routinely reflow whitespace when quoting — the check is whitespace-insensitive,
    # but the ORIGINAL string (not the normalised one) is what gets returned and measured.
    evidence = "Trek  gently\nalong the Marsyangdi River  to Bahundanda"
    assert checkable_evidence(evidence, DAY_BODY) == evidence


def test_paraphrase_falls_back_to_whole_day_body():
    # Not a quote — the model restated place+action in its own words. That is not evidence,
    # it carries no new said signal beyond text, so it must not be trusted as one.
    evidence = "The group treks along a river towards a village."
    assert checkable_evidence(evidence, DAY_BODY) == DAY_BODY


def test_empty_evidence_falls_back_to_whole_day_body():
    assert checkable_evidence("", DAY_BODY) == DAY_BODY
    assert checkable_evidence(None, DAY_BODY) == DAY_BODY
    assert checkable_evidence("   ", DAY_BODY) == DAY_BODY


def test_case_insensitive_match_still_checks_out():
    evidence = "TREK GENTLY ALONG THE MARSYANGDI RIVER"
    assert checkable_evidence(evidence, DAY_BODY) == evidence


def test_never_returns_none():
    # A Segment whose atom mis-quoted must still get SOME said signal for that day, never 0.
    assert checkable_evidence("totally unrelated text", DAY_BODY) is not None
    assert checkable_evidence("", "") == ""
