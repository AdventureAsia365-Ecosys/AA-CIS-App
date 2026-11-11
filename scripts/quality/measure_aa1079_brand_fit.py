"""AA-1079 (S277) — brand-fit judge agreement against the brand labels of the AA-729 golden set, the deterministic
cross-brand overlap check, and a threshold decision section for the tech lead. $0: no model call.

Items: the golden items that carry a brand label (``label.brand``, format ``services/quality/brand_labels.py``), read
from the ACTIVE golden set in the DB (``shared.eval_golden_set`` name ``aa729`` + ``shared.eval_golden_item``) or
from a fixture file (``--fixture <path>|s3:<key>``). Instruments, each against the label (FAIL = off-brand is the
positive class, ``services/quality/brand_agreement.py``):

- ``t2_judge_brand_fit``  the T2 judge's brand_fit score of the item's version
  (``gold_aa_internal.tenant_tour_versions.judge_breakdown``, by ``refs.version_id``), swept over thresholds;
- ``t2_stored_score``     the version's stored ``quality_score`` (what the agency list shows), swept too;
- ``t2_judge_core_idea``  the judge's ``mission_present`` against ``label.brand.core_idea`` (``unclear`` left out);
- ``t9_f9_brand_style``   the T10 F9 brand-voice gate of the stored piece (last ``F9_brand_style`` entry of the
  frozen ``gate_ledger``: passed or not).

``--judge-scores <file>`` ({sample_id: {"brand_fit", "mission_present", "stored_score", "f9_passed"}}) adds or
overrides judge values (a later judge run over the frozen outputs, for example). Overlap: the golden T2 items of one
published tour written by several tenants (``--overlap-live`` adds the latest version of every tenant x tour of the
named tenants, ``--tenant-slug``, with the Master text as the source).

    python -m scripts.quality.measure_aa1079_brand_fit [--fixture s3:scripts/out/aa729/fixture-x.y.z.json] \\
        [--judge-scores scores.json] [--overlap-live --tenant-slug a --tenant-slug b] --out m.json --report m.md

DB login: ``DATABASE_URL`` (cis_app in the api container), ssl required unless ``MEASURE_DB_SSL=disable``; sessions
are read only. Output: JSON (ids, labels and numbers, no text) between MEASURE-BEGIN / MEASURE-END on stdout, at
``--out``, and the Markdown report at ``--report``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Optional

SCHEMA = "aa1079-brand-fit-measure/1"
GOLDEN_SET_NAME = "aa729"

_ITEMS_SQL = """
    SELECT i.sample_id, i.writer, i.channel, i.refs, i.label, i.control_output
    FROM shared.eval_golden_item i JOIN shared.eval_golden_set s ON s.set_id = i.set_id
    WHERE s.name = $1 AND s.status = 'active' AND NOT i.trap AND i.writer = ANY($2::text[])
"""
_SET_SQL = "SELECT version FROM shared.eval_golden_set WHERE name = $1 AND status = 'active'"
_VERSIONS_SQL = """
    SELECT id::text AS id, quality_score::float AS quality_score, judge_breakdown
    FROM gold_aa_internal.tenant_tour_versions WHERE id = ANY($1::uuid[])
"""
_LIVE_SQL = """
    SELECT DISTINCT ON (v.tenant_id, v.published_tour_id) v.published_tour_id::text AS tour, t.slug,
           v.rewritten_content, v.judge_breakdown
    FROM gold_aa_internal.tenant_tour_versions v JOIN shared.tenants t ON t.tenant_id = v.tenant_id
    WHERE t.slug = ANY($1::text[]) AND v.edit_source <> 'tenant_edit'
    ORDER BY v.tenant_id, v.published_tour_id, v.version_number DESC
"""
_MASTER_SQL = """
    SELECT id::text AS tour, aa_name, aa_subtitle, aa_summary, aa_description, aa_highlights, aa_itineraries
    FROM gold_aa_internal.published_tours WHERE id = ANY($1::uuid[])
