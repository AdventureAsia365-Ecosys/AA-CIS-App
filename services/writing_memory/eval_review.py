"""AA-1169 — read an evaluation run by hand: the runs, the objects (golden items) of one run, and one object's stored
output, the source its judge read, its claim flags, checks and verdicts side by side ($0, read-only).

Writing Lab › Eval runs (frontend app/admin/lab/eval-runs) reads it through api/routers/admin_eval_runs.py:

  runs        ``shared.prompt_eval_runs`` of the writing memory (pipeline ``writing_memory``): kind, writer, verdict,
              stop reason, cost, the pre-registration it belongs to (``details.prereg.id``, e.g. a W5 proof) and its
              result rows counted per verdict. Ids, codes and numbers only.
  run items   the result rows of one run (``shared.eval_item_result`` x ``shared.eval_golden_item``) grouped per
              golden item: the sample id, split and label verdict, and per row its arm, mode, verdict, failure codes,
              hard stop and the policy class of each judge claim (counted; the claim text is not sent here).
  one item    the golden item's stored writer input as the judge read it (``eval_judge.judge_source_text``) and the
              raw input, then every row of that item in the run with its stored output split into its reader fields
              (``instrument.output_fields``), the judge's claims (``details.judge_claims``) each typed and placed by
              ``writer_proof.first_pass`` (class, field, reading position), the row's stored checks, and for a T9
              output the deterministic T10 gate replay on the stored draft (``first_pass.gate_replay``: no model, no
              write). A control row re-judged (mode ``rejudge``) has no output of its own: its output is the scored
              row's (``scored_result_id``).

Stored fields only: nothing is regenerated, no model is called, nothing is written. Table and column names: root
docs/architecture/db-schema-reference.md (``shared.prompt_eval_runs``, ``shared.eval_item_result``,
``shared.eval_golden_item``).
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Optional

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
T9_WRITERS = ("t9_blog", "t9_social")
# The keys of a row's stored `details` shown as its checks (r2.result_row / the candidate `info`); the judge claims are
# sent typed in `claims`, the rest is not a check.
CHECK_KEYS = ("qa_artefacts", "isolation", "injection", "new_hard", "hard_stop", "paired", "new_fail", "fixed",
              "same_family", "seeded_fault", "source_mode", "frozen", "calibration", "error")

# ── SQL (READ ONLY) ────────────────────────────────────────────────────────────────────────────────────────────────

RUN_COLUMNS = """
    p.id::text AS run_id, p.run_kind, p.writer, p.prompt_version, p.created_at, p.finished_at, p.verdict,
    p.stop_reason, p.cost_usd, p.cost_cap_usd, p.tour_count, p.judge_kappa, p.golden_set_id::text AS golden_set_id,
    p.release_id::text AS release_id, p.triggered_by, p.details->'prereg'->>'id' AS prereg_id,
    p.details->'prereg'->>'arm' AS prereg_arm,
    (SELECT count(*) FROM shared.eval_item_result r WHERE r.run_id = p.id)::int AS results,
    (SELECT count(*) FROM shared.eval_item_result r WHERE r.run_id = p.id AND r.verdict = 'pass')::int AS passes,
    (SELECT count(*) FROM shared.eval_item_result r WHERE r.run_id = p.id AND r.verdict = 'fail')::int AS fails,
    (SELECT count(*) FROM shared.eval_item_result r WHERE r.run_id = p.id AND r.verdict = 'error')::int AS errors,
    (SELECT count(*) FROM shared.eval_item_result r
      WHERE r.run_id = p.id AND r.output IS NOT NULL)::int AS outputs
