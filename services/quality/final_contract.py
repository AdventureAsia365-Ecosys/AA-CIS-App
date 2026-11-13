"""AA-1047 W1 (root ADR 0020 "Final contract", invariant 4) — the one exit contract of every AI-written tour page,
blog and social post, in REPORT mode.

Every path that stores or publishes text of S1 (the A1 Master rewrite), T2 (the tenant tour rewrite) and T9 (blogs and
social posts) passes through here as its last step (``EXITS`` names them; the topology test
``tests/unit/test_aa1047_final_contract_topology.py`` fails when a path does not). The contract runs deterministic,
$0 checks on the text as it is stored, decides the verdict the target design gives it, and RECORDS it next to what
the current gates decided. In W1 it never blocks, never edits and never changes a status (``MODE = "report"``): W3
switches the codes that passed their W2 measurement to enforce, one by one.

Verdicts (ADR 0020, audit README §4):

* S1  ``publish`` | ``policy_hold`` (elephant-riding rule, D8) | ``review`` (typed codes) | ``source_hold``
  (the source itself is not writable: empty itinerary, contradicting day count) | ``degraded`` (a hard fail-open
  detector)
* T2  ``ai_generated`` | ``needs_review`` | ``policy_hold``
* T9  ``approved`` | ``held`` | ``degraded``

A record (``Contract.record``) carries the verdict, every finding ({code, severity, class_id, detail, field}),
the sha256 of the gated text (``text_sha``), the exit, the status the current gates gave (``actual``), whether the
two agree and, for the publish-flip report, which way the verdict would move the object (``flip``). The store step
compares the sha of what it writes with ``text_sha`` (``stamp_stored``): ``sha_match`` false = a post-gate edit.

Policy values (meta band, FAQ counts, CTA count) are read through ``policy_value``: the W1 policy registry
(lane l1, ``writing_policy.yaml``) replaces ``_W1_DEFAULTS`` there without touching the checks.

Pure: no DB, no LLM, no I/O. The DB side (stored rows, the per-object record) is ``final_contract_store``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import structlog

logger = structlog.get_logger()

CONTRACT_VERSION = "fc-w1-report-1"
MODE_REPORT = "report"
MODE = MODE_REPORT   # W1: records, never blocks (ADR 0020 roadmap W1 -> enforce per code in W3)

S1, T2, T9 = "s1", "t2", "t9"

# ── verdicts ──────────────────────────────────────────────────────────────────────────────────────

PUBLISH = "publish"
POLICY_HOLD = "policy_hold"
REVIEW = "review"
SOURCE_HOLD = "source_hold"
DEGRADED = "degraded"
AI_GENERATED = "ai_generated"
NEEDS_REVIEW = "needs_review"
APPROVED = "approved"
HELD = "held"

# The verdict that lets the object out as a clean pass, per workflow (the rest keep it for a human or a label).
PASS_VERDICT = {S1: PUBLISH, T2: AI_GENERATED, T9: APPROVED}

# The contract's own finding for an elephant-riding detector hit (``elephant_policy.POLICY_HOLD``). Not a brand-audit
# code: the brand audit records the policy as FACT_CHECK_MANUAL_CHECK (``elephant_policy.POLICY_CODE``).
ELEPHANT_RIDING = "ELEPHANT_RIDING"

# Severity of one finding: ``block`` decides the verdict (in enforce mode it would hold), ``warn`` is recorded only.
BLOCK, WARN = "block", "warn"

# ── exits ─────────────────────────────────────────────────────────────────────────────────────────

# Every exit of every workflow, and where it is wired. The topology test reads this table: a graph edge to END that
# does not come from the final_contract node, or a write of stored text / a publish in a function not listed here,
# fails it. Keys: "<repo path>::<function>" of the function that stores or publishes, value: the exit names it
# records.
EXITS: dict[str, dict[str, tuple[str, ...]]] = {
    S1: {
        # the A1 graph (graph.final_contract_node): the normal exit, the hitl exit, a resumed job, a Bedrock batch
        # seed, a reviewer's re-validation
        "services/content_generation/graph.py::final_contract_node":
            ("done", "hitl", "resumed", "resumed_hitl", "batch", "batch_hitl", "revalidation"),
        # the version INSERT of an S1 run (records the graph's contract with the stored sha)
        "api/routers/admin_pipeline/_engine.py::_execute_run_tour": ("done", "hitl", "resumed", "batch"),
        # the re-validation of a human edit (stores the graph's revalidation record)
        "api/routers/admin_pipeline/_revalidate.py::_revalidate_tour": ("revalidation",),
        # a reviewer's edit of a version's text
        "api/routers/admin_pipeline/tour_detail.py::update_generated_content_fields": ("human_edit",),
        # every publish of a Master (auto publish after a run, review approve, promote, the legacy Lambda); each
        # caller names its exit (contract_exit=)
        "services/export/handler.py::process_export": ("auto_publish", "approve", "promote", "lambda", "recompose"),
        "api/routers/admin_pipeline/versions.py::promote_tour_version": ("promote",),
        "services/export/handler.py::lambda_handler": ("lambda",),
        # AA-1049 path A-new: the fixed-field recompose of a served Master (s1_recompose_store): the new version's
        # insert (no model call) and its publish through process_export
        "services/content_generation/s1_recompose_store.py::insert_recomposed_version": ("recompose",),
        "services/content_generation/s1_recompose_store.py::_default_publish": ("recompose",),
        # the review approve (also the legacy Step Functions branch that does not publish here)
        "api/routers/v1_pipeline.py::approve_review": ("approve",),
    },
    T2: {
        "services/jobs/t2_rewrite_job.py::_rewrite_and_save": ("done",),
        "services/jobs/t2_rewrite_job.py::_save_policy_hold": ("policy_hold",),
        # AA-1049 W3: the opt-in linear orchestrator (T2_ORCHESTRATOR=linear) runs the contract here and passes the
        # record to its one store step (``save_version``, listed in the topology test's NOT_EXITS)
        "services/content_generation/t2_orchestrator.py::run_and_save": ("done",),
        # a tenant's own inline edit / approve (tenant-owned text, BYO: recorded in the log only, never stored)
        "api/routers/v1_tours.py::_apply_version_action": ("tenant_edit", "tenant_approve"),
        "api/routers/admin_content_quality.py::apply_tour_sentence": ("admin_sentence_apply",),
    },
    T9: {
        "services/acp_content_writing/service.py::run_write_background": ("done",),
        # a tenant's hand edit (tenant-owned text, BYO: recorded in the log only, never stored)
        "services/acp_content_writing/service.py::update_piece_content_text": ("tenant_edit",),
        "api/routers/admin_content_quality.py::apply_sentence": ("admin_sentence_apply",),
    },
}

# ── policy values (from services/quality/writing_policy.yaml through the registry, AA-1047 part A) ───────────────


def policy_value(key: str) -> Any:
    """One policy value the contract checks against, read from the compiled writing policy (one policy_sha)."""
    from services.quality import writing_policy as wp

    if key == "meta_band":
        return wp.band("meta")
    if key == "title_max":
        return wp.band("title")[1]
    if key == "faq_counts":
        from services.acp_content_writing.quality_gates import FAQ_MAX_PAIRS  # the merge cap, not a policy value
        pairs = wp.faq_pairs()
        return (0, int(pairs["min"]), int(pairs["max"]), int(FAQ_MAX_PAIRS))
    if key == "cta_count":
        return int(wp.compiled()["cta"]["count"])
    raise KeyError(key)


# ── findings / record ─────────────────────────────────────────────────────────────────────────────


def finding(code: str, severity: str, *, class_id: str, detail: str = "", field_name: Optional[str] = None,
            count: int = 1) -> dict:
    out = {"code": code, "severity": severity, "class_id": class_id, "count": int(count)}
    if detail:
        out["detail"] = str(detail)[:300]
    if field_name:
        out["field"] = field_name
    return out


@dataclass
class Contract:
    workflow: str
    exit: str
    verdict: str
    findings: list = field(default_factory=list)
    text_sha: str = ""
    actual: Optional[str] = None

    @property
    def agrees(self) -> Optional[bool]:
        if self.actual is None:
            return None
        return (self.verdict == PASS_VERDICT[self.workflow]) == is_pass_status(self.workflow, self.actual)

    @property
    def flip(self) -> Optional[str]:
        """``unpublish``: the gates let it out, the contract would not; ``publish``: the reverse; None: same side."""
        if self.agrees is not False:
            return None
        return "unpublish" if is_pass_status(self.workflow, self.actual) else "publish"

    def codes(self, severity: Optional[str] = None) -> list[str]:
        return [f["code"] for f in self.findings if severity is None or f["severity"] == severity]

    def record(self) -> dict:
        return {"version": CONTRACT_VERSION, "mode": MODE, "workflow": self.workflow, "exit": self.exit,
                "verdict": self.verdict, "findings": self.findings, "text_sha": self.text_sha,
                "actual": self.actual, "agrees": self.agrees, "flip": self.flip}


def is_pass_status(workflow: str, status: Optional[str]) -> bool:
    """Whether a status the current gates gave lets the object out as a clean pass."""
    s = (status or "").lower()
    if workflow == S1:
        return s in ("approved", "published", "publish", "passed")
    if workflow == T2:
        return s in ("ai_generated", "approved")
    return s == "approved"


def with_actual(record: Optional[dict], actual: Optional[str]) -> Optional[dict]:
    """``record`` with the current gates' status filled in (agrees / flip recomputed)."""
    if not isinstance(record, dict):
        return record
    c = _from_record(record)
    c.actual = actual
    return {**record, **c.record()}


