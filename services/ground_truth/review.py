"""AA-1063 — the claim ground truth in the Writing Lab: the review of golden_claim queue items, the agent proposals
(shared.eval_claim_proposal, CIS 394), the label set browser and the tech lead's corrections of a final label
(shared.eval_claim_relabel, CIS 394).

Review. A golden_claim item of the disagreement queue (shared.writing_memory_disagreement, CIS 314 + 388) names a
disputed or audit-drawn claim of shared.eval_claim_label by its first panel row (source_ref). The view shows the
sentence, its context and evidence, the full source, the three lenses and the newest agent proposal; one answer
(correct = ok, wrong = violation, unsure) resolves the item AND writes the tech-lead row in one transaction
(`answer_item`):
  - a panel with a majority label (machine_verdict ok / violation): judge_right when the answer keeps it, human_right
    when it flips it, unclear for unsure, exactly as `store.resolution_label` reads it; the tech-lead row carries
    that label (what `store.pull_resolutions` would write later);
  - a panel with no majority or an unsure majority (machine_verdict split / unsure): the tech-lead row carries the
    answered label and the item is resolved unclear (the S275 path: a split panel cannot become a label through
    `resolution_label`).
`pull_resolutions` skips a claim that already has a tech-lead row or a correction, so running it afterwards changes
nothing.

Corrections. A final label (tech-lead row, an agreed panel label or an earlier correction) can be changed later:
append only, a new shared.eval_claim_relabel row that supersedes the current one (a chain per claim). The newest
correction is the claim's final label: `effective_rows` adds one synthetic tech-lead row per correction after the
stored ones, so `panel.final_labels` (lead[-1]) and `panel.agreement_report` read it unchanged.

One decision per claim. A correction is allowed on an agreed panel claim whose audit-drawn queue item is still open,
so the queue and the browser can meet on one claim. The rule: the newest effective tech-lead row (`newest_lead`:
stored or a correction, the row `final_labels` reads) is the decision everywhere. An answer that disagrees with it is
refused (409 `relabelled` / `labelled`: change it from the label set browser); an answer that agrees resolves the
item without a new row; `pull_resolutions` skips a corrected claim; the browser's who / when / why, the queue card's
decision and a new correction's previous_source all come from that same row. `relabel` and `answer_item` lock the
claim's queue items first, so the two paths never interleave.

Context. Label rows hold the claim span only. The object the claim was cut from gives the field text, the context, the
evidence lines and the full source, built like `panel.export_records`, found by `resolve_sources` (one resolver for the
page, the context route and the pre-filter): the golden item (shared.eval_golden_item, by sample_id and the content sha
of its output as the panel saw it), else the claim's own stored object of the claim's tenant: the live T9 piece, or the
T2 version (S276: a round-2 addendum's T2 samples were never imported as golden items and their sample ids name other
items), whose source is its Master as the T2 writer reads it.

Text. Every read here returns tour or tenant text: the routes are Writing Lab text reads (LAB_TEXT_LOGGED).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from services.ground_truth import claims as cl
from services.ground_truth import panel as pn
from services.ground_truth import policy
from services.ground_truth import store

ANSWERS = {"correct": "ok", "wrong": "violation", "unsure": "unsure"}
LABEL_ANSWER = {v: k for k, v in ANSWERS.items()}
CONFIDENCE = ("high", "medium", "low")
STATES = ("open", "resolved", "all")
PROPOSAL_FILTERS = ("ok", "violation", "unsure", "none")
SOURCES = ("tech_lead", "prefilter", "panel_agreed", "none")
NOTE_KEYS = ("claim_vi", "claim_en", "source_says_en", "source_says_vi", "doubt_en", "doubt_vi")
NOTE_MAX = 1200
REASON_MAX = pn.REASON_MAX
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:@._-]{0,63}$")
MAX_PROPOSALS = 2000
MAX_BULK = 500
CONTEXT_CHARS = 300
PANEL_HIDDEN_KEYS = frozenset({"gate_ledger", "flags"})     # what the panel never saw (scripts/aa729 panel_output)
AUDIT_ANSWER = "ground_truth.claim_review_answer"
AUDIT_PROPOSALS = "ground_truth.claim_proposals_import"
AUDIT_RELABEL = "ground_truth.claim_relabel"
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class ReviewError(Exception):
    """A refused review action: HTTP status, a short code and a sentence for the page."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, code, detail)
        self.status, self.code, self.detail = status, code, detail


def _clean(text: Any, limit: int) -> Optional[str]:
    t = " ".join(str(text or "").split())[:limit]
    return t or None


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


# ── pure: answers ───────────────────────────────────────────────────────────────────────────────────────────────────


def answer_plan(machine_verdict: str, answer: str) -> tuple[str, str]:
    """(label of the tech-lead row, queue resolution) for an answer to an item whose panel said `machine_verdict`."""
    if answer not in ANSWERS:
        raise ReviewError(422, "invalid", f"answer must be one of {list(ANSWERS)}")
    label = ANSWERS[answer]
    if machine_verdict in ("ok", "violation"):
        resolution = ("judge_right" if label == machine_verdict else "human_right" if label != "unsure"
                      else "unclear")
        return store.resolution_label(machine_verdict, resolution), resolution
    return label, "unclear"


def pick_class(panel_rows: list[dict], label: str, anchor: dict) -> str:
    """The class of a tech-lead row: the most common class among the lenses that gave `label`, else the anchor's."""
    same = [r["claim_class"] for r in panel_rows if r.get("label") == label and r.get("claim_class")]
    if same:
        return Counter(same).most_common(1)[0][0]
    return anchor["claim_class"]


def lead_row(anchor: dict, *, label: str, claim_class: str, labeller: str, reason: Optional[str],
             policy_sha: Optional[str]) -> dict:
    """A tech-lead row for the claim of `anchor` (one of its stored rows), in store.COLUMNS."""
    row = {c: anchor.get(c) for c in store.COLUMNS}
    row.update(labeller_kind="tech_lead", labeller=labeller[:128], lens=None, label=label, claim_class=claim_class,
               adjudication="final", reason=_clean(reason, REASON_MAX),
               policy_sha=policy_sha or anchor.get("policy_sha"))
    return row


# ── pure: corrections and final labels ─────────────────────────────────────────────────────────────────────────────


def effective_rows(rows: list[dict], relabels: Iterable[dict]) -> list[dict]:
    """The label rows with one synthetic tech-lead row per correction; per claim, the tech-lead rows come after the
    panel rows in time order (created_at; stored rows without one first), so panel.final_labels' lead[-1] is the
    newest decision. Corrections of a claim with no stored row are ignored (the anchor FK keeps that from happening)."""
    by_hash = {r["claim_hash"]: r for r in rows}
    relabels = list(relabels)
    depth = chain_depths(relabels)
    synth = []
    for x in relabels:
        base = by_hash.get(x["claim_hash"])
        if base is None:
            continue
        r = {c: base.get(c) for c in store.COLUMNS}
        r.update(label_id=x.get("relabel_id"), created_at=x.get("created_at"), labeller_kind="tech_lead",
                 labeller=x.get("labeller"), lens=None, claim_class=x["claim_class"], label=x["label"],
                 reason=x.get("reason"), adjudication="final", relabel=True,
                 chain_depth=depth.get(str(x.get("relabel_id")), 1))
        synth.append(r)
    if not synth:
        return list(rows)
    rest = [r for r in rows if r.get("labeller_kind") != "tech_lead"]
    lead = [r for r in rows if r.get("labeller_kind") == "tech_lead"] + synth

    def when(r: dict) -> tuple:
        # time first; at the same instant a correction comes after a stored row, and a later link of the chain after
        # the one it supersedes (two corrections in one transaction share now())
        t = r.get("created_at")
        return (t is not None, _iso(t) or "", bool(r.get("relabel")), r.get("chain_depth") or 0)

    return rest + sorted(lead, key=when)


