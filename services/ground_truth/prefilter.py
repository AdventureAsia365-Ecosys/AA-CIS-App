"""AA-1063 — the Jev pre-filter of the claim adjudication (rule "jev090+panel", measured in S275).

A disputed claim of a label set version waits for the tech lead. The pre-filter asks Jev the existing
`a1_claim_supported` question (the T3 / A1 Noul question: is this sentence supported by this source?) and settles the
claim as supported, without the tech lead, only when ALL of these hold (`rule`):
  1. Jev's probability p >= 0.90;
  2. the panel's majority label is ok (a strict majority of the 3 lenses, as the queue's machine_verdict);
  3. no lens that scored the claim NOT ok (violation or unsure) gives the class fact.misbinding.
The S275 back-test on the held-out CL-1 labels: 341/341 right. Rule 3 reads the non-ok lenses only: an ok lens also
names a class, and flagging fact.misbinding on every lens kept 94 claims back for nothing (S275 lesson).

What it writes (enforce only): one tech-lead row of shared.eval_claim_label with label ok, labeller
'prefilter:jev090+panel' (panel.PREFILTER_PREFIX) and a reason that names the run id and p; the claim's open
golden_claim queue items are resolved judge_right in the same transaction, so the item leaves the tech lead's queue.
The final label of such a claim has source 'prefilter' (panel.final_labels): it is never release evidence
(panel.release_evidence) and never an audit adjudication (panel.agreement_report), so it stays out of the R1 decision
record (critic F14). The reject side does nothing (S275: no measured reject zone).

Which claims. The adjudication candidates of the version (store.queue_candidates: disputed or audit-drawn, no
tech-lead row, no correction), queued or not, minus:
  - audit-drawn claims: the audit draw measures the panel's error rate; a machine answer there measures the panel
    against itself;
  - claims whose queue item is already resolved (their decision is on its way through pull-resolutions);
  - tenant claims whose tenant may not reach Jev: tenant content goes to TypeSafe only for a tenant on
    shared.jev_tenant_allowlist that is a demo tenant, not a pilot and not offboarded (decide() checks the
    allow-list again);
  - claims with no source: the source is the bytes the panel read, rebuilt in the S275 order (review.claim_sources):
    the golden item with the claim's content sha, then the live T9 piece (acp_shared.content_piece) whose object
    still has that sha (its writer context rebuilt as the AA-729 freeze did, review.live_t9_context), then the golden
    item / live piece by sample id / piece id only; never across tenants. In the S275 run every eligible CL-1 1.1.0
    claim took the live path, so its answers are read back only through it.

Switch. CLAIM_PREFILTER_MODE (environment of the api service): 'shadow' (default, also any other value) or
'enforce'. In shadow the pre-filter asks Jev and reports what it would do; it never writes a label. A write needs
the switch on enforce AND an explicit apply, and only for the writers the rule was measured on (ENFORCE_WRITERS, the
T9 surface); any other writer stays shadow.

Cost. A call asks at most `limit` claims (MAX_ASK) and stops starting calls once the estimated spend passes
`max_usd` (MAX_USD_CAP). A claim already answered under this stage, question and subject key (shared.decision_log) is
read back, never asked again (the S275 subject key, so its answers count). Every ask goes through
shared.llm_client.decide.decide() on stage adhoc_aa1063: one shared.llm_call_log row per call (provider typesafe,
written by decide with the call's billed fields) and one shared.decision_log row per verdict.

No text in the answer: hashes, labels, probabilities, counts and dollars only.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Optional

from services.ground_truth import panel as pn
from services.ground_truth import policy
from services.ground_truth import review as rv
from services.ground_truth import store

RULE = "jev090+panel"
LABELLER = pn.PREFILTER_PREFIX + RULE
QUESTION = "a1_claim_supported"
STAGE = "adhoc_aa1063"
ACCEPT_P = 0.90
MISBINDING = "fact.misbinding"
MODE_ENV = "CLAIM_PREFILTER_MODE"
MODES = ("shadow", "enforce")
# the writers whose claims the rule was measured on (S275: held-out CL-1, the T9 surface); others stay shadow
ENFORCE_WRITERS = ("t9_blog", "t9_social")
SOURCE_VIEW = "panel"
DEFAULT_ASK = 50
MAX_ASK = 100
CONCURRENCY = 8
DEFAULT_MAX_USD = 0.05
MAX_USD_CAP = 0.50
MAX_CONSEC_ERRORS = 10
AUDIT_ACTION = "ground_truth.claim_prefilter"

TENANT_SQL = """
    SELECT t.tenant_id::text AS tenant_id, t.kind, t.is_pilot, (t.cancelled_at IS NOT NULL) AS offboarded,
           (a.tenant_id IS NOT NULL) AS listed
      FROM shared.tenants t
      LEFT JOIN shared.jev_tenant_allowlist a ON a.tenant_id = t.tenant_id
     WHERE t.tenant_id = ANY($1::uuid[])