"""


def _j(v: Any, default: Any = None) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return default if v is None else v


def items_from_fixture(fixture: dict) -> list[dict]:
    from services.golden_set.derive import writer_and_channel
    from services.quality import brand_labels

    out = []
    for s in fixture.get("samples") or []:
        if not brand_labels.is_brand_item(s):
            continue
        writer, channel = writer_and_channel(s)
        out.append({"sample_id": s["sample_id"], "writer": writer, "channel": channel, "refs": s.get("refs") or {},
                    "label": s.get("label") or {}, "output": s.get("output") or {}})
    return out


def f9_passed(output: Any) -> Optional[bool]:
    """The last F9_brand_style result of a frozen T9 gate ledger, or None when it never ran."""
    ledger = _j((output or {}).get("gate_ledger"), []) if isinstance(output, dict) else []
    hits = [e for e in ledger or [] if isinstance(e, dict) and e.get("gate") == "F9_brand_style"]
    return None if not hits else bool(hits[-1].get("passed"))


def judge_values(items: list[dict], versions: dict[str, dict], overrides: Optional[dict] = None) -> dict:
    """sample_id -> {brand_fit, mission_present, stored_score, f9_passed} (None where the instrument has nothing)."""
    out = {}
    for it in items:
        v = versions.get(str((it.get("refs") or {}).get("version_id") or "")) or {}
        jb = _j(v.get("judge_breakdown"), {}) or {}
        rec = {"brand_fit": jb.get("brand_fit"), "mission_present": jb.get("mission_present"),
               "stored_score": v.get("quality_score"),
               "f9_passed": f9_passed(it.get("output")) if it["writer"] != "t2" else None}
        rec.update({k: val for k, val in ((overrides or {}).get(it["sample_id"]) or {}).items() if k in rec})
        out[it["sample_id"]] = rec
    return out


def measure(items: list[dict], judge: dict, *, current: float) -> dict:
    """Agreement of every instrument with the brand labels, strict and lenient, plus the decision per instrument."""
    from services.quality import brand_agreement as ba
    from services.quality import brand_labels

    labelled = []
    for it in items:
        lab = brand_labels.brand_label_of(it)
        if lab is not None:
            labelled.append((it, lab))
    coverage = {"brand_items": len(items), "labelled": len(labelled),
                "by_writer": {}, "brand_fit": {}, "core_idea": {}, "by_source": {}}
    for it, lab in labelled:
        for key, val in (("by_writer", it["writer"]), ("brand_fit", lab["brand_fit"]),
                         ("core_idea", lab["core_idea"]), ("by_source", lab["source"])):
            coverage[key][val] = coverage[key].get(val, 0) + 1

    instruments: dict = {}
    for mode, lenient in (("strict", False), ("lenient", True)):
        t2 = [(it, lab) for it, lab in labelled if it["writer"] == "t2"]
        for name, field in (("t2_judge_brand_fit", "brand_fit"), ("t2_stored_score", "stored_score")):
            scored = [(float(judge[it["sample_id"]][field]), brand_labels.label_passes(lab, lenient=lenient))
                      for it, lab in t2 if judge.get(it["sample_id"], {}).get(field) is not None]
            rows = ba.sweep(scored)
            at_current = ba.agreement((s >= current, lp) for s, lp in scored)
            best = ba.best_threshold(rows, current)
            instruments.setdefault(name, {})[mode] = {
                "at_current": at_current, "best": best, "sweep": rows,
                "decision": ba.decision(at_current["n"], at_current["label_fail"], at_current["label_pass"], best,
                                        current)}
        t9 = [(bool(judge[it["sample_id"]]["f9_passed"]), brand_labels.label_passes(lab, lenient=lenient))
              for it, lab in labelled if it["writer"] != "t2"
              and judge.get(it["sample_id"], {}).get("f9_passed") is not None]
        a9 = ba.agreement(t9)
        instruments.setdefault("t9_f9_brand_style", {})[mode] = {
            "at_current": a9, "decision": ba.decision(a9["n"], a9["label_fail"], a9["label_pass"],
                                                      a9 if a9["kappa"] is not None else None, current)}
    core = [(bool(judge[it["sample_id"]]["mission_present"]), lab["core_idea"] == "present")
            for it, lab in labelled if it["writer"] == "t2" and lab["core_idea"] != "unclear"
            and judge.get(it["sample_id"], {}).get("mission_present") is not None]
    instruments["t2_judge_core_idea"] = {"strict": {"at_current": ba.agreement(core)}}
    return {"coverage": coverage, "current_threshold": current, "instruments": instruments}


def golden_overlap(items: list[dict], texts: dict[str, str]) -> dict:
    """Overlap of the golden T2 items that share a published tour (``texts``: sample_id -> text)."""
    from services.quality import brand_overlap

    tours: dict[str, dict] = {}
    for it in items:
        refs = it.get("refs") or {}
        tour, brand = refs.get("published_tour_id"), refs.get("tenant_slug")
        if it["writer"] == "t2" and tour and brand and texts.get(it["sample_id"]):
            tours.setdefault(str(tour), {})[brand] = texts[it["sample_id"]]
    return brand_overlap.overlap_report({t: by for t, by in tours.items() if len(by) >= 2})


def report_markdown(result: dict) -> str:
    cov, ins = result["measure"]["coverage"], result["measure"]["instruments"]
    cur = result["measure"]["current_threshold"]
    lines = [f"# AA-1079 brand-fit calibration ({result.get('golden_set_version') or 'fixture'})", "",
             "Generated by `scripts/quality/measure_aa1079_brand_fit.py` ($0, no model call). FAIL (off-brand) is "
             "the positive class; strict = only `fits` passes, lenient = `fits` or `partly` passes.", "",
             "## Labels", "",
             f"- Brand items: {cov['brand_items']}, labelled: {cov['labelled']}",
             f"- By writer: {cov['by_writer']}; brand_fit: {cov['brand_fit']}; core_idea: {cov['core_idea']}; "
             f"source: {cov['by_source']}", "",
             f"## Agreement at the current pass line ({cur})", "",
             "| Instrument | Mode | n | label off-brand | judge off-brand | kappa | accuracy | recall (fail) | "
             "precision (fail) |", "|---|---|---|---|---|---|---|---|---|"]
    for name in ("t2_judge_brand_fit", "t2_stored_score", "t9_f9_brand_style", "t2_judge_core_idea"):
        for mode, rec in (ins.get(name) or {}).items():
            a = rec["at_current"]
            lines.append(f"| {name} | {mode} | {a['n']} | {a['label_fail']} | {a['judge_fail']} | {a['kappa']} | "
                         f"{a['accuracy']} | {a['recall_fail']} | {a['precision_fail']} |")
    for name in ("t2_judge_brand_fit", "t2_stored_score"):
        rows = ((ins.get(name) or {}).get("strict") or {}).get("sweep") or []
        if rows:
            lines += ["", f"## Threshold sweep: {name} (strict)", "",
                      "| threshold | n | kappa | judge off-brand | recall (fail) | precision (fail) |",
                      "|---|---|---|---|---|---|"]
            lines += [f"| {r['threshold']} | {r['n']} | {r['kappa']} | {r['judge_fail']} | {r['recall_fail']} | "
                      f"{r['precision_fail']} |" for r in rows]
    ov = result.get("overlap") or {}
    lines += ["", "## Cross-brand overlap (word 5-gram Jaccard, deterministic)", ""]
    for key in ("golden", "live"):
        rep = ov.get(key)
        if not rep:
            continue
        rw = rep["rewritten"]
        lines.append(f"- {key}: {rw['tours']} rewritten tours, {rw['pairs']} pairs, Jaccard mean {rw['jaccard_mean']} "
                     f"(max {rw['jaccard_max']}); warn line {rep['warn_line']}; tours over it: "
                     f"{len(rep['warn_tours'])}; identical (policy hold) tours: {len(rep['identical_tours'])}")
        for pair, m in (rep.get("per_pair_mean") or {}).items():
            lines.append(f"  - {pair}: {m}")
    lines += ["", "## Threshold decision (for the tech lead)", "",
              "Bar (writing policy D9 for a brand judge): kappa >= 0.6 on >= 40 labelled pairs, >= 5 of each class. "
              "Proposed per instrument; the tech lead ticks one option.", ""]
    for name in ("t2_judge_brand_fit", "t2_stored_score", "t9_f9_brand_style"):
        for mode in ("strict", "lenient"):
            d = ((ins.get(name) or {}).get(mode) or {}).get("decision")
            if d:
                lines.append(f"- **{name} ({mode})**: `{d['status']}`, proposed threshold {d['proposed_threshold']}, "
                             f"keep {d['keep']}. {d['why']}")
    lines += ["", "Options:", "",
              "- [ ] A. Keep T7 Debate rank only (`T7_DEBATE_MODE` unset) and the T2 brand-fit score report only; "
              "label more items (recommended unless an instrument above is `calibrated`).",
              "- [ ] B. Bring the Debate cut back at the proposed threshold of `t2_judge_brand_fit` "
              "(`T7_DEBATE_MODE=cut`, `t7.brand_fit_cut` set to it, with its calibration record).",
              "- [ ] C. Retire the brand-fit score from the stored quality score until a rubric change is measured.",
              ""]
    return "\n".join(lines)


async def _pool():
    import asyncpg

    ssl = None if os.environ.get("MEASURE_DB_SSL") == "disable" else "require"
    return await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl=ssl, min_size=1, max_size=2,
                                     server_settings={"default_transaction_read_only": "on"})


def _read_fixture(ref: str) -> dict:
    from services.golden_set.importer import _read

    data, _uri = _read(ref)
    return json.loads(data)


async def run(args) -> dict:
    from services.quality import brand_overlap
    from services.quality.registry import threshold

    current = float(threshold("t7.brand_fit_cut"))
    overrides = {}
    if args.judge_scores:
        with open(args.judge_scores, encoding="utf-8") as f:
            overrides = json.load(f)
    pool = None if args.no_db else await _pool()
    try:
        set_version = None
        if args.fixture:
            fixture = _read_fixture(args.fixture)
            items, set_version = items_from_fixture(fixture), fixture.get("fixture_version")
        else:
            async with pool.acquire() as conn:
                set_version = await conn.fetchval(_SET_SQL, GOLDEN_SET_NAME)
                rows = await conn.fetch(_ITEMS_SQL, GOLDEN_SET_NAME, ["t2", "t9_blog", "t9_social"])
            items = [{"sample_id": r["sample_id"], "writer": r["writer"], "channel": r["channel"],
                      "refs": _j(r["refs"], {}) or {}, "label": _j(r["label"], {}) or {},
                      "output": _j(r["control_output"], {}) or {}} for r in rows]
        versions: dict = {}
        ids = [str(it["refs"].get("version_id")) for it in items
               if it["writer"] == "t2" and it["refs"].get("version_id")]
        if pool is not None and ids:
            async with pool.acquire() as conn:
                versions = {r["id"]: dict(r) for r in await conn.fetch(_VERSIONS_SQL, ids)}
        judge = judge_values(items, versions, overrides)
        measured = measure(items, judge, current=current)
        overlap = {"golden": golden_overlap(items, {it["sample_id"]: brand_overlap.tour_text(it.get("output"))
                                                    for it in items if it["writer"] == "t2"})}
        if args.overlap_live and pool is not None and args.tenant_slug:
            async with pool.acquire() as conn:
                live = await conn.fetch(_LIVE_SQL, list(args.tenant_slug))
                tours: dict = {}
                for r in live:
                    tours.setdefault(r["tour"], {})[r["slug"]] = brand_overlap.tour_text(_j(r["rewritten_content"], {}))
                tours = {t: by for t, by in tours.items() if len(by) >= 2}
                masters = {r["tour"]: brand_overlap.tour_text({k: r[k] for k in r.keys() if k != "tour"})
                           for r in await conn.fetch(_MASTER_SQL, list(tours))} if tours else {}
            overlap["live"] = brand_overlap.overlap_report(tours, masters)
    finally:
        if pool is not None:
            await pool.close()
    return {"schema": SCHEMA, "cost_usd": 0, "golden_set_version": set_version, "measure": measured,
            "overlap": overlap}


def parse_args(argv: Optional[list[str]] = None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--fixture", help="a fixture file (path or s3:<key>) instead of the active golden set in the DB")
    p.add_argument("--judge-scores", help="JSON {sample_id: {brand_fit, mission_present, stored_score, f9_passed}}")
    p.add_argument("--no-db", action="store_true", help="no DB read (needs --fixture; judge values from the file)")
    p.add_argument("--overlap-live", action="store_true", help="also check the live versions of --tenant-slug")
    p.add_argument("--tenant-slug", action="append", default=[])
    p.add_argument("--out")
    p.add_argument("--report")
    a = p.parse_args(argv)
    if a.no_db and not a.fixture:
        p.error("--no-db needs --fixture")
    return a


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(run(args))
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report_markdown(result))
    print("MEASURE-BEGIN")
    print(text)
    print("MEASURE-END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
