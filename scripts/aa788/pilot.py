#!/usr/bin/env python3
"""AA-788 — the 20-piece T10 pilot of the three promoted checks, and the measurement record a mode flip cites.
PAID (the real T9 writer loop on ~20 pieces): run only by the orchestrator, after the tech lead's own yes for the run.

What it measures (issue Done-when, S279 additions; root ADR 0020 "a gate may hold content only with a passing
measurement record"):
  F12_scope (block)        every finding per piece and attempt (a FIRE: one violation text, once per piece), body
                           or title / meta, the place it names, whether it blocked, got its sentence repair or held the
                           piece. The fires are labelled afterwards (tp: the place really is outside the piece's scope;
                           fp: it is not) and `score` gives the precision and its Wilson-95 lower bound against the
                           deterministic-detector bar (services/ground_truth/sample_size.BARS, "deterministic").
  F14 under-length         the length codes of attempt 1 and of the final text (length_contract.ledger_length_codes:
                           the AA-1077 blog floor, F4, and the AA-780 social band floor, F14, both as `length_short`;
                           no floor of its own) and the repair attempt 1 got (the F14 rewrite with the unused seed
                           facts, or the AA-1077 extend).
  F9_brand_style (repair)  attempt 1's finding, its one sentence repair, and that it never holds (`f9_held` must be 0).
  holds                    the piece's status, the holding gate and the hold reason codes (gate_eval), next to the
                           stored piece's status and panel label: new holds on pieces the panel labelled as_is / light
                           (must be 0), and approvals of pieces it labelled heavy / reject (the "false passes"; a proxy:
                           the label is of the stored text).

How it runs (mode RUN): per input it rebuilds the T9 writer context from the LIVE request exactly as the tenant's
Write button does (service.start_write: no model call) and runs the real writer / gate / repair loop
(service.run_write_background) in this process, with, in this process only:
  - the promoted modes laid over quality_gates' mode readers (F12_scope = block, F9_brand_style = repair, the F14
    floor = repair); the platform settings are untouched (the flip is the settings page's, with this record);
  - every model call through the Model Gateway (LLMClient.generate; record_call with **billed_fields(resp) inside the
    writer code) relabelled to stage `adhoc_aa788` by call_log.bind_eval_stage (real stage in quality_signal.real_stage,
    run id in quality_signal.eval_run, tenant / piece ids NULL: no tenant usage), roles unchanged (writer, judge, ...);
  - NOTHING written to app tables: the placeholder INSERT and the final UPDATE of the piece are captured in memory,
    the FactSheet record and the claim shadow are skipped, the gateway's A/B shadow call is off;
  - a HARD cost cap, checked before EVERY model call (`Budget.check_call`: spent so far + the largest call seen must
    fit) and before every piece (spent + the larger of the largest piece seen and the stored piece's own ledger cost
    must fit). The cap is the setting cis.spend.run_cap_adhoc_usd (Platform › Settings, default 5 USD; the gateway
    enforces it too, per call); CAP_USD may only lower it (a trial, or the rest of the budget after a trial).
Output on stdout: progress lines, then `RECORD <json>` (the record: per piece the verdicts above, the fires with the
violation text, which quotes the sentence) and `LABELS <json>` (the label template). The record quotes tenant piece
text: keep it in the private root `.tmp-session/` (the ecsx copy is there), never in a repo. `score` prints counts and
ids only: that output may go into the root audits folder.

  RUN   (paid)  tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa788/pilot.py MODE=run CONFIRM=PAID N=20 [CAP_USD=4.5]
                [PIECES=aaaaaaaa,bbbbbbbb] [SKIP=cccccccc,...] [TENANTS=slug,slug]
                (ECSX_SERVICE=aa-cis-dev-worker, ECSX_TIMEOUT / ECSX_SCRIPT_TIMEOUT 7200: ~1-3 min a piece)
  PLAN  ($0)    the same command with MODE=plan: the inputs it would take, the stored pieces' ledger cost (the
                estimate) and the cap; builds every context (no model call) to show which inputs would fail; and
                `EVIDENCE_RULE`: what the deployed settings API answers a `block` proposal with replay evidence
                (evidence_mismatch) and with a pilot reference (accepted). Nothing is stored.
  SCORE ($0, local)
                python3 scripts/aa788/pilot.py score --record r1.json [--record r2.json] --labels labels.json
                prints the measurement: fires, labelled fires (the tech lead's label counts; the panel's gives the
                agreement), precision, Wilson-95 lower bound, the bar and whether it passes, how many more labelled
                fires the bar needs, and the Done-when counts. Only a passing measurement prints `evidence_ref`, the
                text the settings change of cis.quality_modes.t10_f12_scope to `block` must carry
                (quality_settings.evidence_problem refuses any other).
Env (RUN / PLAN): N (default 20, the issue's pilot size), PIECES (8-char ids of stored pieces whose requests to write:
default the newest piece of each approved request on TENANTS, labelled pieces first, round-robin by tenant), SKIP,
TENANTS (default the four AA-741 audit tenants), CAP_USD (<= the setting), RUN_ID (default generated).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import re
import sys
import threading
import uuid
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

sys.path.insert(0, os.getcwd())

STAGE = "adhoc_aa788"
SCHEMA = "aa788-pilot/1"
MEASUREMENT_SCHEMA = "aa788-pilot-measurement/1"
DEFAULT_N = 20  # the issue's pilot size (Done-when: "20-piece pilot"); a run argument, not a policy value
AUDIT_TENANTS = "audit-atlas-hearth,audit-terra-family,audit-trail-pulse,audit-wildkind-travel"
# The modes the pilot measures: {quality_gates mode reader: value}.
PROMOTED_MODES = {"f12_scope_mode": "block", "f9_brand_style_mode": "repair", "f14_floor_mode": "repair"}
LIGHT_LABELS = frozenset({"as_is", "light"})
HEAVY_LABELS = frozenset({"heavy", "reject"})
TRUE_FIRE, FALSE_FIRE = "tp", "fp"
_PLACE_RE = re.compile(r"^(?:title |meta )?names '([^']+)'")
_SENTENCE_REPAIR_MODES = ("sentences", "truth_cut+sentences")

INPUTS_SQL = """
    SELECT DISTINCT ON (cp.angle_gate_request_id)
           cp.piece_id::text AS piece_id, cp.angle_gate_request_id::text AS request_id,
           cp.tenant_id::text AS tenant_id, t.slug AS tenant, COALESCE(cp.channel, r.channel) AS channel,
           cp.status AS stored_status, cp.created_at,
           (SELECT array_agg(l.label ORDER BY l.labelled_at DESC) FROM acp_shared.content_piece_label l
             WHERE l.piece_id = cp.piece_id) AS labels
    FROM acp_shared.content_piece cp
    JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE t.slug = ANY($1::text[]) AND r.status = 'approved' AND cp.status IN ('approved', 'held')
    ORDER BY cp.angle_gate_request_id, cp.created_at DESC