"""
DONE_SQL = """
    SELECT DISTINCT ON (subject_key) subject_key, probability::float AS p
      FROM shared.decision_log
     WHERE stage = $1 AND question_key = $2 AND subject_key = ANY($3::text[])
       AND zone IN ('accept', 'grey', 'reject') AND NOT cached AND probability IS NOT NULL
     ORDER BY subject_key, created_at DESC
"""
QUEUED_SQL = """
    SELECT item_id::text AS item_id, source_ref::text AS source_ref, status
      FROM shared.writing_memory_disagreement
     WHERE source = 'golden_claim' AND source_ref = ANY($1::uuid[])
"""


class PrefilterError(Exception):
    """A refused pre-filter call: HTTP status, a short code and a sentence for the page."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, code, detail)
        self.status, self.code, self.detail = status, code, detail


# ── pure: the switch and the rule ───────────────────────────────────────────────────────────────────────────────────


def switch_mode(env: Optional[Mapping[str, str]] = None) -> str:
    """The pre-filter switch: 'enforce' only when CLAIM_PREFILTER_MODE says so exactly; anything else is 'shadow'."""
    value = str((os.environ if env is None else env).get(MODE_ENV) or "").strip().lower()
    return "enforce" if value == "enforce" else "shadow"


def panel_majority(panel_rows: Iterable[dict]) -> str:
    """The lenses' common label (a strict majority), else 'split' (store.queue_candidates' machine_verdict)."""
    top = Counter(r["label"] for r in panel_rows).most_common()
    if not top:
        return "split"
    return top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else "split"


def non_ok_misbinding(panel_rows: Iterable[dict]) -> bool:
    """True when a lens that scored the claim NOT ok gives the class fact.misbinding. An ok lens naming the class does
    not count (S275: counting every lens kept 94 claims back for nothing)."""
    return any(r.get("label") != "ok" and r.get("claim_class") == MISBINDING for r in panel_rows)


def rule(p: Optional[float], panel_rows: list[dict]) -> tuple[str, str]:
    """('auto_ok', 'rule_met') when the claim settles as supported, else ('review', why): no_verdict, majority_<label>,
    misbinding_on_non_ok_lens or p_below_floor (the first condition that fails, in that order)."""
    if p is None:
        return "review", "no_verdict"
    majority = panel_majority(panel_rows)
    if majority != "ok":
        return "review", f"majority_{majority}"
    if non_ok_misbinding(panel_rows):
        return "review", "misbinding_on_non_ok_lens"
    if float(p) < ACCEPT_P:
        return "review", "p_below_floor"
    return "auto_ok", "rule_met"


def subject_key(claim_hash: str, source: str) -> str:
    """The decision_log subject of one ask (the S275 shape, so its answers are read back): claim and source digest."""
    digest = hashlib.sha256((source or "").encode("utf-8")).hexdigest()[:10]
    return f"aa1063:{SOURCE_VIEW}:{claim_hash[:24]}:{digest}"


def tenant_gate(scope: Optional[str], tenant_id: Optional[str], state: Optional[dict]) -> Optional[str]:
    """Why a claim's text may not go to Jev (None: it may). Platform content always may; tenant content only for an
    allow-listed demo tenant that is not a pilot and not offboarded."""
    if scope == "platform" and not tenant_id:
        return None
    if not tenant_id or state is None:
        return "tenant_unknown"
    if state.get("kind") != "demo":
        return "tenant_not_demo"
    if state.get("is_pilot"):
        return "tenant_pilot"
    if state.get("offboarded"):
        return "tenant_offboarded"
    if not state.get("listed"):
        return "tenant_not_allowlisted"
    return None


def reason_text(run_id: str, p: float) -> str:
    return (f"pre-filter {RULE} (run {run_id}): Jev {QUESTION} p={float(p):.2f} >= {ACCEPT_P:.2f}, panel majority ok, "
            f"no non-ok {MISBINDING} lens")[:pn.REASON_MAX]


