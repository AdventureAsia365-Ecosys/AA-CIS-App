"""AA-1049 W3 (root ADR 0020 S5) — stable keys of a T9 attempt's blocking failures.

Opt-in only: the regression guard reads these keys when T9_REGRESSION_GUARD_MODE=stable_keys (service.guard_decision);
the default `gate_names` is main's gate-name rule. The two S272 review findings on this mode are fixed (S275, see
docs/implementation-notes/AA-1049-l3-stable-keys.md): a new hold-capable key is weighed on its own (`hold_keys`, fail
closed), F14_social_shape keys by violation class, and an attempt with the AA-780 under-floor block is reverted to
only when main's rule reverts.

The AA-528 regression guard and the stop rule used to compare GATE NAMES between two attempts: a rewrite that fixed
three F9 claims and added one F17 finding counted as "one new failing gate, not fewer gates" and was reverted, while a
rewrite that swapped one F9 claim for a different one counted as "no change". A stable key names WHAT failed, read
from the gate's row in the route table (route_table.parse_of):

- a sentence finding (quoted / F1 / F9 claim / F9 TEXT_INTEGRITY) → (source gate, the folded quoted sentence);
- an F9 CTA failure → (F9_cta_fact, "cta_not_reflected"); any other F9 violation → (F9_cta_fact, folded text);
- F14_social_shape → (F14, its violation class): "far_over" (the one that may hold), "under_floor" (the AA-780
  attempt-1 block), "over", "hashtags" (S275 review fix: one whole-gate key let a far-over block and an under-floor
  block look the same);
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
from services.acp_content_writing.quality_gates import F14_GATE, expand_ledger, never_holds
from services.acp_content_writing.seed_align import adjudicate as seed_adjudicate
from services.acp_content_writing.sentence_repair import (_BRAND_QUOTE_RE, _F1_QUOTE_RE, TEXT_INTEGRITY_PREFIX,
                                                          _f9_items)
from services.acp_shared.citation_tags import strip_citation_tags

WHOLE_GATE = "*"
CTA_KEY = "cta_not_reflected"
# F14_social_shape violation classes (quality_gates.gate_social_shape texts). Only FAR_OVER may hold a piece.
F14_FAR_OVER = "far_over"
F14_UNDER_FLOOR = "under_floor"
F14_OVER = "over"
F14_HASHTAGS = "hashtags"
# The rule that decided compare()'s `revert` (evidence for the replay).
RULE_MAIN_FLOOR = "main_floor"   # the previous attempt has the AA-780 under-floor block: main's gate-name rule
RULE_HOLD = "hold_keys"          # a new hold-capable key without fewer hold-capable keys / gates: revert
RULE_KEYS = "keys"               # the S272 all-key rule
RULE_KEEP = "keep"
_EDGE = " \t\n\"'“”‘’.,;:!?…()[]"


def _norm(text: Optional[str]) -> str:
    return " ".join(fold(strip_citation_tags(text or "", " ")).split()).strip(_EDGE)


def f14_class(violation: str) -> str:
    """The class of one F14_social_shape violation (the word count and the band are left out: they change between
    attempts). Checked far-over first: its text also contains "over the"."""
    v = str(violation)
    if "far over the" in v:
        return F14_FAR_OVER
    if " under the " in v:
        return F14_UNDER_FLOOR
    if " over the " in v:
        return F14_OVER
    if "hashtags, more than" in v:
        return F14_HASHTAGS
    return WHOLE_GATE


def violation_keys(gate: str, violation: str) -> list[tuple[str, str]]:
    """The stable keys of one violation of a failed blocking legacy entry `gate`."""
    v = str(violation)
    if gate == F14_GATE:
        return [(gate, f14_class(v))]
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


def _failed(gate_ledger: Optional[list]) -> list[dict]:
    """The failed blocking legacy results of one attempt, without a tier-2-only seed-alignment result."""
    return [g for g in expand_ledger(list(gate_ledger or []))
            if not g.get("passed") and g.get("blocking", True) and not seed_adjudicate.tier2_only_failure(g)]


def _keys_of(g: dict) -> set:
    out: set[tuple[str, str]] = set()
    for v in [str(v) for v in g.get("violations") or []] or [""]:
        out.update(violation_keys(g["gate"], v))
    return out


def blocking_keys(gate_ledger: Optional[list]) -> frozenset:
    """The stable keys of every failed blocking result of one attempt (legacy view; tier-2-only results left out)."""
    out: set[tuple[str, str]] = set()
    for g in _failed(gate_ledger):
        out.update(_keys_of(g))
    return frozenset(out)


def hold_keys(gate_ledger: Optional[list]) -> frozenset:
    """The hold-capable subset of `blocking_keys`: the keys of a failed blocking result that may hold the piece
    (quality_gates.never_holds is False, the same test hold_failure uses), so a style-only key (F15_voice,
    F2_superlatives, F9_brand_style) and the AA-780 under-floor block are left out. Of an F14_social_shape result only
    the far-over class may hold (a hashtag residue in the same result does not)."""
    out: set[tuple[str, str]] = set()
    for g in _failed(gate_ledger):
        if never_holds(g):
            continue
        out.update(k for k in _keys_of(g) if k[0] != F14_GATE or k[1] == F14_FAR_OVER)
    return frozenset(out)


def has_floor_block(gate_ledger: Optional[list]) -> bool:
    """The attempt carries the AA-780 under-floor F14 block (a failed blocking F14_social_shape that never holds):
    reverting to it makes the call site waive that block (service._waive_floor_block)."""
    return any(g["gate"] == F14_GATE and never_holds(g) for g in _failed(gate_ledger))


def shrank(before: Optional[list], after: Optional[list]) -> bool:
    """The stop rule's test: did `after` strictly shrink the stable-key blocking set of `before` (a proper subset)?"""
    a, b = blocking_keys(before), blocking_keys(after)
    return b < a