def newest_lead(rows: list[dict], relabels: Iterable[dict]) -> Optional[dict]:
    """The decision of one claim: its newest effective tech-lead row (a stored row, or a correction marked
    relabel=True), the row panel.final_labels reads; None when nobody decided it."""
    lead = [r for r in effective_rows(rows, relabels) if r.get("labeller_kind") == "tech_lead"]
    return lead[-1] if lead else None


def chain_depths(relabels: list[dict]) -> dict[str, int]:
    """relabel_id -> its position in its claim's chain (1 = the first correction, which supersedes nothing)."""
    parent = {str(x["relabel_id"]): (str(x["supersedes"]) if x.get("supersedes") else None) for x in relabels}
    out: dict[str, int] = {}
    for rid in parent:
        n, cur, seen = 0, rid, set()
        while cur is not None and cur not in seen and n < len(parent) + 1:
            seen.add(cur)
            n += 1
            cur = parent.get(cur)
        out[rid] = n
    return out


def chain_head(relabels: list[dict]) -> Optional[dict]:
    """The newest correction of one claim: the one no other correction supersedes."""
    if not relabels:
        return None
    superseded = {str(x["supersedes"]) for x in relabels if x.get("supersedes")}
    heads = [x for x in relabels if str(x["relabel_id"]) not in superseded]
    depth = chain_depths(relabels)
    return max(heads or relabels, key=lambda x: (depth.get(str(x["relabel_id"]), 0), _iso(x.get("created_at")) or ""))


def set_summary(rows: list[dict], relabels: list[dict]) -> dict:
    """Counts of one label set version: claims, who decided (tech lead / agreed panel / none), corrections, the audit
    draw (audited, flipped, panel error rate), disputed claims, splits and writers. No text."""
    eff = effective_rows(rows, relabels)
    finals = pn.final_labels(eff)
    agreement = pn.agreement_report(eff)
    panel_state: dict[str, str] = {}
    for r in rows:
        if r.get("labeller_kind") == "agent_panel":
            panel_state.setdefault(r["claim_hash"], r.get("adjudication") or "disputed")
    times = [_iso(r.get("created_at")) for r in list(rows) + list(relabels) if r.get("created_at")]
    return {"rows": len(rows), "claims": len(finals), "relabels": len(relabels),
            "evidence_sources": {s: sum(1 for f in finals.values() if f.source == s) for s in SOURCES},
            "labels": dict(sorted(Counter(f.label for f in finals.values()).items())),
            "panel_states": dict(sorted(Counter(panel_state.values()).items())),
            "audit": agreement["audit"], "splits": dict(sorted(Counter(f.split for f in finals.values()).items())),
            "writers": dict(sorted(Counter(f.writer for f in finals.values()).items())),
            "last_change": max(times) if times else None}


def claim_entries(rows: list[dict], relabels: list[dict]) -> list[dict]:
    """One entry per claim of a set version: its final label, who decided it, when and why (from `newest_lead`, the row
    the final label comes from), the panel lenses and the history in time order (tech-lead row and corrections).
    `relabelled`: the final label is a correction; `head_relabel_id`: the newest correction, which the next one must
    supersede. Text included (a Lab text read)."""
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_hash[r["claim_hash"]].append(r)
    rl: dict[str, list[dict]] = defaultdict(list)
    for x in relabels:
        rl[x["claim_hash"]].append(x)
    finals = pn.final_labels(effective_rows(rows, relabels))
    out = []
    for h, rs in by_hash.items():
        f = finals[h]
        panel = sorted((r for r in rs if r["labeller_kind"] == "agent_panel"), key=lambda r: r.get("lens") or "")
        lead = [r for r in rs if r["labeller_kind"] == "tech_lead"]
        depth = chain_depths(rl.get(h, []))
        chain = sorted(rl.get(h, []),
                       key=lambda x: (depth.get(str(x["relabel_id"]), 0), _iso(x.get("created_at")) or ""))
        history = [{"kind": "tech_lead", "label": r["label"], "claim_class": r["claim_class"], "by": r["labeller"],
                    "at": _iso(r.get("created_at")), "reason": r.get("reason"), "id": r.get("label_id"), "_d": 0}
                   for r in lead]
        history += [{"kind": "relabel", "label": x["label"], "claim_class": x["claim_class"], "by": x["labeller"],
                     "at": _iso(x.get("created_at")), "reason": x.get("reason"), "id": str(x["relabel_id"]),
                     "previous_label": x.get("previous_label"), "previous_source": x.get("previous_source"),
                     "_d": depth.get(str(x["relabel_id"]), 0)}
                    for x in chain]
        # the order effective_rows gives the decisions: time, a correction after a stored row, chain order
        history.sort(key=lambda e: (e["at"] is not None, e["at"] or "", e["kind"] == "relabel", e["_d"]))
        for e in history:
            del e["_d"]
        head = chain_head(chain)
        top = newest_lead(rs, chain)
        if top is not None:
            decided = {"by": top.get("labeller"), "at": _iso(top.get("created_at")), "why": top.get("reason")}
        elif f.source == "panel_agreed" and panel:
            decided = {"by": "panel", "at": _iso(panel[0].get("created_at")),
                       "why": "; ".join(f"{r.get('lens')}: {r.get('reason')}" for r in panel if r.get("reason"))[:900]
                       or None}
        else:
            decided = {"by": None, "at": None, "why": None}
        base = panel[0] if panel else rs[0]
        out.append({"claim_hash": h, "anchor_label_id": base.get("label_id"), "writer": base["writer"],
                    "field": base.get("field"), "claim_text": base.get("claim_text"), "split": f.split,
                    "stratum": f.stratum, "sample_id": base.get("sample_id"), "claim_class": f.claim_class,
                    "label": f.label, "source": f.source, "relabelled": bool(top is not None and top.get("relabel")),
                    "head_relabel_id": str(head["relabel_id"]) if head is not None else None,
                    "panel_state": panel[0].get("adjudication") if panel else None,
                    "decided_by": decided["by"], "decided_at": decided["at"], "why": decided["why"],
                    "panel": [{"lens": r.get("lens"), "label": r["label"], "claim_class": r["claim_class"],
                               "reason": r.get("reason")} for r in panel],
                    "history": history})
    return sorted(out, key=lambda e: (e["split"] != "held_out", e["writer"], e["claim_hash"]))


def filter_claims(entries: list[dict], *, source: Optional[str] = None, label: Optional[str] = None,
                  writer: Optional[str] = None, claim_class: Optional[str] = None, split: Optional[str] = None,
                  relabelled: Optional[bool] = None, q: Optional[str] = None) -> list[dict]:
    needle = cl.normalise(q) if q else ""
    out = []
    for e in entries:
        if source and e["source"] != source or label and e["label"] != label or writer and e["writer"] != writer:
            continue
        if claim_class and e["claim_class"] != claim_class or split and e["split"] != split:
            continue
        if relabelled is not None and e["relabelled"] != relabelled:
            continue
        if needle and needle not in cl.normalise(e.get("claim_text") or ""):
            continue
        out.append(e)
    return out


# ── pure: proposals ─────────────────────────────────────────────────────────────────────────────────────────────────


def check_proposal(p: Any) -> Optional[str]:
    """Why a proposal line cannot be imported (None when it can)."""
    if not isinstance(p, dict):
        return "not an object"
    if not p.get("item_id") and not p.get("claim_hash"):
        return "needs item_id or claim_hash"
    if p.get("claim_hash") is not None and not cl.valid_hex64(p.get("claim_hash")):
        return "claim_hash is not a 64-hex sha"
    if p.get("label") not in cl.LABELS:
        return f"label must be one of {list(cl.LABELS)}"
    if not _clean(p.get("reason_en"), REASON_MAX) and not _clean(p.get("reason_vi"), REASON_MAX):
        return "needs reason_en or reason_vi"
    if p.get("confidence") is not None and p["confidence"] not in CONFIDENCE:
        return f"confidence must be one of {list(CONFIDENCE)}"
    notes = p.get("notes")
    if notes is not None and (not isinstance(notes, dict) or set(notes) - set(NOTE_KEYS)
                              or not all(isinstance(v, str) for v in notes.values())):
        return f"notes is an object of strings with keys among {list(NOTE_KEYS)}"
    return None


