"""Quality System v1 registry (AA-926, design §3): the one place gate names and quality thresholds live.

- **Gate constants.** Every name a gate ledger, repair_log, flag list, held_reason, quality_signal or gate log
  event carries is a constant here. Code imports the constant; `python -m services.quality.lint` fails on a
  gate-name literal anywhere else. Names are persisted: never rename one; a removed gate stays (retired).
- **Taxonomy** (`taxonomy.yaml`, qtax-v1): error classes, and the class every gate, Jev question key, admin
  chip and A1 validate code maps to. `owner_gates`, `metric` and `tolerance` are derived (design §3.2).
- **Thresholds** (`thresholds.yaml`): the single source for the 7.0 pass line and the other cut-offs.

This module imports nothing from `services` (the CI Lint job runs the lint with only PyYAML installed) and
loads the YAML with `yaml.safe_load`. Lookups used on request paths (`gate`, `gate_label`, `class_info`) are
total: an unknown or legacy name returns None, never raises.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

_DIR = Path(__file__).resolve().parent
TAXONOMY_PATH = _DIR / "taxonomy.yaml"
THRESHOLDS_PATH = _DIR / "thresholds.yaml"
PATHS_PATH = _DIR / "paths.yaml"

TAXONOMY_VERSION = "qtax-v1"

# ------------------------------------------------------------------------------------- T10 gates
# Channel order is the ledger order of quality_gates.run_quality_gates.
F6_CTA_PRESENT = "F6_cta_present"
F1_GROUNDING = "F1_grounding"
F2_BANNED_PATTERNS = "F2_banned_patterns"
F2_BRAND_FORBIDDEN = "F2_brand_forbidden"
F2_PLATFORM_TRUTH = "F2_platform_truth"
F2_SUPERLATIVES = "F2_superlatives"
F2_STYLE_WARN = "F2_style_warn"
F11_BRAND_NAME = "F11_brand_name"
F19_AUTHENTICITY = "F19_authenticity"
F10_CANNIBALIZATION_CROSS_TENANT = "F10_cannibalization_cross_tenant"
F4_EXTREME_LENGTH = "F4_extreme_length"
F0_COMPLETE = "F0_complete"
F12_SCOPE = "F12_scope"
F13_HYGIENE = "F13_hygiene"
F13_LEAK = "F13_leak"
F13_REPEAT = "F13_repeat"
F17_TRIP_FACT = "F17_trip_fact"
F18_RARITY = "F18_rarity"
F14_SOCIAL_SHAPE = "F14_social_shape"
F15_VOICE = "F15_voice"
F15_VOICE_WARN = "F15_voice_warn"
F4_SEO_SURFACE = "F4_seo_surface"
F9_CTA_FACT = "F9_cta_fact"
F9_BRAND_STYLE = "F9_brand_style"
F16_FAQ = "F16_faq"
F14_FIRST_LINE = "F14_first_line"
# T2 tenant-version sentence checks (admin Content Quality, AA-802).
T3_GROUNDING = "T3_grounding"
T2_FORBIDDEN_WORD = "FORBIDDEN_WORD"
# Retired by AA-787 — old ledgers still carry them.
PROMISES_AN_OPTION = "promises_an_option"
F8_FRAMEWORK = "F8_framework"
F3_STRUCTURAL_VARIANCE = "F3_structural_variance"
F5_ATOM_DENSITY = "F5_atom_density"
F7_FAQ_DEDUP = "F7_faq_dedup"
# Flags (non-blocking signals stored with the piece).
F7_FAQ_AUTOFIX = "F7_faq_autofix"
F13_AUTOFIX = "F13_autofix"
F13_FINAL_PASS = "F13_final_pass"
F15_STOCK_CUT = "F15_stock_cut"
WITHIN_TENANT_REUSE = "within_tenant_reuse"
# Telemetry (llm_call_log.quality_signal.gate, structlog events) — never a ledger gate.
F9_BRAND_VOICE = "F9_brand_voice"
F9_STICKY_PASS = "F9_sticky_pass"
F9_STICKY_CARRY = "F9_sticky_carry"
F9_STICKY_PASS_SKIPPED = "F9_sticky_pass_skipped"

GATE_CONSTANTS: dict[str, str] = {k: v for k, v in dict(globals()).items()
                                  if k.isupper() and isinstance(v, str) and k not in
                                  ("TAXONOMY_VERSION",)}

SEVERITY_DEFAULTS = {
    # design §4.2 — the pass criterion per severity (metric, tolerance), used when a class stores none.
    "hard": ("new_verified_errors", "0 new (verified), clean-control samples only"),
    "block": ("new_minus_fixed", "new - fixed <= 0 and new <= noise_k (1)"),
    "warn": ("report_only", "report only"),
}


# ----------------------------------------------------------------------------------------- loading

@lru_cache(maxsize=1)
def taxonomy() -> dict:
    with open(TAXONOMY_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def thresholds_doc() -> dict:
    with open(THRESHOLDS_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def paths_doc() -> dict:
    with open(PATHS_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ------------------------------------------------------------------------------------------ classes

@lru_cache(maxsize=1)
def classes() -> dict[str, dict]:
    """class_id → class, with the derived fields: owner_gates (detector refs in mode enforce), metric and
    tolerance (default per severity), covered_writers (writers with at least one detector)."""
    out: dict[str, dict] = {}
    for c in taxonomy().get("classes") or []:
        dets = c.get("detectors") or []
        metric, tol = SEVERITY_DEFAULTS.get(c.get("severity"), ("report_only", "report only"))
        out[c["id"]] = {
            **c,
            "owner_gates": [d["ref"] for d in dets if d.get("mode") == "enforce"],
            "metric": c.get("metric", metric),
            "tolerance": c.get("tolerance", tol),
            "covered_writers": sorted({w for d in dets for w in d.get("writers") or []}),
        }
    return out


def class_info(class_id: str) -> Optional[dict]:
    return classes().get(class_id)


def hard_fact_classes() -> list[str]:
    """Writing Memory hard stop 1 (WM README §8.2): every fact.* class with severity hard."""
    return [cid for cid, c in classes().items() if cid.startswith("fact.") and c.get("severity") == "hard"]


# -------------------------------------------------------------------------------------------- gates

def gates() -> dict[str, dict]:
    return taxonomy().get("gates") or {}


def gate(name: Optional[str]) -> Optional[dict]:
    """The registry entry of a gate / flag / telemetry name; None for an unknown or legacy name."""
    if not isinstance(name, str):
        return None
    return gates().get(name)


def gate_label(name: Optional[str]) -> Optional[str]:
    g = gate(name)
    return g.get("label") if g else None


def gate_classes(name: Optional[str]) -> list[str]:
    g = gate(name)
    return list(g.get("classes") or []) if g else []


def retired_gates() -> list[str]:
    return [n for n, g in gates().items() if g.get("retired")]


def jev_questions() -> dict[str, dict]:
    return taxonomy().get("jev_questions") or {}


def chips() -> dict[str, dict]:
    return taxonomy().get("chips") or {}


def a1_codes() -> dict[str, dict]:
    return taxonomy().get("a1_codes") or {}


def code_checks() -> dict[str, dict]:
    """Detector modules named by their module (services/quality/*): the Q0 corpus `detector` of a contract or
    degraded-marker case."""
    return taxonomy().get("code_checks") or {}


def aliases() -> dict[str, list[str]]:
    """Q0 corpus class id → the canonical class ids it stands for (taxonomy.yaml aliases; one id or a list)."""
    out = {}
    for k, v in (taxonomy().get("aliases") or {}).items():
        out[k] = [v] if isinstance(v, str) else list(v or [])
    return out


def resolve_class(class_id: Optional[str]) -> list[str]:
    """The canonical class ids of a class id or a Q0 alias; [] when it resolves to nothing."""
    if not isinstance(class_id, str):
        return []
    if class_id in classes():
        return [class_id]
    return [c for c in aliases().get(class_id, []) if c in classes()]


def detector_refs() -> set[str]:
    """Every name a class detector or a Q0 case `detector` may use: gates, Jev keys, A1 codes, code checks."""
    return set(gates()) | set(jev_questions()) | set(a1_codes()) | set(code_checks())


# --------------------------------------------------------------------------------------- thresholds

def _resolve(key: str, seen: tuple = ()) -> Any:
    entries = thresholds_doc().get("thresholds") or {}
    if key not in entries:
        raise KeyError(f"unknown quality threshold {key!r} (services/quality/thresholds.yaml)")
    if key in seen:
        raise ValueError(f"threshold ref cycle: {' -> '.join(seen + (key,))}")
    e = entries[key]
    if "ref" in e:
        return _resolve(e["ref"], seen + (key,))
    v = e["value"]
    return tuple(v) if isinstance(v, list) else v


def threshold(key: str) -> Any:
    """The resolved value of a threshold key (a `ref` is followed). Unknown key → KeyError: thresholds are
    read at import time by the modules that use them, so a typo fails the build, not a request."""
    return _resolve(key)


def pass_line() -> float:
    return float(threshold("pass_line"))


# ------------------------------------------------------------------------------------------- parity

def parity_report() -> list[dict]:
    """design §3.4 #4: a hard/block class that has a detector for one of its writers but none for another.
    Warnings only (printed in the lint report, never a failure)."""
    rows = []
    for cid, c in classes().items():
        if c.get("severity") not in ("hard", "block") or c.get("kind") == "meta":
            continue
        writers = list(c.get("writers") or [])
        covered = set(c["covered_writers"])
        missing = [w for w in writers if w not in covered]
        if missing and covered:
            rows.append({"class": cid, "severity": c["severity"], "covered": sorted(covered), "missing": missing})
    return rows


def enforce_gaps() -> list[str]:
    """hard/block classes with no detector in mode enforce (warning in the lint report)."""
    return [cid for cid, c in classes().items()
            if c.get("severity") in ("hard", "block") and c.get("kind") != "meta" and not c["owner_gates"]]
