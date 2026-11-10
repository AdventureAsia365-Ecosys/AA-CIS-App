"""AA-1048 — the claim-label panel: export / import format (JSONL keyed by claim hash), consolidation of the 3 lenses,
the random audit draw, contradictions, the agreement report, final labels and verdict_rule.

The agent panel is NOT run in code: the orchestrator gives the export file and the brief
(docs/calibration/aa1048-claim-panel-brief.md) to three agents, one per lens, and imports their JSONL. Every function
here is pure (no DB, no network, no model call); store.py writes what `consolidate` returns.

Export record (one line per claim):
  {"claim_hash", "set_id", "writer", "scope", "tenant_id", "refs": {sample_id | piece_id | tour_id | version_id},
   "content_sha256", "field", "span": [start, end], "claim_text", "context": {"before", "after"} (300 chars each),
   "field_text": "<the whole field>", "source_text": "<the full source the writer read>",
   "evidence": ["<up to 3 source lines aligned by code: the panel checks them>"], "candidate_classes": [...],
   "stratum", "split_group": "<tour_id:... | sample_id:... | ... | content:<sha>>", "split"}
  The split is recomputed from refs + content sha on import (claims.split_group): a line whose split disagrees is
  refused.
Panel import record (one line per claim per lens):
  {"claim_hash", "lens": "source" | "promise" | "skeptic", "class": "<policy class id>",
   "label": "violation" | "ok" | "unsure", "reason": "<= 600 chars", "labeller": "panel:<run id>"}
Tech-lead import record (one line per adjudicated claim):
  {"claim_hash", "labeller_kind": "tech_lead", "class", "label", "reason"}
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from services.ground_truth import claims as cl
from services.ground_truth import policy

LENSES = ("source", "promise", "skeptic")
AUDIT_PCT = 0.20
AUDIT_SEED = "aa1048-audit"
EXPORT_SCHEMA = "aa1048-claim-export/1"
REASON_MAX = 600
# AA-1063: a tech-lead row written by a machine rule (the Jev pre-filter, services/ground_truth/prefilter.py) carries a
# labeller that starts with this prefix. Its final label has source 'prefilter': it decides the claim's label (and
# keeps it out of the review queue) but it is never release evidence and never an audit adjudication (critic F14:
# "labels with panel-only provenance never count as release evidence"; S275 lesson: machine-chosen labels stay out of
# the R1 decision record).
PREFILTER_PREFIX = "prefilter:"


def is_prefilter(row: dict) -> bool:
    """A tech-lead row written by a machine rule (labeller 'prefilter:<rule>')."""
    return row.get("labeller_kind") == "tech_lead" and str(row.get("labeller") or "").startswith(PREFILTER_PREFIX)


class PanelError(ValueError):
    """The import breaks a rule of the format: nothing is written."""


# ── export ─────────────────────────────────────────────────────────────────────────────────────────────────────


def export_records(*, set_id: str, writer: str, scope: str, tenant_id: Optional[str], refs: dict, output: Any,
                   source_text: str, stratum: str = "representative",
                   candidate_classes: Optional[Iterable[str]] = None,
                   fields: Optional[Iterable[str]] = None) -> list[dict]:
    """The export lines of one object: one per sentence claim, with its context and the full source (the panel reads
    the whole source, never an excerpt: lesson S245 AA-729)."""
    if writer not in cl.WRITERS:
        raise PanelError(f"unknown writer {writer!r}")
    if stratum not in cl.STRATA:
        raise PanelError(f"unknown stratum {stratum!r}")
    if (scope == "platform") != (writer == "a1") or (scope == "tenant") != bool(tenant_id):
        raise PanelError(f"scope {scope!r} / tenant does not fit writer {writer!r}")
    if not any(refs.get(k) for k in ("sample_id", "piece_id", "version_id")):
        raise PanelError("an object needs a sample_id, piece_id or version_id")
    classes = sorted(candidate_classes) if candidate_classes else sorted(
        c.id for c in policy.policy_classes().values() if writer in c.writers)
    texts = dict(cl.field_texts(output))
    refs = {k: v for k, v in refs.items() if v is not None}
    out = []
    for c in cl.claims_of(output, fields=fields):
        group = cl.split_group(refs, c.content_sha256)
        text = texts[c.field]
        before = text[max(0, c.span_start - 300):c.span_start].strip()
        after = text[c.span_end:c.span_end + 300].strip()
        out.append({"schema": EXPORT_SCHEMA, "claim_hash": c.claim_hash, "set_id": set_id, "writer": writer,
                    "scope": scope, "tenant_id": tenant_id, "refs": refs,
                    "content_sha256": c.content_sha256, "field": c.field, "span": [c.span_start, c.span_end],
                    "claim_text": c.claim_text, "context": {"before": before, "after": after},
                    "field_text": text, "source_text": source_text,
                    "evidence": cl.align(c.claim_text, source_text), "candidate_classes": classes, "stratum": stratum,
                    "split_group": group, "split": cl.split_of(group)})
    return out


# ── import ─────────────────────────────────────────────────────────────────────────────────────────────────────


def read_jsonl(text: str) -> list[dict]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise PanelError(f"line {n}: not JSON ({e.msg})") from e
        if not isinstance(obj, dict):
            raise PanelError(f"line {n}: not an object")
        out.append(obj)
    return out


@dataclass
class Judgement:
    claim_hash: str
    labeller_kind: str          # agent_panel | tech_lead
    labeller: str
    lens: Optional[str]
    claim_class: str
    label: str
    reason: str = ""


def parse_judgements(rows: list[dict], *, export_by_hash: dict[str, dict]) -> list[Judgement]:
    """Validate the panel / tech-lead lines against the export. Any problem refuses the whole file (nothing is half
    applied)."""
    problems: list[str] = []
    out: list[Judgement] = []
    seen: set[tuple] = set()
    for i, r in enumerate(rows, 1):
        h = str(r.get("claim_hash") or "")
        kind = str(r.get("labeller_kind") or "agent_panel")
        lens = r.get("lens")
        if h not in export_by_hash:
            problems.append(f"#{i}: claim_hash not in the export")
            continue
        if kind not in ("agent_panel", "tech_lead"):
            problems.append(f"#{i}: labeller_kind {kind!r}")
            continue
        if kind == "agent_panel" and lens not in LENSES:
            problems.append(f"#{i}: lens {lens!r} is not one of {list(LENSES)}")
            continue
        if kind == "tech_lead":
            lens = None
        if not policy.known_class(r.get("class")):
            problems.append(f"#{i}: class {r.get('class')!r} is not a policy class")
            continue
        if r.get("label") not in cl.LABELS:
            problems.append(f"#{i}: label {r.get('label')!r}")
            continue
        key = (h, kind, lens)
        if key in seen:
            problems.append(f"#{i}: a second judgement of the same claim by the same {kind} lens")
            continue
        seen.add(key)
        labeller = str(r.get("labeller") or ("tech_lead" if kind == "tech_lead" else ""))[:128]
        if not labeller:
            problems.append(f"#{i}: no labeller")
            continue
        out.append(Judgement(h, kind, labeller, lens, str(r["class"]), str(r["label"]),
                             " ".join(str(r.get("reason") or "").split())[:REASON_MAX]))
    if problems:
        raise PanelError("; ".join(problems[:20]) + (f" (+{len(problems) - 20} more)" if len(problems) > 20 else ""))
    return out


def _audit_drawn(claim_hashes: list[str], pct: float = AUDIT_PCT) -> set[str]:
    """A random, reproducible AUDIT_PCT of the agreed claims (ordered by a seeded hash; rounded half up, >= 1)."""
    if not claim_hashes:
        return set()
    ordered = sorted(claim_hashes, key=lambda h: cl.sha256_text(f"{AUDIT_SEED}:{h}"))
    k = max(1, int(len(ordered) * pct + 0.5))
    return set(ordered[:k])


@dataclass
class Consolidated:
    rows: list[dict]                       # one per judgement, ready for store.insert_rows
    states: dict[str, str]                 # claim_hash -> agreed | disputed | audit (panel state)
    panel_label: dict[str, Optional[str]]  # claim_hash -> the panel majority label (None = no majority)
    problems: list[str] = field(default_factory=list)


def consolidate(export_lines: list[dict], judgements: list[Judgement], *, set_id: str, set_version: str,
                policy_sha: Optional[str] = None, audit_pct: float = AUDIT_PCT) -> Consolidated:
    """The rows of one label set version. Panel state per claim: agreed (every lens gave the same class and label and
    none is unsure), disputed (anything else, or fewer than 3 lenses), audit (an agreed claim in the random draw).
    A tech-lead judgement is stored as 'final'."""
    if not cl.valid_set(set_id, set_version):
        raise PanelError(f"bad set {set_id!r} / version {set_version!r}")
    by_hash = {e["claim_hash"]: e for e in export_lines}
    panel: dict[str, list[Judgement]] = defaultdict(list)
    for j in judgements:
        if j.labeller_kind == "agent_panel":
            panel[j.claim_hash].append(j)
    states: dict[str, str] = {}
    majority: dict[str, Optional[str]] = {}
    for h, js in panel.items():
        pairs = {(j.claim_class, j.label) for j in js}
        agreed = len(js) == len(LENSES) and len(pairs) == 1 and js[0].label != "unsure"
        states[h] = "agreed" if agreed else "disputed"
        top = Counter(j.label for j in js).most_common()
        majority[h] = top[0][0] if top and (len(top) == 1 or top[0][1] > top[1][1]) else None
    for h in _audit_drawn(sorted(h for h, s in states.items() if s == "agreed"), audit_pct):
        states[h] = "audit"
    splits: dict[str, str] = {}
    for h, e in by_hash.items():
        split = cl.split_of(cl.split_group(e.get("refs"), e["content_sha256"]))
        if e.get("split") is not None and e["split"] != split:
            raise PanelError(f"{h[:12]}: the export says split {e['split']!r}, its group key gives {split!r}")
        splits[h] = split
    rows = []
    for j in judgements:
        e = by_hash[j.claim_hash]
        s, t = e["span"]
        if len(e["claim_text"]) != t - s:
            raise PanelError(f"{j.claim_hash[:12]}: the export span does not fit its claim text")
        rows.append({
            "set_id": set_id, "set_version": set_version, "writer": e["writer"], "scope": e["scope"],
            "tenant_id": e.get("tenant_id"), "sample_id": (e.get("refs") or {}).get("sample_id"),
            "piece_id": (e.get("refs") or {}).get("piece_id"), "tour_id": (e.get("refs") or {}).get("tour_id"),
            "version_id": (e.get("refs") or {}).get("version_id"), "content_sha256": e["content_sha256"],
            "field": e["field"], "span_start": s, "span_end": t, "claim_text": e["claim_text"],
            "claim_hash": j.claim_hash, "claim_class": j.claim_class, "label": j.label,
            "labeller_kind": j.labeller_kind, "labeller": j.labeller, "lens": j.lens, "reason": j.reason or None,
            "split": splits[j.claim_hash],
            "stratum": e.get("stratum") or "representative",
            "adjudication": "final" if j.labeller_kind == "tech_lead" else states.get(j.claim_hash, "disputed"),
            "policy_sha": policy_sha})
    missing = sorted(h for h in by_hash if h not in panel and not any(
        j.claim_hash == h for j in judgements if j.labeller_kind == "tech_lead"))
    return Consolidated(rows, states, majority,
                        [f"{len(missing)} exported claim(s) have no judgement"] if missing else [])


# ── final labels, contradictions, agreement ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FinalLabel:
    claim_hash: str
    claim_class: str
    label: str
    source: str                 # tech_lead | prefilter | panel_agreed | none
    writer: str
    content_sha256: str
    claim_text: str
    sample_id: Optional[str] = None
    stratum: str = "representative"
    split: str = "train"


def final_labels(rows: list[dict]) -> dict[str, FinalLabel]:
    """claim_hash -> final label: a tech-lead row wins (source tech_lead; source prefilter when the newest one was
    written by a machine rule, AA-1063); else an agreed or audit-drawn panel claim takes the panel's common label
    (source panel_agreed); a disputed claim without a tech-lead row has no final label (source none, label unsure).
    Which labels may count as release evidence is `release_evidence`."""
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_hash[r["claim_hash"]].append(r)
    out: dict[str, FinalLabel] = {}
    for h, rs in by_hash.items():
        base = rs[0]
        common = dict(writer=base["writer"], content_sha256=base["content_sha256"], claim_text=base["claim_text"],
                      sample_id=base.get("sample_id"), stratum=base.get("stratum") or "representative",
                      split=base.get("split") or "train")
        lead = [r for r in rs if r["labeller_kind"] == "tech_lead"]
        if lead:
            out[h] = FinalLabel(h, lead[-1]["claim_class"], lead[-1]["label"],
                                "prefilter" if is_prefilter(lead[-1]) else "tech_lead", **common)
            continue
        panel = [r for r in rs if r["labeller_kind"] == "agent_panel"]
        state = panel[0]["adjudication"] if panel else "disputed"
        if state in ("agreed", "audit"):
            out[h] = FinalLabel(h, panel[0]["claim_class"], panel[0]["label"], "panel_agreed", **common)
        else:
            out[h] = FinalLabel(h, panel[0]["claim_class"] if panel else "", "unsure", "none", **common)
    return out


def release_evidence(finals: dict[str, FinalLabel], *, audit_ok: bool = False) -> dict[str, FinalLabel]:
    """The labels that may count for a gate's measurement record (critic F14: "labels with panel-only provenance never
    count as release evidence"): tech-lead labels always; agreed panel labels only when the caller states that the
    set's audit draw was adjudicated and its panel error rate accepted (`audit_ok`). A machine-rule label (source
    prefilter, AA-1063) never counts, with or without `audit_ok`."""
    keep = {"tech_lead", "panel_agreed"} if audit_ok else {"tech_lead"}
    return {h: f for h, f in finals.items() if f.source in keep}


def contradictions(finals: dict[str, FinalLabel]) -> list[dict]:
    """Internal contradictions of a label set: the same normalised claim text labelled under the same class with
    different labels (on any object), and one claim hash with more than one final label (impossible by the DB key,
    checked for imported files). The W2 bar is 0."""
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    hashes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f in finals.values():
        if f.label == "unsure" or not f.claim_class:
            continue
        key = (f.claim_class, cl.normalise(f.claim_text))
        groups[key].add(f.label)
        hashes[key].append(f.claim_hash)
    return [{"class": k[0], "labels": sorted(v), "claims": sorted(hashes[k])} for k, v in groups.items() if len(v) > 1]


def agreement_report(rows: list[dict]) -> dict:
    """Per class: claims, panel agreement (share of claims whose 3 lenses agreed), and on claims the tech lead
    labelled: how often the panel's common label stood. The panel error rate is measured on the AUDIT draw only (a
    random sample of agreed claims, critic F14); disputes are not an error-rate sample. Machine-rule rows (source
    prefilter) are not adjudications here."""
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_hash[r["claim_hash"]].append(r)
    per_class: dict[str, Counter] = defaultdict(Counter)
    audit_n = audit_flipped = 0
    for rs in by_hash.values():
        panel = [r for r in rs if r["labeller_kind"] == "agent_panel"]
        # AA-1063: only a human decision adjudicates (a machine-rule row would measure the panel against itself)
        lead = [r for r in rs if r["labeller_kind"] == "tech_lead" and not is_prefilter(r)]
        if not panel:
            continue
        cls = Counter(r["claim_class"] for r in panel).most_common(1)[0][0]
        c = per_class[cls]
        c["claims"] += 1
        state = panel[0]["adjudication"]
        c["agreed"] += state in ("agreed", "audit")
        c["disputed"] += state == "disputed"
        if lead:
            c["adjudicated"] += 1
            stood = (state in ("agreed", "audit") and lead[-1]["label"] == panel[0]["label"]
                     and lead[-1]["claim_class"] == panel[0]["claim_class"])
            c["panel_stood"] += stood
            if state == "audit":
                audit_n += 1
                audit_flipped += not stood
    classes = {k: {**dict(v), "agreement": round(v["agreed"] / v["claims"], 4) if v["claims"] else None}
               for k, v in sorted(per_class.items())}
    return {"classes": classes, "audit": {"audited": audit_n, "flipped": audit_flipped,
                                          "panel_error_rate": round(audit_flipped / audit_n, 4) if audit_n else None}}


def manifest(rows: list[dict], *, set_id: str, set_version: str) -> dict:
    """The public manifest of a label set version (root repo; the Dev DB is the master copy): ids, counts and one
    digest, never a claim text or a reason. The digest is sha256 over the sorted "claim_hash|labeller_kind|lens|class|
    label" lines, so any change of a label shows as a new digest."""
    finals = final_labels(rows)
    lines = sorted(f"{r['claim_hash']}|{r['labeller_kind']}|{r.get('lens') or ''}|{r['claim_class']}|{r['label']}"
                   for r in rows)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for f in finals.values():
        counts[f.claim_class or "-"][f.label] += 1
    return {"set_id": set_id, "set_version": set_version, "rows": len(rows), "claims": len(finals),
            "objects": len({r["content_sha256"] for r in rows}),
            "writers": sorted({r["writer"] for r in rows}),
            "policy_sha": sorted({r.get("policy_sha") or "" for r in rows}),
            "evidence_sources": dict(Counter(f.source for f in finals.values())),
            "splits": dict(Counter(f.split for f in finals.values())),
            "labels_by_class": {k: dict(v) for k, v in sorted(counts.items())},
            "digest": cl.sha256_text("\n".join(lines))}