def estimate_tokens(source: str, sentence: str) -> int:
    return (len(source or "") + len(sentence or "")) // 4 + 60


# ── the run ─────────────────────────────────────────────────────────────────────────────────────────────────────────


async def _sources(pool, anchors: list[dict]) -> tuple[dict[str, str], dict[str, int]]:
    """claim_hash -> the full source the panel read, and the count of each source kind (review.claim_sources: the
    golden item by content sha, then the live T9 piece by content sha, then by sample id / piece id; absent: none)."""
    found, kinds = await rv.claim_sources(pool, anchors)
    return {h: text for h, (text, _kind) in found.items()}, dict(sorted(kinds.items()))


async def _ask(pool, todo: list[dict], *, limit: int, max_usd: float, price_in: float) -> dict:
    """Ask Jev about each claim of `todo` (in order) until `limit` asks or the estimated `max_usd`; stops after
    MAX_CONSEC_ERRORS answers in a row without a probability (switch off, breaker, outage)."""
    from shared.llm_client.decide import decide

    sem = asyncio.Semaphore(CONCURRENCY)
    st: dict[str, Any] = {"asked": 0, "answered": 0, "errors": 0, "consec": 0, "est_usd": 0.0, "usd": 0.0,
                          "stopped": None, "error_kinds": Counter(), "probs": {}}

    async def one(e: dict) -> None:
        async with sem:
            if st["stopped"]:
                return
            if st["asked"] >= limit:
                st["stopped"] = f"limit {limit}"
                return
            if st["est_usd"] >= max_usd:
                st["stopped"] = f"max_usd {max_usd}"
                return
            st["asked"] += 1
            st["est_usd"] += e["est_tokens"] * price_in / 1_000_000
            d = await decide(STAGE, e["subject"], {"source": e["source"], "sentence": e["sentence"]}, [QUESTION],
                             tenant_id=e["tenant_id"], pool=pool, use_cache=False)
            st["usd"] += float(getattr(d, "cost_usd", 0.0) or 0.0)
            v = d.verdicts.get(QUESTION)
            if v is not None and v.zone in ("accept", "grey", "reject") and v.probability is not None:
                st["probs"][e["claim_hash"]] = float(v.probability)
                st["answered"] += 1
                st["consec"] = 0
                return
            st["errors"] += 1
            st["consec"] += 1
            st["error_kinds"][f"{v.zone if v else 'none'}:{str((v.error if v else '') or '')[:60]}"] += 1
            if st["consec"] >= MAX_CONSEC_ERRORS:
                st["stopped"] = f"{MAX_CONSEC_ERRORS} answers in a row without a verdict"

    await asyncio.gather(*(one(e) for e in todo))
    return st


async def _apply(pool, entries: list[dict], *, run_id: str) -> dict:
    """Write the label of each auto_ok entry and resolve its open queue items (one transaction per claim; a claim
    decided meanwhile is skipped)."""
    from services.writing_memory import disagreements as dis

    out = {"written": 0, "resolved_items": 0, "decided_meanwhile": 0}
    sha = policy.policy_sha()
    async with pool.acquire() as conn:
        for e in entries:
            async with conn.transaction():
                items = await rv.lock_claim_items(conn, e["set_id"], e["set_version"], e["claim_hash"])
                rows = await rv.claim_rows(conn, [(e["set_id"], e["set_version"], e["claim_hash"])])
                fixes = await rv.read_relabels(conn, e["set_id"], e["set_version"], e["claim_hash"])
                if fixes or any(r["labeller_kind"] == "tech_lead" for r in rows) or any(
                        i["status"] != "open" for i in items):
                    out["decided_meanwhile"] += 1
                    continue
                panel = sorted((r for r in rows if r["labeller_kind"] == "agent_panel"),
                               key=lambda r: r.get("lens") or "")
                if not panel or rule(e["p"], panel)[0] != "auto_ok":
                    out["decided_meanwhile"] += 1
                    continue
                row = rv.lead_row(panel[0], label="ok", claim_class=rv.pick_class(panel, "ok", panel[0]),
                                  labeller=LABELLER, reason=reason_text(run_id, e["p"]), policy_sha=sha)
                out["written"] += (await store.insert_rows(conn, [row], apply=True))["inserted"]
                for i in items:
                    await dis.resolve(conn, i["item_id"], resolution="judge_right", actor=LABELLER,
                                      note=f"pre-filter {RULE} run {run_id}: p={e['p']:.2f}")
                    out["resolved_items"] += 1
    return out