def gates_of(keys: frozenset) -> frozenset:
    """The gate names of a set of stable keys (the S271 gate-name view of the same ledger)."""
    return frozenset(g for g, _ in keys)


def compare(prev_ledger: Optional[list], cur_ledger: Optional[list]) -> dict:
    """The regression guard's evidence for one pair of attempts: the keys added and removed, both key counts and both
    gate sets, the same for the hold-capable keys, the rule that decided, and `revert`.

    In this order:
    1. The previous attempt has the AA-780 under-floor block (`has_floor_block`): main's gate-name rule decides
       (a new failing gate and not fewer failing gates). A revert there waives the floor block at the call site, so
       a stable-key revert main would not make could ship the short post approved (review S272 round 3, finding 3:
       under-floor F14 + F15 s1 -> far-over F14 + F15 s2 kept by main, reverted by the S272 key rule).
    2. Hold-capable keys (`hold_keys`), fail closed: the rewrite has a hold-capable key the previous attempt did not
       AND it is not net-better on either hold measure (as many hold-capable keys or more, and as many hold-capable
       gates or more) -> revert. So a rewrite never trades style findings for a new truth failure (review S272 round
       3, finding 2: F15_voice x2 -> F1_grounding x1 was kept, a held text with a made-up number delivered), and a
       rewrite that clears more truth findings than it adds is still kept.
    3. All keys (the S272 rule): a key the previous attempt did not have, and not net-better on either measure (as
       many keys or more, and as many failing gates or more) -> revert. So a same-gate swap is reverted (D-guard-1),
       a rewrite that clears more findings than it adds is kept (D-guard-2), and a rewrite that clears a whole gate is
       never reverted by this step (as on main)."""
    prev, cur = blocking_keys(prev_ledger), blocking_keys(cur_ledger)
    new, removed = cur - prev, prev - cur
    pg, cg = gates_of(prev), gates_of(cur)
    hp, hc = hold_keys(prev_ledger), hold_keys(cur_ledger)
    new_hold = hc - hp
    hpg, hcg = gates_of(hp), gates_of(hc)
    if has_floor_block(prev_ledger):
        rule, revert = RULE_MAIN_FLOOR, bool(cg - pg) and len(cg) >= len(pg)
    elif new_hold and len(hc) >= len(hp) and len(hcg) >= len(hpg):
        rule, revert = RULE_HOLD, True
    elif new and len(cur) >= len(prev) and len(cg) >= len(pg):
        rule, revert = RULE_KEYS, True
    else:
        rule, revert = RULE_KEEP, False
    return {"new": frozenset(new), "removed": frozenset(removed), "prev_keys": len(prev), "cur_keys": len(cur),
            "prev_gates": pg, "cur_gates": cg, "new_hold": frozenset(new_hold), "prev_hold_keys": len(hp),
            "cur_hold_keys": len(hc), "prev_hold_gates": hpg, "cur_hold_gates": hcg, "rule": rule, "revert": revert}


def regressed(prev_ledger: Optional[list], cur_ledger: Optional[list]) -> tuple[frozenset, bool]:
    """(new keys, revert?) for the regression guard (compare)."""
    c = compare(prev_ledger, cur_ledger)
    return c["new"], c["revert"]
