"""AA-1049 W3 (root ADR 0020 S5) — stable keys of a T9 attempt's blocking failures.

Opt-in only: the regression guard reads these keys when T9_REGRESSION_GUARD_MODE=stable_keys (service.guard_decision);
the default `gate_names` is main's gate-name rule. Two review findings stay open on this mode (a style-only key weighs
as much as a hold-class key; F14_social_shape is one whole-gate key for an under-floor and a far-over block): see
docs/implementation-notes/AA-1049-l13-t9.md "Known limits".

The AA-528 regression guard and the stop rule used to compare GATE NAMES between two attempts: a rewrite that fixed
three F9 claims and added one F17 finding counted as "one new failing gate, not fewer gates" and was reverted, while a
rewrite that swapped one F9 claim for a different one counted as "no change". A stable key names WHAT failed, read
from the gate's row in the route table (route_table.parse_of):

- a sentence finding (quoted / F1 / F9 claim / F9 TEXT_INTEGRITY) → (source gate, the folded quoted sentence);
- an F9 CTA failure → (F9_cta_fact, "cta_not_reflected"); any other F9 violation → (F9_cta_fact, folded text);
- every other blocking failure (length, CTA presence, FAQ contract, cannibalization, shape …) → (gate, "*"): its
  violation text carries counts that change between attempts without meaning a new problem.

A result failed only by a tier-2 seed-alignment finding is left out (as the AA-1000 review did for the gate-name
guard): tier 2 is re-asked every attempt. Keys are compared as sets; the judge's reason wording is never part of a
key (it changes between two verdicts on the same sentence).

Pure (no model call).
"""
from __future__ import annotations

from typing import Optional

from services.acp_content_writing import route_table as rt
from services.acp_content_writing.hygiene import fold
from services.acp_content_writing.quality_gates import expand_ledger
from services.acp_content_writing.seed_align import adjudicate as seed_adjudicate
from services.acp_content_writing.sentence_repair import (_BRAND_QUOTE_RE, _F1_QUOTE_RE, TEXT_INTEGRITY_PREFIX,
                                                          _f9_items)
from services.acp_shared.citation_tags import strip_citation_tags

WHOLE_GATE = "*"
CTA_KEY = "cta_not_reflected"
_EDGE = " \t\n\"'“”‘’.,;:!?…()[]"


def _norm(text: Optional[str]) -> str:
    return " ".join(fold(strip_citation_tags(text or "", " ")).split()).strip(_EDGE)


def violation_keys(gate: str, violation: str) -> list[tuple[str, str]]:
    """The stable keys of one violation of a failed blocking legacy entry `gate`."""
    v = str(violation)
    parse = rt.parse_of(gate)
    if parse == rt.PARSE_QUOTED:
        m = _BRAND_QUOTE_RE.search(v)
        return [(gate, _norm(m.group(2)) if m else _norm(v))]
    if parse == rt.PARSE_F1:
        m = _F1_QUOTE_RE.search(v)
        return [(gate, _norm(m.group(1)) if m else _norm(v))]
    if parse == rt.PARSE_F9:
        if "CTA_NOT_REFLECTED" in v and "unsupported claim(s)" not in v:
            return [(gate, CTA_KEY)]
        items = _f9_items(v, allow_cta=True)
        if items:
            keys = [(gate, _norm(q)) for q, _, _ in items]
            return keys + ([(gate, CTA_KEY)] if "CTA_NOT_REFLECTED" in v else [])
        if v.startswith(TEXT_INTEGRITY_PREFIX):
            return [(gate, _norm(v[len(TEXT_INTEGRITY_PREFIX):]))]
        return [(gate, _norm(v))]
    if parse == rt.PARSE_BRAND_STYLE:
        return [(gate, _norm(v))]
    return [(gate, WHOLE_GATE)]


def blocking_keys(gate_ledger: Optional[list]) -> frozenset:
    """The stable keys of every failed blocking result of one attempt (legacy view; tier-2-only results left out)."""
    out: set[tuple[str, str]] = set()
    for g in expand_ledger(list(gate_ledger or [])):
        if g.get("passed") or not g.get("blocking", True) or seed_adjudicate.tier2_only_failure(g):
            continue
        vs = [str(v) for v in g.get("violations") or []] or [""]
        for v in vs:
            out.update(violation_keys(g["gate"], v))
    return frozenset(out)


def shrank(before: Optional[list], after: Optional[list]) -> bool:
    """The stop rule's test: did `after` strictly shrink the stable-key blocking set of `before` (a proper subset)?"""
    a, b = blocking_keys(before), blocking_keys(after)
    return b < a


def gates_of(keys: frozenset) -> frozenset:
    """The gate names of a set of stable keys (the S271 gate-name view of the same ledger)."""
    return frozenset(g for g, _ in keys)


def compare(prev_ledger: Optional[list], cur_ledger: Optional[list]) -> dict:
    """The regression guard's evidence for one pair of attempts: the keys added and removed, both key counts and both
    gate sets, and `revert`.

    Revert only when the rewrite has a key the previous attempt did not AND it is not net-better on EITHER measure:
    as many keys or more, and as many failing gates or more. So a same-gate swap is reverted (D-guard-1), a rewrite
    that clears more findings than it adds is kept (D-guard-2), and a rewrite that clears a whole gate is never
    reverted (as on main: putting it back would bring back a product-truth failure the rewrite cleared)."""
    prev, cur = blocking_keys(prev_ledger), blocking_keys(cur_ledger)
    new, removed = cur - prev, prev - cur
    pg, cg = gates_of(prev), gates_of(cur)
    revert = bool(new) and len(cur) >= len(prev) and len(cg) >= len(pg)
    return {"new": frozenset(new), "removed": frozenset(removed), "prev_keys": len(prev), "cur_keys": len(cur),
            "prev_gates": pg, "cur_gates": cg, "revert": revert}


def regressed(prev_ledger: Optional[list], cur_ledger: Optional[list]) -> tuple[frozenset, bool]:
    """(new keys, revert?) for the regression guard (compare)."""
    c = compare(prev_ledger, cur_ledger)
    return c["new"], c["revert"]