"""
STORED_COST_SQL = """
    SELECT content_piece_id::text AS piece_id, coalesce(sum(cost_usd), 0)::float AS usd
    FROM shared.llm_call_log WHERE content_piece_id = ANY($1::uuid[]) GROUP BY content_piece_id
"""
RUN_SPEND_SQL = """
    SELECT coalesce(sum(cost_usd), 0)::float AS usd FROM shared.llm_call_log
    WHERE stage = $1 AND created_at >= $2 AND quality_signal ->> 'eval_run' = $3
"""


class PilotCapReached(RuntimeError):
    """The next model call would pass the pilot's cap: refused before the call."""


# ── pure parts (unit-tested) ─────────────────────────────────────────────────────────────────────────────────────


def _j(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def pick_inputs(rows: list[dict], *, n: int, pieces: list[str], skip: list[str]) -> list[dict]:
    """The inputs of a run: the rows whose 8-char piece id is in `pieces` (in that order) when given, else labelled
    pieces first, then the others, each group round-robin by tenant (newest first in a tenant), `n` at most. `skip`
    leaves pieces out (the inputs of an earlier trial)."""
    skip_set = {s[:8] for s in skip}
    rows = [r for r in rows if r["piece_id"][:8] not in skip_set]
    if pieces:
        by = {r["piece_id"][:8]: r for r in rows}
        return [by[p[:8]] for p in pieces if p[:8] in by][:n]
    out: list[dict] = []
    for group in ([r for r in rows if r.get("labels")], [r for r in rows if not r.get("labels")]):
        per: dict[str, list[dict]] = defaultdict(list)
        for r in sorted(group, key=lambda r: (str(r.get("created_at")), r["piece_id"]), reverse=True):
            per[r["tenant"]].append(r)
        tenants = sorted(per)
        while any(per.values()):
            for t in tenants:
                if per[t]:
                    out.append(per[t].pop(0))
    return out[:n]


def _entries(ledger: Any) -> list[dict]:
    from services.acp_content_writing.quality_gates import expand_ledger
    return [g for g in expand_ledger([g for g in (ledger or []) if isinstance(g, dict) and g.get("gate")])]


def _gate(ledger: Any, gate: str) -> Optional[dict]:
    return next((g for g in _entries(ledger) if g["gate"] == gate), None)


def attempt_ledgers(cap: dict) -> list[tuple[int, list]]:
    """(attempt, ledger) of every attempt the loop recorded: each failed attempt's ledger snapshot in repair_log, then
    the final ledger under the final attempt number."""
    out = [(int(e["attempt"]), e["gate_ledger"]) for e in _j(cap.get("repair_log")) or []
           if isinstance(e, dict) and isinstance(e.get("attempt"), int) and isinstance(e.get("gate_ledger"), list)]
    out.append((int(cap.get("attempt_number") or 1), _j(cap.get("gate_ledger")) or []))
    return out


def _repairs(cap: dict, gate: str, *, attempt: Optional[int] = None) -> int:
    """Sentence repairs the loop ran after an attempt on which `gate` blocked (repair_log `failing` rows)."""
    n = 0
    for e in _j(cap.get("repair_log")) or []:
        if not isinstance(e, dict) or (attempt is not None and e.get("attempt") != attempt):
            continue
        blocked = any(isinstance(r, dict) and r.get("source") == gate and r.get("blocking")
                      for r in e.get("failing") or [])
        n += bool(blocked and str(e.get("repair_mode") or "") in _SENTENCE_REPAIR_MODES)
    return n


def f12_fires(cap: dict, piece_ref: str, run_id: str) -> list[dict]:
    """Every F12_scope finding of the piece, once per violation text (the attempt it first appeared in)."""
    from services.quality import registry as qr
    seen: dict[str, dict] = {}
    for attempt, ledger in attempt_ledgers(cap):
        g = _gate(ledger, qr.F12_SCOPE)
        if not g or g.get("passed"):
            continue
        for v in g.get("violations") or []:
            v = str(v)
            if v in seen:
                continue
            m = _PLACE_RE.match(v)
            seen[v] = {"attempt": attempt, "where": "body" if is_body_finding(v) else "title_meta",
                       "place": m.group(1) if m else None, "blocking": bool(g.get("blocking")), "violation": v}
    fires = sorted(seen.values(), key=lambda f: (f["attempt"], f["violation"]))
    return [{"fire_id": f"{run_id}:{piece_ref}:{i}", **f} for i, f in enumerate(fires, 1)]


def piece_verdicts(cap: dict) -> dict:
    """The pilot's reading of one captured piece (no text): status, hold, F12 / F14 / F9_brand_style outcomes."""
    from services.acp_content_writing import gate_eval, length_contract
    from services.acp_content_writing.quality_gates import hold_failure
    from services.quality import registry as qr
    final = _j(cap.get("gate_ledger")) or []
    ledgers = attempt_ledgers(cap)
    first = next((lg for a, lg in ledgers if a == 1), final)
    status = cap.get("status")
    held_gate = (hold_failure(final) or {}).get("gate") if status == "held" else None
    f12_final = _gate(final, qr.F12_SCOPE)
    f12_blocked = sorted({a for a, lg in ledgers if (g := _gate(lg, qr.F12_SCOPE))
                          and not g.get("passed") and g.get("blocking")})
    f9_first = _gate(first, qr.F9_BRAND_STYLE)
    repair_1 = next((e for e in _j(cap.get("repair_log")) or [] if isinstance(e, dict) and e.get("attempt") == 1
                     and isinstance(e.get("gate_ledger"), list)), None)
    return {
        "status": status, "attempts": int(cap.get("attempt_number") or 1), "held_gate": held_gate,
        "hold_codes": gate_eval.hold_reason_codes(final, cap.get("held_reason")) if status == "held" else [],
        "f12": {"blocked_attempts": f12_blocked, "sentence_repairs": _repairs(cap, qr.F12_SCOPE),
                "left_in_body": bool(f12_final and not f12_final.get("passed")
                                     and any(is_body_finding(v) for v in f12_final.get("violations") or [])),
                "held": held_gate == qr.F12_SCOPE},
        "f14": {"attempt1_codes": length_contract.ledger_length_codes(first),
                "final_codes": length_contract.ledger_length_codes(final),
                "attempt1_repair": (str(repair_1.get("repair_mode") or "rewrite") if repair_1 else None)},
        "f9_brand_style": {"attempt1_failed": bool(f9_first and not f9_first.get("passed")),
                           "attempt1_blocked": bool(f9_first and not f9_first.get("passed")
                                                    and f9_first.get("blocking")),
                           "sentence_repairs": _repairs(cap, qr.F9_BRAND_STYLE, attempt=1),
                           "held": held_gate == qr.F9_BRAND_STYLE},
    }


def compare_stored(row: dict, verdict: dict) -> dict:
    """The Done-when comparisons against the stored piece and its panel labels."""
    labels = set(row.get("labels") or [])
    pilot, stored = verdict.get("status"), row.get("stored_status")
    return {"new_hold_on_light": bool(pilot == "held" and stored == "approved" and labels & LIGHT_LABELS),
            "heavy_label": bool(labels & HEAVY_LABELS),
            "false_pass_stored": bool(stored == "approved" and labels & HEAVY_LABELS),
            "false_pass_pilot": bool(pilot == "approved" and labels & HEAVY_LABELS)}


def summarize(pieces: list[dict], fires: list[dict]) -> dict:
    """Run totals and the per-tenant Done-when counts (counts only, no text)."""
    done = [p for p in pieces if p.get("verdict")]
    by_tenant: dict[str, Counter] = defaultdict(Counter)
    for p in done:
        c, v, cmp = by_tenant[p["tenant"]], p["verdict"], p["compare"]
        c["pieces"] += 1
        c["held"] += v["status"] == "held"
        c["false_pass_stored"] += cmp["false_pass_stored"]
        c["false_pass_pilot"] += cmp["false_pass_pilot"]
        c["new_hold_on_light"] += cmp["new_hold_on_light"]
    v_ = [p["verdict"] for p in done]
    return {
        "pieces": len(pieces), "written": len(done), "errors": sorted(p["piece"] for p in pieces if p.get("error")),
        "status": dict(Counter(v["status"] for v in v_)),
        "hold_codes": dict(Counter(c for v in v_ for c in v["hold_codes"])),
        "f12": {"fires": len(fires), "body_fires": sum(f["where"] == "body" for f in fires),
                "pieces_with_fires": len({f["fire_id"].rsplit(":", 1)[0] for f in fires}),
                "pieces_blocked": sum(bool(v["f12"]["blocked_attempts"]) for v in v_),
                "sentence_repairs": sum(v["f12"]["sentence_repairs"] for v in v_),
                "held": sum(v["f12"]["held"] for v in v_)},
        "f14": {"under_length_attempt1": sum("length_short" in v["f14"]["attempt1_codes"] for v in v_),
                "under_length_final": sum("length_short" in v["f14"]["final_codes"] for v in v_),
                "attempt1_repairs": dict(Counter(v["f14"]["attempt1_repair"] for v in v_
                                                 if "length_short" in v["f14"]["attempt1_codes"]))},
        "f9_brand_style": {"attempt1_failed": sum(v["f9_brand_style"]["attempt1_failed"] for v in v_),
                           "attempt1_blocked": sum(v["f9_brand_style"]["attempt1_blocked"] for v in v_),
                           "sentence_repairs": sum(v["f9_brand_style"]["sentence_repairs"] for v in v_),
                           "held": sum(v["f9_brand_style"]["held"] for v in v_)},
        "done_when": {"new_holds_on_light": sum(p["compare"]["new_hold_on_light"] for p in done),
                      "false_pass_stored": sum(p["compare"]["false_pass_stored"] for p in done),
                      "false_pass_pilot": sum(p["compare"]["false_pass_pilot"] for p in done),
                      "f9_brand_style_holds": sum(v["f9_brand_style"]["held"] for v in v_)},
        "by_tenant": {t: dict(c) for t, c in sorted(by_tenant.items())},
    }


def is_body_finding(violation: str) -> bool:
    """An F12_scope finding in the body (headings included), not one naming a place in the SEO title or meta."""
    from services.acp_content_writing.quality_gates import SEO_FINDING_PREFIXES
    return not str(violation or "").startswith(SEO_FINDING_PREFIXES)


def evidence_rule_check() -> dict:
    """$0 live check of the deployed settings rule (no DB: the evidence is checked before the proposal is stored):
    what `propose` answers a `block` proposal of the F12 mode with replay evidence and with a pilot reference."""
    from api.services import settings_registry as reg
    from services.quality import quality_settings, settings_keys
    from shared import settings
    d = settings.definition(settings_keys.MODES["t10_f12_scope"])
    out = {}
    for name, ref in (("replay_evidence", "replay run of stored pieces"),
                      ("pilot_reference", f"{quality_settings.F12_PILOT_EVIDENCE_PREFIX}p0check:000000000000")):
        try:
            reg._check_evidence(d, "block", ref)
            out[name] = "accepted"
        except reg.SettingsError as e:
            out[name] = e.code
    return out


def detector_sha() -> str:
    """sha256 (12 hex) of the F12_scope detector's code: gate_scope, the body / title rule and the hygiene scope
    finder. Records measured under another detector do not pool (`score` refuses them)."""
    from services.acp_content_writing import hygiene
    from services.acp_content_writing import quality_gates as qg
    parts = [inspect.getsource(f) for f in (qg.gate_scope, hygiene.build_scope_lexicon, hygiene.places_named,
                                            hygiene.find_scope_violations)] + [repr(qg.SEO_FINDING_PREFIXES)]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]