"""

# $1 writer or NULL, $2 run kind or NULL, $3 pre-registration id or NULL, $4 limit, $5 offset
RUNS_SQL = f"""
    SELECT {RUN_COLUMNS}, count(*) OVER ()::int AS total
      FROM shared.prompt_eval_runs p
     WHERE p.pipeline = 'writing_memory'
       AND ($1::text IS NULL OR p.writer = $1)
       AND ($2::text IS NULL OR p.run_kind = $2)
       AND ($3::text IS NULL OR p.details->'prereg'->>'id' = $3)
     ORDER BY p.created_at DESC, p.id
     LIMIT $4 OFFSET $5
"""

RUN_SQL = f"""
    SELECT {RUN_COLUMNS}
      FROM shared.prompt_eval_runs p
     WHERE p.id = $1::uuid AND p.pipeline = 'writing_memory'
"""

# the rows of one run, without the output text (the claim texts are typed here, never sent)
RUN_ROWS_SQL = """
    SELECT r.result_id::text AS result_id, r.item_id::text AS item_id, g.sample_id, g.scope, g.split, g.kind,
           g.country, g.days, g.label_verdict, r.arm, r.mode, r.phase, r.verdict, r.hard_stop, r.failure_codes,
           r.cost_usd, r.details->'judge_claims' AS judge_claims, (r.output IS NOT NULL) AS has_output,
           r.scored_result_id::text AS scored_result_id
      FROM shared.eval_item_result r
      JOIN shared.eval_golden_item g ON g.item_id = r.item_id
     WHERE r.run_id = $1::uuid
     ORDER BY g.sample_id, r.arm, r.mode
"""

ITEM_SQL = """
    SELECT g.item_id::text AS item_id, g.sample_id, g.writer, g.channel, g.scope, g.tenant_id::text AS tenant_id,
           g.split, g.kind, g.tiers, g.country, g.days, g.label_verdict, g.label_source, g.label, g.input,
           g.input_sha256, g.pick_reason
      FROM shared.eval_golden_item g
     WHERE g.item_id = $1::uuid
"""

ITEM_ROWS_SQL = """
    SELECT r.result_id::text AS result_id, r.arm, r.mode, r.phase, r.verdict, r.score, r.failure_codes, r.hard_stop,
           r.judge_model, r.judge_version, r.cost_usd, r.created_at, r.output, r.output_sha256, r.details,
           r.scored_result_id::text AS scored_result_id
      FROM shared.eval_item_result r
     WHERE r.run_id = $1::uuid AND r.item_id = $2::uuid
     ORDER BY r.arm, r.mode
"""

SCORED_SQL = """
    SELECT r.result_id::text AS result_id, r.run_id::text AS run_id, r.output
      FROM shared.eval_item_result r
     WHERE r.result_id = ANY($1::uuid[])
