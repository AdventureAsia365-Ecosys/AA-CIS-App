#!/usr/bin/env python3
"""AA-919 — pick CANDIDATE calibration pairs for the under-represented strata of the golden set. Read-only, $0.

Why: the R2 gate measures the judge on the golden labels (kappa >= 0.6 on >= 10 pairs, >= 3 of each label). With few
pairs per stratum, one item swings kappa by ~0.2 (S255 leave-one-out), and some strata cannot be measured at all
(t2 has 10 items, 3 pass; t9_social is LinkedIn only; 2-5 LinkedIn items per tenant). More labelled pairs per stratum
is the cure (S255 notes: "more fail labels for t9_social is the real cure"), but labels must come from people or the
agent panel, never from this script.

What it does (no label is written or guessed):
  1. Reads the active golden set and counts the calibration items (labelled, not trap, not post_edit) per writer and
     stratum (`eval_checks.strata_label_counts`: channel, tenant, tier, split).
  2. Finds the GAPS: per writer, a label class under --target-per-label overall; per channel / tenant / tier value
     (and every T9 social channel that has real pieces but no golden item), a label class under --stratum-target.
  3. For each gap, picks candidate outputs from existing data that the golden set does not already hold (excluded by
     piece id / tenant tour version id / tour id), in the gap's stratum. Each candidate carries the evidence that
     already exists about it (`lean`: held, edited, human label, review decision, approved and published untouched),
     only to steer sampling toward the label class the stratum lacks; the label itself comes from the labelling
     round. It oversamples (--oversample, default 2x the gap), because a lean is often wrong. Deterministic order:
     sha256("<seed>:<id>").
  Output: one JSON document on stdout (ids, strata, counts, codes; never text) for the next step.

Next step (not built here; proposal in the AA-919 notes): freeze the chosen ids into a fixture addendum with the AA-729
freezer (`scripts.aa729.select_samples`, an `--ids` mode), label them in an agent-panel round with the S256 rubric
rules (`scripts.aa729.labels pack`, then the tech lead's review), import as golden set 1.3.0 (WM-17 importer, a new
version: labels are immutable per version), then re-run the R2 calibration and the judge comparison.

Runs inside aa-cis-dev-worker / -api as cis_app (container DATABASE_URL), read-only session. scripts/ is not in the
image: the file is exec()'d with no argv; prepend `import sys; sys.argv = ["calibration_candidates", ...]` to a copy.
  python3 calibration_candidates.py [--writer t9_social] [--target-per-label 5] [--stratum-target 3] [--seed 919]
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from typing import Any, Optional

for _p in (os.getcwd(), "/app"):
    if os.path.isdir(os.path.join(_p, "services")) and _p not in sys.path:
        sys.path.insert(0, _p)

from services.writing_memory import config as cfg  # noqa: E402
from services.writing_memory import eval_checks as ec  # noqa: E402

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
GAP_DIMS = ("channel", "tenant", "tier")
LONG_DAYS = 9                     # services/golden_set/derive.py LONG_DAYS (tier long_9d)
MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"   # the AA internal (A1) tenant (aa729 common)

# ── pure parts (unit-tested) ───────────────────────────────────────────────────────────────────────────────────────


def order_key(seed: int, ident: str) -> str:
    return hashlib.sha256(f"{seed}:{ident}".encode()).hexdigest()


def gaps(writer: str, items: list[dict], *, target_per_label: int, stratum_target: int,
         extra_channels: tuple[str, ...] = ()) -> list[dict]:
    """The label shortfalls of one writer's calibration items: [{writer, dim, value, label, have, need}]. Overall:
    each label up to `target_per_label`; per channel / tenant / tier value: each label up to `stratum_target`.
    `extra_channels`: channels with real pieces but no golden item (a whole stratum missing: need both labels)."""
    rows = ec.strata_label_counts(items)
    out = []
    for r in rows:
        if r["dim"] == "all":
            target = target_per_label
        elif r["dim"] in GAP_DIMS and r["value"] != "untiered":
            target = stratum_target
        else:
            continue
        for label in ("pass", "fail"):
            have = r["labels"][label]
            if have < target:
                out.append({"writer": writer, "dim": r["dim"], "value": r["value"], "label": label, "have": have,
                            "need": target - have})
    present = {r["value"] for r in rows if r["dim"] == "channel"}
    for ch in sorted(set(extra_channels) - present):
        for label in ("pass", "fail"):
            out.append({"writer": writer, "dim": "channel", "value": ch, "label": label, "have": 0,
                        "need": stratum_target})
    return out


def lean(signals: dict) -> Optional[str]:
    """The label class the existing evidence points to (only to steer sampling, never a label): fail when the
    pipeline held it, someone edited it, a human label / review says fail; pass when a human label / review says
    pass, or it was approved and published with no edit; None otherwise."""
    if signals.get("human_label") in ("pass", "fail"):
        return signals["human_label"]
    if signals.get("review") in ("pass", "fail"):
        return signals["review"]
    if signals.get("held") or signals.get("edited") or signals.get("qa_failed"):
        return "fail"
    if signals.get("approved") and signals.get("published") and not signals.get("edited"):
        return "pass"
    return None


def candidate_strata(c: dict) -> set[tuple[str, str]]:
    s = {("tenant", str(c.get("tenant_id") or "platform"))}
    if c.get("channel"):
        s.add(("channel", c["channel"]))
    if c.get("days") is not None and c["days"] >= LONG_DAYS:
        s.add(("tier", "long_9d"))
    return s


def pick(gap_rows: list[dict], cands: list[dict], *, seed: int, oversample: float) -> list[dict]:
    """For each gap, up to ceil(need x oversample) candidates in the gap's stratum whose lean is the missing label
    (then unknown lean), not picked for an earlier gap; deterministic. Returns the picks with the gap they fill."""
    taken: set[str] = set()
    out = []
    ordered = sorted(cands, key=lambda c: order_key(seed, c["id"]))
    for g in gap_rows:
        k = max(1, int(-(-g["need"] * oversample // 1)))
        key = (g["dim"], g["value"])
        pool = [c for c in ordered if c["id"] not in taken and (g["dim"] == "all" or key in candidate_strata(c))]
        pref = [c for c in pool if lean(c["signals"]) == g["label"]] + [c for c in pool if lean(c["signals"]) is None]
        for c in pref[:k]:
            taken.add(c["id"])
            out.append({**c, "fills": f"{g['dim']}:{g['value']}:{g['label']}", "lean": lean(c["signals"])})
    return out


def golden_refs(items: list[dict]) -> set[str]:
    """Every id the golden set already holds (refs: piece_id, version_id, tour_id, ...): never picked again."""
    out = set()
    for it in items:
        refs = it.get("refs") or {}
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except ValueError:
                refs = {}
        for k in ("piece_id", "version_id", "tour_id", "generated_content_id"):
            if refs.get(k):
                out.add(str(refs[k]))
    return out


# ── reads ──────────────────────────────────────────────────────────────────────────────────────────────────────────

_ITEMS_SQL = """
    SELECT item_id::text AS item_id, sample_id, writer, channel, tenant_id::text AS tenant_id, split, trap, tiers,
           post_edit, label_verdict, refs, (control_output IS NOT NULL) AS has_output
      FROM shared.eval_golden_item WHERE set_id = $1::uuid
