"""AA-791 — gate evaluation data for the T9 write/check loop.

Only the persisted attempt's `gate_ledger` used to be stored (S234 report 12 §1.5: attempt-1 warn
results known for 45 of 80 pieces), so what a gate catches before a repair masks it could not be
measured. Each failed attempt's `repair_log` entry now carries `ledger_snapshot(gate_ledger)`; the
final attempt's ledger is the piece's own `gate_ledger` column.

AA-900 W0 (ADR 0020, critic F15) — `failing_gates` is the stable record of EVERY failing gate of one attempt (not
only the first one `held_reason` names), with the F9 failure classes kept apart (`judge_unavailable` and
`text_integrity` are not fidelity failures), and `hold_reason_codes` types why a piece is held.
"""
from __future__ import annotations

from typing import Optional

from services.acp_content_writing import f9_policy, length_contract
from services.acp_content_writing.quality_gates import expand_ledger, ledger_gate, ledger_is_merged, never_holds
from services.quality import registry as qr

MAX_VIOLATIONS_PER_GATE = 5
MAX_VIOLATION_CHARS = 300


def ledger_snapshot(gate_ledger: list[dict] | None, lexicon=None) -> list[dict]:
    """Every gate of one attempt, trimmed to what gate evaluation needs: gate, passed (the verdict),
    blocking and the violations (at most 5, each cut to 300 chars). Passed gates are kept with no
    violations so a gate's runs per attempt can be counted, not only its failures. AA-924: a merged entry (F2_lexicon,
    F15_voice) also keeps `severity` and its `findings` ({text, severity, source}, the same cut).
    AA-1047 W1 (W0 review #1356): `lexicon` (the piece's scope lexicon) classifies the F9 claims the way
    `failing_gates` does, so a place name is not read as a trip promise in one record and not in the other."""
    out: list[dict] = []
    for g in gate_ledger or []:
        if not isinstance(g, dict) or "gate" not in g:
            continue
        passed = bool(g.get("passed"))
        violations = [] if passed else [
            str(v)[:MAX_VIOLATION_CHARS] for v in (g.get("violations") or [])[:MAX_VIOLATIONS_PER_GATE]]
        entry = {"gate": g["gate"], "passed": passed, "blocking": g.get("blocking", True), "violations": violations}
        if isinstance(g.get("findings"), list):
            # AA-924 — a merged entry keeps each finding's severity and source (the same 5 / 300-char cut)
            entry["severity"] = g.get("severity")
            entry["findings"] = [] if passed else [
                {"text": str(f.get("text"))[:MAX_VIOLATION_CHARS], "severity": f.get("severity"),
                 "source": f.get("source")}
                for f in g["findings"][:MAX_VIOLATIONS_PER_GATE] if isinstance(f, dict)]
        classes = f9_policy.entry_classes(g, lexicon)
        if classes:  # AA-900 W0 — the F9 failure classes, read before the violations are cut
            entry["classes"] = classes
        if g.get("degraded"):
            entry["degraded"] = g["degraded"]
        out.append(entry)
    return out


# AA-900 W0 — the typed reason a held piece needs review for, per hold-capable gate (legacy source names; merged
# entries are read on the legacy view). A gate not listed is `gate_failure`; a hard degraded detector is
# `detector_degraded`. Persisted in repair_log: never rename a code.
FAILING_SCHEMA = "failing/1"
DETECTOR_DEGRADED = "detector_degraded"
GATE_FAILURE = "gate_failure"
HOLD_REASON_CODES: dict[str, str] = {
    qr.F6_CTA_PRESENT: "cta_missing",
    qr.F1_GROUNDING: "unsupported_number",
    qr.F9_CTA_FACT: "unsupported_trip_promise",
    qr.F17_TRIP_FACT: "unsupported_trip_fact",
    qr.F18_RARITY: "rarity_claim",
    qr.F10_CANNIBALIZATION_CROSS_TENANT: "cross_tenant_duplicate",
    qr.F19_AUTHENTICITY: "authenticity",
    qr.F2_PLATFORM_TRUTH: "platform_truth",
    qr.F2_BANNED_PATTERNS: "lexicon",
    qr.F2_BRAND_FORBIDDEN: "lexicon",
    qr.F2_LEXICON: "lexicon",
    qr.F11_BRAND_NAME: "brand_name",
    qr.F4_EXTREME_LENGTH: "length",
    qr.F4_SEO_SURFACE: "seo_surface",
    qr.F0_COMPLETE: "incomplete",
    qr.F12_SCOPE: "scope",
    qr.F13_HYGIENE: "text_hygiene",
    qr.F13_LEAK: "text_hygiene",
    qr.F13_REPEAT: "text_hygiene",
    qr.F14_SOCIAL_SHAPE: "channel_shape",
    qr.F14_FIRST_LINE: "channel_shape",
    qr.F16_FAQ: "faq_contract",
}


