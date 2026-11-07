"""AA-1048 — the deterministic detectors measured on claim labels (root audit W2: "meal/clock regex, elephant, day
codes, novel numbers, Optional, meta band, T3 lexicon, F1/F12/F17/F18; thresholds rows filled in, gate modes
derived"), the tripwires (critic F7: TRIP_RE, CONCRETE_TRIP_RE, PROMISE_FRAME_RE) and recorded producers (critic
F10: an A3 evidence producer's stored verdict per claim, measured the same way).

Each detector is a function of one claim (its sentence, its field, the source the writer read, the piece's scope)
that says whether it fires. The harness runs every detector over a label set and writes one row per (detector,
class): fires, true / false positives, precision and its Wilson-95 lower bound, recall on the labelled violations,
and the gate mode the numbers allow (enforce only when the bar passes; a tripwire that does not pass may only route a
sentence to the claim engine, never hold). The rows have the shape of services/quality/thresholds.yaml calibration
fields; they are written to a file for review, never into thresholds.yaml by this code.

Precision is the detector's, over EVERY labelled claim it ran on (a gate in enforce mode holds on any fire): a true
positive is a fire on a violation of one of its classes; any other fire is a false positive (on an ok claim of any
class, or on a violation of an unrelated class, counted apart as fp_other_class). Recall is per class. The bar
(sample_size.BARS[1]) also needs the class's labeller agreement >= 0.7 (panel.agreement_report; unknown does not
pass) and at least one caught violation of the class (tp_class >= 1: a multi-class detector's precision never lets a
class it does not catch pass), and only a held-out measurement may pass: a train or pooled measurement is exploration
(passes false, mode never enforce). A recorded producer is measured against the classes it declares (the export line's
`recorded_classes`); one with no declared class is measured against every labelled class for exploration and never
passes (a fire on a violation of an unrelated class would otherwise count as a true positive, review S272).

Detectors call the production functions (no copy of a rule). Pure apart from those functions: no DB, no network, no
model call.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from services.ground_truth import sample_size as ss
from services.quality import registry as qr
from services.quality import writing_policy as _wp

# D5: one meta band for every workflow (from writing_policy.yaml).
META_BAND = _wp.band("meta")
META_FIELDS = frozenset({"meta_description", "seo_meta", "meta"})
MODE_ENFORCE, MODE_WARN, MODE_ROUTE = "enforce", "warn", "route_only"


@dataclass(frozen=True)
class ClaimInput:
    """What a detector reads about one labelled claim."""
    claim_hash: str
    writer: str
    field: str
    text: str
    source_text: str
    scope_name: Optional[str] = None
    country: Optional[str] = None
    recorded: dict = field(default_factory=dict)      # producer name -> fired (bool), critic F10
    recorded_classes: dict = field(default_factory=dict)   # producer name -> the policy classes it checks
    split: str = "train"                              # claims.split_of(claims.split_group(refs, content sha))


@dataclass(frozen=True)
class Detector:
    name: str
    classes: tuple[str, ...]          # the policy classes its fires are measured against (() = none declared)
    kind: str                         # rule | tripwire | recorded
    fn: Callable[[ClaimInput], bool]

    @property
    def declared(self) -> bool:
        """The detector names its classes: only then may a row pass (an undeclared recorded producer is explored)."""
        return bool(self.classes)


# ── adapters over the production functions ─────────────────────────────────────────────────────────────────────


def _meal_clock(c: ClaimInput) -> bool:
    """S1 ITINERARY_MEAL_TIME_INVENTED, its regex part (brand_audit_node): a clock time the source does not have, or
    a meal claim (the Jev clearance that follows in production is an LLM step, measured with the instruments)."""
    from services.content_generation.brand_audit_node import ITIN_CLOCK_TIME, ITIN_MEAL_INVENTED, _norm_clock

    src = {_norm_clock(m.group(0)) for m in ITIN_CLOCK_TIME.finditer(c.source_text)}
    if any(_norm_clock(m.group(0)) not in src for m in ITIN_CLOCK_TIME.finditer(c.text)):
        return True
    return bool(ITIN_MEAL_INVENTED.search(c.text))


def _elephant(c: ClaimInput) -> bool:
    from services.quality.elephant_policy import mentions_riding

    return mentions_riding(c.text, c.country)


def _day_codes(c: ClaimInput) -> bool:
    """The day oracle's DAY_OUTPUT_EXTRA at claim level: a "Day N" the claim names that the source does not have."""
    from services.quality.day_oracle import source_days

    days, _markers = source_days(c.source_text, None)
    named = {int(n) for n in re.findall(r"\bday\s+(\d{1,2})\b", c.text, flags=re.I)}
    if not named or days is None:
        return False
    return any(n not in days for n in named)