def proposal_values(p: dict) -> dict:
    notes = {k: _clean(v, NOTE_MAX) for k, v in (p.get("notes") or {}).items()}
    return {"label": p["label"], "reason_en": _clean(p.get("reason_en"), REASON_MAX),
            "reason_vi": _clean(p.get("reason_vi"), REASON_MAX), "bad_part": _clean(p.get("bad_part"), REASON_MAX),
            "confidence": p.get("confidence"), "notes": {k: v for k, v in notes.items() if v}}


# ── pure: context ───────────────────────────────────────────────────────────────────────────────────────────────────


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flatten(v) for v in value)
    return "" if value is None else str(value)


def panel_output(output: Any) -> Any:
    """The output as the panel saw it (scripts/aa729/labels.py panel_output): no gate ledger, no flags."""
    if isinstance(output, dict):
        return {k: v for k, v in output.items() if k not in PANEL_HIDDEN_KEYS}
    return output


def source_text(writer: str, channel: Optional[str], inp: dict) -> str:
    """The full source the panel read (scripts/aa729/labels.py _source_text): S1 / T2 the source tour, T9 the judge's
    source sections of the stored writer input."""
    inp = inp or {}
    src = inp.get("tour") or {}
    if not src and inp.get("context"):
        from services.writing_memory.eval_judge import panel_source_text

        ch = channel or (inp.get("context") or {}).get("channel")
        return panel_source_text("t9_blog" if ch == "blog" else "t9_social", inp)
    return _flatten(src)


def context_from(claim: dict, output: Any, source: Optional[str]) -> dict:
    """The context of one claim in `output` (the object as the panel saw it) with `source` (the full source text)."""
    text = dict(cl.field_texts(output)).get(claim["field"]) or ""
    s, e = int(claim.get("span_start") or 0), int(claim.get("span_end") or 0)
    if text[s:e] != claim.get("claim_text"):
        i = text.find(claim.get("claim_text") or "\x00")
        s, e = (i, i + len(claim["claim_text"])) if i >= 0 else (0, 0)
    src = source or ""
    return {"found": True, "before": text[max(0, s - CONTEXT_CHARS):s].strip() if e else None,
            "after": text[e:e + CONTEXT_CHARS].strip() if e else None,
            "evidence": cl.align(claim.get("claim_text") or "", src), "source_text": src}


def context_of(claim: dict, golden: Optional[dict]) -> dict:
    """The context of one claim from the golden item it was exported from (None: not stored)."""
    if golden is None:
        return {"found": False, "before": None, "after": None, "evidence": [], "source_text": None}
    src = source_text(golden.get("writer") or claim.get("writer"), golden.get("channel"),
                      _json(golden.get("input")) or {})
    return context_from(claim, panel_output(_json(golden.get("control_output"))), src)


def match_golden(claim: dict, candidates: list[dict]) -> Optional[dict]:
    """The golden item whose panel-visible output has the claim's content sha."""
    for g in candidates:
        if cl.content_sha(panel_output(_json(g.get("control_output")))) == claim.get("content_sha256"):
            return g
    return None


# ── DB ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

_CLAIM_ROWS_SQL = (
    "SELECT label_id::text AS label_id, created_at, " + ", ".join(
        f"{c}::text AS {c}" if c in ("tenant_id", "piece_id", "tour_id", "version_id") else c for c in store.COLUMNS)
    + " FROM shared.eval_claim_label WHERE (set_id, set_version, claim_hash) IN "
      "(SELECT * FROM unnest($1::text[], $2::text[], $3::text[])) ORDER BY claim_hash, labeller_kind, lens NULLS LAST")
_RELABELS_SQL = (
    "SELECT relabel_id::text AS relabel_id, created_at, set_id, set_version, claim_hash, anchor_label_id::text AS "
    "anchor_label_id, claim_class, label, reason, labeller, supersedes::text AS supersedes, previous_label, "
    "previous_class, previous_source FROM shared.eval_claim_relabel")


async def read_relabels(conn, set_id: str, set_version: str, claim_hash: Optional[str] = None) -> list[dict]:
    args: list[Any] = [set_id, set_version]
    where = "set_id = $1 AND set_version = $2"
    if claim_hash is not None:
        where += " AND claim_hash = $3"
        args.append(claim_hash)
    return [dict(r) for r in await conn.fetch(f"{_RELABELS_SQL} WHERE {where} ORDER BY created_at, relabel_id", *args)]


async def relabels_for(conn, keys: Iterable[tuple[str, str, str]]) -> list[dict]:
    """The corrections of the claims named by (set_id, set_version, claim_hash)."""
    keys = sorted(set(keys))
    if not keys:
        return []
    a, b, c = (list(x) for x in zip(*keys))
    return [dict(r) for r in await conn.fetch(
        f"{_RELABELS_SQL} WHERE (set_id, set_version, claim_hash) IN "
        "(SELECT * FROM unnest($1::text[], $2::text[], $3::text[])) ORDER BY created_at, relabel_id", a, b, c)]


async def lock_claim_items(conn, set_id: str, set_version: str, claim_hash: str) -> list[dict]:
    """Lock the golden_claim queue items of one claim (FOR UPDATE, item order) inside the caller's transaction: an
    answer and a correction of the same claim then run one after the other, and the second one reads the first."""
    return [dict(r) for r in await conn.fetch(
        "SELECT d.item_id::text AS item_id, d.status FROM shared.writing_memory_disagreement d "
        "JOIN shared.eval_claim_label l ON l.label_id = d.source_ref WHERE d.source = 'golden_claim' "
        "AND l.set_id = $1 AND l.set_version = $2 AND l.claim_hash = $3 ORDER BY d.item_id FOR UPDATE OF d",
        set_id, set_version, claim_hash)]


async def read_relabels_soft(conn, set_id: str, set_version: str) -> list[dict]:
    """read_relabels, or [] before CIS 394 (the report and finals routes keep working on the label rows alone)."""
    import asyncpg

    try:
        async with conn.transaction():
            return await read_relabels(conn, set_id, set_version)
    except asyncpg.UndefinedTableError:
        return []


async def claim_rows(conn, keys: Iterable[tuple[str, str, str]]) -> list[dict]:
    keys = sorted(set(keys))
    if not keys:
        return []
    a, b, c = (list(x) for x in zip(*keys))
    return [dict(r) for r in await conn.fetch(_CLAIM_ROWS_SQL, a, b, c)]


async def golden_candidates(conn, sample_ids: Iterable[str]) -> dict[str, list[dict]]:
    ids = sorted({s for s in sample_ids if s})
    if not ids:
        return {}
    rows = await conn.fetch(
        "SELECT sample_id, writer, channel, input, control_output, tenant_id::text AS tenant_id, trap, refs "
        "FROM shared.eval_golden_item "
        "WHERE sample_id = ANY($1::text[]) AND control_output IS NOT NULL ORDER BY created_at DESC", ids)
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["sample_id"]].append(dict(r))
    return out


# ── the source of a claim: the golden item, else the live T9 piece / T2 version (the S275 pre-filter's order) ─────