def _from_record(record: dict) -> Contract:
    return Contract(workflow=record.get("workflow") or S1, exit=record.get("exit") or "",
                    verdict=record.get("verdict") or "", findings=list(record.get("findings") or []),
                    text_sha=record.get("text_sha") or "", actual=record.get("actual"))


def log_record(record: Optional[dict], **ctx) -> None:
    """One ``final_contract`` event per exit (CloudWatch: verdict vs the current gates, by workflow and exit)."""
    if not isinstance(record, dict):
        return
    logger.info("final_contract", workflow=record.get("workflow"), exit=record.get("exit"),
                verdict=record.get("verdict"), actual=record.get("actual"), agrees=record.get("agrees"),
                flip=record.get("flip"), sha_match=record.get("sha_match"),
                block_codes=[f.get("code") for f in record.get("findings") or [] if f.get("severity") == BLOCK],
                **ctx)


def log_tenant_edit(workflow: str, exit: str, *, content: Any, actual: Optional[str], **fields) -> Optional[dict]:
    """A tenant's own edit or approve (tenant-owned text, BYO; ADR 0020 critic fix 6 leaves its notice and storage to
    a later decision): the contract runs in report mode and is recorded in the log only (one ``final_contract``
    event), never stored and never shown. ``content``: a T2 version dict or a T9 piece text; ``fields``: the T9
    piece fields (channel, seo_title, meta_description, slug, gate_ledger). Never raises."""
    try:
        if workflow == T2:
            c = check_t2(T2Input(content=content if isinstance(content, dict) else {}, master={}), exit=exit,
                         actual=actual)
        else:
            c = check_t9(T9Input(content_text=str(content or ""), **{k: v for k, v in fields.items()
                                                                     if k in T9Input.__dataclass_fields__}),
                         exit=exit, actual=actual)
        record = c.record()
        log_record(record, tenant_owned=True)
        return record
    except Exception as exc:  # noqa: BLE001 — report mode never fails the edit
        logger.warning("final_contract_failed", workflow=workflow, exit=exit, error_type=type(exc).__name__,
                       error=str(exc)[:300])
        return None


