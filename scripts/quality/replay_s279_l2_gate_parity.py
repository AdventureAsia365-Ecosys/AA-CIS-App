"""S279 thresholds lane L2 (AA-1096) — $0 replay: the gate verdicts on stored rows, recomputed by the readers that
now take their values from the settings registry (CIS 412), equal the verdicts STORED on those rows before the move,
and equal the verdicts with the values before the move (the YAML / env / literal values, restated in ``BEFORE``).

No LLM, no Jev, no write. It reads the approved values (``settings.ensure_fresh``).

Section ``stored`` (the reader check: a reader that reads the wrong key or unit gives another verdict than the one the
pre-move code stored). Each stored verdict against the post-move reader's verdict on the same row:

- ``pieces_length`` — the F4 side of a blog piece (``length_contract.side`` on the tag-stripped text) against its
                     stored ``F4_extreme_length`` ledger entry (passed = inside, else ``length_contract.entry_side``);
- ``pieces_f9``    — the held state of a stored F9_cta_fact entry the F9 policy decided (``blocking``) against the
                     policy re-applied to the entry as it was before the policy (``apply_policy`` legacy, then the
                     mode in force);
- ``t2_versions``  — ``t2_rewrite_job._t2_status(score)`` against the stored status of an untouched AI version
                     (``edit_source = 'ai_generated'``, status ``ai_generated`` / ``needs_review``). A stored
                     ``needs_review`` with a passing score can be a hard code T3 left or a source-score fallback: those
                     rows are listed under ``t2_review_with_pass_score`` (information, not a mismatch);
- ``review_queue`` — the low-quality reason ``_build_failure_summary`` wrote (``low_quality(score=…<floor)``) against
                     ``score < _low_quality_floor()`` on the stored score, for rows the builder wrote (a summary that
                     names ``brand_audit=`` or ``low_quality(``); a row whose stored floor differs from the floor in
                     force is listed under ``review_floor_moved``.

``--since`` keeps only rows created on or after a date (rows older than a deliberate gate change are not evidence).

Section ``arms`` (the values check): every verdict once with the values in force and once with ``BEFORE`` served in
their place:

- ``settings``     — every key of ``BEFORE``: the value in force, its source (env / platform / default) and any diff;
- ``review_queue`` — each row's Review Queue score band (``admin_pipeline._score_band_where`` edges) and its low-quality
                     flag (``silver_aa_internal.review_queue.score_overall``);
- ``t2_versions``  — each tenant version's T2 status from its stored score (``t2_rewrite_job._t2_status``, no hard
                     codes: the score rule alone) (``gold_aa_internal.tenant_tour_versions.quality_score``);
- ``pieces``       — each stored blog piece's F4 length side (``length_contract.side`` on the tag-stripped text) and
                     every piece's F9_cta_fact ledger entry under the F9 policy (``f9_policy.apply_policy``)
                     (``acp_shared.content_piece``);
- ``publish``      — the A1 publish floor / retry route on a fixed score grid (``admin_pipeline._is_publishable``,
                     ``graph.should_retry``).

    python3 /tmp/replay.py [--limit 5000] [--out r.json]

``scripts/`` is not in the api image. On Dev, run it with the S3-mediated ECS exec in the api container: fetch the
presigned file to /tmp, then ``cd /app && PYTHONPATH=/app python3 /tmp/replay.py``. The DB login is ``DATABASE_URL``
(cis_app), ssl required unless ``REPLAY_DB_SSL=disable``; the session is read only. Output: one JSON document (counts,
ids of mismatching rows, no text) between REPLAY-BEGIN / REPLAY-END; exit 1 when any verdict or setting differs (a
stored mismatch included).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date
from contextlib import contextmanager
from typing import Any, Callable
from unittest.mock import patch

# The SEO bands and meta aims are not listed: the API refuses any value but their seed (quality_settings
# START_TIME_REASON), and the writing-policy lint keeps band literals out of code.
# The value of each key before the move (thresholds.yaml / writing_policy.yaml / taxonomy.yaml, the worker's Terraform
# env values, the code literals). Restated here on purpose: the replay compares against what main ran.
BEFORE: dict[str, Any] = {
    "cis.quality.pass_line": 7.0, "cis.quality.a1_missing_field_cap": 4.0, "cis.quality.judge_tie_band": 0.5,
    "cis.quality.judge_mission_absent_cap": 6.0, "cis.quality.brand_fit_repeat_band_low": 6.0,
    "cis.quality.brand_fit_repeat_band_high": 8.0, "cis.quality.t2_cross_brand_overlap_warn": 0.35,
    "cis.quality.t9_blog_length_target_margin": 0.17, "cis.quality.t10_reuse_similarity": 0.92,
    "cis.quality.review_score_band_low": 5, "cis.quality.ui_score_band_top": 9,
    "cis.quality.auto_upgrade_threshold": 8.5,
    "cis.t9.blog_chars_min": 4700, "cis.t9.blog_chars_max": 13000,
    "cis.quality_modes.t10_f12_scope": "warn", "cis.quality_modes.t10_f9_brand_style": "warn",
    "cis.quality_modes.t10_f14_floor": "repair", "cis.quality_modes.t10_f9_policy": "interim",
    "cis.quality_modes.t10_seed_align": "off", "cis.quality_modes.t10_adjudicate": "off",
    "cis.quality_modes.t9_brief_grounding": "off", "cis.quality_modes.t7_debate": "rank",
    "cis.quality_modes.t2_judge_gate": "brand_fit", "cis.quality_modes.t3_background_facts": True,
    "cis.quality_claims.rules_hypothetical": "warn", "cis.quality_claims.rules_uncited_background": "warn",
    "cis.quality_claims.f9_trip_promise": "hold", "cis.quality_claims.f9_unsupported_other": "warn",
    "cis.quality_claims.f9_fact_check_unquoted": "warn", "cis.quality_claims.f9_seo_field_claim": "warn",
    "cis.quality_claims.f9_cta_not_reflected": "warn", "cis.quality_claims.f9_text_integrity": "warn",
    "cis.quality_claims.f9_judge_unavailable": "warn", "cis.quality_claims.f9_reasonless": "warn",
    "cis.quality_claims.f9_other": "warn",
    "cis.t7.debate_contested_cut": 0.5, "cis.t7.debate_max_cut_fraction": 0.5,
}

_SCORES = [0.0, 3.99, 4.0, 4.99, 5.0, 6.0, 6.5, 6.99, 7.0, 7.01, 7.5, 8.0, 8.49, 8.5, 8.99, 9.0, 10.0]

_RQ_SQL = ("SELECT id::text AS id, score_overall::float AS score, failure_summary FROM silver_aa_internal.review_queue "
           "WHERE score_overall IS NOT NULL AND created_at >= $2 ORDER BY id LIMIT $1")
_T2_SQL = ("SELECT id::text AS id, quality_score::float AS score, status, edit_source "
           "FROM gold_aa_internal.tenant_tour_versions "
           "WHERE quality_score IS NOT NULL AND created_at >= $2 ORDER BY id LIMIT $1")
_PIECE_SQL = ("SELECT piece_id::text AS id, channel, content_text, gate_ledger FROM acp_shared.content_piece "
              "WHERE created_at >= $2 ORDER BY created_at DESC LIMIT $1")
_LOW_QUALITY_RE = re.compile(r"low_quality\(score=(-?[\d.]+)<(-?[\d.]+)\)")
_T2_UNTOUCHED = ("ai_generated", "needs_review")


@contextmanager
def _before():
    from shared import settings
    real = settings.READER.get
    with patch.object(settings.READER, "get", side_effect=lambda k, t=None: BEFORE[k] if k in BEFORE else real(k, t)):
        yield


def _both(fn: Callable[[], Any]) -> tuple[Any, Any]:
    """(verdict with the values in force, verdict with BEFORE)."""
    now = fn()
    with _before():
        was = fn()
    return now, was


def _ledger(raw: Any) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, dict):
        raw = raw.get("gates") or raw.get("ledger") or list(raw.values())
    return [g for g in (raw or []) if isinstance(g, dict)]


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def _settings_diff() -> dict:
    from shared import settings
    diffs = {}
    for k, v in BEFORE.items():
        r = settings.resolve(k)
        if r.value != v:
            diffs[k] = {"before": v, "in_force": r.value, "source": r.source}
    return diffs


def _band(score: float) -> str:
    """The Review Queue band of a score and the SQL edge text of that band (admin_pipeline)."""
    from api.routers import admin_pipeline as ap
    low, floor = ap._score_band_low(), ap._low_quality_floor()
    name = "critical" if score < low else "low" if score < floor else "ok"
    return f"{name}|{ap._score_band_where(name)}"


def _differ(values: list, verdict: Callable[[Any], Any]) -> list:
    """The values whose verdict differs between the values in force and BEFORE."""
    return [v for v in values if len(set(map(_canon, _both(lambda v=v: verdict(v))))) > 1]


def _piece_mismatches(pieces: list) -> tuple[list, list, int, int]:
    """(length mismatches, F9 mismatches, blog pieces, F9 entries) over stored pieces."""
    from services.acp_content_writing import f9_policy, length_contract as lc
    blog = [r for r in pieces if r["channel"] == "blog"]
    length = [r["id"] for r in blog if _differ([lc.measure(r["content_text"])], lc.side)]
    f9 = [(r["id"], g) for r in pieces for g in _ledger(r["gate_ledger"])
          if g.get("gate") == "F9_cta_fact" and not g.get("passed")]
    f9_bad = sorted({pid for pid, g in f9 if _differ([g], lambda e: f9_policy.apply_policy(dict(e)))})
    return length, f9_bad, len(blog), len(f9)


def _last_entry(ledger: Any, gate: str) -> Any:
    hits = [g for g in _ledger(ledger) if g.get("gate") == gate]
    return hits[-1] if hits else None


def _stored_length(pieces: list) -> list:
    """Blog pieces whose stored F4 side differs from the side the post-move reader gives the stored text."""
    from services.acp_content_writing import length_contract as lc
    out = []
    for r in pieces:
        entry = _last_entry(r["gate_ledger"], "F4_extreme_length") if r["channel"] == "blog" else None
        if entry is None:
            continue
        stored = None if entry.get("passed") else lc.entry_side(entry)
        if (entry.get("passed") or stored) and stored != lc.side(lc.measure(r["content_text"])):
            out.append(r["id"])
    return out


def _stored_f9(pieces: list) -> list:
    """Pieces whose stored F9 held state (an entry the policy decided) differs from the policy re-applied now."""
    from services.acp_content_writing import f9_policy
    out = []
    for r in pieces:
        for g in _ledger(r["gate_ledger"]):
            if g.get("gate") != "F9_cta_fact" or g.get("passed") or not isinstance(g.get("f9_policy"), dict):
                continue
            raw = f9_policy.apply_policy(dict(g), mode=f9_policy.LEGACY_MODE)
            if bool(f9_policy.apply_policy(raw).get("blocking")) != bool(g.get("blocking")):
                out.append(r["id"])
                break
    return out


def _stored_t2(versions: list) -> tuple[list, list]:
    """(mismatches, needs_review rows with a passing score) of untouched AI versions."""
    from services.jobs import t2_rewrite_job as t2
    bad, info = [], []
    for r in versions:
        if r["edit_source"] != "ai_generated" or r["status"] not in _T2_UNTOUCHED:
            continue
        now = t2._t2_status(r["score"])
        if now == r["status"]:
            continue
        (info if r["status"] == "needs_review" else bad).append(r["id"])
    return bad, info


def _stored_review(rows: list) -> tuple[list, list]:
    """(mismatches, rows whose stored floor is not the floor in force) of builder-written Review Queue summaries."""
    from api.routers import admin_pipeline as ap
    floor = float(ap._low_quality_floor())
    bad, moved = [], []
    for r in rows:
        summary = r["failure_summary"] or ""
        if "brand_audit=" not in summary and "low_quality(" not in summary:
            continue
        m = _LOW_QUALITY_RE.search(summary)
        if m and abs(float(m.group(2)) - floor) > 1e-9:
            moved.append(r["id"])
        elif bool(m) != (r["score"] < floor):
            bad.append(r["id"])
    return bad, moved


def _stored(rq: list, tv: list, pieces: list) -> tuple[dict, dict]:
    """(mismatches, information) of the stored-verdict section."""
    t2_bad, t2_info = _stored_t2(tv)
    rq_bad, rq_moved = _stored_review(rq)
    mism = {"pieces_length": _stored_length(pieces), "pieces_f9": _stored_f9(pieces), "t2_versions": t2_bad,
            "review_queue": rq_bad}
    return mism, {"t2_review_with_pass_score": t2_info, "review_floor_moved": rq_moved}


def _publish_verdict(score: float) -> tuple:
    from api.routers import admin_pipeline as ap
    from services.content_generation import graph
    res = {"quality_score": score, "brand_audit_status": "pass", "failure_codes": []}
    return ap._is_publishable(res), graph.should_retry({**res, "retry_count": 0})


async def _rows(pool: Any, sql: str, name: str, limit: int, since: date, missing: list) -> list:
    import asyncpg
    try:
        async with pool.acquire() as conn:
            return list(await conn.fetch(sql, limit, since))
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        missing.append(name)          # a database without that table (a local scratch DB): reported, skipped
        return []


async def run(limit: int, since: date = date(2000, 1, 1)) -> dict:
    import asyncpg

    from services.jobs import t2_rewrite_job as t2
    from shared import settings

    ssl = None if os.environ.get("REPLAY_DB_SSL") == "disable" else "require"
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl=ssl, min_size=1, max_size=2,
                                     server_settings={"default_transaction_read_only": "on"})
    try:
        fresh = await settings.ensure_fresh(pool)
        diffs = _settings_diff()
        missing: list[str] = []
        rq = await _rows(pool, _RQ_SQL, "review_queue", limit, since, missing)
        tv = await _rows(pool, _T2_SQL, "tenant_tour_versions", limit, since, missing)
        pieces = await _rows(pool, _PIECE_SQL, "content_piece", limit, since, missing)
    finally:
        await pool.close()
    length, f9_bad, blog, f9 = _piece_mismatches(pieces)
    stored, info = _stored(rq, tv, pieces)
    mism = {"review_queue": [r["id"] for r in rq if _differ([r["score"]], _band)],
            "t2_versions": [r["id"] for r in tv if _differ([r["score"]], t2._t2_status)],
            "pieces_length": length, "pieces_f9": f9_bad, "publish": _differ(_SCORES, _publish_verdict)}
    return {"settings_fresh": fresh, "settings_error": settings.READER.last_error,
            "settings": {"keys": len(BEFORE), "differ": diffs}, "tables_missing": missing,
            "rows": {"review_queue": len(rq), "t2_versions": len(tv), "pieces": len(pieces), "blog_pieces": blog,
                     "f9_entries": f9, "publish_grid": len(_SCORES)},
            "since": since.isoformat(),
            "stored": {"mismatches": {k: v[:50] for k, v in stored.items()},
                       "counts": {k: len(v) for k, v in stored.items()},
                       "information": {k: v[:50] for k, v in info.items()}},
            "arms": {"mismatches": {k: v[:50] for k, v in mism.items()}},
            "identical": not diffs and not any(mism.values()) and not any(stored.values())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--since", type=date.fromisoformat, default=date(2000, 1, 1),
                    help="only rows created on or after this date (YYYY-MM-DD)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    result = asyncio.run(run(a.limit, a.since))
    doc = json.dumps(result, indent=2, default=str)
    print("REPLAY-BEGIN\n" + doc + "\nREPLAY-END")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(doc + "\n")
    return 0 if result["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