LIVE_OUTPUT_KEYS = ("content_text", "seo_title", "meta_description", "slug")    # a T9 piece's object (content sha)
# a T2 version's object: the projection of rewritten_content scripts/aa729/select_samples.freeze_t2 freezes
T2_OUTPUT_KEYS = ("name", "subtitle", "summary", "highlights", "itineraries", "seo_title", "seo_meta", "trip_type")
SOURCE_KINDS = ("golden_sha", "live_sha", "t2_sha", "golden_id", "live_id", "t2_id", "live_wi")
_T2_VERSIONS_SQL = """
    SELECT v.id::text AS version_id, v.tenant_id::text AS tenant_id, v.published_tour_id::text AS published_tour_id,
           v.rewritten_content, v.created_at, v.fs_id::text AS fs_id
      FROM gold_aa_internal.tenant_tour_versions v
     WHERE v.id = ANY($1::uuid[]) AND v.tenant_id = ANY($2::uuid[])
"""
# when the Master text a T2 version was written from last changed: its generated_content row (regenerated or edited)
_MASTER_AT_SQL = """
    SELECT pt.id::text AS published_tour_id, greatest(gc.created_at, gc.edited_at) AS master_at
      FROM gold_aa_internal.published_tours pt
      JOIN silver_aa_internal.generated_content gc ON gc.id = pt.generated_content_id
     WHERE pt.id = ANY($1::uuid[])
"""
_LIVE_PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id, cp.channel, cp.writer_input,
           cp.content_text, cp.seo_title, cp.meta_description, cp.slug, cp.job_id::text AS job_id,
           cp.angle_gate_request_id::text AS request_id, cp.route_hub_name
      FROM acp_shared.content_piece cp
     WHERE cp.piece_id = ANY($1::uuid[]) AND cp.tenant_id = ANY($2::uuid[])