"""
_ACTIONS_SQL = """
    SELECT resource_id::text AS resource_id, action, count(*) AS n FROM acp_shared.audit_log
     WHERE action LIKE 'content_piece.admin_sentence_%' OR action LIKE 'tenant_tour_version.admin_sentence_%'
        OR action IN ('content_piece.edited', 'content_piece.published')
     GROUP BY resource_id, action
"""
_T9_SQL = """
    SELECT cp.piece_id::text AS id, cp.tenant_id::text AS tenant_id, cp.channel, cp.status, cp.created_at
      FROM acp_shared.content_piece cp
     WHERE cp.status IN ('approved', 'held') AND cp.writer_input IS NOT NULL AND length(cp.content_text) > 0
       AND cp.channel IS NOT NULL AND (($1 AND cp.channel = 'blog') OR (NOT $1 AND cp.channel <> 'blog'))
"""
_PIECE_LABEL_SQL = "SELECT piece_id::text AS piece_id, label FROM acp_shared.content_piece_label"
_T2_SQL = """
    SELECT v.id::text AS id, v.tenant_id::text AS tenant_id, v.status, v.qa_status, pt.tour_id::text AS tour_id,
           rt.duration,
           EXISTS (SELECT 1 FROM gold_aa_internal.tenant_tour_versions c
                    WHERE c.parent_version_id = v.id AND c.edit_source = 'tenant_edit') AS tenant_edited
      FROM gold_aa_internal.tenant_tour_versions v
      JOIN gold_aa_internal.published_tours pt ON pt.id = v.published_tour_id
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
     WHERE v.edit_source IN ('ai_generated', 're_rewrite') AND v.status NOT IN ('pending', 'failed')
