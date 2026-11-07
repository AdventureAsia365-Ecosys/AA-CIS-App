"""AA-1049 W3 (root ADR 0020, eval item R0b) — free offline T9 replay with a declared-diff report.

No LLM, no DB, no network. It runs over stored T9 pieces: the golden fixture (root docs/eval/aa729/fixture-<v>.json,
the T9 samples) and/or a read-only export of acp_shared.content_piece rows with their repair_log. The stored gate
verdicts are the cassette: the F9 judge is never called; a piece whose F9 verdict is missing or was an outage is a
cassette MISS and is reported as "needs rejudge", never as a pass. An SEO-fit model call is a cassette miss too
(`fit_seo_fields` raises): it is counted, never answered.

Per piece it records, through the code of the checkout it runs in:

- input: the production input builder (piece_store.production_input, or service._grounding_text on a checkout
  without the seam) and its sha;
- route: sentence_repair.plan over the stored ledger (gates + sentences), hold_failure, never_holds, the style-only
  repair choice, the cut gates;
- seo (blog): the SEO-fit calls _fit_seo_meta would make and the fields it would keep;
- guard (pieces with a repair_log): the AA-528 decision for each consecutive pair of attempt ledgers;
- contract: the REPORT-mode final contract over the stored text and the dry store's stamp (stored sha == gated sha).

Two runs make the diff: one on a main checkout (`--out base.json`), one on the branch (`--base base.json`). The
script itself only needs the modules of the checkout it runs in, so the branch copy of this file runs against main:

    PYTHONPATH=<main checkout> python <branch>/scripts/quality/replay_aa1049_t9.py --golden <fixture> --out base.json
    PYTHONPATH=<branch>        python <branch>/scripts/quality/replay_aa1049_t9.py --golden <fixture> \\
        --base base.json --out branch.json --report report.md

The report classifies every difference as a DECLARED diff (DECLARED below: one id per change this train makes on
purpose) or UNDECLARED. Exit code 1 when any difference is undeclared or a dry-store sha mismatches.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

SCHEMA = "aa1049-t9-replay/1"
DECLARED = {
    "D-seo-1": "a title/meta fixed by a whole-sentence / separator prefix of the writer's value: 0 SEO-fit calls",
    "D-seo-2": "at most one SEO-fit call per attempt (the retry call is gone)",
    "D-guard-1": "regression guard on stable keys: a same-gate swap of flagged sentences is reverted",
    "D-guard-2": "regression guard on stable keys: clearing more findings than it adds is kept",
}


class CassetteMiss(RuntimeError):
    """A model call the replay has no recorded answer for (never counted as a pass)."""


def _read_json(path: str) -> Any:
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def load_golden(path: str) -> list[dict]:
    out = []
    for s in _read_json(path).get("samples") or []:
        if s.get("kind") != "t9":
            continue
        o = s.get("output") or {}
        ctx = (s.get("input") or {}).get("context") or {}
        out.append({"id": s["sample_id"], "channel": s.get("channel") or ctx.get("channel"), "context": ctx,
                    "content_text": o.get("content_text") or "", "seo_title": o.get("seo_title"),
                    "meta_description": o.get("meta_description"), "slug": o.get("slug"),
                    "gate_ledger": _json(o.get("gate_ledger")) or [], "repair_log": None,
                    "status": (s.get("pipeline_verdict") or {}).get("status")})
    return out


def load_pieces(path: str) -> list[dict]:
    out = []
    for r in _read_json(path) or []:
        wi = _json(r.get("writer_input")) or {}
        out.append({"id": str(r.get("piece_id") or r.get("id")), "channel": r.get("channel") or wi.get("channel"),
                    "context": wi.get("context") or {}, "writer_input": wi,
                    "content_text": r.get("content_text") or "", "seo_title": r.get("seo_title"),
                    "meta_description": r.get("meta_description"), "slug": r.get("slug"),
                    "gate_ledger": _json(r.get("gate_ledger")) or [], "repair_log": _json(r.get("repair_log")) or [],
                    "status": r.get("status")})
    return out


# ------------------------------------------------------------------------------------------------------- sections

def _input(p: dict) -> dict:
    try:
        from services.acp_content_writing import piece_store
        return {k: v for k, v in piece_store.production_input(p["context"]).items() if k != "grounding_text"}
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 — an unbuildable input is reported, not fatal
        return {"error": type(e).__name__}
    try:
        import hashlib
        from services.acp_content_writing import service
        text = service._grounding_text(p["context"])
        return {"grounding_sha": hashlib.sha256(text.encode()).hexdigest()[:16]}
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}


def _route(p: dict) -> dict:
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import sentence_repair, service
    ledger = p["gate_ledger"]
    plan = sentence_repair.plan(p["content_text"], ledger)
    hold = qg.hold_failure(ledger)
    failed = [g for g in qg.expand_ledger(ledger) if not g.get("passed")]
    return {
        "plan": None if plan is None else [[it.get("gate"), it.get("sentence", "")[:120]] for it in plan],
        "hold_failure": hold["gate"] if hold else None,
        "never_holds": {g["gate"]: qg.never_holds(g) for g in failed},
        "style_only_repair": service._style_only_repair(ledger),
        "truth_cut_gates": sorted(service.TRUTH_CUT_GATES), "cut_finding_gate": service.F17_CUT_GATE,
    }


def _cassette_hit(p: dict) -> bool:
    f9 = [g for g in p["gate_ledger"] if isinstance(g, dict) and g.get("gate") == "F9_cta_fact"]
    return bool(f9) and not any(g.get("degraded") or any("judge unavailable" in str(v) for v in g.get("violations")
                                                            or []) for g in f9)


def _seo(p: dict) -> Optional[dict]:
    if p["channel"] != "blog":
        return None
    from services.acp_content_writing import service
    calls: list[int] = []

    def miss(**_kw):
        calls.append(1)
        raise CassetteMiss("t9_repair SEO fit")

    seo = {"seo_title": p["seo_title"], "meta_description": p["meta_description"], "slug": p["slug"]}
    ctx = dict(p["context"])
    with patch.object(service, "fit_seo_fields", side_effect=miss):
        out, _ = asyncio.run(service._fit_seo_meta(seo, ctx.get("keyword"), body=p["content_text"], context=ctx,
                                                   request_id="replay", hyg=None, taken_titles=[]))
    return {"fit_calls": len(calls), "seo_title": out.get("seo_title"), "meta_description": out.get("meta_description"),
            "slug": out.get("slug")}


def _guard(p: dict) -> Optional[list]:
    if not p.get("repair_log"):
        return None
    from services.acp_content_writing import service
    ledgers = [e.get("gate_ledger") for e in p["repair_log"] if isinstance(e, dict) and e.get("gate_ledger")]
    ledgers.append(p["gate_ledger"])
    out = []
    for prev, cur in zip(ledgers, ledgers[1:]):
        pb, cb = service._regression_failures(prev), service._regression_failures(cur)
        by_names = bool(cb - pb) and len(cb) >= len(pb)
        try:
            from services.acp_content_writing import stable_keys
            if service.regression_guard_mode() == service.GUARD_STABLE_KEYS:
                out.append(stable_keys.regressed(prev, cur)[1])
                continue
        except (ImportError, AttributeError):
            pass
        out.append(by_names)
    return out


def _contract(p: dict) -> dict:
    from services.acp_content_writing import service
    from services.quality import final_contract as fc
    rec = service._t9_final_contract(
        p["content_text"], channel=p["channel"], cta=p["context"].get("cta"),
        seo_meta={"seo_title": p["seo_title"], "meta_description": p["meta_description"], "slug": p["slug"]},
        gate_ledger=p["gate_ledger"], held_reason=None, status=p.get("status") or "approved")
    try:
        from services.acp_content_writing import piece_store
        store = piece_store.DryStore()
        row = asyncio.run(store.finalize(None, content_text=p["content_text"], status=p.get("status"),
                                         final_contract=rec))
        stamped = row["final_contract"] or {}
    except ImportError:
        stamped = fc.stamp_stored(rec, fc.text_sha(p["content_text"])) or {}
    return {"verdict": (rec or {}).get("verdict"), "findings": sorted(str(f.get("code") if isinstance(f, dict) else f)
                                                                   for f in (rec or {}).get("findings") or []),
            "sha_match": stamped.get("sha_match")}


def replay(pieces: list[dict]) -> dict:
    rows = {}
    for p in pieces:
        rows[p["id"]] = {"channel": p["channel"], "cassette_hit": _cassette_hit(p), "input": _input(p),
                         "route": _route(p), "seo": _seo(p), "guard": _guard(p), "contract": _contract(p)}
    return {"schema": SCHEMA, "rows": rows}


# ---------------------------------------------------------------------------------------------------------- diff

def _classify(section: str, base: Any, cur: Any) -> tuple[str, str]:
    """(declared id or 'UNDECLARED', note) for one differing section of one piece."""
    if section == "seo" and base and cur:
        same_out = all(base.get(k) == cur.get(k) for k in ("seo_title", "meta_description", "slug"))
        if cur["fit_calls"] < base["fit_calls"]:
            if cur["fit_calls"] == 0:
                return "D-seo-1", "fixed in code, no fit call"
            return "D-seo-2", "fewer fit calls"
        if same_out:
            return "UNDECLARED", "same fields, more fit calls"
        return "UNDECLARED", "different SEO fields"
    if section == "guard" and base is not None and cur is not None and len(base) == len(cur):
        flips = [(b, c) for b, c in zip(base, cur) if b != c]
        if flips and all(c and not b for b, c in flips):
            return "D-guard-1", "stable keys revert a swap"
        if flips and all(b and not c for b, c in flips):
            return "D-guard-2", "stable keys keep a net-better rewrite"
        return "D-guard-1/2", "both directions"
    return "UNDECLARED", f"{section} changed"


def diff(base: dict, cur: dict) -> dict:
    out: list[dict] = []
    missing = sorted(set(base["rows"]) ^ set(cur["rows"]))
    for pid in sorted(set(base["rows"]) & set(cur["rows"])):
        b, c = base["rows"][pid], cur["rows"][pid]
        for section in ("input", "route", "seo", "guard", "contract"):
            if section == "input" and "fs_sha" not in (b.get("input") or {}):
                b_in = {k: v for k, v in (c.get("input") or {}).items() if k in (b.get("input") or {})}
                if b_in == b.get("input"):
                    continue
            if b.get(section) != c.get(section):
                did, note = _classify(section, b.get(section), c.get(section))
                out.append({"piece": pid, "section": section, "declared": did, "note": note})
    return {"diffs": out, "missing": missing}


def summary(cur: dict, d: Optional[dict]) -> dict:
    rows = cur["rows"].values()
    s = {"pieces": len(cur["rows"]), "by_channel": dict(Counter(r["channel"] for r in rows)),
         "cassette_hits": sum(r["cassette_hit"] for r in rows),
         "cassette_misses_needs_rejudge": sum(not r["cassette_hit"] for r in rows),
         "input_errors": sum(1 for r in rows if "error" in (r["input"] or {})),
         "sha_match": sum(1 for r in rows if r["contract"]["sha_match"] is True),
         "sha_mismatch": sum(1 for r in rows if r["contract"]["sha_match"] is False),
         "seo_fit_calls": sum((r["seo"] or {}).get("fit_calls", 0) for r in rows),
         "guard_pairs": sum(len(r["guard"] or []) for r in rows)}
    if d is not None:
        s["declared"] = dict(Counter(x["declared"] for x in d["diffs"] if x["declared"] != "UNDECLARED"))
        s["undeclared"] = sum(1 for x in d["diffs"] if x["declared"] == "UNDECLARED")
        s["missing_pieces"] = len(d["missing"])
    return s


def report_md(s: dict, d: Optional[dict]) -> str:
    lines = ["# AA-1049 T9 replay — declared-diff report", "", f"schema `{SCHEMA}`", ""]
    lines += [f"- {k}: {v}" for k, v in s.items()]
    if d is not None:
        lines += ["", "## Differences", "", "| piece | section | declared | note |", "|---|---|---|---|"]
        lines += [f"| {x['piece']} | {x['section']} | {x['declared']} | {x['note']} |" for x in d["diffs"]] or ["|"]
        lines += ["", "## Declared diffs", ""] + [f"- **{k}** {v}" for k, v in DECLARED.items()]
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--golden", action="append", default=[])
    ap.add_argument("--pieces", action="append", default=[])
    ap.add_argument("--base", help="the replay JSON of the main checkout to diff against")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report")
    a = ap.parse_args(argv)
    pieces = [p for g in a.golden for p in load_golden(g)] + [p for f in a.pieces for p in load_pieces(f)]
    cur = replay(pieces)
    d = diff(_read_json(a.base), cur) if a.base else None
    s = summary(cur, d)
    Path(a.out).write_text(json.dumps({**cur, "summary": s, "diff": d}, indent=1, default=str), encoding="utf-8")
    if a.report:
        Path(a.report).write_text(report_md(s, d), encoding="utf-8")
    print(json.dumps(s, indent=1))
    bad = s.get("undeclared", 0) or s["sha_mismatch"] or s.get("missing_pieces", 0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