"""


async def live_pieces(conn, piece_ids: Iterable[str], tenant_ids: Iterable[str]) -> dict[str, dict]:
    """piece_id -> the stored T9 piece (writer_input parsed, `out_sha` = the content sha of its object), only pieces
    of the named tenants."""
    pids, tids = sorted({p for p in piece_ids if p}), sorted({t for t in tenant_ids if t})
    if not pids or not tids:
        return {}
    out = {}
    for r in await conn.fetch(_LIVE_PIECES_SQL, pids, tids):
        r = dict(r)
        r["writer_input"] = _json(r.get("writer_input")) or {}
        if not isinstance(r["writer_input"], dict):
            r["writer_input"] = {}
        r["out_sha"] = cl.content_sha({k: r.get(k) for k in LIVE_OUTPUT_KEYS})
        out[r["piece_id"]] = r
    return out


def t2_object(rewritten_content: Any) -> dict:
    """The object of a T2 version as the AA-729 freeze stored it (its content sha is the claims' content_sha256)."""
    rc = _json(rewritten_content)
    rc = rc if isinstance(rc, dict) else {}
    return {k: rc.get(k) for k in T2_OUTPUT_KEYS}


async def t2_versions(conn, version_ids: Iterable[str], tenant_ids: Iterable[str]) -> dict[str, dict]:
    """version_id -> the stored T2 version (`output` = its object, `out_sha` = that object's content sha), only
    versions of the named tenants."""
    vids, tids = sorted({v for v in version_ids if v}), sorted({t for t in tenant_ids if t})
    if not vids or not tids:
        return {}
    out = {}
    for r in await conn.fetch(_T2_VERSIONS_SQL, vids, tids):
        r = dict(r)
        r["output"] = t2_object(r.pop("rewritten_content"))
        r["out_sha"] = cl.content_sha(r["output"])
        out[r["version_id"]] = r
    return out


async def t2_masters(conn, published_tour_ids: Iterable[str]) -> dict[str, dict]:
    """published_tour_id -> the T2 writer's source row (the T2 job's own `_PT_SQL`: the Master text and the raw
    source columns), with `master_at` (when its Master text last changed). One read per Master."""
    ids = sorted({p for p in published_tour_ids if p})
    if not ids:
        return {}
    from services.jobs.t2_rewrite_job import _PT_SQL

    at = {r["published_tour_id"]: r["master_at"] for r in await conn.fetch(_MASTER_AT_SQL, ids)}
    out = {}
    for pid in ids:
        row = await conn.fetchrow(_PT_SQL, pid)
        if row is not None:
            out[pid] = {**dict(row), "master_at": at.get(pid)}
    return out


def t2_source_text(pt: dict) -> str:
    """The full source of a T2 claim: the T2 writer's source dict (`source_tour_dict`, the Master text) flattened as the
    panel read a frozen T2 sample (`source_text` on {"tour": ...}, scripts/aa729/labels._source_text)."""
    from services.jobs.t2_rewrite_job import source_tour_dict

    return source_text("t2", None, {"tour": dict(source_tour_dict(pt))})


def _refs_agree(anchor: dict, golden: dict) -> bool:
    """A golden item matched by sample id names the claim's object: its version id / piece id do not contradict the
    claim's (a round-2 addendum numbers its samples from 1 again, so a sample id alone can name another object)."""
    refs = _json(golden.get("refs")) or {}
    inp = _json(golden.get("input")) or {}
    refs, inp = (refs if isinstance(refs, dict) else {}), (inp if isinstance(inp, dict) else {})
    theirs_by_key = {"version_id": refs.get("version_id"), "piece_id": refs.get("piece_id") or inp.get("piece_id")}
    for key, theirs in theirs_by_key.items():
        mine = anchor.get(key)
        if mine and theirs and str(mine) != str(theirs):
            return False
    return True


async def live_t9_context(pool, piece: dict, wi: dict) -> tuple[dict, str]:
    """The T9 writer context of a stored piece, as the AA-729 freeze built it (scripts/aa729/select_samples.t9_context
    calls this): the t9_write job payload's context when the job row is still kept, else rebuilt from the angle
    request (goal, chosen angle); then the stored `writer_input` (what the writer really got) laid over it, with
    `facts_pending = False` so nothing is re-read. Returns (context, 'job_payload' | 'angle_request'); raises
    ValueError when neither can be rebuilt."""
    import copy
    from uuid import UUID

    from services.acp_angle_gate import service as angle_gate_service
    from services.acp_angle_gate.channel_style import get_channel_style
    from services.acp_angle_gate.goals import get_goal

    ctx: Optional[dict] = None
    source = "job_payload"
    if piece.get("job_id"):
        async with pool.acquire() as conn:
            payload = await conn.fetchval("SELECT payload FROM shared.job WHERE id = $1::uuid", piece["job_id"])
        payload = _json(payload) or {}
        if isinstance(payload, dict) and isinstance(payload.get("context"), dict):
            ctx = copy.deepcopy(payload["context"])
    if ctx is None:
        source = "angle_request"
        req = await angle_gate_service.fetch_request(UUID(piece["tenant_id"]), UUID(piece["request_id"]), pool)
        chosen = next((a for a in req.get("angles") or [] if a.get("chosen")), None)
        goal = get_goal(req.get("goal") or wi.get("goal") or "")
        style = get_channel_style(piece["channel"])
        if chosen is None or goal is None or style is None:
            raise ValueError("cannot rebuild the writer context (no chosen angle / goal / channel style)")
        ctx = {"goal": dict(goal), "channel_style": dict(style), "chosen": dict(chosen),
               "atom_id": req.get("atom_id"), "real_questions": [], "sibling_openers": [],
               "route_hub_name": piece.get("route_hub_name"), "route_segment_count": None, "route_atoms": []}
    moments = wi.get("route_moments") or []
    ctx.update({
        "atom_text": wi.get("seed") or ctx.get("atom_text") or "",
        "route_segments": ([[m.get("atom_id"), m.get("seed")] for m in moments] if moments
                           else ctx.get("route_segments")),
        "facts_text": wi.get("facts") or "", "facts_pending": False,
        "brand_audience": wi.get("brand_pack") or ctx.get("brand_audience") or {},
        "brand_rubric_text": wi.get("brand_rubric") if wi.get("brand_rubric") is not None
        else ctx.get("brand_rubric_text"),
        "channel": piece["channel"], "keyword": wi.get("keyword", ctx.get("keyword")),
        "cta": wi.get("cta") or ctx.get("cta"), "trip_name": wi.get("trip", ctx.get("trip_name")),
        "destination": wi.get("destination", ctx.get("destination")), "scope": wi.get("scope", ctx.get("scope")),
        "trip_places": wi.get("trip_places") or ctx.get("trip_places") or [],
        "tenant_id": piece["tenant_id"],
    })
    if piece["channel"] == "blog" and wi.get("search_questions") is not None:
        ctx["people_also_ask"] = [q.get("q") if isinstance(q, dict) else q for q in wi.get("search_questions") or []]
    ctx.setdefault("people_also_ask", [])
    for k in ("writer_trip_name", "paa_questions", "search_questions", "faq_questions"):
        ctx.pop(k, None)  # derived by run_write_background itself
    return ctx, source


def _t9_panel_source(writer: str, inp: dict) -> str:
    from services.writing_memory.eval_judge import panel_source_text

    return panel_source_text(writer, inp)


class _ConnPool:
    """One connection as a pool (`acquire` yields it): the resolver on a caller's connection."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def one():
            yield self.conn

        return one()


async def resolve_sources(pool, anchors: list[dict]) -> tuple[dict[str, dict], Counter]:
    """claim_hash -> {text: the full source the panel read, kind, output: the object as the panel saw it (the claim's
    context is read in it), t2: the version and Master of a T2 claim (else None)}, and the count of each kind (plus
    `unmatched` and `source_error:<type>`), in the S275 pre-filter's order for each claim object:
      golden_sha  the claim's golden item (by sample id) whose panel-visible output has the claim's content sha;
      live_sha    the live T9 piece (acp_shared.content_piece by piece_id) whose object still has that content sha,
                  its writer context rebuilt (live_t9_context) and the panel's source sections read from it;
      t2_sha      the T2 version (gold_aa_internal.tenant_tour_versions by version_id) whose object still has that
                  content sha; the source is its Master as the T2 writer reads it (t2_source_text);
      golden_id / live_id / t2_id   the same, matched by sample id / piece id / version id only (the text changed
                  since); a golden item by sample id only when its version / piece id does not contradict the claim's;
      live_wi     the piece's writer_input snapshot only (a live_* candidate whose context rebuild failed).
    Never across tenants: a golden item, a piece or a T2 version of another tenant than the claim's is not a
    candidate. A claim with neither a golden item nor its own stored object has no source."""
    t2 = [a for a in anchors if a.get("writer") == "t2" and a.get("version_id")]
    async with pool.acquire() as conn:
        golden = await golden_candidates(conn, (a.get("sample_id") for a in anchors))
        pieces = await live_pieces(conn, (a.get("piece_id") for a in anchors if a.get("writer") != "t2"),
                                   (a.get("tenant_id") for a in anchors))
        versions = await t2_versions(conn, (a.get("version_id") for a in t2), (a.get("tenant_id") for a in t2))
        masters = await t2_masters(conn, (v["published_tour_id"] for v in versions.values()))
    kinds: Counter = Counter()
    by_object: dict[tuple, Optional[dict]] = {}
    live_ctx: dict[str, Any] = {}
    out: dict[str, dict] = {}

    async def live_text(writer: str, p: dict) -> tuple[str, str]:
        if p["piece_id"] not in live_ctx:
            try:
                live_ctx[p["piece_id"]] = (await live_t9_context(pool, p, p["writer_input"]))[0]
            except Exception as exc:  # noqa: BLE001 -- no job payload / angle request: the writer_input snapshot
                live_ctx[p["piece_id"]] = exc
        ctx = live_ctx[p["piece_id"]]
        if isinstance(ctx, Exception):
            return _t9_panel_source(writer, {"writer_input": p["writer_input"], "tenant_id": p["tenant_id"]}), "live_wi"
        return source_text(writer, p.get("channel"), {"context": ctx}), ""

    for a in anchors:
        tenant, writer, sha = a.get("tenant_id"), a.get("writer"), a.get("content_sha256")
        key = (sha, tenant, a.get("sample_id"), a.get("piece_id"), a.get("version_id"), writer)
        if key not in by_object:
            items = [g for g in golden.get(a.get("sample_id") or "", []) if not g.get("trap")]
            p = pieces.get(a.get("piece_id") or "") if writer in ("t9_blog", "t9_social") else None
            if p is not None and p["tenant_id"] != tenant:
                p = None                                                      # never cross tenants
            v = versions.get(a.get("version_id") or "") if writer == "t2" else None
            if v is not None and (v["tenant_id"] != tenant or v["published_tour_id"] not in masters):
                v = None                                                      # never cross tenants; no Master
            cand: list[tuple[str, dict]] = []
            g = match_golden(a, [x for x in items if x.get("tenant_id") in (None, tenant)])
            if g is not None:
                cand.append(("golden_sha", g))
            if p is not None and p["out_sha"] == sha:
                cand.append(("live_sha", p))
            if v is not None and v["out_sha"] == sha:
                cand.append(("t2_sha", v))
            gs = next((x for x in items if x.get("tenant_id") == tenant and isinstance(_json(x.get("input")), dict)
                       and _refs_agree(a, x)), None)
            if gs is not None and gs is not g:
                cand.append(("golden_id", gs))
            if p is not None and p["out_sha"] != sha:
                cand.append(("live_id", p))
            if v is not None and v["out_sha"] != sha:
                cand.append(("t2_id", v))
            found = None
            for kind, src in cand:
                t2_info = None
                try:
                    if kind.startswith("golden"):
                        text = source_text(src.get("writer") or writer, src.get("channel"),
                                           _json(src.get("input")) or {})
                        output = panel_output(_json(src.get("control_output")))
                    elif kind.startswith("t2"):
                        pt = masters[src["published_tour_id"]]
                        text, output = t2_source_text(pt), src["output"]
                        t2_info = {"version": src, "master": pt}
                    else:
                        text, fallback = await live_text(writer, src)
                        kind = fallback or kind
                        output = {k: src.get(k) for k in LIVE_OUTPUT_KEYS}
                except Exception as exc:  # noqa: BLE001 -- a broken stored input: try the next candidate
                    kinds[f"source_error:{type(exc).__name__}"] += 1
                    continue
                if text and text.strip():
                    found = {"text": text, "kind": kind, "output": output, "t2": t2_info}
                    break
            by_object[key] = found
            kinds[found["kind"] if found else "unmatched"] += 1
        if by_object[key] is not None:
            out[a["claim_hash"]] = by_object[key]
    return out, kinds


async def claim_sources(pool, anchors: list[dict]) -> tuple[dict[str, tuple[str, str]], Counter]:
    """claim_hash -> (the full source the panel read, kind), and the count of each kind (`resolve_sources`)."""
    found, kinds = await resolve_sources(pool, anchors)
    return {h: (f["text"], f["kind"]) for h, f in found.items()}, kinds


def _master_changed(t2_info: Optional[dict]) -> Optional[bool]:
    """A T2 source: True when the Master text was regenerated or edited after the version was written (the source
    shown is today's Master, maybe not the one the writer read); None for any other source."""
    if not t2_info:
        return None
    at, written = t2_info["master"].get("master_at"), t2_info["version"].get("created_at")
    return bool(at is not None and written is not None and at > written)


async def raw_facts(conn, t2_info: dict) -> tuple[Optional[str], Optional[str]]:
    """(text, kind) of the raw source facts of a T2 claim's tour: the FactSheet stored with the version (its fs_id,
    the same tenant's row: 'stored'), else the sheet rebuilt from the raw tour row now ('rebuilt'); (None, None)
    when neither exists. Shown beside the source, never part of it: the panel judged against the Master."""
    v, pt = t2_info["version"], t2_info["master"]
    if v.get("fs_id"):
        import asyncpg

        try:
            async with conn.transaction():      # a savepoint: a failed read never aborts the caller's transaction
                text = await conn.fetchval(
                    "SELECT render_text FROM acp_shared.fact_sheet WHERE fs_id = $1::uuid AND tenant_id = $2::uuid",
                    v["fs_id"], v["tenant_id"])
        except asyncpg.PostgresError:           # no table / no grant: the rebuilt sheet below
            text = None
        if text and str(text).strip():
            return str(text), "stored"
    try:
        from services.quality.factsheet import build_tour_factsheet, raw_source_tour

        tour, agency = raw_source_tour(pt)
        if not str(tour.get("name") or "").strip():
            return None, None
        text = build_tour_factsheet(tour, agency=agency).render()
    except Exception:  # noqa: BLE001 -- a raw row the builder cannot read: no facts shown
        return None, None
    return (text, "rebuilt") if text and text.strip() else (None, None)


async def contexts(conn, claims: list[dict], *, pool=None, with_raw_facts: bool = False) -> dict[str, dict]:
    """claim_hash -> the context of each claim (context_from on its resolved source, `resolve_sources`) with
    `source_kind`; a T2 claim also gets `master_changed` and, with `with_raw_facts`, `raw_facts` / `raw_facts_kind`.
    `pool` is the app pool (the live T9 rebuild acquires connections); without one the resolver runs on `conn`."""
    found, _kinds = await resolve_sources(pool or _ConnPool(conn), claims)
    out = {}
    for c in claims:
        f = found.get(c["claim_hash"])
        if f is None:
            out[c["claim_hash"]] = {**context_of(c, None), "source_kind": None}
            continue
        ctx = {**context_from(c, f["output"], f["text"]), "source_kind": f["kind"]}
        if f["t2"] is not None:
            ctx["master_changed"] = _master_changed(f["t2"])
            if with_raw_facts:
                ctx["raw_facts"], ctx["raw_facts_kind"] = await raw_facts(conn, f["t2"])
        out[c["claim_hash"]] = ctx
    return out


_ITEM_COLS = """
    d.item_id::text AS item_id, d.status, d.resolution, d.note, d.resolved_by, d.resolved_at, d.created_at,
    d.machine_verdict, d.human_label, d.writer, d.scope, d.tenant_id::text AS tenant_id,
    l.label_id::text AS anchor_label_id, l.set_id, l.set_version, l.claim_hash, l.claim_text, l.field, l.span_start,
    l.span_end, l.split, l.stratum, l.sample_id, l.content_sha256,
    p.proposal_id::text AS proposal_id, p.label AS proposal_label, p.reason_en, p.reason_vi, p.bad_part,
    p.confidence, p.run_id, p.notes, p.created_at AS proposal_at"""
_ITEM_FROM = """
      FROM shared.writing_memory_disagreement d
      JOIN shared.eval_claim_label l ON l.label_id = d.source_ref
      LEFT JOIN LATERAL (SELECT * FROM shared.eval_claim_proposal x WHERE x.item_id = d.item_id
                          ORDER BY x.created_at DESC, x.proposal_id DESC LIMIT 1) p ON true
     WHERE d.source = 'golden_claim'"""
_ITEM_ORDER = ("ORDER BY (l.split <> 'held_out'), array_position(ARRAY['t9_blog', 't9_social', 't2', 'a1'], d.writer), "
               "l.content_sha256, l.span_start, d.item_id")


def _item_filters(*, set_id: Optional[str], set_version: Optional[str], writer: Optional[str], state: str,
                  proposal: Optional[str], item_ids: Optional[list[str]] = None) -> tuple[str, list]:
    where, args = [], []

    def add(sql: str, value: Any) -> None:
        args.append(value)
        where.append(sql.format(f"${len(args)}"))

    if set_id:
        add("l.set_id = {}", set_id)
    if set_version:
        add("l.set_version = {}", set_version)
    if writer:
        add("d.writer = {}", writer)
    if state in ("open", "resolved"):
        add("d.status = {}", state)
    if proposal == "none":
        where.append("p.proposal_id IS NULL")
    elif proposal:
        add("p.label = {}", proposal)
    if item_ids is not None:
        add("d.item_id = ANY({}::uuid[])", item_ids)
    return "".join(f" AND {w}" for w in where), args


def _item(r: dict) -> dict:
    out = {k: _iso(v) for k, v in r.items() if not k.startswith("proposal") and k not in (
        "reason_en", "reason_vi", "bad_part", "confidence", "run_id", "notes", "total")}
    out["proposal"] = None if r.get("proposal_id") is None else {
        "proposal_id": r["proposal_id"], "label": r["proposal_label"], "reason_en": r.get("reason_en"),
        "reason_vi": r.get("reason_vi"), "bad_part": r.get("bad_part"), "confidence": r.get("confidence"),
        "run_id": r.get("run_id"), "notes": _json(r.get("notes")) or {}, "created_at": _iso(r.get("proposal_at"))}
    return out


async def list_items(conn, *, set_id: Optional[str] = None, set_version: Optional[str] = None,
                     writer: Optional[str] = None, state: str = "open", proposal: Optional[str] = None,
                     limit: int = 25, offset: int = 0, with_context: bool = True, pool=None) -> dict:
    """One page of golden_claim items in adjudication order, each with its claim, panel lenses, newest proposal, the
    tech-lead decision (once answered) and its context (no full source: GET .../context has it); the total and the
    facets of the whole queue (counts per set version, state, writer and proposal) for the filters."""
    where, args = _item_filters(set_id=set_id, set_version=set_version, writer=writer, state=state, proposal=proposal)
    n = len(args)
    rows = [dict(r) for r in await conn.fetch(
        f"SELECT {_ITEM_COLS}, count(*) OVER () AS total {_ITEM_FROM}{where} {_ITEM_ORDER} "
        f"LIMIT ${n + 1} OFFSET ${n + 2}", *args, int(limit), int(offset))]
    total = int(rows[0]["total"]) if rows else 0
    facets = [dict(r) for r in await conn.fetch(
        f"SELECT l.set_id, l.set_version, d.status, d.writer, coalesce(p.label, 'none') AS proposal, count(*) AS n "
        f"{_ITEM_FROM} GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5")]
    items = [_item(r) for r in rows]
    await attach_claims(conn, items, with_context=with_context, pool=pool)
    return {"items": items, "total": total, "limit": int(limit), "offset": int(offset),
            "facets": [{**f, "n": int(f["n"])} for f in facets]}


async def attach_claims(conn, items: list[dict], *, with_context: bool, pool=None) -> None:
    keys = [(i["set_id"], i["set_version"], i["claim_hash"]) for i in items]
    rows = await claim_rows(conn, keys)
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r["set_id"], r["set_version"], r["claim_hash"])].append(r)
    fixes: dict[tuple, list[dict]] = defaultdict(list)
    for x in await relabels_for(conn, keys):
        fixes[(x["set_id"], x["set_version"], x["claim_hash"])].append(x)
    # the context is read from the claim's own label row (its tenant, piece and version ids name its object)
    claims = [{**i, **(sorted(by[k], key=lambda r: (r["labeller_kind"] != "agent_panel", r.get("lens") or ""))[0]
                       if by.get(k) else {})}
              for i in items for k in [(i["set_id"], i["set_version"], i["claim_hash"])]]
    ctx = await contexts(conn, claims, pool=pool) if with_context else {}
    for i in items:
        key = (i["set_id"], i["set_version"], i["claim_hash"])
        rs = by.get(key, [])
        i["panel"] = [{"lens": r.get("lens"), "label": r["label"], "claim_class": r["claim_class"],
                       "reason": r.get("reason"), "labeller": r["labeller"], "adjudication": r.get("adjudication")}
                      for r in sorted((r for r in rs if r["labeller_kind"] == "agent_panel"),
                                      key=lambda r: r.get("lens") or "")]
        top = newest_lead(rs, fixes.get(key, []))
        i["decision"] = None if top is None else {"label": top["label"], "claim_class": top["claim_class"],
                                                  "by": top.get("labeller"), "at": _iso(top.get("created_at")),
                                                  "reason": top.get("reason"), "relabel": bool(top.get("relabel"))}
        if with_context:
            c = ctx.get(i["claim_hash"]) or {}
            i["context"] = {k: c.get(k) for k in ("found", "before", "after", "evidence")}


