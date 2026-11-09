"""AA-1048 — the ONE seam between the ground-truth tooling and the policy registry.

Today the claim classes come from the taxonomy on main (services/quality/taxonomy.yaml, qtax-v1, read through
services/quality/registry.py). When the W1 policy registry (writing_policy.yaml v1, AA-1047) lands, only
`_load()` changes: it reads the claim classes and severities of writing_policy instead, and `policy_sha()` becomes
the sha of that file. Every other module of services/ground_truth asks this module and nothing else, so the switch is
one function and the label rows record which policy text they were made under (`policy_sha`).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

# Where the classes come from: "taxonomy" (qtax-v1, today) or "writing_policy" (after AA-1047 merges).
POLICY_SOURCE = "taxonomy"
# Severities that make a violation fail the item (verdict_rule); warn-only classes never fail an item.
FAILING_SEVERITIES = frozenset({"hard", "block"})
# Taxonomy writers (A1, T2, T9_blog, T9_social) -> the DB writer keys of the eval tables.
WRITER_KEYS = {"A1": "a1", "T2": "t2", "T9_blog": "t9_blog", "T9_social": "t9_social"}


@dataclass(frozen=True)
class PolicyClass:
    id: str
    severity: str
    writers: tuple[str, ...]
    definition: str

    @property
    def fails_item(self) -> bool:
        return self.severity in FAILING_SEVERITIES


def _load() -> tuple[dict[str, PolicyClass], str]:
    """(classes, policy text sha). THE switch point to writing_policy.yaml (AA-1047)."""
    from services.quality import registry

    with open(registry.TAXONOMY_PATH, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    out: dict[str, PolicyClass] = {}
    for cid, c in registry.classes().items():
        writers = tuple(WRITER_KEYS[w] for w in c.get("writers") or [] if w in WRITER_KEYS)
        out[cid] = PolicyClass(id=cid, severity=str(c.get("severity") or "warn"), writers=writers,
                               definition=" ".join(str(c.get("definition") or "").split()))
    return out, sha


@lru_cache(maxsize=1)
def _cached() -> tuple[dict[str, PolicyClass], str]:
    return _load()


def policy_classes() -> dict[str, PolicyClass]:
    return dict(_cached()[0])


def policy_sha() -> str:
    return _cached()[1]


def policy_class(class_id: Optional[str]) -> Optional[PolicyClass]:
    return _cached()[0].get(class_id or "")


def known_class(class_id: Optional[str]) -> bool:
    return policy_class(class_id) is not None


__all__ = ["FAILING_SEVERITIES", "POLICY_SOURCE", "PolicyClass", "WRITER_KEYS", "known_class", "policy_class",
           "policy_classes", "policy_sha"]