def label_template(record: dict) -> dict:
    return {"run_id": record["run_id"], "how": "per fire: tp = the place really is outside the piece's scope, fp = it "
            "is not (a false finding); tech_lead counts, panel gives the agreement",
            "labels": {f["fire_id"]: {"tech_lead": None, "panel": None} for f in record.get("fires") or []}}


class Budget:
    """The pilot's hard cap. spent = max(in-process reply cost, last ledger read + in-process cost since that read)
    (the ledger read, before every piece, also counts the embedding and Jev rows of the run)."""

    def __init__(self, cap_usd: float) -> None:
        self._lock = threading.Lock()
        self.cap_usd = float(cap_usd)
        self.local = self.ledger = self.local_at_read = 0.0
        self.largest_call = 0.0
        self.calls = 0
        self.tripped: Optional[str] = None

    def add(self, usd: float) -> None:
        with self._lock:
            usd = float(usd or 0.0)
            self.local += usd
            self.calls += 1
            self.largest_call = max(self.largest_call, usd)

    def note_ledger(self, usd: float) -> None:
        with self._lock:
            self.ledger, self.local_at_read = float(usd or 0.0), self.local

    def spent(self) -> float:
        with self._lock:
            return max(self.local, self.ledger + self.local - self.local_at_read)

    def check_call(self) -> None:
        """Before every model call: the spend so far plus the largest call seen must fit the cap."""
        s = self.spent()
        if s + self.largest_call > self.cap_usd + 1e-9 or s >= self.cap_usd:
            msg = f"cap {self.cap_usd:.2f} USD: spent {s:.4f} + largest call {self.largest_call:.4f} would pass it"
            with self._lock:
                self.tripped = self.tripped or msg
            raise PilotCapReached(msg)

    def may_start(self, estimate_usd: float) -> bool:
        """Before every piece: the spend so far plus the piece's estimate must fit the cap."""
        return self.spent() + max(0.0, float(estimate_usd or 0.0)) <= self.cap_usd + 1e-9


