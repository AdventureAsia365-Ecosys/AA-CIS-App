"""AA-1081 (S278) — $0 offline benchmark of the T7 Debate brand-fit pass: one judge call at a time vs bounded
concurrency (`T7_DEBATE_CONCURRENCY`).

Runs the production `services.acp_shared.debate.assess_candidates()` over invented Segment candidates with an
in-memory connection (contested, cache read / write, evidence) and a fake judge that sleeps a fixed reply time
(default 2.2 s, the S278 median of `t7_debate_judge`, scaled by --scale so the run takes seconds). No database, no
Jev, no model call, nothing written. Prints the wall time per width, the speed-up, the time projected to the real reply
time, and checks that every width returns the same signals as the serial run.

    PYTHONPATH=. python3 scripts/quality/bench_aa1081_debate_concurrency.py --candidates 320 --widths 1,4,8,16
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Optional
from unittest.mock import patch
from uuid import UUID

from services.acp_shared import debate
from services.acp_shared.slate import Candidate
from services.content_generation.brand_fit import BrandFitResult

TENANT = UUID("00000000-0000-0000-0000-00000000a081")
PROFILE = {"brand_core_idea": "slow travel", "brand_customer_segment": "couples", "brand_customer_mindset": "calm",
           "brand_voice_examples": ["unhurried"], "brand_good_examples": "", "_version": 1}


class _MemConn:
    """The one connection Debate reads and writes through; refuses overlapping statements like asyncpg."""

    def __init__(self):
        self.cache: dict[str, dict] = {}
        self.busy = False

    async def _op(self):
        if self.busy:
            raise RuntimeError("another operation is in progress")
        self.busy = True
        await asyncio.sleep(0)
        self.busy = False

    async def fetchrow(self, sql, *args):
        await self._op()
        if "FROM acp_contract.atom_segment" in sql:
            return {"contested": 0.1}
        if "debate_brand_fit_cache" in sql:
            return self.cache.get(args[1])
        return None

    async def fetch(self, sql, *args):
        await self._op()
        return [{"text": f"Walk the ridge above {args[0]} at first light."}]

    async def execute(self, sql, *args):
        await self._op()
        self.cache[args[1]] = {"judge_score": args[4], "feedback": args[8]}


class _Grey:
    def accepted(self, q):
        return False

    def rejected(self, q):
        return False


def _candidates(n: int) -> list[Candidate]:
    return [Candidate(segment_id=f"bench_{i:04d}", route_id=None, score=float(i), demand=1500, questions=5,
                      said=200, place=f"Invented Place {i}", action="walk") for i in range(n)]


def _judge(reply_s: float):
    def score_candidate_fit(brand_profile, topic, *, stage):
        time.sleep(reply_s)
        n = int(topic["place"].rsplit(" ", 1)[1])
        score = (5.0, 7.5, 9.0)[n % 3]
        return BrandFitResult(brand_fit_score=score, cross_brand_distinct=score, mission_present=True, feedback="",
                              judge_score=score, model_used="bench-judge", cost_usd=0.0, input_tokens=0,
                              output_tokens=0, stop_reason="stop", account=None, fallback_used=False)
    return score_candidate_fit


async def _no_log(**kwargs):
    return None


async def _prefilter(*args):
    return _Grey()


async def _profile(conn, tenant_id):
    return PROFILE


def _one(n: int, width: int, reply_s: float) -> tuple[float, list]:
    with patch.object(debate, "_fetch_brand_profile", _profile), \
         patch.object(debate, "t7_prefilter", _prefilter), \
         patch.object(debate, "score_candidate_fit", _judge(reply_s)), \
         patch.object(debate, "record_call", _no_log), \
         patch.object(debate, "record_billed_failure", _no_log):
        t0 = time.perf_counter()
        signals = asyncio.run(debate.assess_candidates(TENANT, _candidates(n), _MemConn(), concurrency=width))
        return time.perf_counter() - t0, signals


def run_benchmark(n: int = 320, widths: tuple[int, ...] = (1, 8), reply_s: float = 2.2,
                  scale: float = 0.01) -> dict:
    """Wall time per width (scaled reply time), its projection to `reply_s`, and whether the signals match the
    serial run. Pure: no I/O beyond the clock."""
    scaled = reply_s * scale
    rows: list = []
    baseline: Optional[list] = None
    serial_s = None
    for width in widths:
        wall, signals = _one(n, width, scaled)
        if baseline is None:
            baseline, serial_s = signals, wall
        rows.append({"width": width, "wall_s": round(wall, 3), "projected_s": round(wall / scale, 1),
                     "speedup": round(serial_s / wall, 2) if wall else None, "same_signals": signals == baseline})
    return {"candidates": n, "reply_s": reply_s, "scale": scale, "runs": rows}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--candidates", type=int, default=320, help="cache misses to judge (S278: 319-326 per job)")
    p.add_argument("--widths", default="1,4,8,16", help="concurrency widths, the first is the baseline")
    p.add_argument("--reply-s", type=float, default=2.2, help="judge reply time to project to (S278 median 2.2 s)")
    p.add_argument("--scale", type=float, default=0.01, help="reply time actually slept = reply-s x scale")
    args = p.parse_args(argv)
    out = run_benchmark(args.candidates, tuple(int(w) for w in args.widths.split(",")), args.reply_s, args.scale)
    print(json.dumps(out, indent=2))
    return 0 if all(r["same_signals"] for r in out["runs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
