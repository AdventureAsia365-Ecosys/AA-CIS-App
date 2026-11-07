"""AA-900 W0 (root ADR 0020, decisions D1/D3/D4/D15) — the interim F9 policy and the F9 failure classes.

Why: the F9 judge (`quality_gates.gate_brand_voice`) files every product-truth reason under one blocking
`F9_cta_fact` entry: an unsupported claim of any kind, a missing CTA, a broken sentence (TEXT_INTEGRITY), and, as a
non-blocking entry, a judge outage. The S271 audit (critic F15) found that counting that entry inflates the fidelity
baseline with infrastructure and text failures, and that most holds are claims the policy allows (place background,
reflection, hypotheticals). Until the policy registry and a calibrated claim instrument exist (W1/W2), F9 may hold
only on the class every decision agrees on.

Interim rule (declared diff, mode `interim`, the default):

* `F9_cta_fact` stays BLOCKING only for a quoted body claim with a `why_not_in_source` that names a concrete trip
  activity, service, time or view (`trip_promise_kinds`). Its violations are rebuilt with those claims only, in the
  stored format, so the sentence repair targets them alone.
* every other F9 class warns: the entry is stored `blocking: false` (it goes to the flags, the admin still sees
  it) — place background and other unsupported claims, a missing or unclear CTA (the configured CTA is put in word
  for word by `hygiene.ensure_cta`), TEXT_INTEGRITY, a title / meta claim, a fact flag with no named claim.
* the entry records what was decided in `f9_policy` (`version`, `classes`, `warned`, `blocking_before`).

`T10_F9_POLICY_MODE=legacy` keeps the pre-W0 verdict (the rollback switch; a replay sets it to show the diff).

`violation_classes` / `entry_classes` classify a stored entry (any era: the pre-AA-925 "… treated as fail" outage
text too), so counts can keep `judge_unavailable` and `text_integrity` apart from the fidelity classes.

Pure: no DB, no LLM, no I/O. Imports nothing from quality_gates / sentence_repair (both import this module).
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

from services.acp_content_writing.claim_rules import mask_names
from services.quality import registry as qr
from services.quality import writing_policy as _wp

POLICY_VERSION = "f9-interim-v1"
F9_POLICY_MODE_ENV = "T10_F9_POLICY_MODE"
INTERIM_MODE = "interim"
LEGACY_MODE = "legacy"

# The stored form of one unsupported body claim inside an F9_cta_fact violation (sentence_repair and the admin
# highlight parse it through this one pattern).
F9_CLAIM_RE = re.compile(r'"(.+?)" \(claims: (.+?); source: (.+?)\)(?:; |$)', re.DOTALL)
CLAIMS_MARK = "unsupported claim(s):"
TEXT_INTEGRITY_PREFIX = "TEXT_INTEGRITY: "
_SEO_FIELD_PREFIXES = ("title ", "meta ")
# quality_gates.F9_NO_REASON (a test keeps the two equal; this module cannot import quality_gates)
NO_REASON_MARK = "judge gave no checkable reason"
_STATUS_RE = re.compile(r"^audit (\w+):")

# ---------------------------------------------------------------------------------------------- classes

TRIP_PROMISE = "trip_promise"                  # holds (interim)
UNSUPPORTED_OTHER = "unsupported_other"        # place background, generalisation … (warn until W1)
FACT_CHECK_UNQUOTED = "fact_check_unquoted"    # FACT_CHECK with phrases, no named claim (no-source prompt)
SEO_FIELD_CLAIM = "seo_field_claim"            # a claim in the search title / meta
CTA_NOT_REFLECTED = "cta_not_reflected"
TEXT_INTEGRITY = "text_integrity"
JUDGE_UNAVAILABLE = "judge_unavailable"        # infrastructure: no verdict
REASONLESS = "reasonless"                      # a verdict with no checkable reason
OTHER = "other"

FIDELITY_CLASSES = frozenset({TRIP_PROMISE, UNSUPPORTED_OTHER, FACT_CHECK_UNQUOTED, SEO_FIELD_CLAIM})
INFRASTRUCTURE_CLASSES = frozenset({JUDGE_UNAVAILABLE})
# AA-1047: the classes that may hold are the writing policy's F9 route rows (writing_policy.yaml f9_class_rows).
HOLD_CLASSES = _wp.f9_hold_classes()

# ---------------------------------------------------------------------------------------------- trip promise

_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|several|a few|a couple of|half an?)"
# Who the sentence makes the trip happen to: the reader, the operator, the group, the trip itself, a day of it.
TRIP_SUBJECT_RE = re.compile(
    r"\b(?:you|your|yourself|we|our|us|travell?ers?|guests?|participants|everyone on (?:the|this) (?:trip|tour)"
    r"|(?:the|your|our|each|every) group|(?:this|the) (?:trip|tour|journey|itinerary|route|holiday|walk|day)"
    r"|(?:on|by) day " + _NUM + r"|day \d+)\b", re.I)
# A service or inclusion the trip provides.
SERVICE_RE = re.compile(
    r"\b(?:guides?|guided|guiding|transfers?|drivers?|porters?|hotels?|lodges?|camps?|tents?|homestays?|"
    r"guesthouses?|breakfasts?|lunch(?:es)?|dinners?|meals?|picnics?|tastings?|tickets?|permits?|visas?|"
    r"(?:entry|entrance) fees?|boat (?:ride|trip)s?|jeeps?|safaris?|(?:cooking|craft) (?:class|lesson)(?:es|s)?|"
    r"workshops?|briefings?)\b", re.I)
INCLUSION_RE = re.compile(
    r"\b(?:includ\w*|arranged|provided|organi[sz]ed|laid on|booked|reserved|planned|scheduled|built in|"
    r"on the itinerary|part of (?:the|this|your) (?:trip|tour|day|itinerary))\b", re.I)
TIME_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)(?!\w)|\b\d{1,2}:\d{2}\b|\bo'clock\b"
    r"|\b(?:at|by|before|after|around|until|from) (?:dawn|sunrise|sunset|dusk|daybreak|first light|nightfall|noon|"
    r"midday|midnight|lunchtime|night)\b"
    r"|\b(?:early|late|this|that|next|each|every|in the|by|one|the following) (?:morning|afternoon|evening|night)\b"
    r"|\b" + _NUM + r"[- ](?:hours?|minutes?|nights?|days?)\b|\bovernight\b"
    r"|\b(?:all|half)[- ](?:day|morning|afternoon)\b",
    re.I)
VIEW_RE = re.compile(
    r"\b(?:views?|viewpoints?|vistas?|panoram\w*|overlook\w*|sweeping|look(?:s|ing)? (?:out )?(?:over|across|down|"
    r"onto)|(?:see|sees|seeing|watch|watches|watching|spot|spotting|glimpse|catch sight of|witness)|"
    r"sunrise over|sunset over)\b", re.I)
# A reader / operator / group subject followed (within three words, no clause break) by a trip activity verb.
_ACT_SUBJECT = r"(?:you|yourself|we|travell?ers|guests|the group|your group|our group|everyone)"
_ACT_VERB = (r"(?:board|ride|rode|hike|trek|walk|cycl|bik|kayak|paddl|raft|swim|swam|snorkel|div|climb|sail|cruis|"
             r"visit|explor|stop|stay|spend|spent|sleep|slept|meet|met|join|tast|sampl|cook|learn|driv|drove|fly|flew|"
             r"cross|camp|reach|arriv|continu|head|descend|ascend|tour|picnic)\w*")
ACTIVITY_RE = re.compile(
    rf"\b{_ACT_SUBJECT}(?:'ll|'re|'ve)?(?:\s+(?:will|can|could|may|might|then|also|first|finally|get to|go on to|"
    rf"set off to|are able to))?(?:\s+[a-z]+){{0,2}}?\s+{_ACT_VERB}\b", re.I)
_QUOTES = str.maketrans({"’": "'", "‘": "'"})


def trip_promise_kinds(sentence: Optional[str], lexicon=None) -> tuple[str, ...]:
    """The concrete trip promise kinds a sentence names: `service`, `time`, `view`, `activity` (D1/D4/D15), () when
    it names none. A question names none (an FAQ question asks, it does not promise). Place names the scope lexicon
    knows are blanked first (claim_rules.mask_names: 'Sunset Point', 'Camp Road' do not count)."""
    text = (sentence or "").translate(_QUOTES).strip()
    if not text or text.rstrip(" *\"')").endswith("?"):
        return ()
    masked = mask_names(text, lexicon=lexicon) if lexicon is not None else text
    subject = bool(TRIP_SUBJECT_RE.search(masked))
    kinds: list[str] = []
    if SERVICE_RE.search(masked) and (subject or INCLUSION_RE.search(masked)) or (
            subject and INCLUSION_RE.search(masked)):
        kinds.append("service")
    if subject and TIME_RE.search(masked):
        kinds.append("time")
    if subject and VIEW_RE.search(masked):
        kinds.append("view")
    if ACTIVITY_RE.search(masked):
        kinds.append("activity")
    return tuple(kinds)


# ---------------------------------------------------------------------------------------------- classification

def parse_claims(violation: str) -> list[tuple[str, str, str]]:
    """(sentence, claim, why_not_in_source) of every unsupported body claim in one stored violation."""
    if CLAIMS_MARK not in (violation or ""):
        return []
    return [(s.strip(), c.strip(), w.strip()) for s, c, w in
            F9_CLAIM_RE.findall(violation.split(CLAIMS_MARK, 1)[1])]


def violation_items(violation: str, lexicon=None) -> list[dict]:
    """One {class, …} item per reason in one stored F9_cta_fact violation (any era). A claim item carries its
    `sentence`, `claim`, `why` and, for a trip promise, its `kinds`."""
    v = str(violation or "")
    if v.startswith(_SEO_FIELD_PREFIXES):
        return [{"class": SEO_FIELD_CLAIM}]
    if v.startswith(TEXT_INTEGRITY_PREFIX):
        return [{"class": TEXT_INTEGRITY}]
    if "judge unavailable" in v or "treated as fail" in v:
        return [{"class": JUDGE_UNAVAILABLE}]
    if v.startswith(NO_REASON_MARK):
        return [{"class": REASONLESS}]
    head = v.split(CLAIMS_MARK, 1)[0]
    items: list[dict] = []
    if "CTA_NOT_REFLECTED" in head:
        items.append({"class": CTA_NOT_REFLECTED})
    claims = parse_claims(v)
    for sentence, claim, why in claims:
        kinds = trip_promise_kinds(sentence, lexicon) if why else ()
        items.append({"class": TRIP_PROMISE if kinds else UNSUPPORTED_OTHER, "sentence": sentence, "claim": claim,
                      "why": why, **({"kinds": list(kinds)} if kinds else {})})
    if not claims and "FACT_CHECK_MANUAL_CHECK" in head:
        items.append({"class": FACT_CHECK_UNQUOTED})
    return items or [{"class": OTHER}]


def violation_classes(violation: str, lexicon=None) -> list[str]:
    return [it["class"] for it in violation_items(violation, lexicon)]


def entry_classes(g: dict, lexicon=None) -> dict[str, int]:
    """{class: count} of one failed F9_cta_fact entry ({} for any other entry or a pass). An entry the interim policy
    already decided keeps the classes it recorded (its warned violations are no longer in `violations`)."""
    if not isinstance(g, dict) or g.get("gate") != qr.F9_CTA_FACT or g.get("passed"):
        return {}
    recorded = (g.get("f9_policy") or {}).get("classes") if isinstance(g.get("f9_policy"), dict) else None
    if isinstance(recorded, dict):
        return {str(k): int(n) for k, n in recorded.items()}
    out: dict[str, int] = {}
    for v in g.get("violations") or []:
        for cls in violation_classes(v, lexicon):
            out[cls] = out.get(cls, 0) + 1
    return out


def _claims_violation(status: str, claims: Iterable[dict]) -> str:
    return (f"audit {status}: FACT_CHECK_MANUAL_CHECK — {CLAIMS_MARK} "
            + "; ".join(f'"{c["sentence"]}" (claims: {c["claim"]}; source: {c["why"]})' for c in claims))


def policy_mode() -> str:
    raw = (os.environ.get(F9_POLICY_MODE_ENV) or "").strip().lower()
    return raw if raw in (INTERIM_MODE, LEGACY_MODE) else INTERIM_MODE


def apply_policy(g: dict, lexicon=None, *, mode: Optional[str] = None) -> dict:
    """The F9_cta_fact entry under the F9 policy mode (`policy_mode()` unless given). Any other entry, a pass and
    the legacy mode on an undecided entry are returned unchanged; the legacy mode on an entry the interim policy
    decided puts its stored violations and blocking back. Idempotent."""
    if not isinstance(g, dict) or g.get("gate") != qr.F9_CTA_FACT or g.get("passed"):
        return g
    mode = mode or policy_mode()
    decided = g.get("f9_policy") if isinstance(g.get("f9_policy"), dict) else None
    if mode == LEGACY_MODE:
        if decided is None:
            return g
        out = {k: v for k, v in g.items() if k != "f9_policy"}
        out["violations"] = list(g.get("violations") or []) + list(decided.get("warned") or [])
        out["blocking"] = bool(decided.get("blocking_before", True))
        return out
    if decided is not None:
        return g
    blocking_before = g.get("blocking", True)
    classes: dict[str, int] = {}
    hold_claims: list[dict] = []
    warned: list[str] = []
    status = "flagged"
    for v in g.get("violations") or []:
        items = violation_items(v, lexicon)
        for it in items:
            classes[it["class"]] = classes.get(it["class"], 0) + 1
        m = _STATUS_RE.match(str(v))
        status = m.group(1) if m else status
        held = [it for it in items if it["class"] in HOLD_CLASSES]
        hold_claims += held
        rest = [it for it in items if it["class"] not in HOLD_CLASSES]
        if not held:
            warned.append(v)
        elif rest:
            # the claims that warn are kept as their own violation (the CTA part of a combined one is dropped from
            # the repair: the configured CTA is put in word for word in code)
            other = [it for it in rest if "sentence" in it]
            warned.append(_claims_violation(status, other) if other else str(v).split(" — ", 1)[0])
    hold = bool(hold_claims) and blocking_before
    record = {"version": POLICY_VERSION, "classes": classes, "warned": warned if hold else [],
              "blocking_before": blocking_before}
    if hold:
        return {**g, "violations": [_claims_violation(status, hold_claims)], "blocking": True, "f9_policy": record}
    return {**g, "blocking": False, "f9_policy": record}


__all__ = [
    "POLICY_VERSION", "F9_POLICY_MODE_ENV", "INTERIM_MODE", "LEGACY_MODE", "F9_CLAIM_RE", "CLAIMS_MARK",
    "NO_REASON_MARK", "TRIP_PROMISE", "UNSUPPORTED_OTHER", "FACT_CHECK_UNQUOTED", "SEO_FIELD_CLAIM",
    "CTA_NOT_REFLECTED", "TEXT_INTEGRITY", "JUDGE_UNAVAILABLE", "REASONLESS", "OTHER", "FIDELITY_CLASSES",
    "INFRASTRUCTURE_CLASSES", "HOLD_CLASSES", "trip_promise_kinds", "parse_claims", "violation_items",
    "violation_classes", "entry_classes", "policy_mode", "apply_policy",
]
