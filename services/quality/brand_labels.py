"""AA-1079 (S277) — brand-fit and core-idea labels on the AA-729 golden set: the label format, the panel pack and the
apply step (pure; no DB, no model call).

Why. The AA-741 phase 3 audit (root ``docs/audits/2026-11-10-S276-aa741-phase3/`` §5) found the T2 judge scoring brand
fit 5.9-6.3 against a 7 pass line while the stored scores read 9.9-10, the T7 Debate flagging 499 of 502 candidates
with the same judge, and the brand's core idea in 5 of 32 texts. Nothing says which number is right: no human-anchored
brand label exists. This module defines one, per golden item, next to the item's content label:

    sample["label"]["brand"] = {
        "rubric": "aa1079-brand-v1",
        "brand_fit": "fits" | "partly" | "off",
        "core_idea": "present" | "absent" | "unclear",
        "reason": "<= 600 chars", "source": "agent_panel" | "tech_lead", "status": "agreed" | "disputed",
        "votes": [{"lens", "brand_fit", "core_idea"}...], "approved_by": "<who>", "labelled_at": "<iso>"}

It lives in the golden item's ``label`` jsonb (``shared.eval_golden_item.label``, imported unchanged by
``services/golden_set/importer.py``), so no table or migration is added: a label set version is a fixture version,
as for the content labels (``scripts/aa729/labels.py``). The content verdict (``label.verdict``) is untouched.

Which items: the tenant-voiced writers (T2 tour versions, T9 blog and social pieces), never the Master (A1 has no
tenant brand) and never an injection trap. The panel pack shows the brand pack and the text, never the pipeline's own
verdict, scores, gate ledger or flags (the label must not be anchored on the judge it calibrates; lesson S245).

The panel run itself (agents labelling the pack) is the orchestrator's step, not code here:
``scripts/aa729/brand_labels.py export-pack`` / ``apply``.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Optional

BRAND_RUBRIC_VERSION = "aa1079-brand-v1"
BRAND_FIT_VALUES = ("fits", "partly", "off")
CORE_IDEA_VALUES = ("present", "absent", "unclear")
BRAND_SOURCES = ("agent_panel", "tech_lead")
BRAND_STATUSES = ("agreed", "disputed")
BRAND_WRITERS = ("t2", "t9_blog", "t9_social")
REASON_MAX = 600
# Output keys that carry the pipeline's judgement (T9 gate results) and are hidden from the panel.
HIDDEN_OUTPUT_KEYS = frozenset({"gate_ledger", "flags"})
# Brand-pack fields shown to the panel (T2: the tenant brand rules of the fixture's shared inputs).
T2_BRAND_FIELDS = ("brand_type", "core_idea", "customer_segment", "customer_mindset", "voice_examples",
                   "good_examples", "style_guide", "forbidden_words")

BRAND_RUBRIC = {
    "version": BRAND_RUBRIC_VERSION,
    "brand_fit": {
        "fits": "The text reads as this brand: its voice matches the brand's voice examples and style, it speaks to "
                "the brand's customer segment and mindset, and nothing in it pulls against the brand.",
        "partly": "Recognisably adapted to the brand, but generic in places, or one of voice and audience is missing.",
        "off": "Generic travel copy any agency could publish, or a voice or audience that contradicts the brand.",
    },
    "core_idea": {
        "present": "The brand's core idea is stated or clearly shown in the text (a paraphrase counts).",
        "absent": "The core idea is not shown anywhere in the text.",
        "unclear": "The brand pack has no core idea, or the text cannot be judged on it.",
    },
    "rules": [
        "Judge the text you see against the brand pack only.",
        "Facts against the source are not part of this label (the claim labels cover them); neither is length.",
        "Do not reward a brand name or a slogan pasted in: the voice and the audience decide.",
        "Two different brands writing the same tour may both fit; label each text on its own brand.",
    ],
}


class BrandLabelError(ValueError):
    """A brand label that does not follow the format."""


def is_brand_item(sample: dict) -> bool:
    """A golden item that takes a brand label: a T2 or T9 output that is not an injection trap."""
    from services.golden_set.derive import writer_and_channel

    if sample.get("stratum") == "injection" or str(sample.get("kind") or "").startswith("injection"):
        return False
    try:
        writer, _channel = writer_and_channel(sample)
    except Exception:
        return False
    return writer in BRAND_WRITERS


def make_brand_label(brand_fit: str, core_idea: str, *, source: str, reason: str = "", status: str = "agreed",
                     votes: Optional[list] = None, approved_by: Optional[str] = None,
                     labelled_at: Optional[str] = None) -> dict:
    label = {"rubric": BRAND_RUBRIC_VERSION, "brand_fit": brand_fit, "core_idea": core_idea,
             "reason": (reason or "")[:REASON_MAX], "source": source, "status": status,
             "votes": [{k: v.get(k) for k in ("lens", "brand_fit", "core_idea")} for v in (votes or [])],
             "approved_by": approved_by,
             "labelled_at": labelled_at or datetime.now(timezone.utc).isoformat()}
    validate_brand_label(label)
    return label


def validate_brand_label(label: Any) -> None:
    """Raises BrandLabelError when ``label`` does not follow the format of the module docstring."""
    if not isinstance(label, dict):
        raise BrandLabelError("a brand label is an object")
    if label.get("rubric") != BRAND_RUBRIC_VERSION:
        raise BrandLabelError(f"rubric must be {BRAND_RUBRIC_VERSION!r}")
    if label.get("brand_fit") not in BRAND_FIT_VALUES:
        raise BrandLabelError(f"brand_fit must be one of {BRAND_FIT_VALUES}")
    if label.get("core_idea") not in CORE_IDEA_VALUES:
        raise BrandLabelError(f"core_idea must be one of {CORE_IDEA_VALUES}")
    if label.get("source") not in BRAND_SOURCES:
        raise BrandLabelError(f"source must be one of {BRAND_SOURCES}")
    if label.get("status") not in BRAND_STATUSES:
        raise BrandLabelError(f"status must be one of {BRAND_STATUSES}")
    if len(label.get("reason") or "") > REASON_MAX:
        raise BrandLabelError(f"reason is longer than {REASON_MAX} chars")
    if label["source"] == "agent_panel" and not (label.get("approved_by") or "").strip():
        raise BrandLabelError("a panel brand label needs approved_by")
    for v in label.get("votes") or []:
        if v.get("brand_fit") not in BRAND_FIT_VALUES or v.get("core_idea") not in CORE_IDEA_VALUES:
            raise BrandLabelError("every vote carries a brand_fit and a core_idea value")


def brand_label_of(sample: dict) -> Optional[dict]:
    """The item's brand label, or None (no label, or one that does not follow the format)."""
    label = ((sample.get("label") or {}) if isinstance(sample.get("label"), dict) else {}).get("brand")
    try:
        validate_brand_label(label)
    except BrandLabelError:
        return None
    return label