def piece_estimate(stored_usd: Optional[float], largest_piece_usd: float) -> float:
    """A piece's reservation: the larger of the stored piece's own ledger cost and the largest pilot piece so far."""
    return max(float(stored_usd or 0.0), float(largest_piece_usd or 0.0))


def resolve_cap(setting_usd: Any, requested: Optional[str]) -> float:
    """The run's cap: the setting cis.spend.run_cap_adhoc_usd, or CAP_USD when it is lower. Refuses (ValueError) an
    unreadable setting or a CAP_USD that is not a positive number."""
    if isinstance(setting_usd, bool) or not isinstance(setting_usd, (int, float)) or setting_usd <= 0:
        raise ValueError(f"the setting cis.spend.run_cap_adhoc_usd is not readable ({setting_usd!r})")
    if requested in (None, ""):
        return float(setting_usd)
    try:
        cap = float(requested)
    except ValueError:
        raise ValueError(f"CAP_USD must be a number, got {requested!r}") from None
    if cap <= 0:
        raise ValueError("CAP_USD must be > 0")
    return min(cap, float(setting_usd))


# ── score: the measurement record (pure) ─────────────────────────────────────────────────────────────────────────


def _label(value: Any) -> Optional[str]:
    v = str(value or "").strip().lower()
    return v if v in (TRUE_FIRE, FALSE_FIRE) else None


