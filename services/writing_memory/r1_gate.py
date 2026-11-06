"""AA-900 W0 (root ADR 0020; S271 critic fix F4 "freeze writing-memory mining until R1 exists") — the R1 freeze.

R1 is the judge-only calibration of the claim instrument (the Writing Lab ladder R0 / R1 / R2: README of the S271
writing-quality audit). Until a writer has a passing R1 record, nothing new may change what that writer produces on
the strength of the current, uncalibrated judge:

  mining          the paid `writing_memory_mine` run (and the preference-summary drafter on the same stage) refuses
                  at $0 (`mining.decide_run`, the run's first check); a run only drafts for writers with a record;
  activation      a first activation of a release with content (`releases.check`, so the Releases drawer shows the
                  reason before the click) and an L0 lexicon activation (the candidate's `l0_activation`) refuse
                  with code `r1_missing`.

Not frozen: rollback, retire, the memory switch and an EMPTY release (release 0 and the rollback drill render nothing:
they cannot change an output), R0 replays ($0) and R2 / shadow evidence runs (their own refusals, #1352).

An R1 record = a finished `shared.prompt_eval_runs` row with pipeline `writing_memory`, run_kind `r1`, verdict `pass`
for the writer (CIS 284 vocabulary; no new table). Break-glass switch: WRITING_MEMORY_R1_GATE=off on the api and the
worker restores the pre-W0 behaviour (CONTEXT.md, writing memory). $0: one indexed read
(prompt_eval_runs_wm_writer_idx).
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

import structlog

logger = structlog.get_logger()

R1_GATE_ENV = "WRITING_MEMORY_R1_GATE"
CODE = "r1_missing"
WRITERS = ("a1", "t2", "t9_blog", "t9_social")
_R1_SQL = """
    SELECT DISTINCT ON (writer) writer, id::text AS run_id, finished_at
      FROM shared.prompt_eval_runs
     WHERE pipeline = 'writing_memory' AND run_kind = 'r1' AND verdict = 'pass' AND finished_at IS NOT NULL
       AND writer = ANY($1::text[])
     ORDER BY writer, finished_at DESC
"""


def gate_on() -> bool:
    """True unless WRITING_MEMORY_R1_GATE is off / 0 / false / no (default on: frozen until R1)."""
    return (os.environ.get(R1_GATE_ENV) or "on").strip().lower() not in ("off", "0", "false", "no")


async def records(conn, writers: Iterable[str] = WRITERS) -> Optional[dict[str, dict]]:
    """{writer: {run_id, finished_at}} of the newest passing R1 run of each writer that has one. None when the read
    fails (no table, no grant): the caller treats that as "no record" (fail closed, $0)."""
    try:
        tx = getattr(conn, "transaction", None)
        if tx is None:
            rows = await conn.fetch(_R1_SQL, list(writers))
        else:
            async with tx():
                rows = await conn.fetch(_R1_SQL, list(writers))
    except Exception as e:  # noqa: BLE001 — fail closed: no record
        logger.info("wm_r1_records_unavailable", error=f"{type(e).__name__}: {str(e)[:160]}")
        return None
    return {r["writer"]: {"run_id": r["run_id"],
                          "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None} for r in rows}


def detail(writer: str, what: str) -> str:
    return (f"{what} is frozen until the claim instrument is calibrated: {writer} has no passing R1 run yet "
            f"(ADR 0020 W0). Rollback, the memory switch and empty releases still work.")


def problem(writer: str, recs: Optional[dict], what: str = "Activation") -> Optional[tuple[str, str]]:
    """(r1_missing, why) when the gate is on and `writer` has no R1 record in `recs`, else None."""
    if not gate_on():
        return None
    if recs and writer in recs:
        return None
    return CODE, detail(writer, what)


async def check(conn, writer: str, what: str = "Activation") -> Optional[tuple[str, str]]:
    """`problem` over a fresh read (no read when the gate is off)."""
    if not gate_on():
        return None
    return problem(writer, await records(conn, [writer]), what)


def summary(recs: Optional[dict]) -> dict:
    """What the admin summary shows: the switch, the writers with a record, the writers still frozen."""
    have = sorted((recs or {}).keys())
    return {"gate": "on" if gate_on() else "off", "env": R1_GATE_ENV, "writers_with_r1": have,
            "frozen": [w for w in WRITERS if w not in have] if gate_on() else []}


__all__ = ["CODE", "R1_GATE_ENV", "WRITERS", "check", "detail", "gate_on", "problem", "records", "summary"]