def _novel_numbers(c: ClaimInput) -> bool:
    from services.acp_shared.grounding import find_novel_numeric_claims

    return bool(find_novel_numeric_claims(c.text, [c.source_text]))


def _optional_as_fixed(c: ClaimInput) -> bool:
    from services.acp_content_writing.trip_facts import optional_as_fixed, parse_moments

    return bool(optional_as_fixed(c.text, parse_moments(c.source_text)))


def _meta_band(c: ClaimInput) -> bool:
    if c.field not in META_FIELDS:
        return False
    return not META_BAND[0] <= len(c.text.strip()) <= META_BAND[1]


def _t3_lexicon(c: ClaimInput) -> bool:
    """T3 grounding's service-level lexicon (AA-800, signature_vocabulary.service_level_claims)."""
    from services.acp_shared.signature_vocabulary import service_level_claims

    return bool(service_level_claims(c.text, c.source_text))


def _f1(c: ClaimInput) -> bool:
    from services.acp_content_writing.quality_gates import gate_grounding

    return bool(gate_grounding(c.text, c.source_text)["violations"])


def _f12(c: ClaimInput) -> bool:
    from services.acp_content_writing.hygiene import HygieneContext
    from services.acp_content_writing.quality_gates import gate_scope

    if not c.scope_name:
        return False
    return bool(gate_scope(c.text, HygieneContext(seed=c.source_text, scope=c.scope_name))["violations"])


def _trip_facts(c: ClaimInput) -> tuple[bool, bool]:
    from services.acp_content_writing.quality_gates import gate_trip_facts
    from services.acp_content_writing.trip_facts import TripFactContext

    f17, f18 = gate_trip_facts(c.text, TripFactContext(seed=c.source_text, source=c.source_text,
                                                       channel="blog" if c.writer == "t9_blog" else None))
    return bool(f17["violations"]), bool(f18["violations"])


def _names(c: ClaimInput) -> bool:
    """S1 NAME_NOT_IN_SOURCE (s1_checks.check_names) on the claim alone, the source as the tour's itinerary text."""
    from services.quality.s1_checks import check_names

    return bool(check_names({"itineraries": c.source_text}, {"summary": c.text}))


def _tripwire(attr: str) -> Callable[[ClaimInput], bool]:
    def fn(c: ClaimInput) -> bool:
        from services.acp_content_writing import claim_rules

        return bool(getattr(claim_rules, attr).search(c.text))
    fn.__name__ = f"tripwire_{attr.lower()}"
    return fn


DETECTORS: tuple[Detector, ...] = (
    Detector("meal_clock_regex", ("fact.inclusion", "other.f7"), "rule", _meal_clock),
    Detector("elephant_riding", ("fact.inclusion",), "rule", _elephant),
    Detector("day_codes", ("struct.day_structure",), "rule", _day_codes),
    Detector("novel_numbers", ("fact.number",), "rule", _novel_numbers),
    Detector("optional_as_fixed", ("other.f6",), "rule", _optional_as_fixed),
    Detector("meta_band", ("other.length_shape",), "rule", _meta_band),
    Detector("t3_lexicon", ("fact.inclusion",), "rule", _t3_lexicon),
    Detector("s1_names", ("fact.name",), "rule", _names),
    Detector(qr.F1_GROUNDING, ("fact.number",), "rule", _f1),
    Detector(qr.F12_SCOPE, ("scope.framing",), "rule", _f12),
    Detector(qr.F17_TRIP_FACT, ("other.f6", "other.f7", "fact.misbinding", "other.c3"), "rule",
             lambda c: _trip_facts(c)[0]),
    Detector(qr.F18_RARITY, ("fact.embellish",), "rule", lambda c: _trip_facts(c)[1]),
    Detector("TRIP_RE", ("fact.inclusion",), "tripwire", _tripwire("TRIP_RE")),
    Detector("CONCRETE_TRIP_RE", ("fact.inclusion",), "tripwire", _tripwire("CONCRETE_TRIP_RE")),
    Detector("PROMISE_FRAME_RE", ("fact.inclusion",), "tripwire", _tripwire("PROMISE_FRAME_RE")),
)