def measure(records: list[dict], label_docs: list[dict]) -> dict:
    """The F12_scope measurement over the fires of one or more pilot records (same schema and detector sha). Only the
    tech lead's label counts toward precision (panel-only labels are not release evidence:
    ground_truth.panel.release_evidence); where both labelled a fire, their agreement is the bar's labeller
    agreement. n_pos is the labelled true fires (a lower bound of the violations: recall is not measured, nothing was
    labelled outside the fires)."""
    from services.ground_truth import sample_size as ss
    from services.quality.quality_settings import F12_PILOT_EVIDENCE_PREFIX
    if not records:
        raise ValueError("no record")
    if {r.get("schema") for r in records} != {SCHEMA}:
        raise ValueError(f"every record must have schema {SCHEMA}")
    shas = {r.get("detector_sha") for r in records}
    if len(shas) != 1:
        raise ValueError(f"records of different F12 detectors do not pool: {sorted(map(str, shas))}")
    run_ids = sorted(r["run_id"] for r in records)
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("a record is given twice")
    fires = {f["fire_id"]: f for r in records for f in r.get("fires") or []}
    labels: dict[str, dict] = {}
    for doc in label_docs:
        for fid, lab in (doc.get("labels") or {}).items():
            if fid in fires and isinstance(lab, dict):
                cur = labels.setdefault(fid, {})
                for who in ("tech_lead", "panel"):
                    if _label(lab.get(who)):
                        cur[who] = _label(lab.get(who))
    lead = {fid: lab["tech_lead"] for fid, lab in labels.items() if lab.get("tech_lead")}
    both = [lab for lab in labels.values() if lab.get("tech_lead") and lab.get("panel")]
    fired, tp = len(lead), sum(v == TRUE_FIRE for v in lead.values())
    agreement = round(sum(lab["tech_lead"] == lab["panel"] for lab in both) / len(both), 4) if both else None
    bar = next(b for b in ss.BARS if b.name == "deterministic")
    precision = round(tp / fired, 4) if fired else None
    lb = ss.wilson_lower(tp, fired)
    passes = ss.passes(tp, fired, tp, bar, agreement=agreement)
    need = ss.min_flagged(precision, bar) if precision is not None else None
    body = {
        "schema": MEASUREMENT_SCHEMA, "detector": "F12_scope", "detector_sha": shas.pop(), "run_ids": run_ids,
        "pieces_written": sum((r.get("summary") or {}).get("written", 0) for r in records),
        "fires": len(fires), "body_fires": sum(f.get("where") == "body" for f in fires.values()),
        "labelled_fires": fired, "unlabelled_fires": len(fires) - fired, "true_fires": tp, "false_fires": fired - tp,
        "panel_labelled": sum(bool(lab.get("panel")) for lab in labels.values()), "agreement": agreement,
        "precision": precision, "wilson95_lower": round(lb, 4) if lb is not None else None,
        "bar": {"name": bar.name, "precision": bar.precision, "lower_bound": bar.lower_bound, "n_pos": bar.n_pos,
                "agreement": bar.agreement},
        "passes": bool(passes),
        "labelled_fires_needed_at_this_precision": need,
        "more_labelled_fires_needed": (max(0, need - fired) if need is not None else None),
        "more_true_fires_needed": max(0, bar.n_pos - tp),
        "false_fire_ids": sorted(fid for fid, v in lead.items() if v == FALSE_FIRE),
        "done_when": {k: sum(((r.get("summary") or {}).get("done_when") or {}).get(k, 0) for r in records)
                      for k in ("new_holds_on_light", "false_pass_stored", "false_pass_pilot",
                                "f9_brand_style_holds")},
        "labels_digest": hashlib.sha256("\n".join(f"{fid}|{lab.get('tech_lead') or ''}|{lab.get('panel') or ''}"
                                                  for fid, lab in sorted(labels.items())).encode()).hexdigest(),
    }
    sha = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    body["record_sha256"] = sha
    body["evidence_ref"] = f"{F12_PILOT_EVIDENCE_PREFIX}{'+'.join(run_ids)}:{sha[:12]}" if passes else None
    return body