"""


# ── pure ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _num(v: Any) -> Any:
    return float(v) if v is not None and not isinstance(v, (int, float, bool)) else v


def run_view(row: Any) -> dict:
    """One run row as sent: dates as ISO text, numerics as floats."""
    d = dict(row)
    d.pop("total", None)
    for k in ("created_at", "finished_at"):
        d[k] = _iso(d.get(k))
    for k in ("cost_usd", "cost_cap_usd", "judge_kappa"):
        d[k] = _num(d.get(k))
    return d


def _claim_texts(claims: Any) -> list[str]:
    out = []
    for c in _json(claims) or []:
        text = c.get("claim") if isinstance(c, dict) else c
        if text:
            out.append(str(text))
    return out


def claim_classes(claims: Any) -> dict[str, int]:
    """The policy class of each judge claim (first_pass.claim_class), counted. Pure."""
    from services.writer_proof import first_pass
    c = Counter(first_pass.claim_class(t) for t in _claim_texts(claims))
    return {k: c[k] for k in first_pass.CLASS_ORDER if c.get(k)}


def items_view(rows: list[Any]) -> list[dict]:
    """The rows of one run grouped per golden item, in sample order: no text, only ids, codes and counts. Pure."""
    items: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        it = items.setdefault(r["item_id"], {
            "item_id": r["item_id"], "sample_id": r["sample_id"], "scope": r["scope"], "split": r["split"],
            "kind": r["kind"], "country": r["country"], "days": r["days"], "label_verdict": r["label_verdict"],
            "rows": []})
        it["rows"].append({
            "result_id": r["result_id"], "arm": r["arm"], "mode": r["mode"], "phase": r["phase"],
            "verdict": r["verdict"], "hard_stop": bool(r["hard_stop"]), "failure_codes": list(r["failure_codes"] or []),
            "cost_usd": _num(r["cost_usd"]), "claims": len(_claim_texts(r["judge_claims"])),
            "claim_classes": claim_classes(r["judge_claims"]), "has_output": bool(r["has_output"]),
            "scored_result_id": r["scored_result_id"]})
    return list(items.values())


def run_summary(items: list[dict]) -> dict:
    """Per arm: rows judged (pass / fail), passes, the share that passed and the judge claims in all. Pure."""
    out: dict[str, dict] = {}
    for it in items:
        for row in it["rows"]:
            a = out.setdefault(row["arm"], {"rows": 0, "judged": 0, "passes": 0, "claims": 0, "hard_stops": 0})
            a["rows"] += 1
            if row["verdict"] in ("pass", "fail"):
                a["judged"] += 1
                a["passes"] += int(row["verdict"] == "pass")
            a["claims"] += row["claims"]
            a["hard_stops"] += int(row["hard_stop"])
    for a in out.values():
        a["pass_share"] = round(a["passes"] / a["judged"], 6) if a["judged"] else None
    return out


def output_fields(writer: str, output: Any) -> list[dict]:
    """The reader fields of a stored output as the judge reads them, in its order: [{field, text}]. Pure."""
    from services.writer_proof import instrument
    from services.writing_memory import eval_judge as ej
    if not isinstance(output, dict):
        return []
    return [{"field": f, "text": t} for f, t in instrument.output_fields(ej.judge_output_text(writer, output))]


def typed_claims(claims: Any, output: Any) -> list[dict]:
    """The judge's claims of one row, each with its policy class and its place in the output (first_pass), in the
    order a reader meets them. Pure."""
    from services.writer_proof import first_pass
    out = []
    for c in _json(claims) or []:
        if isinstance(c, dict):
            text, why, judge_class = str(c.get("claim") or ""), c.get("why"), c.get("class")
        else:
            text, why, judge_class = str(c), None, None
        if not text:
            continue
        field, pos = first_pass.locate(text, output)
        out.append({"claim": text, "why": why, "judge_class": judge_class,
                    "policy_class": first_pass.claim_class(text), "field": field,
                    "pos": pos if pos < 10 ** 9 else None})
    out.sort(key=lambda c: (c["pos"] is None, c["pos"] or 0))
    return out


def source_text(writer: str, inp: Any) -> Optional[str]:
    """What the judge read as the item's source (eval_judge.judge_source_text); None when it cannot be rebuilt."""
    from services.writing_memory import eval_judge as ej
    if not isinstance(inp, dict):
        return None
    try:
        return ej.judge_source_text(writer, inp)
    except Exception:  # noqa: BLE001 — an old input shape: the raw input is still sent
        return None


def gate_replay(writer: str, inp: Any, output: Any) -> Optional[dict]:
    """The deterministic T10 gates on a stored T9 draft (first_pass.gate_replay: no model, no write); None for another
    writer or no output, {"error": <type>} when the stored input cannot be rebuilt."""
    if writer not in T9_WRITERS or not isinstance(output, dict) or not isinstance(inp, dict):
        return None
    from services.writer_proof import first_pass
    from services.writing_memory import eval_writers as ew
    try:
        return first_pass.gate_replay(ew._t9_context(inp), output)
    except Exception as e:  # noqa: BLE001 — shown as "could not be replayed", never fatal
        return {"error": type(e).__name__}


