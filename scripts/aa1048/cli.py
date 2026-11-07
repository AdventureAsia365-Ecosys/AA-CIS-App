#!/usr/bin/env python3
"""AA-1048 (Quality W2) — the claim-label tooling, $0. Every DB write is DRY RUN unless --apply; no model is called
(the gateway entry points are refused for the whole process, services/golden_set/llm_guard).

  export-fixture FIXTURE --set CL-1 --out claims.jsonl [--writers t9_blog,t9_social]
      Claim lines (JSONL keyed by claim hash) of the frozen outputs of an AA-729 golden fixture, for the 3-lens panel
      (docs/calibration/aa1048-claim-panel-brief.md). Real text: write to the gitignored scratch folder only.
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
      The deterministic detectors, tripwires and recorded producers measured on the labels.
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
from services.ground_truth import mutations as mut
from services.ground_truth import panel as pn
from services.ground_truth import policy
from services.ground_truth import sample_size as ss
from services.ground_truth import store


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _jsonl(rows: Iterable[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows)


async def _connect():
    import asyncpg

    from shared.secrets import get_database_url

    ssl: Any = None if os.environ.get("GT_DB_SSL", "require") == "disable" else "require"
    return await asyncpg.connect(get_database_url().strip(), ssl=ssl)


# ── fixture export (scripts side: it reads the AA-729 panel view) ──────────────────────────────────────────────


def export_fixture(fixture: dict, *, set_id: str, writers: Optional[Iterable[str]] = None) -> list[dict]:
    """Export lines for the non-trap samples of an AA-729 golden fixture (their frozen outputs, as the AA-729 panel
    sees them: no gate ledger or flags), each with the full writer source (labels._source_text, never cut). A claim
    is exported once: a second item with the same text (same claim hash) adds no line (`cli rederive` maps labels
    back to every item by content sha)."""
    from scripts.aa729.labels import _source_text, panel_output
    from services.golden_set.derive import FixtureError, writer_and_channel

    wanted = set(writers) if writers else None
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
        new = pn.export_records(
            set_id=set_id, writer=writer, scope="platform" if writer == "a1" else "tenant", tenant_id=tenant,
            refs={"sample_id": s["sample_id"], "piece_id": refs.get("piece_id"), "version_id": refs.get("version_id"),
                  "tour_id": refs.get("tour_id")},
            output=panel_output(s.get("output")), source_text=_source_text(s), stratum=stratum)
        lines += [ln for ln in new if ln["claim_hash"] not in seen]
        seen.update(ln["claim_hash"] for ln in new)
    return lines


def _finals_to_jsonl(finals: dict[str, pn.FinalLabel]) -> list[dict]:
    return [{"claim_hash": f.claim_hash, "claim_class": f.claim_class, "label": f.label, "source": f.source,
             "writer": f.writer, "sample_id": f.sample_id, "stratum": f.stratum, "split": f.split,
             "content_sha256": f.content_sha256, "claim_text": f.claim_text} for f in finals.values()]


def _finals_from_jsonl(rows: list[dict]) -> dict[str, pn.FinalLabel]:
    return {r["claim_hash"]: pn.FinalLabel(r["claim_hash"], r["claim_class"], r["label"], r.get("source") or "file",
                                           r.get("writer") or "", r.get("content_sha256") or "",
                                           r.get("claim_text") or "", r.get("sample_id"),
                                           r.get("stratum") or "representative", r.get("split") or "train")
            for r in rows}


def claim_inputs(export_lines: list[dict]) -> list[det.ClaimInput]:
    """Detector inputs from export lines. `scope_name` and `recorded` (producer -> fired, critic F10) are read when
    the export line carries them (added by whoever exported the producers' stored verdicts)."""
    return [det.ClaimInput(e["claim_hash"], e["writer"], e["field"], e["claim_text"], e.get("source_text") or "",
                           scope_name=e.get("scope_name"), country=e.get("country"),
                           recorded=dict(e.get("recorded") or {})) for e in export_lines]


# ── commands ───────────────────────────────────────────────────────────────────────────────────────────────────


def cmd_export_fixture(a) -> dict:
    fixture = json.loads(_read(a.fixture))
    lines = export_fixture(fixture, set_id=a.set, writers=a.writers.split(",") if a.writers else None)
    _write(a.out, _jsonl(lines))
    return {"claims": len(lines), "objects": len({ln["content_sha256"] for ln in lines}), "out": a.out,
            "by_writer": {w: sum(1 for ln in lines if ln["writer"] == w) for w in cl.WRITERS}}


async def cmd_import(a, conn) -> dict:
    export = pn.read_jsonl(_read(a.export))
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
    rows = det.measure(detectors, inputs, labels, label_set=a.label_set, pooled=not a.per_writer)
    _write(a.out, json.dumps(rows, indent=1))
    if a.thresholds:
        import yaml

        _write(a.thresholds, yaml.safe_dump(det.thresholds_rows(rows), sort_keys=True))
    return {"rows": len(rows), "enforce": sorted(f"{r['detector']}:{r['class']}" for r in rows
                                                 if r["mode"] == det.MODE_ENFORCE), "out": a.out}


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
                  "mutations": cmd_mutations, "sample-size": cmd_sample_size}


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
    except (pn.PanelError, store.StoreRefused, ValueError) as e:
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