# ── the paid run ─────────────────────────────────────────────────────────────────────────────────────────────────


CAPTURE: dict[str, dict] = {}


@contextmanager
def isolated(budget: Budget) -> Iterator[None]:
    """The in-process patches of a run (module attributes of this process only; restored on exit): promoted modes,
    captured piece writes, no FactSheet / claim shadow / A/B shadow write, the per-call cap."""
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import service as svc
    from shared.llm_client.client import LLMClient

    async def capture_finalize(pool, *, piece_id, **kw) -> dict:  # noqa: ARG001
        CAPTURE[str(piece_id)] = {"piece_id": str(piece_id), **kw}
        return CAPTURE[str(piece_id)]

    async def capture_placeholder(pool, *, tenant_id, request_id, **kw) -> dict:  # noqa: ARG001
        return {"piece_id": uuid.uuid4(), "tenant_id": tenant_id, "angle_gate_request_id": request_id,
                "attempt_number": 1, "content_text": "", "status": "processing", **kw}

    async def no_fact_sheet(pool, sheet, context) -> None:  # noqa: ARG001
        context["fs_sha"] = sheet.sha()

    async def no_claim_shadow(*a, **k) -> None:  # noqa: ARG001
        return None

    original_generate = LLMClient.generate

    def generate(self, request):
        budget.check_call()
        resp = original_generate(self, request)
        budget.add(getattr(resp, "cost_usd", 0.0))
        return resp

    patches = [(qg, name, (lambda v=value: v)) for name, value in PROMOTED_MODES.items()]
    patches += [(svc, "_finalize_piece", capture_finalize), (svc, "_insert_placeholder_piece", capture_placeholder),
                (svc, "_store_fact_sheet", no_fact_sheet), (svc, "_claim_shadow_after_write", no_claim_shadow),
                (LLMClient, "_start_shadow", lambda self, *a, **k: None), (LLMClient, "generate", generate)]
    for owner, name, _ in patches:
        if not hasattr(owner, name):
            raise RuntimeError(f"{getattr(owner, '__name__', owner)}.{name} is gone: update scripts/aa788/pilot.py")
    saved = [(owner, name, getattr(owner, name)) for owner, name, _ in patches]
    try:
        for owner, name, new in patches:
            setattr(owner, name, new)
        yield
    finally:
        for owner, name, old in saved:
            setattr(owner, name, old)


