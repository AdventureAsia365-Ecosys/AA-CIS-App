"""AA-1051 — the pre-registration of one W5 writer proof (root ADR 0020 D12; S271 audit W5 "pre-registered per
workflow"; S275 decision "pre-registration file").

A pre-registration is a JSON file in this package (`prereg/<id>.json`), reviewed in a PR, that fixes BEFORE any paid
call what the proof measures and how it decides:

  {
    "format": 1,
    "id": "w5-null-t9-blog",                     the run rows carry it (prompt_eval_runs.details.prereg.id)
    "workflow": "null" | "t9" | "s1" | "t2",     WORKFLOWS below: writer, D12 cap, minimum sample
    "status": "draft" | "registered",            only a registered file may start a paid run
    "registered_by": "tech_lead", "registered_on": "2026-11-20",
    "golden_set": {"name": "aa729", "version": "1.4.0"},
    "eval_split": "gate",                         the sealed side (splits.py)
    "sample_ids": ["t9b-...", ...],               the fixed sample, sorted, all on the GATE side of that version
    "noise_sample_ids": [...],                    the A' repeat arm (a subset), optional
    "release_id": null,                           a candidate release (a UUID) or null for the current writers
    "judge": {"model_key": "...", "connection": "..."},          pinned (D7); the run refuses a mixed judge
    "metrics": [{"name": "item_fail_rate", "kind": "computed", "direction": "lower", "primary": true},
                {"name": "unsupported_trip_promises", "kind": "external", "direction": "lower",
                 "target": {"op": "<=", "value": 0}}],
    "decision": {"rule": "non_inferior" | "improve", "metric": "item_fail_rate", "margin_abs": 0.10,
                 "alpha": 0.05, "noise_se_k": 2.0},
    "requirements": {"paired_all": true, "max_mixed_judges": 0, "max_hard_stop_1": 0},
    "power": {"min_power": 0.8, "writer_sd": null, "claims_per_item": 60},
    "cap_usd": 5.0                                <= the workflow's D12 cap
  }

The margin is ABSOLUTE (in the metric's own unit), fixed, and > 0. `computed` metrics come from the run's result
rows and the ledger (decision.py, cost_report.py); `external` ones (a human read, the blind S1 panel) are recorded
by hand and reported as pending until then.

The lock: `sha256` is taken over the canonical JSON (sorted keys, no whitespace), so reformatting the file changes
nothing and any edit of a value changes it. Every run of a pre-registration records the sha (R2 details.prereg); a
run is refused, and a report is refused, when an earlier run with at least one result recorded another sha
(r2.prereg_refusal / `lock_problem` here): the file was edited after its first result.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional

FORMAT = 1
PREREG_DIR = Path(__file__).resolve().parent / "prereg"
# Root ADR 0020 D12: eval null run <= $5, T9 <= $15, S1 <= $8, T2 <= $5, <= $33 in total; each after its own yes.
WORKFLOWS: dict[str, dict[str, Any]] = {
    "null": {"writer": "t9_blog", "cap_usd": 5.0, "min_items": 12},
    "t9": {"writer": "t9_blog", "cap_usd": 15.0, "min_items": 12},
    "s1": {"writer": "a1", "cap_usd": 8.0, "min_items": 25},
    "t2": {"writer": "t2", "cap_usd": 5.0, "min_items": 20},
}
TOTAL_CAP_USD = 33.0
STATUSES = ("draft", "registered")
RULES = ("non_inferior", "improve")
DIRECTIONS = ("lower", "higher")
TARGET_OPS = ("<=", ">=", "==")
# What decision.py / cost_report.py compute per object from the run (per item of arm B, or per paired item).
COMPUTED_METRICS = ("item_fail_rate", "fail_claims_per_item", "fail_classes_per_item", "first_pass_yield",
                    "hard_stops", "cost_per_object_usd", "requests_per_object", "writer_requests_per_object")
# Metrics a paired decision can read (a value per item on both arms).
PAIRED_METRICS = ("item_fail_rate", "fail_claims_per_item", "fail_classes_per_item")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SAMPLE_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SEMVER_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")


class PreregRefused(ValueError):
    """The pre-registration is missing, malformed, a draft where a registered file is needed, or locked to another
    sha: nothing may be spent."""


@dataclass(frozen=True)
class Prereg:
    doc: dict
    sha256: str
    path: Optional[str] = None

    @property
    def id(self) -> str:
        return self.doc["id"]

    @property
    def workflow(self) -> str:
        return self.doc["workflow"]

    @property
    def writer(self) -> str:
        return WORKFLOWS[self.workflow]["writer"]

    @property
    def registered(self) -> bool:
        return self.doc.get("status") == "registered"


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_of(doc: dict) -> str:
    return hashlib.sha256(canonical(doc)).hexdigest()


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def problems(doc: Any) -> list[str]:
    """Every reason the document is not a valid pre-registration (a draft may have an empty sample). Pure."""
    if not isinstance(doc, dict):
        return ["the pre-registration is not a JSON object"]
    p: list[str] = []
    if doc.get("format") != FORMAT:
        p.append(f"format must be {FORMAT}")
    if not (isinstance(doc.get("id"), str) and _ID_RE.match(doc["id"])):
        p.append("id must match ^[a-z0-9][a-z0-9-]{0,63}$")
    wf = doc.get("workflow")
    if wf not in WORKFLOWS:
        p.append(f"workflow must be one of {sorted(WORKFLOWS)}")
        return p
    spec = WORKFLOWS[wf]
    status = doc.get("status")
    if status not in STATUSES:
        p.append(f"status must be one of {STATUSES}")
    gs = doc.get("golden_set")
    if not (isinstance(gs, dict) and isinstance(gs.get("name"), str) and gs["name"]):
        p.append("golden_set.name is required")
    elif status == "registered" and not (isinstance(gs.get("version"), str) and _SEMVER_RE.match(gs["version"])):
        p.append("a registered pre-registration names its golden_set.version")
    if doc.get("eval_split") != "gate":
        p.append("eval_split must be gate (the sealed side: proofs decide on GATE only)")
    ids = doc.get("sample_ids")
    if not isinstance(ids, list) or any(not isinstance(i, str) or not _SAMPLE_RE.match(i) for i in ids):
        p.append("sample_ids must be a list of golden sample ids")
        ids = []
    else:
        if len(set(ids)) != len(ids):
            p.append("sample_ids must be distinct")
        if ids != sorted(ids):
            p.append("sample_ids must be sorted")
        if status == "registered" and len(ids) < spec["min_items"]:
            p.append(f"a registered {wf} proof needs >= {spec['min_items']} sample ids, has {len(ids)}")
    noise = doc.get("noise_sample_ids", [])
    if not isinstance(noise, list) or any(not isinstance(i, str) for i in noise):
        p.append("noise_sample_ids must be a list of sample ids")
    elif set(noise) - set(ids):
        p.append("noise_sample_ids must be a subset of sample_ids")
    rel = doc.get("release_id")
    if rel is not None and not (isinstance(rel, str) and _UUID_RE.match(rel)):
        p.append("release_id must be null or a release UUID")
    judge = doc.get("judge")
    if not (isinstance(judge, dict) and isinstance(judge.get("model_key"), str) and _KEY_RE.match(judge["model_key"])
            and (judge.get("connection") is None or (isinstance(judge["connection"], str)
                                                      and _KEY_RE.match(judge["connection"])))):
        p.append("judge.model_key (and optional judge.connection) must pin the judge (D7)")
    p.extend(_metric_problems(doc.get("metrics")))
    p.extend(_decision_problems(doc.get("decision"), doc.get("metrics")))
    req = doc.get("requirements", {})
    if not isinstance(req, dict) or any(k not in ("paired_all", "max_mixed_judges", "max_hard_stop_1") for k in req):
        p.append("requirements may hold paired_all, max_mixed_judges, max_hard_stop_1 only")
    pw = doc.get("power")
    if not isinstance(pw, dict):
        p.append("power is required ({min_power, writer_sd, claims_per_item})")
    else:
        mp = _num(pw.get("min_power"))
        if mp is None or not 0.5 <= mp < 1:
            p.append("power.min_power must be in [0.5, 1)")
        sd = pw.get("writer_sd")
        if sd is not None and (_num(sd) is None or _num(sd) < 0):
            p.append("power.writer_sd must be null or >= 0")
        cpi = pw.get("claims_per_item")
        if cpi is not None and (not isinstance(cpi, int) or isinstance(cpi, bool) or cpi < 1):
            p.append("power.claims_per_item must be a positive integer")
    cap = _num(doc.get("cap_usd"))
    if cap is None or not 0 < cap <= spec["cap_usd"]:
        p.append(f"cap_usd must be in (0, {spec['cap_usd']}] (root ADR 0020 D12 for {wf})")
    if status == "registered":
        if not (isinstance(doc.get("registered_by"), str) and doc["registered_by"].strip()):
            p.append("a registered pre-registration names registered_by")
        try:
            date.fromisoformat(str(doc.get("registered_on")))
        except ValueError:
            p.append("a registered pre-registration has registered_on (YYYY-MM-DD)")
    return p


def _metric_problems(metrics: Any) -> list[str]:
    if not isinstance(metrics, list) or not metrics:
        return ["metrics must be a non-empty list"]
    p: list[str] = []
    names = set()
    primaries = 0
    for m in metrics:
        if not isinstance(m, dict) or not (isinstance(m.get("name"), str) and _NAME_RE.match(m["name"])):
            p.append("each metric needs a name (^[a-z][a-z0-9_]*$)")
            continue
        n = m["name"]
        if n in names:
            p.append(f"metric {n} is listed twice")
        names.add(n)
        kind = m.get("kind")
        if kind not in ("computed", "external"):
            p.append(f"metric {n}: kind must be computed or external")
        elif kind == "computed" and n not in COMPUTED_METRICS:
            p.append(f"metric {n}: not a computed metric ({', '.join(COMPUTED_METRICS)})")
        if m.get("direction") not in DIRECTIONS:
            p.append(f"metric {n}: direction must be lower or higher")
        t = m.get("target")
        if t is not None and not (isinstance(t, dict) and t.get("op") in TARGET_OPS and _num(t.get("value")) is not None):
            p.append(f"metric {n}: target must be {{op: <=|>=|==, value: number}}")
        if m.get("primary"):
            primaries += 1
            if n not in PAIRED_METRICS:
                p.append(f"metric {n}: the primary metric must be a paired one ({', '.join(PAIRED_METRICS)})")
    if primaries != 1:
        p.append("exactly one metric is primary")
    return p


def _decision_problems(dec: Any, metrics: Any) -> list[str]:
    if not isinstance(dec, dict):
        return ["decision is required ({rule, metric, margin_abs, alpha, noise_se_k})"]
    p: list[str] = []
    if dec.get("rule") not in RULES:
        p.append(f"decision.rule must be one of {RULES}")
    primary = [m.get("name") for m in metrics or [] if isinstance(m, dict) and m.get("primary")]
    if dec.get("metric") not in primary:
        p.append("decision.metric must be the primary metric")
    margin = _num(dec.get("margin_abs"))
    if margin is None or margin <= 0:
        p.append("decision.margin_abs must be a fixed absolute number > 0")
    alpha = _num(dec.get("alpha"))
    if alpha is None or not 0 < alpha <= 0.2:
        p.append("decision.alpha must be in (0, 0.2]")
    k = _num(dec.get("noise_se_k"))
    if k is None or not 0 < k <= 4:
        p.append("decision.noise_se_k must be in (0, 4]")
    return p


def from_doc(doc: dict, *, path: Optional[str] = None) -> Prereg:
    bad = problems(doc)
    if bad:
        raise PreregRefused("; ".join(bad))
    return Prereg(doc=doc, sha256=sha256_of(doc), path=path)


def path_for(prereg_id: str) -> Path:
    if not _ID_RE.match(prereg_id or ""):
        raise PreregRefused(f"not a pre-registration id: {prereg_id!r}")
    return PREREG_DIR / f"{prereg_id}.json"


def load(path_or_id: str) -> Prereg:
    """A pre-registration by file path or by id (prereg/<id>.json). Missing or malformed: PreregRefused."""
    p = Path(path_or_id) if path_or_id.endswith(".json") else path_for(path_or_id)
    if not p.is_file():
        raise PreregRefused(f"pre-registration missing: {p.name}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PreregRefused(f"pre-registration {p.name} is not JSON: {e}") from e
    pre = from_doc(doc, path=str(p))
    if pre.id != p.stem:
        raise PreregRefused(f"file {p.name} holds id {pre.id}: the file name is the id")
    return pre


def require_registered(pre: Prereg) -> None:
    if not pre.registered:
        raise PreregRefused(f"{pre.id} is a draft: a paid run needs status registered (fill it with `runner draft`, "
                            "have it reviewed in a PR)")


def lock_problem(pre: Prereg, prior_runs: Iterable[dict]) -> Optional[str]:
    """Why the file no longer matches what its first result was measured under, or None (the same rule as
    r2.prereg_refusal). `prior_runs`: [{run_id, sha256, results}] (r2.PREREG_RUNS_SQL)."""
    for r in prior_runs or []:
        if int(r.get("results") or 0) > 0 and r.get("sha256") != pre.sha256:
            return (f"{pre.id} was edited after its first result: run {r.get('run_id')} recorded sha "
                    f"{str(r.get('sha256'))[:12]}…, the file hashes to {pre.sha256[:12]}…")
    return None


def pick_sample(candidates: Iterable[str], n: int, *, seed: str) -> list[str]:
    """`n` sample ids from `candidates`, chosen by a seeded hash (no data is read to choose), returned sorted. Pure."""
    ranked = sorted(set(candidates), key=lambda s: hashlib.sha256(f"{seed}:{s}".encode("utf-8")).hexdigest())
    return sorted(ranked[:max(0, n)])


def draft(template: dict, *, items: list[dict], golden_version: str, n: Optional[int] = None,
          noise: int = 0) -> dict:
    """A filled draft: the template with the golden version and a fixed sample of the writer's non-trap GATE items
    (`items` as r2.PgStore.items returns them), chosen by a seeded hash of the pre-registration id. The status stays
    draft: the tech lead registers it in a reviewed PR. Pure."""
    wf = template["workflow"]
    writer = WORKFLOWS[wf]["writer"]
    size = n if n is not None else max(WORKFLOWS[wf]["min_items"], len(template.get("sample_ids") or []))
    gate = [it["sample_id"] for it in items
            if it.get("writer") == writer and not it.get("trap") and it.get("eval_split") == "gate"]
    picked = pick_sample(gate, size, seed=template["id"])
    out = json.loads(json.dumps(template))
    out["golden_set"] = {**(out.get("golden_set") or {}), "version": golden_version}
    out["sample_ids"] = picked
    out["noise_sample_ids"] = pick_sample(picked, noise, seed=f"{template['id']}:noise") if noise else []
    out["status"] = "draft"
    out["draft_note"] = {"gate_items_available": len(gate), "asked": size, "picked": len(picked)}
    return out


__all__ = ["COMPUTED_METRICS", "FORMAT", "PAIRED_METRICS", "PREREG_DIR", "Prereg", "PreregRefused", "TOTAL_CAP_USD",
           "WORKFLOWS", "canonical", "draft", "from_doc", "load", "lock_problem", "path_for", "pick_sample",
           "problems", "require_registered", "sha256_of"]