def failing_gates(gate_ledger: list[dict] | None, lexicon=None) -> list[dict]:
    """Every failed gate of one attempt, one row per legacy source (a merged F2_lexicon / F15_voice entry counts each
    source on its own), in ledger order:
    {"gate": the stored entry name, "source": the legacy gate, "blocking", "may_hold", "n": violations,
     "classes": {class: n} (F9_cta_fact only), "degraded": marker (when present)}.
    `may_hold` = blocking and not a style-only / floor result (quality_gates.never_holds)."""
    entries = [g for g in gate_ledger or [] if isinstance(g, dict) and "gate" in g]
    merged = ledger_is_merged(entries)
    out: list[dict] = []
    for g in expand_ledger(entries):
        if g.get("passed"):
            continue
        blocking = bool(g.get("blocking", True))
        row = {"gate": ledger_gate(g) if merged else g["gate"], "source": g["gate"], "blocking": blocking,
               "may_hold": blocking and not never_holds(g), "n": len(g.get("violations") or [])}
        classes = f9_policy.entry_classes(g, lexicon)
        if classes:
            row["classes"] = classes
        if g.get("degraded"):
            row["degraded"] = g["degraded"]
        out.append(row)
    return out


def f9_class_counts(rows: list[dict]) -> dict[str, int]:
    """{class: n} over the F9_cta_fact rows of `failing_gates` output (fidelity vs infrastructure vs text)."""
    out: dict[str, int] = {}
    for r in rows or []:
        for cls, n in (r.get("classes") or {}).items():
            out[cls] = out.get(cls, 0) + int(n)
    return out


# AA-1077 — F4_extreme_length is typed by its side: `length_short` / `length_long` (length_contract.code_for, read from
# the violation, so an old ledger re-read gets the split code too). `length` stays the code of an F4 failure whose
# side does not read, and the code every label stored before AA-1077 carries: readers compare through
# `reason_family` / LENGTH_CODES, never against one literal.
LENGTH_CODE = HOLD_REASON_CODES[qr.F4_EXTREME_LENGTH]
LENGTH_CODES = frozenset({LENGTH_CODE, length_contract.CODE_SHORT, length_contract.CODE_LONG})


def reason_family(code: str) -> str:
    """The code a report groups by: every length code (old `length`, new `length_short` / `length_long`) is `length`;
    any other code is itself."""
    return LENGTH_CODE if code in LENGTH_CODES else code


def _f4_entry(gate_ledger: list[dict] | None) -> Optional[dict]:
    entries = [g for g in gate_ledger or [] if isinstance(g, dict) and "gate" in g]
    return next((g for g in expand_ledger(entries) if g["gate"] == qr.F4_EXTREME_LENGTH and not g.get("passed")),
                None)


def hold_reason_codes(gate_ledger: list[dict] | None, held_reason: Optional[str] = None) -> list[str]:
    """The typed reasons a piece is held for, one per hold-capable failing gate (in ledger order, deduplicated),
    plus `detector_degraded` when the hold came from a hard degraded detector (held_reason 'degraded: …').
    AA-1077: an F4_extreme_length failure is `length_short` / `length_long` by its violation (`length` when the side
    does not read)."""
    f4_code = length_contract.code_for(_f4_entry(gate_ledger)) or LENGTH_CODE
    codes = [f4_code if r["source"] == qr.F4_EXTREME_LENGTH else HOLD_REASON_CODES.get(r["source"], GATE_FAILURE)
             for r in failing_gates(gate_ledger) if r["may_hold"]]
    if (held_reason or "").startswith("degraded:"):
        codes.append(DETECTOR_DEGRADED)
    return list(dict.fromkeys(codes))


__all__ = ["ledger_snapshot", "MAX_VIOLATIONS_PER_GATE", "MAX_VIOLATION_CHARS", "FAILING_SCHEMA", "HOLD_REASON_CODES",
           "DETECTOR_DEGRADED", "GATE_FAILURE", "LENGTH_CODE", "LENGTH_CODES", "failing_gates", "f9_class_counts",
           "hold_reason_codes", "reason_family"]