async def item_context(conn, item_id: str, *, pool=None) -> dict:
    """The full context of one golden_claim item: the sentence's context, evidence lines and full source, the source
    kind, and for a T2 claim whether its Master changed since and the tour's raw source facts."""
    row = await conn.fetchrow(
        "SELECT l.claim_hash, l.claim_text, l.field, l.span_start, l.span_end, l.sample_id, l.content_sha256, "
        "l.writer, l.tenant_id::text AS tenant_id, l.piece_id::text AS piece_id, l.version_id::text AS version_id "
        "FROM shared.writing_memory_disagreement d "
        "JOIN shared.eval_claim_label l ON l.label_id = d.source_ref "
        "WHERE d.item_id = $1::uuid AND d.source = 'golden_claim'", str(item_id))
    if row is None:
        raise ReviewError(404, "not_found", "Unknown claim item")
    claim = dict(row)
    ctx = (await contexts(conn, [claim], pool=pool, with_raw_facts=True))[claim["claim_hash"]]
    return {"item_id": str(item_id), **ctx}


async def answer_item(conn, item_id: str, *, answer: str, actor: str, note: Optional[str] = None,
                      proposal_id: Optional[str] = None) -> dict:
    """Resolve one open golden_claim item and write its tech-lead row, in one transaction (module docstring)."""
    from services.acp_shared.audit_log import write_audit_log
    from services.writing_memory import disagreements as dis

    if answer not in ANSWERS:
        raise ReviewError(422, "invalid", f"answer must be one of {list(ANSWERS)}")
    async with conn.transaction():
        item = await conn.fetchrow(
            "SELECT d.item_id::text AS item_id, d.status, d.machine_verdict, d.tenant_id::text AS tenant_id, "
            "l.set_id, l.set_version, l.claim_hash FROM shared.writing_memory_disagreement d "
            "JOIN shared.eval_claim_label l ON l.label_id = d.source_ref "
            "WHERE d.item_id = $1::uuid AND d.source = 'golden_claim' FOR UPDATE OF d", str(item_id))
        if item is None:
            raise ReviewError(404, "not_found", "Unknown claim item")
        if item["status"] != "open":
            raise ReviewError(409, "resolved", "the item is already resolved")
        await lock_claim_items(conn, item["set_id"], item["set_version"], item["claim_hash"])
        rows = await claim_rows(conn, [(item["set_id"], item["set_version"], item["claim_hash"])])
        panel = sorted((r for r in rows if r["labeller_kind"] == "agent_panel"), key=lambda r: r.get("lens") or "")
        if not panel:
            raise ReviewError(409, "no_panel", "the claim has no panel rows")
        label, resolution = answer_plan(item["machine_verdict"], answer)
        top = newest_lead(rows, await read_relabels(conn, item["set_id"], item["set_version"], item["claim_hash"]))
        if top is not None and top["label"] != label:
            if top.get("relabel"):
                raise ReviewError(409, "relabelled", f"the claim was corrected to {top['label']} in the label set "
                                                     "browser: answer that here, or change it there")
            raise ReviewError(409, "labelled", "the claim already has another tech-lead label: change it from the "
                                               "label set browser")
        proposal = None
        if proposal_id:
            proposal = await conn.fetchrow(
                "SELECT label, run_id FROM shared.eval_claim_proposal "
                "WHERE proposal_id = $1::uuid AND item_id = $2::uuid",
                str(proposal_id), str(item_id))
        how = "" if proposal is None else (
            f"; proposal {proposal['label']} ({proposal['run_id']}) "
            + ("accepted" if proposal["label"] == ANSWERS[answer] else "overridden"))
        reason = f"review: {answer}{how}" + (f"; {note}" if _clean(note, 400) else "")
        inserted = 0
        if top is None:     # an agreeing decision (stored row or correction) stays the decision: no new row
            row = lead_row(panel[0], label=label, claim_class=pick_class(panel, label, panel[0]), labeller=actor,
                           reason=reason, policy_sha=policy.policy_sha())
            if not policy.known_class(row["claim_class"]):
                raise ReviewError(422, "bad_class", f"{row['claim_class']!r} is not a policy class")
            inserted = (await store.insert_rows(conn, [row], apply=True))["inserted"]
        resolved = await dis.resolve(conn, str(item_id), resolution=resolution, actor=actor,
                                     note=_clean(f"{answer}{how}" + (f"; {note}" if note else ""), 500))
        await write_audit_log(conn, tenant_id=item["tenant_id"] or PLATFORM_TENANT_ID, actor=actor[:128],
                              action=AUDIT_ANSWER, resource_type="writing_memory_disagreement",
                              resource_id=str(item_id),
                              details={"set": f"{item['set_id']}@{item['set_version']}", "answer": answer,
                                       "label": label, "resolution": resolution, "inserted": inserted,
                                       "proposal_id": proposal_id if proposal is not None else None})
    return {"item_id": str(item_id), "answer": answer, "label": label, "resolution": resolution,
            "inserted": inserted, "status": resolved["status"]}