def compact(record: dict, at: Optional[str] = None) -> dict:
    """The short form kept in the per-object exit log (``final_contract_log``)."""
    out = {k: record.get(k) for k in ("exit", "verdict", "actual", "agrees", "flip", "text_sha", "sha_match",
                                      "lineage")
           if record.get(k) is not None}
    out["codes"] = [f.get("code") for f in record.get("findings") or [] if f.get("severity") == BLOCK][:20]
    if at:
        out["at"] = at
    return out


# ── text sha ──────────────────────────────────────────────────────────────────────────────────────

# The stored text fields of a tour page (S1 generated_content aa_* / seo_*, T2 rewritten_content), in one order.
TOUR_TEXT_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "seo_title",
                    "seo_meta")


def _field_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):   # a jsonb list read back as its JSON text (asyncpg, no codec)
            try:
                parsed = json.loads(s)
            except ValueError:
                return value
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        return value
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return value


def tour_text_sha(doc: Optional[dict]) -> str:
    """sha256 of a tour page's stored text fields (missing = empty; a list read as its JSON text = the list)."""
    canon = {k: _field_value((doc or {}).get(k)) for k in TOUR_TEXT_FIELDS}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def text_sha(text: Optional[str]) -> str:
    """sha256 of a piece's stored text (T9 content_text), byte for byte."""
    return hashlib.sha256((text or "").encode()).hexdigest()