async def run(pool, set_id: str, set_version: str, *, ask: bool = False, apply: bool = False,
              split: Optional[str] = None, writers: Optional[Iterable[str]] = None, limit: int = DEFAULT_ASK,
              max_usd: float = DEFAULT_MAX_USD, run_id: Optional[str] = None, actor: str = "admin:unknown",
              mode: Optional[str] = None) -> dict:
    """Plan (ask=False: no Jev call; the rule over the verdicts already known), shadow (ask=True: ask up to `limit`
    claims, report) or apply (enforce switch only: write the auto_ok labels of the enforced writers). The module
    docstring has the rule, the candidates and the guards."""
    from services.acp_shared.audit_log import write_audit_log

    mode = mode or switch_mode()
    if apply and mode != "enforce":
        raise PrefilterError(409, "switched_off", f"the pre-filter is in shadow mode ({MODE_ENV} is not 'enforce'): "
                                                  "it reports, it never writes a label")
    if not 1 <= int(limit) <= MAX_ASK:
        raise PrefilterError(422, "invalid", f"limit must be 1..{MAX_ASK}")
    if not 0 < float(max_usd) <= MAX_USD_CAP:
        raise PrefilterError(422, "invalid", f"max_usd must be > 0 and <= {MAX_USD_CAP}")
    run_id = run_id or f"pf-{set_id}-{set_version}"
    if not rv.RUN_ID_RE.match(run_id):
        raise PrefilterError(422, "invalid", "run_id is not a run id")

    async with pool.acquire() as conn:
        rows = await store.read_rows(conn, set_id, set_version)
        corrected = await store.relabelled_claims(conn, set_id, set_version)
        cands = store.queue_candidates(rows, split=split, writers=writers, skip_claims=corrected)
        by_id = {r["label_id"]: r for r in rows}
        panels: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            if r["labeller_kind"] == "agent_panel":
                panels[r["claim_hash"]].append(r)
        refs = [c["source_ref"] for c in cands]
        queued = {r["source_ref"]: dict(r) for r in await conn.fetch(QUEUED_SQL, refs)} if refs else {}
        tids = sorted({c["tenant_id"] for c in cands if c.get("tenant_id")})
        tenants = {r["tenant_id"]: dict(r) for r in await conn.fetch(TENANT_SQL, tids)} if tids else {}
        disputed = [c for c in cands if c["human_label"] == "disputed"]

    skipped: Counter = Counter({"audit_draw": len(cands) - len(disputed)})
    entries: list[dict] = []
    for c in disputed:
        anchor = by_id[c["source_ref"]]
        q = queued.get(c["source_ref"])
        e = {"claim_hash": c["claim_hash"], "writer": c["writer"], "split": c.get("split"), "scope": c["scope"],
             "tenant_id": c.get("tenant_id"), "set_id": set_id, "set_version": set_version,
             "queue": q["status"] if q else None, "panel": panels[c["claim_hash"]], "p": None, "skip": None,
             "sentence": anchor.get("claim_text") or "", "anchor": anchor}
        if q is not None and q["status"] != "open":
            e["skip"] = "queue_resolved"
        else:
            e["skip"] = tenant_gate(c["scope"], c.get("tenant_id"), tenants.get(c.get("tenant_id") or ""))
        entries.append(e)

    # the source is rebuilt only for the claims that passed the queue and tenant gates
    sources, source_kinds = await _sources(pool, [e.pop("anchor") for e in entries if e["skip"] is None])
    for e in entries:
        e.pop("anchor", None)
        if e["skip"] is None and e["claim_hash"] not in sources:
            e["skip"] = "no_source"
        if e["skip"] is None:
            e["source"] = sources[e["claim_hash"]]
            e["subject"] = subject_key(e["claim_hash"], e["source"])
            e["est_tokens"] = estimate_tokens(e["source"], e["sentence"])
        else:
            skipped[e["skip"]] += 1

    live = [e for e in entries if e["skip"] is None]
    if live:
        async with pool.acquire() as conn:
            done = {r["subject_key"]: r["p"] for r in await conn.fetch(
                DONE_SQL, STAGE, QUESTION, [e["subject"] for e in live])}
        for e in live:
            if e["subject"] in done:
                e["p"] = done[e["subject"]]
    known = sum(1 for e in live if e["p"] is not None)
    todo = [e for e in live if e["p"] is None]

    from shared.llm_client.decide import _price_in_per_mtok

    price_in = _price_in_per_mtok()
    verdicts: dict[str, Any] = {"known": known, "to_ask": len(todo), "asked": 0, "answered": 0, "errors": 0,
                                "stopped": None, "error_kinds": {}}
    usd = {"estimated_to_ask": round(sum(e["est_tokens"] for e in todo) * price_in / 1e6, 6),
           "estimated_this_call": 0.0, "actual_this_call": 0.0}
    if ask and todo:
        st = await _ask(pool, todo, limit=int(limit), max_usd=float(max_usd), price_in=price_in)
        for e in todo:
            if e["claim_hash"] in st["probs"]:
                e["p"] = st["probs"][e["claim_hash"]]
        verdicts.update(asked=st["asked"], answered=st["answered"], errors=st["errors"], stopped=st["stopped"],
                        error_kinds=dict(st["error_kinds"].most_common(5)))
        usd.update(estimated_this_call=round(st["est_usd"], 6), actual_this_call=round(st["usd"], 6))

    decisions: Counter = Counter()
    reasons: Counter = Counter()
    by_writer: dict[str, Counter] = defaultdict(Counter)
    to_write: list[dict] = []
    shadow_writer = 0
    items = []
    for e in entries:
        decision, why = ("review", e["skip"]) if e["skip"] else rule(e["p"], e["panel"])
        decisions[decision] += 1
        by_writer[e["writer"]][decision] += 1
        if decision == "review":
            reasons[why] += 1
        elif e["writer"] in ENFORCE_WRITERS:
            to_write.append(e)
        else:
            shadow_writer += 1
        items.append({"claim_hash": e["claim_hash"], "writer": e["writer"], "split": e["split"], "queue": e["queue"],
                      "majority": panel_majority(e["panel"]), "non_ok_misbinding": non_ok_misbinding(e["panel"]),
                      "p": None if e["p"] is None else round(e["p"], 5), "decision": decision, "why": why})

    applied = None
    if apply:
        applied = {**await _apply(pool, to_write, run_id=run_id), "shadow_writer": shadow_writer}
        async with pool.acquire() as conn:
            await write_audit_log(conn, tenant_id=None, actor=actor[:128], action=AUDIT_ACTION,
                                  resource_type="eval_claim_label", resource_id=run_id[:64],
                                  details={"set": f"{set_id}@{set_version}", "rule": RULE, **applied,
                                           "auto_ok": decisions["auto_ok"], "asked": verdicts["asked"]})

    return {"set": f"{set_id}@{set_version}", "switch": mode, "run_id": run_id,
            "call": "apply" if apply else "shadow" if ask else "plan",
            "rule": {"id": RULE, "labeller": LABELLER, "question": QUESTION, "accept_p": ACCEPT_P,
                     "panel_majority": "ok", "no_non_ok_lens_class": MISBINDING,
                     "enforce_writers": list(ENFORCE_WRITERS)},
            "filter": {"split": split, "writers": sorted(writers) if writers is not None else None},
            "candidates": len(cands), "corrected_left_out": len(
                {c["claim_hash"] for c in store.queue_candidates(rows, split=split, writers=writers)} & corrected),
            "skipped": dict(sorted(skipped.items())), "sources": source_kinds, "verdicts": verdicts, "usd": usd,
            "decisions": {"auto_ok": decisions["auto_ok"], "review": decisions["review"]},
            "auto_ok_writable": len(to_write), "auto_ok_shadow_writer": shadow_writer,
            "review_reasons": dict(sorted(reasons.items())),
            "by_writer": {w: dict(c) for w, c in sorted(by_writer.items())},
            "applied": applied, "items": items}


__all__ = ["ACCEPT_P", "ENFORCE_WRITERS", "LABELLER", "MAX_ASK", "MAX_USD_CAP", "MISBINDING", "MODE_ENV", "MODES",
           "PrefilterError", "QUESTION", "RULE", "STAGE", "estimate_tokens", "non_ok_misbinding", "panel_majority",
           "reason_text", "rule", "run", "subject_key", "switch_mode", "tenant_gate"]