"""
_T2_REVIEW_SQL = """
    SELECT DISTINCT ON (tenant_tour_version_id) tenant_tour_version_id::text AS version_id,
           review_status::text AS review_status
      FROM silver_aa_internal.review_queue WHERE tenant_tour_version_id IS NOT NULL
     ORDER BY tenant_tour_version_id, created_at DESC
"""
_A1_SQL = """
    SELECT rt.tour_id::text AS id, rt.duration, pt.master_status::text AS master_status, gc.human_edited,
           gc.id::text AS gc_id, (pt.generated_content_id = gc.id) AS published
      FROM silver_aa_internal.raw_tours rt
      LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id AND pt.deleted_at IS NULL
      JOIN LATERAL (SELECT g.id, g.human_edited FROM silver_aa_internal.generated_content g
                     WHERE g.tour_id = rt.tour_id AND g.metadata->>'writer_version' = 'v2'
                     ORDER BY g.version_num DESC LIMIT 1) gc ON true
     WHERE rt.tenant_id = $1::uuid AND rt.source_status = 'active' AND rt.deleted_at IS NULL
"""
_A1_REVIEW_SQL = """
    SELECT DISTINCT ON (tour_id) tour_id::text AS tour_id, review_status::text AS review_status
      FROM silver_aa_internal.review_queue WHERE tenant_tour_version_id IS NULL AND tenant_id = $1::uuid
     ORDER BY tour_id, created_at DESC