def stamp_stored(record: Optional[dict], stored_sha: str, *, actual: Optional[str] = None) -> Optional[dict]:
    """The record as the store step writes it: ``stored_sha`` / ``sha_match`` (stored text == gated text), the
    current gates' status when given, a ``POST_GATE_EDIT`` block finding on a mismatch (ADR 0020 invariant 4) and
    the ``lineage`` stamp of THIS exit (AA-1052 W6: the policy_sha in force, and the route config version only at a
    writer exit, ``lineage.WRITER_EXITS``). The object's writer-time lineage is a separate key the writer store step
    writes once (``lineage.writer_lineage``); a later exit never replaces it (``services/quality/lineage.py``)."""
    if not isinstance(record, dict):
        return record
    c = _from_record(record)
    if actual is not None:
        c.actual = actual
    match = bool(c.text_sha) and c.text_sha == stored_sha
    if not match and "POST_GATE_EDIT" not in c.codes():
        c.findings.append(finding("POST_GATE_EDIT", BLOCK, class_id="contract.exit",
                                  detail="the stored text differs from the text the contract checked"))
        if c.verdict == PASS_VERDICT[c.workflow]:
            c.verdict = {S1: REVIEW, T2: NEEDS_REVIEW, T9: HELD}[c.workflow]
    if not match:
        logger.error("final_contract_sha_mismatch", workflow=c.workflow, exit=c.exit, gated_sha=c.text_sha,
                     stored_sha=stored_sha)
    from services.quality import lineage   # lineage imports nothing from here; local import keeps this module light
    return {**record, **c.record(), "stored_sha": stored_sha, "sha_match": match,
            "lineage": lineage.stamp(route=lineage.is_writer_exit(c.workflow, c.exit))}


# ── shared checks ─────────────────────────────────────────────────────────────────────────────────


def _len(value: Any) -> int:
    return len(str(value or "").strip())


def _meta_band(meta: Any, field_name: str = "seo_meta") -> list[dict]:
    lo, hi = policy_value("meta_band")
    n = _len(meta)
    if not n:
        return [finding("META_MISSING", WARN, class_id="struct.seo", field_name=field_name)]
    if n < lo or n > hi:
        return [finding("META_OUT_OF_BAND", WARN, class_id="struct.seo", field_name=field_name,
                        detail=f"{n} characters (band {lo}-{hi})")]
    return []


def _title_band(title: Any, field_name: str = "seo_title") -> list[dict]:
    n = _len(title)
    if n > policy_value("title_max"):
        return [finding("TITLE_OUT_OF_BAND", WARN, class_id="struct.seo", field_name=field_name,
                        detail=f"{n} characters (max {policy_value('title_max')})")]
    return []


def _missing(doc: dict, fields: Iterable[str]) -> list[dict]:
    out = []
    for f in fields:
        v = _field_value(doc.get(f))
        if not (v if isinstance(v, list) else str(v or "").strip()):
            out.append(finding("MISSING_FIELD", BLOCK, class_id="struct.field", field_name=f))
    return out


def _forbidden(doc: dict, words: Iterable[str]) -> list[dict]:
    from services.content_generation.forbidden_words import forbidden_in

    found = sorted(forbidden_in({k: _field_value(doc.get(k)) for k in TOUR_TEXT_FIELDS}, list(words)))
    if not found:
        return []
    return [finding("FORBIDDEN_WORD", BLOCK, class_id="lexicon.forbidden", detail=", ".join(found[:10]),
                    count=len(found))]


# ── S1 ────────────────────────────────────────────────────────────────────────────────────────────

# The day-oracle codes the contract counts as blocking (the source-vs-output count and order); the departure title
# is a warn, a source contradiction is a source hold, a skipped check is degraded-count only.
_DAY_BLOCK = frozenset({"DAY_COUNT_VS_DURATION", "DAY_OUTPUT_GAP", "DAY_OUTPUT_DUPLICATE", "DAY_OUTPUT_ORDER",
                        "DAY_OUTPUT_EXTRA", "DAY_SUBDAY_SPLIT"})


@dataclass
class S1Input:
    """What the S1 contract reads, from a graph state (``s1_input_from_state``) or a stored row
    (``s1_input_from_row``): the page text, the raw source, and the current gates' own outputs."""
    content: dict
    # source: name, subtitle, summary, description, highlights, itineraries, inclusions, activities, price, country,
    # duration
    source: dict
    failure_codes: list = field(default_factory=list)
    brand_audit_status: Optional[str] = None
    brand_audit_codes: list = field(default_factory=list)
    fix_pass_applied: bool = False
    writer_version: Optional[str] = None
    grounding_ran: bool = False
    grounding_violations: list = field(default_factory=list)
    grounding_stale: bool = False      # the stored grounding belongs to a text a reviewer has edited since
    degraded: list = field(default_factory=list)
    quality_score: Optional[float] = None
    brand_forbidden_words: list = field(default_factory=list)