async def accept_proposals(conn, item_ids: list[str], *, actor: str) -> dict:
    """Answer each open item with its newest proposal's label (one transaction per item: a refusal stops nothing
    else). Items without a proposal or already resolved are counted, never answered."""
    if len(item_ids) > MAX_BULK:
        raise ReviewError(422, "too_many", f"at most {MAX_BULK} items per call")
    rows = {r["item_id"]: dict(r) for r in await conn.fetch(
        "SELECT d.item_id::text AS item_id, d.status, p.proposal_id::text AS proposal_id, p.label "
        "FROM shared.writing_memory_disagreement d LEFT JOIN LATERAL (SELECT proposal_id, label FROM "
        "shared.eval_claim_proposal x WHERE x.item_id = d.item_id ORDER BY x.created_at DESC, x.proposal_id DESC "
        "LIMIT 1) p ON true WHERE d.item_id = ANY($1::uuid[]) AND d.source = 'golden_claim'", list(item_ids))}
    out = {"accepted": 0, "no_proposal": 0, "not_open": 0, "unknown": 0, "refused": [], "labels": {}}
    for iid in item_ids:
        r = rows.get(iid)
        if r is None:
            out["unknown"] += 1
        elif r["status"] != "open":
            out["not_open"] += 1
        elif not r.get("proposal_id"):
            out["no_proposal"] += 1
        else:
            try:
                res = await answer_item(conn, iid, answer=LABEL_ANSWER[r["label"]], actor=actor,
                                        proposal_id=r["proposal_id"])
            except ReviewError as e:
                out["refused"].append({"item_id": iid, "code": e.code})
                continue
            out["accepted"] += 1
            out["labels"][res["label"]] = out["labels"].get(res["label"], 0) + 1
    return out