def result_view(writer: str, inp: Any, row: Any, scored: dict[str, Any]) -> dict:
    """One result row of the item, with the output it was judged on. Pure but for the gate replay (CPU only)."""
    r = dict(row)
    details = _json(r.get("details")) or {}
    own = _json(r.get("output"))
    output, source = (own, "own") if own is not None else (None, None)
    if output is None and r.get("scored_result_id") and r["scored_result_id"] in scored:
        output, source = _json(scored[r["scored_result_id"]]), "scored"
    return {
        "result_id": r["result_id"], "arm": r["arm"], "mode": r["mode"], "phase": r["phase"],
        "verdict": r["verdict"], "score": _num(r.get("score")), "failure_codes": list(r.get("failure_codes") or []),
        "hard_stop": bool(r.get("hard_stop")), "judge_model": r.get("judge_model"),
        "judge_version": r.get("judge_version"), "cost_usd": _num(r.get("cost_usd")),
        "created_at": _iso(r.get("created_at")), "scored_result_id": r.get("scored_result_id"),
        "output_source": source, "output_sha256": r.get("output_sha256"),
        "output_fields": output_fields(writer, output),
        "claims": typed_claims(details.get("judge_claims"), output),
        "checks": {k: details[k] for k in CHECK_KEYS if k in details},
        "judge": details.get("judge"),
        "gate_replay": gate_replay(writer, inp, output),
    }


def item_view(run: dict, item: Any, rows: list[Any], scored: dict[str, Any]) -> dict:
    """One golden item of a run: its source, label and every row of it in the run, side by side."""
    it = dict(item)
    inp = _json(it.pop("input", None))
    writer = it.get("writer") or run.get("writer") or ""
    return {
        "run": run,
        "item": {**it, "label": _json(it.get("label")), "tiers": list(it.get("tiers") or [])},
        "source_text": source_text(writer, inp),
        "input": inp,
        "results": [result_view(writer, inp, r, scored) for r in rows],
    }


# ── reads (READ ONLY transactions) ──────────────────────────────────────────────────────────────────────────────────

async def list_runs(conn, *, writer: Optional[str], run_kind: Optional[str], prereg: Optional[str], limit: int,
                    offset: int) -> dict:
    async with conn.transaction(readonly=True):
        rows = await conn.fetch(RUNS_SQL, writer, run_kind, prereg, limit, offset)
    total = rows[0]["total"] if rows else 0
    return {"runs": [run_view(r) for r in rows], "total": total, "limit": limit, "offset": offset}


async def read_run(conn, run_id: str) -> Optional[dict]:
    async with conn.transaction(readonly=True):
        run = await conn.fetchrow(RUN_SQL, run_id)
        if run is None:
            return None
        rows = await conn.fetch(RUN_ROWS_SQL, run_id)
    items = items_view(rows)
    return {"run": run_view(run), "items": items, "summary": run_summary(items)}


async def read_item(conn, run_id: str, item_id: str) -> Optional[dict]:
    async with conn.transaction(readonly=True):
        run = await conn.fetchrow(RUN_SQL, run_id)
        if run is None:
            return None
        rows = await conn.fetch(ITEM_ROWS_SQL, run_id, item_id)
        if not rows:
            return None
        item = await conn.fetchrow(ITEM_SQL, item_id)
        if item is None:
            return None
        want = sorted({r["scored_result_id"] for r in rows if r["output"] is None and r["scored_result_id"]})
        scored = ({s["result_id"]: s["output"] for s in await conn.fetch(SCORED_SQL, want)} if want else {})
    return item_view(run_view(run), item, rows, scored)


__all__ = ["CHECK_KEYS", "ITEM_ROWS_SQL", "ITEM_SQL", "RUNS_SQL", "RUN_ROWS_SQL", "RUN_SQL", "SCORED_SQL",
           "WRITERS", "claim_classes", "gate_replay", "item_view", "items_view", "list_runs", "output_fields",
           "read_item", "read_run", "result_view", "run_summary", "run_view", "source_text", "typed_claims"]