def s1_input_from_state(state: dict) -> S1Input:
    tour = state.get("tour") or {}
    source = {"name": tour.get("name"), "subtitle": tour.get("subtitle"), "summary": tour.get("summary"),
              "description": tour.get("description"), "highlights": tour.get("highlights"),
              "itineraries": tour.get("itineraries") or tour.get("itinerary"), "inclusions": tour.get("inclusions"),
              "activities": tour.get("activities"), "price": tour.get("price") or tour.get("price_raw"),
              "country": tour.get("country"), "duration": tour.get("duration")}
    return S1Input(
        content=dict(state.get("generated") or {}), source=source,
        failure_codes=list(state.get("failure_codes") or []),
        brand_audit_status=state.get("brand_audit_status"),
        brand_audit_codes=list(state.get("brand_audit_codes") or []),
        fix_pass_applied=bool(state.get("fix_pass_applied")),
        writer_version=state.get("writer_version"),
        grounding_ran=bool(state.get("grounding_ran")),
        grounding_violations=list(state.get("grounding_violations") or []),
        degraded=list(state.get("degraded") or []),
        quality_score=state.get("quality_score"),
        brand_forbidden_words=list(state.get("brand_forbidden_words") or []),
    )


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, type(default)):
        return value
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def s1_input_from_row(row: Any, *, brand_forbidden_words: Optional[list] = None) -> S1Input:
    """A stored version as ``final_contract_store.S1_ROW_SQL`` reads it (generated_content + its latest score row +
    raw_tours src_*).

    AA-1049 (S277): the row holds no brand list, so the caller passes the tenant's active brand forbidden words
    (``final_contract_store.brand_words_of_row``, the one brand resolver); without them only the AA core list is
    checked."""
    from services.quality import elephant_policy

    metadata = _json(_get(row, "metadata"), {})
    grounding = metadata.get("grounding") if isinstance(metadata.get("grounding"), dict) else None
    source = elephant_policy.raw_source_from_row(row)
    source["duration"] = _get(row, "duration")
    score = _get(row, "score_overall")
    return S1Input(
        content=elephant_policy.version_content_from_row(row), source=source,
        failure_codes=[str(c) for c in _json(_get(row, "failure_codes"), [])],
        brand_audit_status=_get(row, "brand_audit_status"),
        brand_audit_codes=[str(c) for c in _json(_get(row, "brand_audit_codes"), [])],
        fix_pass_applied=bool(_get(row, "fix_pass_applied")),
        writer_version=metadata.get("writer_version"),
        grounding_ran=grounding is not None,
        grounding_violations=list((grounding or {}).get("violations") or []),
        grounding_stale=bool(_get(row, "human_edited")) and grounding is not None,
        degraded=list(metadata.get("degraded") or []),
        quality_score=float(score) if score is not None else None,
        brand_forbidden_words=list(brand_forbidden_words or []),
    )


def _s1_day_findings(content: dict, source: dict) -> list[dict]:
    from services.content_generation.graph import itinerary_day_gap
    from services.quality import day_oracle

    out: list[dict] = []
    written = content.get("itineraries") or ""
    if not isinstance(written, str):
        written = str(written)
    src_itin = source.get("itineraries") or ""
    try:
        gap = itinerary_day_gap({"itineraries": src_itin, "duration": source.get("duration")}, written)
    except Exception as exc:   # a detector bug is recorded, never raised (report mode)
        gap = None
        out.append(finding("CONTRACT_CHECK_ERROR", WARN, class_id="contract.degraded",
                           detail=f"itinerary_day_gap: {exc}"[:300]))
    if gap is not None and gap.get("missing"):
        out.append(finding("ITINERARY_DAY_COUNT_MISMATCH", BLOCK, class_id="struct.day_structure",
                           field_name="itineraries",
                           detail="source day " + "/".join(str(d) for d in gap["missing"]) + " missing",
                           count=len(gap["missing"])))
    try:
        oracle = day_oracle.check(str(src_itin), source.get("duration"), written)
    except Exception as exc:
        oracle = []
        out.append(finding("CONTRACT_CHECK_ERROR", WARN, class_id="contract.degraded",
                           detail=f"day_oracle: {exc}"[:300]))
    for f in oracle:
        code = f["code"]
        if code == day_oracle.SOURCE_DAY_CONTRADICTION:
            out.append(finding(code, BLOCK, class_id="source.contradiction", detail=f.get("detail", "")))
        elif code in _DAY_BLOCK:
            out.append(finding(code, BLOCK, class_id=f["class_id"], field_name="itineraries",
                               detail=f.get("detail", "")))
        else:
            out.append(finding(code, WARN, class_id=f["class_id"], field_name="itineraries",
                               detail=f.get("detail", "")))
    return out


