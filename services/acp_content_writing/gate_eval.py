"""AA-791 — gate evaluation data for the T9 write/check loop.

Only the persisted attempt's `gate_ledger` used to be stored (S234 report 12 §1.5: attempt-1 warn
results known for 45 of 80 pieces), so what a gate catches before a repair masks it could not be
measured. Each failed attempt's `repair_log` entry now carries `ledger_snapshot(gate_ledger)`; the
final attempt's ledger is the piece's own `gate_ledger` column.
"""
from __future__ import annotations

MAX_VIOLATIONS_PER_GATE = 5
MAX_VIOLATION_CHARS = 300


def ledger_snapshot(gate_ledger: list[dict] | None) -> list[dict]:
    """Every gate of one attempt, trimmed to what gate evaluation needs: gate, passed (the verdict),
    blocking and the violations (at most 5, each cut to 300 chars). Passed gates are kept with no
    violations so a gate's runs per attempt can be counted, not only its failures. AA-924: a merged entry (F2_lexicon,
    F15_voice) also keeps `severity` and its `findings` ({text, severity, source}, the same cut)."""
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
        out.append(entry)
    return out


__all__ = ["ledger_snapshot", "MAX_VIOLATIONS_PER_GATE", "MAX_VIOLATION_CHARS"]