def label_passes(label: dict, *, lenient: bool = False) -> bool:
    """Binary reading of a brand label: strict = only ``fits`` passes; lenient = ``fits`` or ``partly``."""
    return label["brand_fit"] == "fits" or (lenient and label["brand_fit"] == "partly")


# ── the panel pack ───────────────────────────────────────────────────────────────────────────────────────────────

def _t2_brand_pack(fixture: dict, sample: dict) -> dict:
    ref = (sample.get("input") or {}).get("brand_rules_ref") or ""
    rules = ((fixture.get("shared_inputs") or {}).get("brand_rules") or {}).get(ref) or {}
    return {k: rules.get(k) for k in T2_BRAND_FIELDS if rules.get(k) not in (None, "", [])}


def _t9_brand_pack(sample: dict) -> dict:
    ctx = (sample.get("input") or {}).get("context") or {}
    out = {"audience": ctx.get("brand_audience") or {}}
    if ctx.get("brand_rubric_text"):
        out["rubric_text"] = ctx["brand_rubric_text"]
    return out


def panel_text(output: Any) -> Any:
    if isinstance(output, dict):
        return {k: v for k, v in output.items() if k not in HIDDEN_OUTPUT_KEYS}
    return output


def export_brand_pack(fixture: dict, *, relabel: bool = False) -> dict:
    """The pack the brand panel reads: per brand item without a brand label (every brand item with ``relabel``), the
    brand pack, the writer, the channel and the text. No pipeline verdict, score, gate ledger, flag or selection tag."""
    from services.golden_set.derive import writer_and_channel

    items = []
    for s in fixture.get("samples") or []:
        if not is_brand_item(s) or (brand_label_of(s) is not None and not relabel):
            continue
        writer, channel = writer_and_channel(s)
        pack = _t2_brand_pack(fixture, s) if writer == "t2" else _t9_brand_pack(s)
        items.append({"sample_id": s["sample_id"], "writer": writer, "channel": channel,
                      "brand_group": (s.get("refs") or {}).get("tenant_slug") or s.get("group"),
                      "brand_pack": pack, "output": panel_text(s.get("output"))})
    return {"fixture_version": fixture.get("fixture_version"), "rubric": BRAND_RUBRIC, "count": len(items),
            "answer_shape": {"<sample_id>": {"brand_fit": list(BRAND_FIT_VALUES), "core_idea": list(CORE_IDEA_VALUES),
                                             "reason": "string", "status": list(BRAND_STATUSES),
                                             "votes": [{"lens": "string", "brand_fit": "...", "core_idea": "..."}]}},
            "items": items}