def check_s1(inp: S1Input, *, exit: str, actual: Optional[str] = None) -> Contract:
    """The S1 contract over one page (graph state or stored row). Pure; never raises on a detector error."""
    from services.content_generation.forbidden_words import all_forbidden
    from services.quality import degraded as dg
    from services.quality import elephant_policy

    content, source = inp.content or {}, inp.source or {}
    found: list[dict] = []

    # the source itself: nothing to write from (the writer is not charged)
    if not _len(source.get("itineraries")):
        found.append(finding("SOURCE_EMPTY_ITINERARY", BLOCK, class_id="source.empty"))
    found += _missing(content, ("name", "summary", "itineraries"))
    found += _s1_day_findings(content, source)
    found += _forbidden(content, all_forbidden(inp.brand_forbidden_words))

    # the current gates' surviving hard codes (validate, the day guard, the degraded hold)
    from services.content_generation.graph import hard_block_codes
    hard = sorted(set(inp.failure_codes) & set(hard_block_codes({"writer_version": inp.writer_version})))
    for code in hard:
        if code in ("FORBIDDEN_WORD", "ITINERARY_DAY_COUNT_MISMATCH", dg.HOLD_CODE):
            continue   # re-checked on the stored text above / decided from degraded[] below
        found.append(finding(code, BLOCK, class_id="gate.hard_code"))

    # grounding against the raw source: must have run on THIS text and left no unsupported sentence
    if not inp.grounding_ran:
        found.append(finding("GROUNDING_NOT_RUN", BLOCK, class_id="fact.grounding"))
    elif inp.grounding_stale:
        found.append(finding("GROUNDING_NOT_RUN_ON_EDIT", BLOCK, class_id="fact.grounding",
                             detail="the grounding on record predates the reviewer's edit"))
    elif inp.grounding_violations:
        found.append(finding("UNSUPPORTED_SENTENCE", BLOCK, class_id="fact.grounding",
                             count=len(inp.grounding_violations)))

    # the brand audit (deterministic pre-audit codes + the product-truth codes) and the elephant-riding policy
    policy_hits = elephant_policy.scan(content, source)
    policy_only = elephant_policy.is_policy_only_hold(inp.brand_audit_status, inp.brand_audit_codes, content, source)
    if policy_hits:
        found.append(finding(ELEPHANT_RIDING, BLOCK, class_id="policy.elephant_riding",
                             detail=f"{policy_hits[0]['where']} {policy_hits[0]['field']}"))
    if inp.brand_audit_status == "manual_check" and not policy_only:
        # the policy shares FACT_CHECK_MANUAL_CHECK with the LLM audit's other fact checks: every code is listed
        found.append(finding("MANUAL_CHECK", BLOCK, class_id="gate.brand_audit",
                             detail=", ".join(inp.brand_audit_codes)))
    elif inp.brand_audit_status == "flagged" and not inp.fix_pass_applied:
        found.append(finding("BRAND_FLAGGED_UNFIXED", BLOCK, class_id="gate.brand_audit",
                             detail=", ".join(inp.brand_audit_codes)))

    # report columns (ADR 0020: quality_score is a report column; the meta and title never block)
    from services.quality.registry import threshold
    floor = threshold("a1.publish_floor")
    if inp.quality_score is None or float(inp.quality_score) < floor:
        found.append(finding("SCORE_BELOW_FLOOR", WARN, class_id="report.score",
                             detail=f"score {inp.quality_score} (floor {floor:g})"))
    found += _meta_band(content.get("seo_meta"))
    found += _title_band(content.get("seo_title"))
    for name in dg.hard_names(inp.degraded):
        found.append(finding("DEGRADED_DETECTOR", BLOCK, class_id="contract.degraded", detail=name))

    blocks = [f for f in found if f["severity"] == BLOCK]
    classes = {f["class_id"] for f in blocks}
    if "contract.degraded" in classes:
        verdict = DEGRADED
    elif classes & {"source.empty", "source.contradiction"}:
        verdict = SOURCE_HOLD
    elif classes - {"policy.elephant_riding"}:
        verdict = REVIEW
    elif "policy.elephant_riding" in classes:
        verdict = POLICY_HOLD
    else:
        verdict = PUBLISH
    return Contract(workflow=S1, exit=exit, verdict=verdict, findings=found, text_sha=tour_text_sha(content),
                    actual=actual)