async def run_piece(row: dict, pool, *, run_id: str) -> dict:
    """One input: context from the live request, the full T9 loop under the pilot stage, the captured piece read."""
    from uuid import UUID

    from services.acp_content_writing import service as svc
    from shared.llm_client.call_log import bind_eval_stage
    ref = row["piece_id"][:8]
    out: dict = {"piece": ref, "tenant": row["tenant"], "channel": row.get("channel"),
                 "stored_status": row.get("stored_status"), "labels": sorted(set(row.get("labels") or []))}
    with bind_eval_stage(STAGE, run_id):
        try:
            started = await svc.start_write(UUID(row["tenant_id"]), UUID(row["request_id"]), pool)
        except Exception as e:  # noqa: BLE001 — an input that cannot be written is reported, never fatal
            out["error"] = f"start_write {type(e).__name__}: {str(e)[:200]}"
            return out
        piece_id = str(started["piece"]["piece_id"])
        await svc.run_write_background(UUID(row["request_id"]), UUID(piece_id), started["context"], pool)
    cap = CAPTURE.pop(piece_id, None)
    if not cap:
        out["error"] = "no captured piece (the loop stopped before its final write)"
        return out
    if cap.get("status") == "failed":
        out["error"] = f"failed: {str(cap.get('held_reason') or '')[:200]}"
        return out
    out["verdict"] = piece_verdicts(cap)
    out["compare"] = compare_stored(row, out["verdict"])
    out["fires"] = f12_fires(cap, ref, run_id)
    return out