"""
_REVIEW = {"approved": "pass", "rejected": "fail"}
_PIECE_LABEL = {"as_is": "pass", "light": "pass", "heavy": "fail", "reject": "fail"}


def _days(d) -> Optional[int]:
    try:
        from services.ingestion.near_duplicate import duration_days
        return duration_days(d)
    except Exception:  # noqa: BLE001
        return None


async def candidates(conn, writer: str, actions: dict[str, dict[str, int]]) -> list[dict]:
    out = []
    if writer in ("t9_blog", "t9_social"):
        labels: dict[str, list[str]] = {}
        try:
            for x in await conn.fetch(_PIECE_LABEL_SQL):
                labels.setdefault(x["piece_id"], []).append(_PIECE_LABEL.get(x["label"]))
        except Exception as e:  # noqa: BLE001 — an old DB without the table: no human labels
            print(f"(content_piece_label unreadable: {type(e).__name__})", file=sys.stderr)
        for r in await conn.fetch(_T9_SQL, writer == "t9_blog"):
            act = actions.get(r["id"], {})
            votes = Counter(v for v in labels.get(r["id"], []) if v)
            human = (None if not votes else "pass" if votes["pass"] > votes["fail"] else "fail")
            out.append({"writer": writer, "id_kind": "piece_id", "id": r["id"], "tenant_id": r["tenant_id"],
                        "channel": r["channel"], "days": None,
                        "signals": {"held": r["status"] == "held", "approved": r["status"] == "approved",
                                    "published": bool(act.get("content_piece.published")),
                                    "edited": bool(act.get("content_piece.edited") or any(
                                        k.startswith("content_piece.admin_sentence_") for k in act)),
                                    "human_label": human}})
    elif writer == "t2":
        reviews = {x["version_id"]: _REVIEW.get(x["review_status"]) for x in await conn.fetch(_T2_REVIEW_SQL)}
        for r in await conn.fetch(_T2_SQL):
            act = actions.get(r["id"], {})
            out.append({"writer": writer, "id_kind": "version_id", "id": r["id"], "tenant_id": r["tenant_id"],
                        "channel": None, "days": _days(r["duration"]), "tour_id": r["tour_id"],
                        "signals": {"qa_failed": r["qa_status"] not in (None, "passed"),
                                    "approved": r["status"] == "approved", "published": r["status"] == "approved",
                                    "edited": bool(r["tenant_edited"] or any(
                                        k.startswith("tenant_tour_version.admin_sentence_") for k in act)),
                                    "review": reviews.get(r["id"])}})
    else:  # a1
        reviews = {x["tour_id"]: _REVIEW.get(x["review_status"])
                   for x in await conn.fetch(_A1_REVIEW_SQL, MASTER_TENANT_ID)}
        for r in await conn.fetch(_A1_SQL, MASTER_TENANT_ID):
            out.append({"writer": writer, "id_kind": "tour_id", "id": r["id"], "tenant_id": None, "channel": None,
                        "days": _days(r["duration"]),
                        "signals": {"approved": r["master_status"] == "active", "published": bool(r["published"]),
                                    "edited": bool(r["human_edited"]), "review": reviews.get(r["id"])}})
    return out


def parse(argv):
    ap = argparse.ArgumentParser(description="AA-919 calibration candidate picker (read-only, $0)")
    ap.add_argument("--writer", action="append", choices=WRITERS, help="repeatable; default: all four")
    ap.add_argument("--target-per-label", type=int, default=cfg.MIN_KAPPA_PER_LABEL + 2,
                    help="overall labels wanted per class (default MIN_KAPPA_PER_LABEL + 2: one item no longer "
                         "decides the gate)")
    ap.add_argument("--stratum-target", type=int, default=cfg.MIN_KAPPA_PER_LABEL)
    ap.add_argument("--oversample", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=919)
    return ap.parse_args(argv)


async def main(argv=None) -> int:
    import asyncpg

    a = parse(sys.argv[1:] if argv is None else argv)
    writers = a.writer or list(WRITERS)
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await c.execute("SET default_transaction_read_only = on")
        assert (await c.fetchval("SHOW default_transaction_read_only")) == "on"
        gs = await c.fetchrow("SELECT set_id::text AS set_id, name, version FROM shared.eval_golden_set "
                              "WHERE name = 'aa729' AND status = 'active'")
        if gs is None:
            print("no active golden set")
            return 2
        all_items = [dict(r) for r in await c.fetch(_ITEMS_SQL, gs["set_id"])]
        taken = golden_refs(all_items)
        actions: dict[str, dict[str, int]] = {}
        for x in await c.fetch(_ACTIONS_SQL):
            actions.setdefault(x["resource_id"], {})[x["action"]] = int(x["n"])
        report: dict[str, Any] = {"golden_set": f"{gs['name']}@{gs['version']}", "target_per_label":
                                  a.target_per_label, "stratum_target": a.stratum_target, "seed": a.seed,
                                  "writers": {}}
        for w in writers:
            calib = [it for it in all_items if it["writer"] == w and not it["trap"] and not it["post_edit"]
                     and it["label_verdict"] in ("pass", "fail") and it["has_output"]]
            cands = [x for x in await candidates(c, w, actions)
                     if x["id"] not in taken and str(x.get("tour_id") or "") not in taken]
            extra = tuple(sorted({x["channel"] for x in cands if x["channel"]})) if w == "t9_social" else ()
            g = gaps(w, calib, target_per_label=a.target_per_label, stratum_target=a.stratum_target,
                     extra_channels=extra)
            picks = pick(g, cands, seed=a.seed, oversample=a.oversample)
            report["writers"][w] = {
                "calibration_items": len(calib), "strata": ec.strata_label_counts(calib), "gaps": g,
                "pool": {"candidates": len(cands), "by_lean": dict(Counter(str(lean(x["signals"])) for x in cands)),
                         "by_channel": dict(Counter(str(x["channel"]) for x in cands)),
                         "by_tenant": dict(Counter(str(x["tenant_id"]) for x in cands))},
                "picks": [{k: p.get(k) for k in ("id_kind", "id", "tenant_id", "channel", "days", "fills", "lean",
                                                 "signals")} for p in picks],
                "unfilled": [f"{x['dim']}:{x['value']}:{x['label']}" for x in g
                             if not any(p["fills"] == f"{x['dim']}:{x['value']}:{x['label']}" for p in picks)]}
        print(json.dumps(report, indent=1, default=str))
    finally:
        await c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