# ── T2 ────────────────────────────────────────────────────────────────────────────────────────────

T2_TEXT_FIELDS = TOUR_TEXT_FIELDS + ("trip_type",)


@dataclass
class T2Input:
    """A tenant version (``rewritten_content``), the Master it was written from, and the current gates' outputs."""
    content: dict
    master: dict
    raw_source: dict = field(default_factory=dict)
    structural_issues: list = field(default_factory=list)
    grounding_issues: list = field(default_factory=list)
    fallback_writer_held: bool = False
    policy_hold: bool = False
    score: Optional[float] = None
    gated_sha: Optional[str] = None    # the sha of the draft T3 checked (None = not known at this exit)


def _day_numbers(itineraries: Any) -> list[int]:
    """The distinct days a written itinerary covers. AA-1049 (S273): ``day_parse.written_day_numbers``, the reader the
    T2 orchestrator's slot check uses, not ``output_days`` (canonical "Day N — Title" only): a stored T2 text is
    dash-fixed to "Day N: Title" (AA-778), so every T2 version read as 0 days (T2_DAY_COUNT_VS_MASTER)."""
    from services.content_generation.day_parse import written_day_numbers
    return sorted(set(written_day_numbers(itineraries)))


def check_t2(inp: T2Input, *, exit: str, actual: Optional[str] = None) -> Contract:
    from services.quality import elephant_policy

    content, master = inp.content or {}, inp.master or {}
    found: list[dict] = []
    found += _missing(content, ("name", "summary", "itineraries"))
    try:
        mine, theirs = _day_numbers(content.get("itineraries")), _day_numbers(master.get("itineraries"))
        if theirs and mine != theirs:
            lost = sorted(set(theirs) - set(mine))
            found.append(finding("T2_DAY_COUNT_VS_MASTER", BLOCK, class_id="struct.day_structure",
                                 field_name="itineraries",
                                 detail=f"{len(mine)} day(s) for a {len(theirs)}-day Master"
                                        + (" (missing " + "/".join(map(str, lost)) + ")" if lost else "")))
    except Exception as exc:
        found.append(finding("CONTRACT_CHECK_ERROR", WARN, class_id="contract.degraded", detail=str(exc)[:300]))
    if inp.structural_issues:
        found.append(finding("T3_STRUCTURAL", BLOCK, class_id="gate.t3", count=len(inp.structural_issues),
                             detail=", ".join(sorted({_issue_code(i) for i in inp.structural_issues}))[:300]))
    if inp.grounding_issues:
        found.append(finding("T3_GROUNDING", BLOCK, class_id="fact.grounding", count=len(inp.grounding_issues)))
    if inp.fallback_writer_held:
        found.append(finding("FALLBACK_WRITER", BLOCK, class_id="contract.degraded"))
    hits = elephant_policy.scan(content, {**(inp.raw_source or {}), **{"country": master.get("country")}})
    if inp.policy_hold or hits:
        found.append(finding(ELEPHANT_RIDING, BLOCK, class_id="policy.elephant_riding",
                             detail=(f"{hits[0]['where']} {hits[0]['field']}" if hits else "policy_hold")))
    if any("—" in str(_field_value(content.get(k))) for k in ("subtitle", "summary", "highlights",
                                                                   "itineraries", "seo_title", "seo_meta")):
        found.append(finding("EM_DASH", WARN, class_id="style.dash"))
    if inp.score is not None:
        from services.quality.registry import threshold
        if float(inp.score) < threshold("t2.status_pass"):
            found.append(finding("SCORE_BELOW_PASS", WARN, class_id="report.score", detail=f"score {inp.score}"))
    found += _meta_band(content.get("seo_meta"))
    found += _title_band(content.get("seo_title"))
    sha = tour_text_sha(content)
    if inp.gated_sha is not None and inp.gated_sha != sha:
        found.append(finding("POST_GATE_EDIT", WARN, class_id="contract.exit",
                             detail="the stored text differs from the draft T3 checked (a code fix after T3)"))

    classes = {f["class_id"] for f in found if f["severity"] == BLOCK}
    if classes == {"policy.elephant_riding"}:
        verdict = POLICY_HOLD
    elif classes:
        verdict = NEEDS_REVIEW
    else:
        verdict = AI_GENERATED
    return Contract(workflow=T2, exit=exit, verdict=verdict, findings=found, text_sha=sha, actual=actual)


def _issue_code(issue: Any) -> str:
    if isinstance(issue, dict):
        return str(issue.get("check_id") or issue.get("code") or "issue")
    return str(issue)[:40]