async def _main_run(plan_only: bool) -> int:
    import asyncpg

    from shared import settings
    from shared.llm_client import caps_settings
    from shared.llm_client.call_log import wait_for_pending_writes
    if not plan_only and os.environ.get("CONFIRM") != "PAID":
        print("STOP a paid run needs CONFIRM=PAID (the tech lead's yes for this run); MODE=plan is $0")
        return 2
    n = int(os.environ.get("N") or DEFAULT_N)
    split = lambda k: [p.strip() for p in (os.environ.get(k) or "").split(",") if p.strip()]  # noqa: E731
    tenants = split("TENANTS") or AUDIT_TENANTS.split(",")
    run_id = (os.environ.get("RUN_ID") or
              f"p{datetime.now(timezone.utc):%Y%m%d%H%M}{uuid.uuid4().hex[:4]}").lower()
    started_at = datetime.now(timezone.utc)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl="require", min_size=1, max_size=4,
                                     server_settings={"application_name": "cis-aa788-pilot"})
    try:
        settings.READER.install(pool)
        await settings.READER.refresh(force=True, pool=pool)
        try:
            cap_usd = resolve_cap(caps_settings.run_cap_usd("adhoc"), os.environ.get("CAP_USD"))
        except ValueError as e:
            print(f"STOP {e}")
            return 2
        async with pool.acquire() as conn:
            rows = [dict(r) for r in await conn.fetch(INPUTS_SQL, tenants)]
            inputs = pick_inputs(rows, n=n, pieces=split("PIECES"), skip=split("SKIP"))
            stored = {r["piece_id"]: r["usd"] for r in await conn.fetch(
                STORED_COST_SQL, [r["piece_id"] for r in inputs])}
        modes_in_force = {k: settings.get(k) for k in ("cis.quality_modes.t10_f12_scope",
                                                       "cis.quality_modes.t10_f9_brand_style",
                                                       "cis.quality_modes.t10_f14_floor")}
        print("START " + json.dumps({"run_id": run_id, "mode": "plan" if plan_only else "run", "inputs": len(inputs),
                                     "candidates": len(rows), "cap_usd": cap_usd, "stage": STAGE,
                                     "estimate_usd": round(sum(stored.get(r["piece_id"], 0.0) for r in inputs), 4),
                                     "modes_in_force": modes_in_force, "pilot_modes": PROMOTED_MODES},
                                    sort_keys=True, default=str))
        if plan_only:
            from uuid import UUID
            print("EVIDENCE_RULE " + json.dumps(evidence_rule_check(), sort_keys=True))

            from services.acp_content_writing import service as svc
            with isolated(Budget(0.0)):
                for r in inputs:
                    try:
                        await svc.start_write(UUID(r["tenant_id"]), UUID(r["request_id"]), pool)
                        ok = "ok"
                    except Exception as e:  # noqa: BLE001
                        ok = f"{type(e).__name__}: {str(e)[:120]}"
                    print("INPUT " + json.dumps({"piece": r["piece_id"][:8], "tenant": r["tenant"],
                                                 "channel": r["channel"], "stored_status": r["stored_status"],
                                                 "labels": r["labels"] or [],
                                                 "stored_usd": round(stored.get(r["piece_id"], 0.0), 4),
                                                 "context": ok}, sort_keys=True))
            return 0
        budget = Budget(cap_usd)
        pieces, largest_piece = [], 0.0
        with isolated(budget):
            for r in inputs:
                async with pool.acquire() as conn:
                    budget.note_ledger(await conn.fetchval(RUN_SPEND_SQL, STAGE, started_at, run_id))
                est = piece_estimate(stored.get(r["piece_id"]), largest_piece)
                if budget.tripped or not budget.may_start(est):
                    pieces.append({"piece": r["piece_id"][:8], "tenant": r["tenant"], "error": "stopped by the cap",
                                   "estimate_usd": round(est, 4)})
                    continue
                before = budget.spent()
                try:
                    p = await run_piece(r, pool, run_id=run_id)
                except Exception as e:  # noqa: BLE001 — one piece never ends the run (the cap does)
                    p = {"piece": r["piece_id"][:8], "tenant": r["tenant"],
                         "error": f"{type(e).__name__}: {str(e)[:200]}"}
                wait_for_pending_writes()
                p["cost_usd"] = round(budget.spent() - before, 6)
                largest_piece = max(largest_piece, p["cost_usd"])
                pieces.append(p)
                print("PIECE " + json.dumps({k: v for k, v in p.items() if k != "fires"}, sort_keys=True, default=str))
        async with pool.acquire() as conn:
            budget.note_ledger(await conn.fetchval(RUN_SPEND_SQL, STAGE, started_at, run_id))
        fires = [f for p in pieces for f in p.pop("fires", [])]
        record = {"schema": SCHEMA, "run_id": run_id, "started": started_at.isoformat(),
                  "finished": datetime.now(timezone.utc).isoformat(), "image": os.environ.get("IMAGE_TAG"),
                  "detector_sha": detector_sha(), "stage": STAGE, "pilot_modes": PROMOTED_MODES,
                  "modes_in_force": modes_in_force, "tenants": tenants, "cap_usd": cap_usd,
                  "spent_usd": round(budget.spent(), 6), "model_calls": budget.calls, "cap_tripped": budget.tripped,
                  "pieces": pieces, "fires": fires, "summary": summarize(pieces, fires)}
        print("SUMMARY " + json.dumps(record["summary"], sort_keys=True))
        print("RECORD " + json.dumps(record, sort_keys=True, default=str))
        print("LABELS " + json.dumps(label_template(record), sort_keys=True))
        return 0
    finally:
        await pool.close()


def _main_score(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pilot.py score")
    ap.add_argument("--record", action="append", required=True, help="a RECORD json file (repeatable)")
    ap.add_argument("--labels", action="append", default=[], help="a labels json file (repeatable)")
    a = ap.parse_args(argv)
    records = [json.load(open(p)) for p in a.record]
    labels = [json.load(open(p)) for p in a.labels]
    try:
        print(json.dumps(measure(records, labels), indent=2, sort_keys=True))
    except ValueError as e:
        print(f"STOP {e}")
        return 2
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "score":
        return _main_score(argv[1:])
    mode = (os.environ.get("MODE") or "plan").strip().lower()
    if mode not in ("plan", "run"):
        print("STOP MODE must be plan or run")
        return 2
    return asyncio.run(_main_run(plan_only=mode == "plan"))


if __name__ == "__main__":
    raise SystemExit(main())