def apply_brand_labels(fixture: dict, panel: dict, *, approved_by: str, include_disputed: bool = False,
                       source: str = "agent_panel", bump=None) -> tuple[dict, dict]:
    """A new fixture (minor version bump) with the panel's brand labels. A tech-lead label is never overwritten by a
    panel label; a disputed panel answer is skipped unless ``include_disputed`` (after the tech lead decided it). The
    content label (``label.verdict`` and the rest) is untouched."""
    if not (approved_by or "").strip():
        raise ValueError("approved_by is required (the tech lead approves panel labels)")
    if source not in BRAND_SOURCES:
        raise ValueError(f"source must be one of {BRAND_SOURCES}")
    out = copy.deepcopy(fixture)
    by_id = {s["sample_id"]: s for s in out.get("samples") or []}
    applied, skipped = [], []
    for sid, ans in sorted((panel or {}).items()):
        s = by_id.get(sid)
        if s is None:
            skipped.append((sid, "unknown sample"))
            continue
        if not is_brand_item(s):
            skipped.append((sid, "not a brand item"))
            continue
        old = brand_label_of(s)
        if old is not None and old["source"] == "tech_lead" and source != "tech_lead":
            skipped.append((sid, "has a tech-lead brand label"))
            continue
        status = (ans or {}).get("status") or "agreed"
        if status == "disputed" and not include_disputed:
            skipped.append((sid, "disputed"))
            continue
        try:
            label = make_brand_label(ans.get("brand_fit"), ans.get("core_idea"), source=source,
                                     reason=ans.get("reason") or "", status=status, votes=ans.get("votes"),
                                     approved_by=approved_by)
        except (BrandLabelError, AttributeError) as e:
            skipped.append((sid, f"invalid answer: {e}"))
            continue
        if not isinstance(s.get("label"), dict):
            skipped.append((sid, "no content label object"))
            continue
        if old is not None:
            label["previous"] = {k: old.get(k) for k in ("brand_fit", "core_idea", "source", "labelled_at")}
        s["label"]["brand"] = label
        applied.append(sid)
    bump = bump or bump_minor
    old_version = out.get("fixture_version") or "0.0.0"
    out["fixture_version"] = bump(old_version)
    out.setdefault("label_history", []).append({
        "from_version": old_version, "to_version": out["fixture_version"], "source": f"{source}_brand",
        "rubric": BRAND_RUBRIC_VERSION, "approved_by": approved_by, "applied": len(applied),
        "at": datetime.now(timezone.utc).isoformat()})
    left = sum(1 for s in out.get("samples") or [] if is_brand_item(s) and brand_label_of(s) is None)
    return out, {"applied": applied, "skipped": skipped, "fixture_version": out["fixture_version"],
                 "brand_unlabelled_left": left}


def bump_minor(version: str) -> str:
    """``x.y.z`` -> ``x.(y+1).0`` (the AA-729 fixture rule, scripts/aa729/common.bump_minor)."""
    major, minor, _patch = (str(version).split(".") + ["0", "0"])[:3]
    return f"{int(major)}.{int(minor) + 1}.0"