# ── T9 ────────────────────────────────────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_RE = re.compile(r"\[(?:R|F):")


@dataclass
class T9Input:
    """A piece's stored text and fields, and the current gates' final ledger."""
    content_text: str
    channel: Optional[str] = None
    cta: Optional[str] = None
    seo_title: Optional[str] = None
    meta_description: Optional[str] = None
    slug: Optional[str] = None
    gate_ledger: list = field(default_factory=list)
    held_reason: Optional[str] = None


def _blocking_failures(gate_ledger: list) -> list[str]:
    from services.acp_content_writing.quality_gates import expand_ledger, never_holds

    out = []
    for g in expand_ledger([x for x in gate_ledger or [] if isinstance(x, dict) and "gate" in x]):
        if not g.get("passed") and g.get("blocking", True) and not never_holds(g):
            out.append(str(g["gate"]))
    return list(dict.fromkeys(out))


def _fold(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def check_t9(inp: T9Input, *, exit: str, actual: Optional[str] = None) -> Contract:
    text = inp.content_text or ""
    found: list[dict] = []
    if not text.strip():
        found.append(finding("EMPTY_TEXT", BLOCK, class_id="struct.field"))
    if _TAG_RE.search(text):
        found.append(finding("CITATION_TAG_LEFT", BLOCK, class_id="struct.leak",
                             count=len(_TAG_RE.findall(text))))
    cta = (inp.cta or "").strip()
    if cta:
        n = _fold(text).count(_fold(cta))
        if n != policy_value("cta_count"):
            found.append(finding("CTA_COUNT", BLOCK, class_id="struct.cta", detail=f"{n} time(s)", count=n))
    if inp.channel == "blog":
        try:
            from services.acp_content_writing.quality_gates import faq_pairs
            pairs = len(faq_pairs(text))
        except Exception as exc:
            pairs = None
            found.append(finding("CONTRACT_CHECK_ERROR", WARN, class_id="contract.degraded", detail=str(exc)[:300]))
        if pairs is not None and pairs not in policy_value("faq_counts"):
            found.append(finding("FAQ_COUNT", BLOCK, class_id="struct.faq", detail=f"{pairs} pair(s)", count=pairs))
        if not _len(inp.seo_title):
            found.append(finding("TITLE_MISSING", WARN, class_id="struct.seo", field_name="seo_title"))
        found += _title_band(inp.seo_title)
        found += _meta_band(inp.meta_description, "meta_description")
        slug = (inp.slug or "").strip()
        if slug and (not _SLUG_RE.match(slug) or not _slug_on_word_boundary(slug, inp.seo_title)):
            found.append(finding("SLUG_NOT_ON_WORD_BOUNDARY", WARN, class_id="struct.seo", field_name="slug",
                                 detail=slug[:80]))
    for gate in _blocking_failures(inp.gate_ledger):
        found.append(finding(f"GATE:{gate}", BLOCK, class_id="gate.t10", detail=gate))
    if (inp.held_reason or "").startswith("degraded:"):
        found.append(finding("DEGRADED_DETECTOR", BLOCK, class_id="contract.degraded",
                             detail=inp.held_reason[:200]))

    classes = {f["class_id"] for f in found if f["severity"] == BLOCK}
    verdict = DEGRADED if "contract.degraded" in classes else (HELD if classes else APPROVED)
    return Contract(workflow=T9, exit=exit, verdict=verdict, findings=found, text_sha=text_sha(text),
                    actual=actual)


def _slug_on_word_boundary(slug: str, title: Optional[str]) -> bool:
    """Every slug word is a whole word of the title (folded), so the slug was not cut mid-word. No title: True."""
    words = set(re.findall(r"[a-z0-9]+", _fold(title or "")))
    if not words:
        return True
    return all(tok in words for tok in slug.split("-"))


__all__ = [
    "CONTRACT_VERSION", "MODE", "MODE_REPORT", "S1", "T2", "T9", "PUBLISH", "POLICY_HOLD", "REVIEW", "SOURCE_HOLD",
    "DEGRADED", "AI_GENERATED", "NEEDS_REVIEW", "APPROVED", "HELD", "PASS_VERDICT", "BLOCK", "WARN", "EXITS",
    "policy_value", "finding", "Contract", "is_pass_status", "with_actual", "log_record", "log_tenant_edit",
    "compact",
    "TOUR_TEXT_FIELDS", "tour_text_sha", "text_sha", "stamp_stored", "S1Input", "s1_input_from_state",
    "s1_input_from_row", "check_s1", "T2Input", "check_t2", "T9Input", "check_t9",
]