def by_name(names: Optional[Iterable[str]] = None) -> tuple[Detector, ...]:
    if not names:
        return DETECTORS
    wanted = set(names)
    unknown = wanted - {d.name for d in DETECTORS}
    if unknown:
        raise ValueError(f"unknown detector(s): {sorted(unknown)}")
    return tuple(d for d in DETECTORS if d.name in wanted)


def recorded_detectors(claims: Iterable[ClaimInput]) -> tuple[Detector, ...]:
    """One detector per producer name found in the claims' `recorded` maps (an A3 producer's stored verdict, critic
    F10), with the classes the claims declare for it (`recorded_classes`, their union). A claim without a record for
    it is not counted. With no declared class it is measured against every labelled class, for exploration only:
    `measure` never lets it pass (review S272)."""
    claims = list(claims)
    names = sorted({n for c in claims for n in c.recorded})
    out = []
    for n in names:
        declared = sorted({str(k) for c in claims for k in (c.recorded_classes.get(n) or ()) if str(k).strip()})
        out.append(Detector(f"recorded:{n}", tuple(declared), "recorded", (lambda c, n=n: bool(c.recorded.get(n)))))
    return tuple(out)


# ── measurement ────────────────────────────────────────────────────────────────────────────────────────────────


def fires(detectors: Iterable[Detector], claims: Iterable[ClaimInput]) -> tuple[dict[str, dict[str, bool]],
                                                                             dict[str, int]]:
    """(detector -> claim_hash -> fired, detector -> errors). A detector that raises on a claim is counted as an
    error, never as a fire or a pass, and the claim is left out of its row."""
    claims = list(claims)
    out: dict[str, dict[str, bool]] = defaultdict(dict)
    errors: dict[str, int] = defaultdict(int)
    for d in detectors:
        for c in claims:
            if d.kind == "recorded" and d.name.split(":", 1)[1] not in c.recorded:
                continue
            try:
                out[d.name][c.claim_hash] = bool(d.fn(c))
            except Exception:                                   # noqa: BLE001 — counted in the row, never hidden
                errors[d.name] += 1
    return out, errors


def _mode(kind: str, ok: bool) -> str:
    if ok:
        return MODE_ENFORCE
    return MODE_ROUTE if kind == "tripwire" else MODE_WARN


SPLIT_CHOICES = ("held_out", "train", "all")


