#!/usr/bin/env python3
"""AA-1048 (Quality W2) — the claim-label tooling, $0. Every DB write is DRY RUN unless --apply; no model is called
(the gateway entry points are refused for the whole process, services/golden_set/llm_guard).

  export-fixture FIXTURE --set CL-1 --out <scratch>/cl1.jsonl [--writers t9_blog,t9_social] [--tour-map MAP]
                 [--split held_out|train|all]
      Claim lines (JSONL keyed by claim hash) of the frozen outputs of an AA-729 golden fixture, for the 3-lens panel
      (docs/calibration/aa1048-claim-panel-brief.md). Every T2 / T9 line carries its tour (refs.tour_id, the split
      group: a blog and a social piece of one tour stay on one side); a T2 / T9 sample without one is refused. A
      fixture frozen before select_samples kept refs.tour_id for T9 needs --tour-map: a JSON {piece_id | request_id |
      version_id | sample_id: tour_id} (read-only query: acp_shared.content_piece -> angle_gate_request.trip_id).
      Real text: every --out / --thresholds path is refused inside a git work tree unless it is in the ignored
      scratch folder .tmp-session/ (scripts/aa1048/outpath.py); use the workspace .tmp-session/.
      One set per writer surface (AA-1048 Scope): --set CL-<n> is the T9 set (t9_blog, t9_social), --set L<n> the S1
      set (a1), --set t2@<n> the T2 set (t2). Without --writers a named set exports the writers of its surface; a
      writer of another surface is refused (any other set id: every writer, one surface only). Both splits by
      default; --split held_out keeps the held-out lines only (the side a gate decision reads). Fixture 1.3.0 has
      held-out claims T9 24 (blog 0), t2 173, a1 1,092: the T9 gap is closed by the split-plan picks (held-out
      pieces frozen next) or new held-out pieces, not by exporting more writers into CL-1.
  split-plan --export cl1.jsonl [--export l2.jsonl --export t2.jsonl] [--live split-plan.json] [--ids-out ids.json]
             [--violation-rate 0.06] [--observed-precision 0.92] [--per-tour-cap 6]
      Held-out claims per writer of the exports, the target (R1 bar on one writer surface) and, with --live (the answer
      of GET /admin/writing-memory/claim-labels/split-plan), the existing T9 pieces / T2 versions of held-out tours
      to freeze next (scripts/aa729/select_samples.py --ids <ids-out> --base-version <fixture>) and the writer cost
      of what is still missing. Ids, counts and dollars only (services/ground_truth/split_plan.py).
  payload PANEL.jsonl --export claims.jsonl --set CL-1 --version 1.0.0 --out <scratch>/payload.json [--apply]
      The body of POST /admin/writing-memory/claim-labels/import (the staff API path of `import`: no DB login,
      services/ground_truth/importer.py): the judgement lines and the slim export lines of the claims they judge,
      validated locally first. dry_run unless --apply. Scratch path only (the payload holds claim text).
  import PANEL.jsonl --export claims.jsonl --set CL-1 --version 1.0.0 [--apply]
      Validate the panel (or tech-lead) lines against the export, consolidate the lenses, draw the 20% audit and
      write shared.eval_claim_label (CIS 388).
  report --set CL-1 --version 1.0.0
      Agreement per class, the panel error rate on the audit draw, contradictions (bar: 0), label counts.
  manifest --set CL-1 --version 1.0.0
      The public manifest (ids, counts, one digest; no text) for the root repo: the Dev DB is the master copy.
  enqueue --set CL-1 --version 1.0.0 [--apply]
      Disputed and audit-drawn claims into the disagreement queue (source golden_claim) for the tech lead.
  pull-resolutions --set CL-1 --version 1.0.0 [--apply]
      Resolved golden_claim items -> tech-lead rows.
  finals --set CL-1 --version 1.0.0 --out finals.jsonl [--audit-ok]
      The final labels that count as release evidence (tech lead; agreed panel labels only with --audit-ok).
  rederive FIXTURE --finals finals.jsonl
      Item verdicts re-derived under the policy (verdict_rule) and every golden label that flips.
  detectors --export claims.jsonl --finals finals.jsonl --out rows.json [--thresholds rows.yaml] [--per-writer]
            [--split held_out|train|all] [--agreement report.json]
      The deterministic detectors, tripwires and recorded producers measured on the labels. Default split held_out
      (the only one that may pass / derive enforce); train is for exploration and the label-budget rates. The
      agreement file is `cli report` output (or {class: agreement}); a class without one cannot pass.
  mutations [--texts 50] [--out report.json]
      The synthetic mutation suites per writer (recall per class, false findings).
  sample-size [--markdown] [--rates rates.json [--budget N]]
      The sample-size table, or the label budget per detector / class from measured rates.

DB commands read DATABASE_URL (cis_app) and connect with ssl=require (GT_DB_SSL=disable for a local test DB).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from services.ground_truth import claims as cl
from services.ground_truth import detectors as det
from services.ground_truth import importer
from services.ground_truth import mutations as mut
from services.ground_truth import panel as pn
from services.ground_truth import policy
from services.ground_truth import sample_size as ss
from services.ground_truth import split_plan as sp
from services.ground_truth import store
from scripts.aa1048.outpath import OutRefused, check_out


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path: str, text: str) -> None:
    with open(check_out(path), "w", encoding="utf-8") as f:
        f.write(text)


def _jsonl(rows: Iterable[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows)


async def _connect():
    import asyncpg

    from shared.secrets import get_database_url

    ssl: Any = None if os.environ.get("GT_DB_SSL", "require") == "disable" else "require"
    return await asyncpg.connect(get_database_url().strip(), ssl=ssl)


# ── fixture export (scripts side: it reads the AA-729 panel view) ──────────────────────────────────────────────


# Writers whose objects come several per tour (S1 Master versions carry refs.tour_id by construction): their split
# group must be the tour, so an export line without one is refused (review S272: a T9 blog and social piece of one tour
# landed on different sides).
TOUR_KEY_WRITERS = ("t2", "t9_blog", "t9_social")
_TOUR_KEYS = ("tour_id", "trip_id")
_MAP_KEYS = ("piece_id", "request_id", "version_id", "sample_id")


def tour_key(sample: dict, tour_map: Optional[dict] = None) -> Optional[str]:
    """The tour of a fixture sample: refs.tour_id / trip_id, else the frozen input's (input or input.context), else
    the first of its piece / request / version / sample ids found in `tour_map`. None when none is known."""
    refs = sample.get("refs") or {}
    inp = sample.get("input") if isinstance(sample.get("input"), dict) else {}
    ctx = inp.get("context") if isinstance(inp.get("context"), dict) else {}
    for src in (refs, inp, ctx):
        for k in _TOUR_KEYS:
            v = src.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
    for k in _MAP_KEYS:
        for src in (refs, inp, sample):
            v = src.get(k)
            if v is not None and str(v).strip() and tour_map and tour_map.get(str(v).strip()):
                return str(tour_map[str(v).strip()]).strip()
    return None


def export_fixture(fixture: dict, *, set_id: str, writers: Optional[Iterable[str]] = None,
                   tour_map: Optional[dict] = None) -> list[dict]:
    """Export lines for the non-trap samples of an AA-729 golden fixture (their frozen outputs, as the AA-729 panel
    sees them: no gate ledger or flags), each with the full writer source (labels._source_text, never cut) and its
    tour in refs.tour_id (tour_key; ValueError for a T2 / T9 sample without one). Without `writers` a named set
    exports the writers of its surface (claims.set_writers: CL-<n> T9, L<n> S1, t2@<n> T2; any other set id every
    writer); a writer outside that surface, or writers of two surfaces in one set, is a ValueError (a set never
    pools surfaces, AA-1048 Scope). A claim is exported once: a second
    item with the same text (same claim hash) adds no line (`cli rederive` maps labels back to every item by content
    sha)."""
    from scripts.aa729.labels import _source_text, panel_output
    from services.golden_set.derive import FixtureError, writer_and_channel

    if writers:
        problems = cl.set_writer_problems(set_id, writers)
        if problems:
            raise ValueError(problems[0])
        wanted: Optional[set] = set(writers)
    else:
        named = cl.set_writers(set_id)
        wanted = set(named) if named is not None else None
    lines: list[dict] = []
    seen: set[str] = set()
    for s in fixture.get("samples") or []:
        if str(s.get("kind", "")).startswith("injection") or not s.get("output"):
            continue
        try:
            writer, _channel = writer_and_channel(s)
        except FixtureError:
            continue
        if wanted is not None and writer not in wanted:
            continue
        refs = s.get("refs") or {}
        tenant = None if writer == "a1" else (str(refs.get("tenant_id") or "") or None)
        stratum = "hard" if {"held", "known_failure"} & set(s.get("tags") or []) else "representative"
        tour = tour_key(s, tour_map)
        if tour is None and writer in TOUR_KEY_WRITERS:
            raise ValueError(f"sample {s.get('sample_id')!r} ({writer}) has no tour key (refs.tour_id): its split "
                             "would fall back to the item and leak sentences of one tour across train / held_out; "
                             "re-freeze it (select_samples keeps refs.tour_id) or pass --tour-map")
        new = pn.export_records(
            set_id=set_id, writer=writer, scope="platform" if writer == "a1" else "tenant", tenant_id=tenant,
            refs={"sample_id": s["sample_id"], "piece_id": refs.get("piece_id"), "version_id": refs.get("version_id"),
                  "tour_id": tour},
            output=panel_output(s.get("output")), source_text=_source_text(s), stratum=stratum)
        lines += [ln for ln in new if ln["claim_hash"] not in seen]
        seen.update(ln["claim_hash"] for ln in new)
    return lines


def _finals_to_jsonl(finals: dict[str, pn.FinalLabel]) -> list[dict]:
    return importer.finals_rows(finals)


def _finals_from_jsonl(rows: list[dict]) -> dict[str, pn.FinalLabel]:
    return {r["claim_hash"]: pn.FinalLabel(r["claim_hash"], r["claim_class"], r["label"], r.get("source") or "file",
                                           r.get("writer") or "", r.get("content_sha256") or "",
                                           r.get("claim_text") or "", r.get("sample_id"),
                                           r.get("stratum") or "representative", r.get("split") or "train")
            for r in rows}


def claim_inputs(export_lines: list[dict]) -> list[det.ClaimInput]:
    """Detector inputs from export lines. `scope_name`, `recorded` (producer -> fired, critic F10) and
    `recorded_classes` (producer -> the classes it checks; without it the producer is never measured as enforce) are
    read when the export line carries them (added by whoever exported the producers' stored verdicts)."""
    return [det.ClaimInput(e["claim_hash"], e["writer"], e["field"], e["claim_text"], e.get("source_text") or "",
                           scope_name=e.get("scope_name"), country=e.get("country"),
                           recorded=dict(e.get("recorded") or {}),
                           recorded_classes={str(k): tuple(v or ()) for k, v in
                                             dict(e.get("recorded_classes") or {}).items()},
                           split=cl.split_of(cl.split_group(e.get("refs"), e["content_sha256"])))
            for e in export_lines]


def agreement_map(obj: Any) -> dict[str, Optional[float]]:
    """class -> labeller agreement from `cli report` output (whole, its result, or its agreement part) or a plain
    {class: agreement} map."""
    if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
        obj = obj["result"]
    if isinstance(obj, dict) and isinstance(obj.get("agreement"), dict):
        obj = obj["agreement"]
    if isinstance(obj, dict) and isinstance(obj.get("classes"), dict):
        return {k: (v.get("agreement") if isinstance(v, dict) else None) for k, v in obj["classes"].items()}
    if isinstance(obj, dict) and all(isinstance(v, (int, float)) or v is None for v in obj.values()):
        return {str(k): (float(v) if v is not None else None) for k, v in obj.items()}
    raise ValueError("the agreement file is neither `cli report` output nor a {class: agreement} map")


# ── commands ───────────────────────────────────────────────────────────────────────────────────────────────────


def cmd_export_fixture(a) -> dict:
    fixture = json.loads(_read(a.fixture))
    tour_map = json.loads(_read(a.tour_map)) if a.tour_map else None
    if tour_map is not None and not isinstance(tour_map, dict):
        raise ValueError("--tour-map is a JSON object {piece_id | request_id | version_id | sample_id: tour_id}")
    lines = export_fixture(fixture, set_id=a.set, writers=a.writers.split(",") if a.writers else None,
                           tour_map=tour_map)
    if a.split != "all":
        lines = [ln for ln in lines if ln["split"] == a.split]
    _write(a.out, _jsonl(lines))
    return {"claims": len(lines), "objects": len({ln["content_sha256"] for ln in lines}), "out": a.out,
            "split": a.split, "by_writer": {w: sum(1 for ln in lines if ln["writer"] == w) for w in cl.WRITERS},
            "held_out_by_writer": {w: sum(1 for ln in lines if ln["writer"] == w and ln["split"] == "held_out")
                                   for w in cl.WRITERS}}


def cmd_payload(a) -> dict:
    export = pn.read_jsonl(_read(a.export))
    panel = pn.read_jsonl(_read(a.panel))
    body = importer.build_payload(export, panel, set_id=a.set, set_version=a.version, dry_run=not a.apply)
    cons = importer.consolidate_payload(body["export"], body["judgements"], set_id=a.set, set_version=a.version)
    _write(a.out, json.dumps(body, ensure_ascii=False, sort_keys=True))
    return {"out": a.out, "claims": len(body["export"]), "judgements": len(body["judgements"]),
            "dry_run": body["dry_run"], "refs_dropped": body["refs_dropped"], "bytes": os.path.getsize(a.out),
            **importer.counts(cons.rows),
            "states": {s: sum(1 for v in cons.states.values() if v == s) for s in ("agreed", "disputed", "audit")}}


def live_from_json(obj: Any) -> list[sp.Candidate]:
    """Candidates from the answer of GET /admin/writing-memory/claim-labels/split-plan (or its `candidates` list)."""
    rows = obj.get("candidates") if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        raise ValueError("--live is the split-plan answer (an object with `candidates`) or its candidate list")
    return [sp.Candidate(str(r["id"]), str(r["writer"]), r.get("tour_id"), str(r.get("status") or ""))
            for r in rows]


def cmd_split_plan(a) -> dict:
    stats = sp.export_stats([ln for path in a.export for ln in pn.read_jsonl(_read(path))])
    target = sp.target_held_out_claims(violation_rate=a.violation_rate, observed_precision=a.observed_precision)
    if target is None:
        raise ValueError("the R1 bar is out of reach at this precision / violation rate")
    have = {w: row["held_out_claims"] for w, row in stats["writers"].items()}
    out: dict[str, Any] = {"export": stats["writers"], "target_held_out_claims_per_writer": target,
                           "violation_rate": a.violation_rate, "observed_precision": a.observed_precision,
                           "export_gap": {w: max(0, target - have.get(w, 0)) for w in cl.WRITERS}}
    if a.live:
        result = sp.plan(live_from_json(json.loads(_read(a.live))), target_claims=target, have=have,
                         exclude=stats["ids"], per_tour_cap=a.per_tour_cap)
        out["plan"] = result
        if a.ids_out:
            ids = sp.ids_file(result)
            _write(a.ids_out, json.dumps(ids, indent=1))
            out["ids_out"] = {"path": a.ids_out, "ids": len(ids)}
    return out


async def cmd_import(a, conn) -> dict:
    export = pn.read_jsonl(_read(a.export))
    problems = cl.set_writer_problems(a.set, (e.get("writer") for e in export))
    if problems:
        raise ValueError(problems[0])
    judgements = pn.parse_judgements(pn.read_jsonl(_read(a.panel)),
                                     export_by_hash={e["claim_hash"]: e for e in export})
    cons = pn.consolidate(export, judgements, set_id=a.set, set_version=a.version, policy_sha=policy.policy_sha())
    summary = await store.insert_rows(conn, cons.rows, apply=a.apply)
    states = {s: sum(1 for v in cons.states.values() if v == s) for s in ("agreed", "disputed", "audit")}
    return {**summary, "states": states, "problems": cons.problems, "policy_source": policy.POLICY_SOURCE}


async def cmd_report(a, conn) -> dict:
    rows = await store.read_rows(conn, a.set, a.version)
    finals = pn.final_labels(rows)
    by = {}
    for f in finals.values():
        by.setdefault(f.claim_class or "-", {}).setdefault(f.label, 0)
        by[f.claim_class or "-"][f.label] += 1
    return {"rows": len(rows), "claims": len(finals), "agreement": pn.agreement_report(rows),
            "contradictions": pn.contradictions(finals), "labels_by_class": by,
            "evidence_sources": {s: sum(1 for f in finals.values() if f.source == s)
                                 for s in ("tech_lead", "panel_agreed", "none")}}


async def cmd_manifest(a, conn) -> dict:
    return pn.manifest(await store.read_rows(conn, a.set, a.version), set_id=a.set, set_version=a.version)


async def cmd_enqueue(a, conn) -> dict:
    return await store.enqueue(conn, await store.read_rows(conn, a.set, a.version), today=date.today(),
                               apply=a.apply)


async def cmd_pull(a, conn) -> dict:
    return await store.pull_resolutions(conn, a.set, a.version, apply=a.apply, policy_sha=policy.policy_sha())


async def cmd_finals(a, conn) -> dict:
    finals = pn.release_evidence(pn.final_labels(await store.read_rows(conn, a.set, a.version)),
                                 audit_ok=a.audit_ok)
    _write(a.out, _jsonl(_finals_to_jsonl(finals)))
    return {"finals": len(finals), "out": a.out, "audit_ok": a.audit_ok}


def cmd_rederive(a) -> dict:
    from scripts.aa729.labels import panel_output

    fixture = json.loads(_read(a.fixture))
    shas = {s["sample_id"]: cl.content_sha(panel_output(s.get("output"))) for s in fixture.get("samples") or []
            if s.get("output")}
    return pn.rederive(fixture, _finals_from_jsonl(pn.read_jsonl(_read(a.finals))), content_sha_of=shas)


def cmd_detectors(a) -> dict:
    export = pn.read_jsonl(_read(a.export))
    finals = _finals_from_jsonl(pn.read_jsonl(_read(a.finals)))
    labels = {h: (f.claim_class, f.label) for h, f in finals.items() if f.label in ("violation", "ok")}
    inputs = claim_inputs([e for e in export if e["claim_hash"] in labels])
    detectors = det.by_name(a.only.split(",") if a.only else None) + det.recorded_detectors(inputs)
    agreement = agreement_map(json.loads(_read(a.agreement))) if a.agreement else None
    rows = det.measure(detectors, inputs, labels, label_set=a.label_set, pooled=not a.per_writer, split=a.split,
                       agreement=agreement)
    _write(a.out, json.dumps(rows, indent=1))
    if a.thresholds:
        import yaml

        _write(a.thresholds, yaml.safe_dump(det.thresholds_rows(rows), sort_keys=True))
    return {"rows": len(rows), "split": a.split, "claims": sum(1 for c in inputs if a.split in ("all", c.split)),
            "agreement_given": agreement is not None,
            "enforce": sorted(f"{r['detector']}:{r['class']}" for r in rows if r["mode"] == det.MODE_ENFORCE),
            "out": a.out}


def cmd_mutations(a) -> dict:
    report = mut.run_suite(mut.build_suite(n_texts=a.texts, seed=a.seed))
    if a.out:
        _write(a.out, json.dumps(report, indent=1))
    return {"summary": mut.summary_lines(report), "cases": report["cases"]}


def cmd_sample_size(a) -> Any:
    if a.rates:
        return ss.budget_rows(json.loads(_read(a.rates)), budget=a.budget)
    rows = ss.table()
    return ss.markdown(rows) if a.markdown else rows


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.aa1048.cli", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export-fixture")
    p.add_argument("fixture")
    p.add_argument("--set", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--writers")
    p.add_argument("--tour-map", help="JSON {piece_id | request_id | version_id | sample_id: tour_id}")
    p.add_argument("--split", choices=("all", "held_out", "train"), default="all",
                   help="keep only the lines of one side of the frozen split (default: all)")
    p = sub.add_parser("payload")
    p.add_argument("panel")
    p.add_argument("--export", required=True)
    p.add_argument("--set", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--apply", action="store_true", help="the payload asks the API to write (default: DRY RUN)")
    p = sub.add_parser("split-plan")
    p.add_argument("--export", required=True, action="append",
                   help="a claim export; repeat it for the exports of every set (CL-<n>, L<n>, t2@<n>)")
    p.add_argument("--live", help="the answer of GET /admin/writing-memory/claim-labels/split-plan")
    p.add_argument("--ids-out", help="write the picks as a select_samples --ids list (needs --live)")
    p.add_argument("--violation-rate", type=float, default=sp.PANEL1_VIOLATION_RATE)
    p.add_argument("--observed-precision", type=float, default=sp.DEFAULT_OBSERVED_PRECISION)
    p.add_argument("--per-tour-cap", type=int, default=sp.DEFAULT_PER_TOUR_CAP)
    for name in ("import", "report", "manifest", "enqueue", "pull-resolutions", "finals"):
        p = sub.add_parser(name)
        if name == "import":
            p.add_argument("panel")
            p.add_argument("--export", required=True)
        p.add_argument("--set", required=True)
        p.add_argument("--version", required=True)
        if name in ("import", "enqueue", "pull-resolutions"):
            p.add_argument("--apply", action="store_true", help="write (default: DRY RUN)")
        if name == "finals":
            p.add_argument("--out", required=True)
            p.add_argument("--audit-ok", action="store_true",
                           help="agreed panel labels count too (the audit draw was adjudicated and accepted)")
    p = sub.add_parser("rederive")
    p.add_argument("fixture")
    p.add_argument("--finals", required=True)
    p = sub.add_parser("detectors")
    p.add_argument("--export", required=True)
    p.add_argument("--finals", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--thresholds")
    p.add_argument("--only", help="comma-separated detector names")
    p.add_argument("--label-set", default="CL-1")
    p.add_argument("--per-writer", action="store_true")
    p.add_argument("--split", choices=det.SPLIT_CHOICES, default="held_out",
                   help="held_out (default: the only split that may pass), train (exploration, rates) or all")
    p.add_argument("--agreement", help="`cli report` output (labeller agreement per class); without it nothing passes")
    p = sub.add_parser("mutations")
    p.add_argument("--texts", type=int, default=mut.DEFAULT_TEXTS)
    p.add_argument("--seed", type=int, default=1048)
    p.add_argument("--out")
    p = sub.add_parser("sample-size")
    p.add_argument("--markdown", action="store_true")
    p.add_argument("--rates")
    p.add_argument("--budget", type=int)
    return ap


DB_COMMANDS = {"import": cmd_import, "report": cmd_report, "manifest": cmd_manifest, "enqueue": cmd_enqueue,
               "pull-resolutions": cmd_pull, "finals": cmd_finals}
LOCAL_COMMANDS = {"export-fixture": cmd_export_fixture, "rederive": cmd_rederive, "detectors": cmd_detectors,
                  "mutations": cmd_mutations, "sample-size": cmd_sample_size, "payload": cmd_payload,
                  "split-plan": cmd_split_plan}


def main(argv: Optional[list[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    from services.golden_set import llm_guard

    llm_guard.install("aa1048_ground_truth")
    try:
        if a.cmd in LOCAL_COMMANDS:
            out = LOCAL_COMMANDS[a.cmd](a)
        else:
            async def _go():
                conn = await _connect()
                try:
                    return await DB_COMMANDS[a.cmd](a, conn)
                finally:
                    await conn.close()
            out = asyncio.run(_go())
    except (pn.PanelError, store.StoreRefused, OutRefused, ValueError) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    if isinstance(out, str):
        sys.stdout.write(out)
    else:
        print(json.dumps({"ok": True, "cmd": a.cmd, "at": datetime.now(timezone.utc).isoformat(), "result": out},
                         indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
