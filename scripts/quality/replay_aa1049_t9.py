"""AA-1049 W3 (root ADR 0020, eval item R0b) — free offline T9 replay with a declared-diff report.

No LLM, no DB, no network. It runs over stored T9 pieces: the golden fixture (root docs/eval/aa729/fixture-<v>.json,
the T9 samples) and/or a read-only export of acp_shared.content_piece rows with their repair_log. The stored gate
verdicts are the cassette: the F9 judge is never called; a piece whose F9 verdict is missing or was an outage is a
cassette MISS and is reported as "needs rejudge", never as a pass. An SEO-fit model call is a cassette miss too
(`fit_seo_fields` raises): the piece is counted as a miss (never a hit) and its SEO fields are left out of the diff.

Per piece it records, through the code of the checkout it runs in:

- input: the production input builder (piece_store.production_input, or service._grounding_text on a checkout
  without the seam) and its sha;
- route: sentence_repair.plan over the stored ledger (gates + sentences), hold_failure, never_holds, the style-only
  repair choice, the cut gates;
- seo (blog): the SEO-fit calls _fit_seo_meta would make and the fields it would keep (a call = an SEO miss);
- guard (pieces with a repair_log): the AA-528 decision for each consecutive pair of attempt ledgers, with the
  stable-key evidence (keys added / removed, key counts, gate sets) when the checkout has stable_keys;
- contract: the REPORT-mode final contract over the stored text; for a content_piece export with a stored contract
  (writer_input.final_contract), the gated sha vs the sha _finalize_piece stored (a mismatch = a post-gate edit in
  production) and vs the row's text now (a later tenant edit, reported only). Golden samples carry no stored
  contract: sha "n/a". The replay never re-runs the post-gate tail of run_write_background; the post-gate-edit
  guard of the code itself is tests/unit/quality/test_chain_order.py (+ the dry-store seam unit test).

Two runs make the diff: one on a main checkout (`--out base.json`), one on the branch (`--base base.json`). The
script itself only needs the modules of the checkout it runs in, so the branch copy of this file runs against main:

    PYTHONPATH=<main checkout> python <branch>/scripts/quality/replay_aa1049_t9.py --golden <fixture> --out base.json
    PYTHONPATH=<branch>        python <branch>/scripts/quality/replay_aa1049_t9.py --golden <fixture> \\
        --base base.json --out branch.json --report report.md

The report classifies every difference as a DECLARED diff (DECLARED below: one id per change this train makes on
purpose, checked against the declared SHAPE, not just the direction) or UNDECLARED. Exit code 1 when any difference
is undeclared, a piece is missing, or a stored contract shows a post-gate edit.
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
    "D-seo-2": "at most one SEO-fit call per attempt (the retry call is gone); unit tests only: a replayed fit call is "
               "a cassette miss that aborts before main's retry, so the replay can never show it",
    "D-guard-1": "regression guard on stable keys: a same-gate swap of flagged sentences is reverted",
    "D-guard-2": "regression guard on stable keys: clearing more findings than it adds is kept",
}
SEO_FIELDS = ("seo_title", "meta_description", "slug")
NOT_DECLARED = {
    "MISS": "a section with no recorded answer on either side (an SEO-fit cassette miss): not compared",
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
    return {"fit_calls": len(calls), "miss": bool(calls), **{k: out.get(k) for k in SEO_FIELDS},
            "writer": {k: p[k] for k in SEO_FIELDS}}


def _guard(p: dict) -> Optional[list]:
    """One entry per consecutive pair of attempt ledgers: {"revert": the guard's decision in this checkout, "keys": the
    stable-key evidence (None on a checkout without stable_keys or in gate_names mode)}."""
    if not p.get("repair_log"):
        return None
    from services.acp_content_writing import service
    ledgers = [e.get("gate_ledger") for e in p["repair_log"] if isinstance(e, dict) and e.get("gate_ledger")]
    ledgers.append(p["gate_ledger"])
    try:
        from services.acp_content_writing import stable_keys
        keys_mode = service.regression_guard_mode() == service.GUARD_STABLE_KEYS
    except (ImportError, AttributeError):
        stable_keys, keys_mode = None, False
    out = []
    for prev, cur in zip(ledgers, ledgers[1:]):
        if keys_mode:
            c = stable_keys.compare(prev, cur)
            out.append({"revert": c["revert"], "keys": {
                "new": sorted(map(list, c["new"])), "removed": sorted(map(list, c["removed"])),
                "prev_keys": c["prev_keys"], "cur_keys": c["cur_keys"],
                "prev_gates": sorted(c["prev_gates"]), "cur_gates": sorted(c["cur_gates"])}})
        else:
            pb, cb = service._regression_failures(prev), service._regression_failures(cur)
            out.append({"revert": bool(cb - pb) and len(cb) >= len(pb), "keys": None})
    return out


def _stored_sha(p: dict) -> dict:
    """The stored contract's shas of a content_piece export row: gated (text_sha, written at gate time), stored (what
    _finalize_piece received) and now (the row's text). No stored contract (a golden sample, a pre-W1 piece): n/a."""
    from services.quality import final_contract as fc
    rec = (p.get("writer_input") or {}).get("final_contract")
    if not isinstance(rec, dict) or not rec.get("text_sha"):
        return {"sha": "n/a"}
    gated, now = rec["text_sha"], fc.text_sha(p["content_text"])
    stored = rec.get("stored_sha") or now
    return {"sha": "match" if gated == stored else "post_gate_edit", "edited_since_store": stored != now}


def _contract(p: dict) -> dict:
    from services.acp_content_writing import service
    rec = service._t9_final_contract(
        p["content_text"], channel=p["channel"], cta=p["context"].get("cta"),
        seo_meta={k: p[k] for k in SEO_FIELDS}, gate_ledger=p["gate_ledger"], held_reason=None,
        status=p.get("status") or "approved")
    return {"verdict": (rec or {}).get("verdict"), "findings": sorted(str(f.get("code") if isinstance(f, dict) else f)
                                                                   for f in (rec or {}).get("findings") or [])}


def replay(pieces: list[dict]) -> dict:
    rows = {}
    for p in pieces:
        seo = _seo(p)
        f9_hit = _cassette_hit(p)
        rows[p["id"]] = {"channel": p["channel"], "f9_hit": f9_hit, "seo_miss": bool(seo and seo["miss"]),
                         "cassette_hit": f9_hit and not (seo and seo["miss"]), "input": _input(p),
                         "route": _route(p), "seo": seo, "guard": _guard(p), "contract": _contract(p),
                         "stored": _stored_sha(p)}
    return {"schema": SCHEMA, "rows": rows}


# ---------------------------------------------------------------------------------------------------------- diff

def _seo_prefix(writer: Optional[str], value: Optional[str]) -> bool:
    """D-seo-1's shape: the value is the writer's own value or a non-empty prefix of it (whole sentences / the part
    before a separator: seo_builder never adds a character)."""
    return value == writer or bool(value and writer and writer.startswith(value.rstrip()))


def _classify_seo(base: dict, cur: dict) -> tuple[str, str]:
    if base.get("miss") and cur.get("miss"):
        return "MISS", "SEO-fit cassette miss on both sides: not compared"
    if cur.get("fit_calls", 0) == 0 and base.get("fit_calls", 0) > 0:
        writer = cur.get("writer") or base.get("writer") or {}
        if cur.get("slug") != base.get("slug"):
            return "UNDECLARED", "slug changed"
        changed = [k for k in ("seo_title", "meta_description") if cur.get(k) != writer.get(k)]
        if all(_seo_prefix(writer.get(k), cur.get(k)) for k in changed):
            return "D-seo-1", "fixed in code by a prefix of the writer's value, no fit call"
        return "UNDECLARED", "0 fit calls but a field is not a prefix of the writer's value"
    if cur.get("miss") or base.get("miss"):
        return "UNDECLARED", "a fit call on one side only (not the D-seo-1 shape)"
    return "UNDECLARED", "different SEO fields"


def _classify_guard_pair(base: dict, cur: dict) -> tuple[str, str]:
    """One flipped guard decision. D-guard-1 (branch reverts, main kept): the new keys are all in gates the previous
    attempt already failed and the gate set is unchanged (a same-gate swap). D-guard-2 (branch keeps, main reverted):
    the rewrite has fewer keys. Anything else, or no stable-key evidence, is UNDECLARED."""
    k = cur.get("keys")
    if not k:
        return "UNDECLARED", "guard flipped without stable-key evidence"
    prev_g, cur_g = set(k["prev_gates"]), set(k["cur_gates"])
    new_g = {g for g, _ in k["new"]}
    if cur["revert"] and not base["revert"]:
        if k["new"] and new_g <= prev_g and cur_g == prev_g:
            return "D-guard-1", "stable keys revert a same-gate swap"
        return "UNDECLARED", (f"revert not a same-gate swap (new gates {sorted(new_g - prev_g)}, gates "
                              f"{len(prev_g)}->{len(cur_g)})")
    if base["revert"] and not cur["revert"]:
        if k["cur_keys"] < k["prev_keys"]:
            return "D-guard-2", "stable keys keep a rewrite with fewer findings"
        return "UNDECLARED", f"keep without fewer keys ({k['prev_keys']}->{k['cur_keys']})"
    return "UNDECLARED", "guard changed"


def _revert(x: Any) -> Any:
    return x.get("revert") if isinstance(x, dict) else x


def _classify(section: str, base: Any, cur: Any) -> tuple[str, str]:
    """(declared id or 'UNDECLARED' / 'MISS', note) for one differing section (guard: one attempt pair) of a piece."""
    if section == "seo" and base and cur:
        return _classify_seo(base, cur)
    if section == "guard" and isinstance(base, dict) and isinstance(cur, dict):
        return _classify_guard_pair(base, cur)
    return "UNDECLARED", f"{section} changed"


def diff(base: dict, cur: dict) -> dict:
    out: list[dict] = []
    missing = sorted(set(base["rows"]) ^ set(cur["rows"]))
    for pid in sorted(set(base["rows"]) & set(cur["rows"])):
        b, c = base["rows"][pid], cur["rows"][pid]
        for section in ("input", "route", "seo", "guard", "contract"):
            bs, cs = b.get(section), c.get(section)
            if section == "input" and "fs_sha" not in (bs or {}):
                b_in = {k: v for k, v in (cs or {}).items() if k in (bs or {})}
                if b_in == bs:
                    continue
            if section == "seo" and bs and cs:
                bs = {k: bs.get(k) for k in ("fit_calls", "miss", *SEO_FIELDS)}
                cs = {k: cs.get(k) for k in ("fit_calls", "miss", *SEO_FIELDS)}
                if bs != cs or (bs["miss"] and cs["miss"]):
                    did, note = _classify_seo(b["seo"], c["seo"])
                    out.append({"piece": pid, "section": section, "declared": did, "note": note})
                continue
            if section == "guard" and isinstance(bs, list) and isinstance(cs, list) and len(bs) == len(cs):
                for i, (bp, cp) in enumerate(zip(bs, cs)):
                    if _revert(bp) != _revert(cp):
                        bp = bp if isinstance(bp, dict) else {"revert": bp, "keys": None}
                        cp = cp if isinstance(cp, dict) else {"revert": cp, "keys": None}
                        did, note = _classify_guard_pair(bp, cp)
                        out.append({"piece": pid, "section": f"guard[{i}]", "declared": did, "note": note})
                continue
            if bs != cs:
                did, note = _classify(section, bs, cs)
                out.append({"piece": pid, "section": section, "declared": did, "note": note})
    return {"diffs": out, "missing": missing}


def summary(cur: dict, d: Optional[dict]) -> dict:
    rows = cur["rows"].values()
    s = {"pieces": len(cur["rows"]), "by_channel": dict(Counter(r["channel"] for r in rows)),
         "cassette_hits": sum(r["cassette_hit"] for r in rows),
         "cassette_misses_f9_needs_rejudge": sum(not r["f9_hit"] for r in rows),
         "cassette_misses_seo_fit": sum(r["seo_miss"] for r in rows),
         "input_errors": sum(1 for r in rows if "error" in (r["input"] or {})),
         "stored_sha_na": sum(1 for r in rows if r["stored"]["sha"] == "n/a"),
         "stored_sha_match": sum(1 for r in rows if r["stored"]["sha"] == "match"),
         "post_gate_edit": sum(1 for r in rows if r["stored"]["sha"] == "post_gate_edit"),
         "edited_since_store": sum(1 for r in rows if r["stored"].get("edited_since_store")),
         "seo_fit_calls": sum((r["seo"] or {}).get("fit_calls", 0) for r in rows),
         "guard_pairs": sum(len(r["guard"] or []) for r in rows)}
    if d is not None:
        ids = Counter(x["declared"] for x in d["diffs"])
        s["declared"] = {k: v for k, v in ids.items() if k in DECLARED}
        s["not_compared"] = {k: v for k, v in ids.items() if k in NOT_DECLARED}
        s["undeclared"] = sum(v for k, v in ids.items() if k not in DECLARED and k not in NOT_DECLARED)
        s["missing_pieces"] = len(d["missing"])
    return s


def report_md(s: dict, d: Optional[dict]) -> str:
    lines = ["# AA-1049 T9 replay — declared-diff report", "", f"schema `{SCHEMA}`", ""]
    lines += [f"- {k}: {v}" for k, v in s.items()]
    if d is not None:
        lines += ["", "## Differences", "", "| piece | section | declared | note |", "|---|---|---|---|"]
        lines += [f"| {x['piece']} | {x['section']} | {x['declared']} | {x['note']} |" for x in d["diffs"]] or ["|"]
        lines += ["", "## Declared diffs", ""] + [f"- **{k}** {v}" for k, v in {**DECLARED, **NOT_DECLARED}.items()]
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
    bad = s.get("undeclared", 0) or s["post_gate_edit"] or s.get("missing_pieces", 0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