async def import_proposals(conn, *, run_id: str, proposals: list[dict], apply: bool, actor: str,
                           set_id: Optional[str] = None, set_version: Optional[str] = None) -> dict:
    """Validate and (apply) store agent proposals. A line names its queue item by item_id, or by claim_hash within
    the body's set version. Any bad line refuses the whole file (nothing half applied); a line whose claim has no
    golden_claim item is counted `not_queued`; a proposal of the same run already stored is `unchanged`."""
    if not RUN_ID_RE.match(run_id or ""):
        raise ReviewError(422, "bad_payload", "run_id is not a run id")
    if len(proposals) > MAX_PROPOSALS:
        raise ReviewError(422, "bad_payload", f"at most {MAX_PROPOSALS} proposals per import")
    problems = [f"#{i}: {why}" for i, p in enumerate(proposals, 1) for why in [check_proposal(p)] if why]
    if any(p.get("claim_hash") and not p.get("item_id") for p in proposals if isinstance(p, dict)) \
            and not (set_id and set_version):
        problems.append("a line named by claim_hash needs set_id and set_version in the body")
    if problems:
        raise ReviewError(422, "bad_payload", "; ".join(problems[:20]) + (
            f" (+{len(problems) - 20} more)" if len(problems) > 20 else ""))
    items = await conn.fetch(
        "SELECT d.item_id::text AS item_id, l.claim_hash, l.set_id, l.set_version FROM "
        "shared.writing_memory_disagreement d JOIN shared.eval_claim_label l ON l.label_id = d.source_ref "
        "WHERE d.source = 'golden_claim' AND (d.item_id::text = ANY($1::text[]) OR "
        "(l.set_id = $2 AND l.set_version = $3 AND l.claim_hash = ANY($4::text[])))",
        [str(p["item_id"]) for p in proposals if p.get("item_id")], set_id or "", set_version or "",
        [p["claim_hash"] for p in proposals if p.get("claim_hash") and not p.get("item_id")])
    by_item = {r["item_id"]: r["item_id"] for r in items}
    by_hash = {r["claim_hash"]: r["item_id"] for r in items
               if r["set_id"] == set_id and r["set_version"] == set_version}
    stored = {r["item_id"] for r in await conn.fetch(
        "SELECT item_id::text AS item_id FROM shared.eval_claim_proposal WHERE run_id = $1", run_id)}
    plan, not_queued, unchanged, seen = [], 0, 0, set()
    for p in proposals:
        iid = by_item.get(str(p.get("item_id") or "")) if p.get("item_id") else by_hash.get(p["claim_hash"])
        if iid is None:
            not_queued += 1
            continue
        if iid in stored or iid in seen:
            unchanged += 1
            continue
        seen.add(iid)
        plan.append((iid, proposal_values(p)))
    out = {"run_id": run_id, "dry_run": not apply, "lines": len(proposals), "new": len(plan), "unchanged": unchanged,
           "not_queued": not_queued, "inserted": 0, "labels": dict(Counter(v["label"] for _, v in plan))}
    if not apply or not plan:
        return out
    from services.acp_shared.audit_log import write_audit_log

    async with conn.transaction():
        for iid, v in plan:
            got = await conn.fetchval(
                "INSERT INTO shared.eval_claim_proposal (item_id, run_id, label, reason_en, reason_vi, bad_part, "
                "confidence, notes, imported_by) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9) "
                "ON CONFLICT ON CONSTRAINT eval_claim_proposal_run_key DO NOTHING RETURNING proposal_id::text",
                iid, run_id, v["label"], v["reason_en"], v["reason_vi"], v["bad_part"], v["confidence"],
                json.dumps(v["notes"], ensure_ascii=False), actor[:128])
            out["inserted"] += bool(got)
        await write_audit_log(conn, tenant_id=None, actor=actor[:128], action=AUDIT_PROPOSALS,
                              resource_type="eval_claim_proposal", resource_id=run_id[:64],
                              details={k: out[k] for k in ("lines", "new", "unchanged", "not_queued", "inserted")})
    return out


async def list_sets(conn) -> list[dict]:
    """Every label set version with its counts (set_summary) and its queue items per state."""
    sets = [(r["set_id"], r["set_version"]) for r in await conn.fetch(
        "SELECT DISTINCT set_id, set_version FROM shared.eval_claim_label ORDER BY set_id, set_version LIMIT 200")]
    queue = {(r["set_id"], r["set_version"], r["status"]): int(r["n"]) for r in await conn.fetch(
        "SELECT l.set_id, l.set_version, d.status, count(*) AS n FROM shared.writing_memory_disagreement d "
        "JOIN shared.eval_claim_label l ON l.label_id = d.source_ref WHERE d.source = 'golden_claim' "
        "GROUP BY 1, 2, 3")}
    out = []
    for set_id, version in sets:
        rows = await store.read_rows(conn, set_id, version)
        relabels = await read_relabels_soft(conn, set_id, version)
        out.append({"set_id": set_id, "set_version": version, **set_summary(rows, relabels),
                    "queue": {s: queue.get((set_id, version, s), 0) for s in ("open", "resolved")}})
    return out


async def relabel(conn, set_id: str, set_version: str, claim_hash: str, *, label: str, claim_class: Optional[str],
                  reason: str, supersedes: Optional[str], actor: str) -> dict:
    """Change the final label of one claim (append only, module docstring). `supersedes` must name the current head
    correction (None when there is none): a stale page is refused (409)."""
    from services.acp_shared.audit_log import write_audit_log
    import asyncpg

    if label not in cl.LABELS:
        raise ReviewError(422, "invalid", f"label must be one of {list(cl.LABELS)}")
    why = _clean(reason, REASON_MAX)
    if not why or len(why) < 3:
        raise ReviewError(422, "invalid", "a reason of at least 3 characters is required")
    async with conn.transaction():
        await lock_claim_items(conn, set_id, set_version, claim_hash)
        rows = [r for r in await claim_rows(conn, [(set_id, set_version, claim_hash)])]
        if not rows:
            raise ReviewError(404, "not_found", "Unknown claim")
        chain = await read_relabels(conn, set_id, set_version, claim_hash)
        head = chain_head(chain)
        if (head["relabel_id"] if head else None) != (str(supersedes) if supersedes else None):
            raise ReviewError(409, "stale", "the label changed since the page was loaded: reload and try again")
        final = pn.final_labels(effective_rows(rows, chain))[claim_hash]
        if final.source == "none":
            raise ReviewError(409, "no_final", "the claim has no final label yet: decide it in the review queue")
        cls = claim_class or final.claim_class
        if not policy.known_class(cls):
            raise ReviewError(422, "bad_class", f"{cls!r} is not a policy class")
        if cls == final.claim_class and label == final.label:
            raise ReviewError(422, "unchanged", "the claim already has this label")
        anchor = sorted(rows, key=lambda r: (r["labeller_kind"] != "agent_panel", r.get("lens") or ""))[0]
        top = newest_lead(rows, chain)      # the row the current final label comes from
        # a machine-rule label (source prefilter) is a stored tech-lead row: the column's CHECK names the row kind
        previous_source = ("relabel" if top is not None and top.get("relabel")
                           else "tech_lead" if final.source == "prefilter" else final.source)
        try:
            rid = await conn.fetchval(
                "INSERT INTO shared.eval_claim_relabel (set_id, set_version, claim_hash, anchor_label_id, claim_class, "
                "label, reason, labeller, supersedes, previous_label, previous_class, previous_source) VALUES "
                "($1, $2, $3, $4::uuid, $5, $6, $7, $8, $9::uuid, $10, $11, $12) RETURNING relabel_id::text",
                set_id, set_version, claim_hash, anchor["label_id"], cls, label, why, actor[:128],
                str(supersedes) if supersedes else None, final.label, final.claim_class or "", previous_source)
        except asyncpg.UniqueViolationError:
            raise ReviewError(409, "stale", "the label changed since the page was loaded: reload and try again")
        await write_audit_log(conn, tenant_id=anchor.get("tenant_id") or PLATFORM_TENANT_ID, actor=actor[:128],
                              action=AUDIT_RELABEL, resource_type="eval_claim_relabel", resource_id=rid,
                              details={"set": f"{set_id}@{set_version}", "claim_hash": claim_hash,
                                       "from": final.label, "to": label, "class": cls,
                                       "previous_source": previous_source})
    return {"relabel_id": rid, "claim_hash": claim_hash, "label": label, "claim_class": cls,
            "previous_label": final.label, "previous_source": previous_source}


__all__ = ["ANSWERS", "CONFIDENCE", "MAX_BULK", "NOTE_KEYS", "PROPOSAL_FILTERS", "ReviewError", "SOURCES", "STATES",
           "accept_proposals", "answer_item", "answer_plan", "chain_depths", "chain_head", "check_proposal",
           "claim_entries",
           "context_of", "effective_rows", "filter_claims", "import_proposals", "item_context", "lead_row",
           "list_items", "list_sets", "lock_claim_items", "match_golden", "newest_lead", "pick_class", "read_relabels",
           "read_relabels_soft", "relabel", "relabels_for", "resolve_sources", "context_from", "raw_facts",
           "set_summary", "source_text", "t2_masters", "t2_object", "t2_source_text", "t2_versions"]