def measure(detectors: Iterable[Detector], claims: list[ClaimInput], labels: dict[str, tuple[str, str]], *,
            label_set: str, pooled: bool = True, split: str = "held_out",
            agreement: Optional[dict[str, Optional[float]]] = None) -> list[dict]:
    """One row per (detector, class[, writer]). `labels`: claim_hash -> (class, label) final labels (unsure ones are
    ignored). `split` keeps the claims of that split ("all" pools both: exploration only). `agreement`: class -> the
    panel's labeller agreement (panel.agreement_report); a class without one cannot pass.

    Per detector, over every labelled claim it ran on: tp = fires on violations of one of its classes, fp = every
    other fire (fp_ok on ok claims, fp_other_class on violations of an unrelated class), precision = tp / fires.
    Per class: tp_class / n_pos = recall; a class passes only with tp_class >= 1 and only for a detector that
    declares its classes (an undeclared recorded producer is exploration). fire_rate = fires / labelled claims,
    violation_rate = n_pos / labelled claims (the label-budget inputs, sample_size.budget_rows). `pooled` (default,
    critic F2): writers pooled; False adds the writer to the key."""
    if split not in SPLIT_CHOICES:
        raise ValueError(f"split must be one of {SPLIT_CHOICES}, got {split!r}")
    detectors = tuple(detectors)
    claims = [c for c in claims if split == "all" or c.split == split]
    fired, errors = fires(detectors, claims)
    by_hash = {c.claim_hash: c for c in claims}
    agreement = agreement or {}
    bar = ss.BARS[1]
    rows = []
    for d in detectors:
        f = fired.get(d.name, {})
        ran = [h for h in f if h in labels and labels[h][1] in ("violation", "ok") and h in by_hash]
        classes = d.classes or tuple(sorted({labels[h][0] for h in ran}))
        targets = set(classes)
        groups: dict[str, list[str]] = defaultdict(list)
        for h in ran:
            groups["all" if pooled else by_hash[h].writer].append(h)
        if not groups:
            groups["all"] = []                           # still rows: no labelled claim (or every one raised)
        for writer, hashes in sorted(groups.items()):
            fires_ = [h for h in hashes if f.get(h)]
            tp = sum(1 for h in fires_ if labels[h][1] == "violation" and labels[h][0] in targets)
            fp_ok = sum(1 for h in fires_ if labels[h][1] == "ok")
            fp_other = len(fires_) - tp - fp_ok
            n = len(hashes)
            precision = round(tp / len(fires_), 4) if fires_ else None
            lb = None if not fires_ else round(ss.wilson_lower(tp, len(fires_)), 4)
            for cls in classes:
                n_pos = sum(1 for h in hashes if labels[h] == (cls, "violation"))
                tp_cls = sum(1 for h in fires_ if labels[h] == (cls, "violation"))
                agree = agreement.get(cls)
                meets = (ss.passes(tp, len(fires_), n_pos, bar, agreement=agree) and tp_cls >= 1
                         and d.declared)
                ok = meets and split == "held_out"
                rows.append({
                    "detector": d.name, "kind": d.kind, "class": cls, "writers": writer, "label_set": label_set,
                    "split": split, "n_labelled": n, "fired": len(fires_), "tp": tp, "fp": fp_ok + fp_other,
                    "fp_ok": fp_ok, "fp_other_class": fp_other, "tp_class": tp_cls, "fn": n_pos - tp_cls,
                    "n_pos": n_pos, "precision": precision, "precision_lb": lb,
                    "recall": round(tp_cls / n_pos, 4) if n_pos else None,
                    "fire_rate": round(len(fires_) / n, 4) if n else None,
                    "violation_rate": round(n_pos / n, 4) if n else None,
                    "agreement": agree, "errors": errors.get(d.name, 0), "bar": bar.name,
                    "classes_declared": d.declared, "meets_bar": meets,
                    "passes": ok, "mode": _mode(d.kind, ok)})
    return rows


def thresholds_rows(rows: list[dict]) -> dict:
    """The rows as thresholds.yaml-shaped entries (key det.<detector>.<class>[.<writer>]): the derived mode as the
    value, the measurement in the calibration fields. For review; promoting one into thresholds.yaml is a PR."""
    out = {}
    for r in rows:
        key = f"det.{r['detector']}.{r['class']}" + ("" if r["writers"] == "all" else f".{r['writers']}")
        out[key] = {"value": r["mode"], "used_by": [],
                    "description": f"AA-1048 W2 measurement of {r['detector']} on {r['class']} "
                                   f"({r['label_set']}, split {r['split']}, writers {r['writers']})",
                    "calibration": {"run_id": r["label_set"], "precision": r["precision"], "recall": r["recall"],
                                    "n": r["fired"], "calibrated": bool(r["passes"]),
                                    "precision_lb": r["precision_lb"], "n_pos": r["n_pos"], "split": r["split"],
                                    "agreement": r["agreement"]}}
    return {"version": "aa1048-det-v1", "thresholds": out}


__all__ = ["ClaimInput", "DETECTORS", "Detector", "META_BAND", "MODE_ENFORCE", "MODE_ROUTE", "MODE_WARN",
           "SPLIT_CHOICES", "by_name", "fires", "measure", "recorded_detectors", "thresholds_rows"]