# ── verdict_rule ───────────────────────────────────────────────────────────────────────────────────────────────


def verdict_rule(item_labels: Iterable[FinalLabel]) -> str:
    """The item verdict re-derived from its claim labels under the policy (design-review "GROUND TRUTH"): fail when a
    claim of a failing class (severity hard / block) is a violation; unverified when such a claim has no final
    label (unsure); pass otherwise. Warn-only classes never fail an item."""
    unsure = False
    for f in item_labels:
        pc = policy.policy_class(f.claim_class)
        if pc is None or not pc.fails_item:
            continue
        if f.label == "violation":
            return "fail"
        if f.label == "unsure":
            unsure = True
    return "unverified" if unsure else "pass"


def rederive(fixture: dict, finals: dict[str, FinalLabel], *,
             content_sha_of: Optional[dict[str, str]] = None) -> dict:
    """Item verdicts of a golden fixture re-derived from claim labels, and every item whose label flips. Items with no
    claim label are listed as not covered (their label is kept). `content_sha_of` (sample_id -> the content sha of
    its output as exported) maps claims to items by object: two items with the same text share their claims (the
    claim hash is per text, so a final label names only one of their sample ids)."""
    by_sha: dict[str, list[FinalLabel]] = defaultdict(list)
    by_sample: dict[str, list[FinalLabel]] = defaultdict(list)
    for f in finals.values():
        by_sha[f.content_sha256].append(f)
        if f.sample_id:
            by_sample[f.sample_id].append(f)
    if content_sha_of:
        for sid, sha in content_sha_of.items():
            if by_sha.get(sha):
                by_sample[sid] = list(by_sha[sha])
    flips, same, not_covered, unverified = [], [], [], []
    for s in fixture.get("samples") or []:
        sid = s["sample_id"]
        old = (s.get("label") or {}).get("verdict")
        if sid not in by_sample:
            not_covered.append(sid)
            continue
        new = verdict_rule(by_sample[sid])
        if new == "unverified":
            unverified.append(sid)
        elif new != old:
            flips.append({"sample_id": sid, "from": old, "to": new,
                          "label_source": s.get("label_source"),
                          "violations": sorted({f.claim_class for f in by_sample[sid] if f.label == "violation"})})
        else:
            same.append(sid)
    return {"flips": flips, "unchanged": len(same), "unverified": unverified, "not_covered": len(not_covered)}


__all__ = ["AUDIT_PCT", "Consolidated", "EXPORT_SCHEMA", "FinalLabel", "Judgement", "LENSES", "PREFILTER_PREFIX",
           "PanelError", "agreement_report", "consolidate", "contradictions", "export_records", "manifest",
           "final_labels", "is_prefilter", "parse_judgements", "read_jsonl", "rederive", "release_evidence",
           "verdict_rule"]
