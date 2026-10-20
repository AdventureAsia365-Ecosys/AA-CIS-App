"""AA-791 — T9 fault-injection set: 30 synthetic pieces, each with ONE planted fault for a gate that
fired 0 times on S234's 80 real pieces (report 12 §1.5), plus a clean twin per gate.

Deterministic, no LLM and no DB: every case runs through the real `run_quality_gates()` with the two
judge gates (F8 framework, F9 brand voice) stubbed to pass, so the set measures the deterministic
gates and their wiring for the case's channel. CI runs it via test_aa791_fault_injection.py; the
recall / precision table can also be printed by hand:

    python -m tests.unit.aa791_t9_fault_set

A case is caught when its target gate fails in the ledger AND the piece does not pass; a clean twin
is correct when its target gate passes. Add a case here when a gate is tuned or a new rare gate
lands — never loosen an expectation to make a case pass.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch

from services.acp_content_writing import hygiene
from services.acp_content_writing import quality_gates as qg

BRAND = "Terra Family Expeditions"
CTA = "Ask Terra Family Expeditions to plan this trip for your family"
TRIP = "Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days"
SCOPE = ("SCOPE: this piece covers days 3–14 of the trip, in this order: Sod Camp → Uran Togoo → Moron → "
         "Lake Khövsgöl → ruins of an ancient monastery.")
SEED = ("Sod Camp — overnight in a standard ger camp\n"
        "Location: Sod Camp, Khövsgöl Lake area, Khövsgöl Province, Mongolia\n"
        "Day 3 of the trip\n"
        "Source itinerary: Arrive at Sod Camp in the afternoon. The camp sits opposite a mountain range.\n"
        "Place background (checked facts about the place itself):\n"
        "- Lake Khövsgöl holds approximately 0.4 percent of the world's fresh surface water.\n"
        "- The surrounding area is home to reindeer-herding Tsaatan (Dukha) communities.")
AUDIENCE = "Affluent families with children aged 7–16\nParents who want active holidays"
PACK = {"brand_name": BRAND, "brand_type": "Family adventure travel brand", "customer_segment": AUDIENCE,
        "voice": ["Warm", "Practical", "Reassuring"]}

CLEAN = ("Sod Camp sits opposite a mountain range, a quiet first night in a ger near Lake Khövsgöl. "
         "The surrounding area is home to reindeer-herding Tsaatan communities. " + CTA + ".")
BLOG_BODY = ("## Arriving at Sod Camp\n\n" + ("The camp sits opposite a mountain range, and the afternoon "
             "arrival leaves time to settle into the ger before dinner. ") * 3 + "\n\n" + CTA + ".")


def _faq(n: int) -> str:
    pairs = [("Where do we sleep on day 3?", "In a standard ger camp at Sod Camp."),
             ("What is near the camp?", "A mountain range sits opposite the camp."),
             ("Who lives in the area?", "Reindeer-herding Tsaatan (Dukha) communities."),
             ("When do we arrive?", "In the afternoon.")][:n]
    if not pairs:
        return ""
    return "\n\n## FAQ\n\n" + "\n".join(f"**Q: {q}**\nA: {a}\n" for q, a in pairs)


def _match(sim: float, n: int) -> dict:
    return {"piece_id": f"00000000-0000-0000-0000-00000000079{n}", "tenant_id": "other-tenant",
            "similarity": sim, "writer_missing_brand_rules": n == 1}


@dataclass(frozen=True)
class FaultCase:
    case_id: str
    gate: str
    fault: str  # the planted fault (or "clean twin")
    content_text: str
    channel: str = "facebook"
    cta: Optional[str] = CTA
    atom_text: str = SEED
    brand_pack: dict = field(default_factory=lambda: dict(PACK))
    with_hygiene: bool = False
    cannibalization_match: Optional[dict] = None
    clean: bool = False


FAULT_CASES: list[FaultCase] = [
    # F6_cta_present — the request has no usable CTA (non-repairable, holds at once)
    FaultCase("f6-1", "F6_cta_present", "CTA is None", CLEAN, cta=None),
    FaultCase("f6-2", "F6_cta_present", "CTA is an empty string", CLEAN, cta=""),
    FaultCase("f6-3", "F6_cta_present", "CTA is whitespace only", CLEAN, cta="   "),
    # F0_complete — the text was cut off by the token ceiling
    FaultCase("f0-1", "F0_complete", "ends mid-word", CLEAN + "\n\nFrom the ridge the view is unobstructed t"),
    FaultCase("f0-2", "F0_complete", "ends on a comma", CLEAN + "\n\nAfter dinner the guides light the stove,"),
    FaultCase("f0-3", "F0_complete", "ends with a stray backslash", CLEAN + " \\"),
    # F4_extreme_length — empty-ish or runaway output for the channel's band
    FaultCase("f4-1", "F4_extreme_length", "facebook under 20 chars", "Sod Camp."),
    FaultCase("f4-2", "F4_extreme_length", "facebook over 6,000 chars", (CLEAN + " ") * 30),
    FaultCase("f4-3", "F4_extreme_length", "blog under 4,700 chars", BLOG_BODY + _faq(3), channel="blog"),
    # F11_brand_name — speaks as another brand or names the platform
    FaultCase("f11-1", "F11_brand_name", "'At <other brand>, we'",
              "At Arcadia Adventures, we plan every day around the lake. " + CLEAN),
    FaultCase("f11-2", "F11_brand_name", "'Here at <other brand>, our'",
              "Here at Wild Atlas, our guides know the camp. " + CLEAN),
    FaultCase("f11-3", "F11_brand_name", "names the platform (Adventure Asia)",
              "Adventure Asia plans this trip around the lake. " + CLEAN),
    # F10_cannibalization_cross_tenant — near-copy of another tenant's published piece
    FaultCase("f10-1", "F10_cannibalization_cross_tenant", "0.93 similar, tenant has no brand rules", CLEAN,
              cannibalization_match=_match(0.93, 1)),
    FaultCase("f10-2", "F10_cannibalization_cross_tenant", "0.97 similar", CLEAN,
              cannibalization_match=_match(0.97, 2)),
    FaultCase("f10-3", "F10_cannibalization_cross_tenant", "0.99 similar (near-verbatim)", CLEAN,
              cannibalization_match=_match(0.99, 3)),
    # F2_brand_forbidden — a word the tenant's own brand pack forbids
    FaultCase("f2b-1", "F2_brand_forbidden", "forbidden single word",
              "A luxury night at Sod Camp. " + CLEAN, brand_pack={**PACK, "forbidden_words": ["luxury"]}),
    FaultCase("f2b-2", "F2_brand_forbidden", "forbidden word, different case",
              "Cheap flights are not part of this. " + CLEAN, brand_pack={**PACK, "forbidden_words": ["cheap", "deal"]}),
    FaultCase("f2b-3", "F2_brand_forbidden", "forbidden multi-word phrase",
              "A once in a lifetime night at Sod Camp. " + CLEAN,
              brand_pack={**PACK, "forbidden_words": ["once in a lifetime"]}),
    # F2_banned_patterns — the core cliché list
    # AA-800: 'breathtaking' is a capped stock superlative now (F2_superlatives), not a banned pattern
    FaultCase("f2-1", "F2_banned_patterns", "'unforgettable'", "The unforgettable range faces the camp. " + CLEAN),
    FaultCase("f2-2", "F2_banned_patterns", "'hidden gem'", "Sod Camp is a hidden gem. " + CLEAN),
    FaultCase("f2-3", "F2_banned_patterns", "'embark on'", "Embark on a quiet night by the lake. " + CLEAN),
    # F13_leak — writer instructions / source notes / the brand's audience leak into the copy
    FaultCase("f13l-1", "F13_leak", "'This guide covers' (unsafe to auto-delete)",
              "Lake Khövsgöl sits in the north.\n\nThis guide covers the Khövsgöl region in depth, from the "
              "caldera to the camp. It is written for families who want to understand a place first. " + CTA + ".",
              with_hygiene=True),
    FaultCase("f13l-2", "F13_leak", "source-notes wording ('content seed')",
              "The content seed describes many things in great detail here. " + CLEAN, with_hygiene=True),
    FaultCase("f13l-3", "F13_leak", "the SCOPE's day span",
              "Sod Camp faces a mountain range. The pacing across days three to fourteen is designed well. " + CLEAN,
              with_hygiene=True),
    # F14_first_line — LinkedIn hook over 140 chars after the split and the first-line rewrite
    FaultCase("f14-1", "F14_first_line", "first line 150+ chars",
              "Sod Camp sits opposite a mountain range and the first night in a ger near Lake Khövsgöl is quieter "
              "than most families expect from a camp this far north\n\n" + CLEAN, channel="linkedin"),
    FaultCase("f14-2", "F14_first_line", "first line ~200 chars, one long sentence",
              ("The afternoon arrival at Sod Camp leaves time to settle into the ger, walk to the shore, and watch "
               "the light change on the mountain range opposite the camp while the guides start the stove for dinner"
               "\n\n") + CLEAN, channel="linkedin"),
    FaultCase("f14-3", "F14_first_line", "long first line after leading blank lines (lesson S234 r3 #3)",
              "\n\n" + ("Sod Camp sits opposite a mountain range, " * 5).strip() + "\n\n" + CLEAN, channel="linkedin"),
    # F16_faq — blog FAQ under 3 pairs after the droppers
    FaultCase("f16-1", "F16_faq", "no FAQ section", BLOG_BODY, channel="blog"),
    FaultCase("f16-2", "F16_faq", "1 FAQ pair", BLOG_BODY + _faq(1), channel="blog"),
    FaultCase("f16-3", "F16_faq", "2 FAQ pairs", BLOG_BODY + _faq(2), channel="blog"),
]

# One clean twin per gate: the same shape without the fault — the target gate must pass
# (catches a gate that fires on everything).
CLEAN_CASES: list[FaultCase] = [
    FaultCase("f6-ok", "F6_cta_present", "clean twin", CLEAN, clean=True),
    FaultCase("f0-ok", "F0_complete", "clean twin", CLEAN, clean=True),
    FaultCase("f4-ok", "F4_extreme_length", "clean twin", CLEAN, clean=True),
    FaultCase("f11-ok", "F11_brand_name", "clean twin: the tenant's own name, and a place in 'At <place>, we'",
              "At Terra Family Expeditions, we plan every day around the lake. At Sod Camp, we stop for tea. "
              + CLEAN, clean=True),
    FaultCase("f10-ok", "F10_cannibalization_cross_tenant", "clean twin (no match over 0.92)", CLEAN, clean=True),
    FaultCase("f2b-ok", "F2_brand_forbidden", "clean twin: the forbidden word is in the source itself",
              "A standard ger camp for the night. " + CLEAN, brand_pack={**PACK, "forbidden_words": ["standard"]},
              clean=True),
    FaultCase("f2-ok", "F2_banned_patterns", "clean twin: the cliché is quoted from the source",
              "Locals call it a hidden gem. " + CLEAN, atom_text=SEED + "\nLocals call it a hidden gem.", clean=True),
    FaultCase("f13l-ok", "F13_leak", "clean twin", CLEAN, with_hygiene=True, clean=True),
    FaultCase("f14-ok", "F14_first_line", "clean twin (short hook)",
              "A quiet first night opposite a mountain range.\n\n" + CLEAN, channel="linkedin", clean=True),
    FaultCase("f16-ok", "F16_faq", "clean twin (3 FAQ pairs)", BLOG_BODY + _faq(3), channel="blog", clean=True),
]

RARE_GATES = sorted({c.gate for c in FAULT_CASES})


def _hygiene_ctx() -> hygiene.HygieneContext:
    return hygiene.HygieneContext(seed=SEED, scope=SCOPE, trip_name=TRIP, brand_name=BRAND, cta=CTA,
                                  audience=AUDIENCE)


@contextmanager
def judges_pass():
    """F8 (framework) and F9 (brand voice) are LLM judges — stubbed to pass so the set stays free and
    deterministic. Their own behaviour is covered by test_aa450_quality_gates.py."""
    def _voice(*_a, **_k):
        return [qg._result("F9_cta_fact", []), qg._result("F9_brand_style", [], blocking=False)]

    with patch.object(qg, "gate_framework", return_value=qg._result("F8_framework", [])), \
            patch.object(qg, "gate_brand_voice", side_effect=_voice):
        yield


def run_case(case: FaultCase) -> dict:
    """run_quality_gates() on one case (call inside judges_pass()). Returns the outcome plus the
    target gate's ledger entry (None when the gate did not run)."""
    outcome = qg.run_quality_gates(
        content_text=case.content_text, atom_text=case.atom_text, cta=case.cta, goal_key="promotion",
        brand_rubric_text="rubric", channel=case.channel, brand_pack=case.brand_pack,
        hygiene_ctx=_hygiene_ctx() if case.with_hygiene else None,
        cannibalization_match=case.cannibalization_match,
    )
    entry = next((g for g in outcome["gate_ledger"] if g["gate"] == case.gate), None)
    return {**outcome, "target": entry}


def caught(case: FaultCase, result: dict) -> bool:
    """A planted fault is caught when its gate fails and the piece does not pass."""
    return result["target"] is not None and not result["target"]["passed"] and not result["passed"]


def recall_table() -> list[dict]:
    rows = []
    with judges_pass():
        for gate in RARE_GATES:
            faults = [c for c in FAULT_CASES if c.gate == gate]
            twins = [c for c in CLEAN_CASES if c.gate == gate]
            hit = sum(caught(c, run_case(c)) for c in faults)
            clean_ok = sum((r := run_case(c))["target"] is not None and r["target"]["passed"] for c in twins)
            rows.append({"gate": gate, "planted": len(faults), "caught": hit, "clean_twins": len(twins),
                         "clean_passed": clean_ok})
    return rows


if __name__ == "__main__":  # pragma: no cover — manual report
    print(f"{'gate':36} planted caught  clean-twin-passed")
    for r in recall_table():
        print(f"{r['gate']:36} {r['planted']:7} {r['caught']:6}  {r['clean_passed']}/{r['clean_twins']}")
