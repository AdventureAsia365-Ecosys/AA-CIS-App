"""AA-1141 (P1 feature 3) — the freshness footer of the Ecosystem status page: the background producers the page relies
on, each with its last run and refresh time, grey "stale" after ``cis.ops.status_stale_factor`` x its refresh time
(S285 review idea 3: a dead sampler must never leave the page green).

- ``sampler``      the 5-minute status sampler: the newest ``acp_shared.ops_sample`` row (one index read).
- ``digest``       the daily OPS-12 health digest: the newest finished ``shared.report_run`` of OPS-12.
- ``guards``       the nightly probes CIS reads (``shared.kpi_daily``, via the delivery section): the newest result.
- ``delivery``     the published delivery summary (CI and deploys of the six repos): its ``generated_at``.
- ``schema_dump``  the nightly DB schema dump runs on GitHub and in an ECS task without a verdict line CIS can read:
                   "not measured yet", never green.

The sections' own freshness (each card's "as of" chip) is ``__init__.freshness``. :func:`rows` is pure.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

SAMPLER_S, DAY_S, HOUR_S = 300, 86400, 3600
SAMPLER_SQL = "SELECT max(sampled_at) AS t FROM acp_shared.ops_sample"
DIGEST_SQL = """
    SELECT max(finished_at) AS t FROM shared.report_run
     WHERE report_id = 'OPS-12' AND status IN ('succeeded', 'draft', 'approved', 'posted')
"""
SCHEMA_DUMP_REASON = "Runs on GitHub and in an ECS task without a verdict line the CIS server can read."


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _parse(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


def row(key: str, label: str, refresh_s: int, as_of: Any, now: datetime, factor: float, *,
        reason: Optional[str] = None, state: Optional[str] = None) -> dict:
    """One producer: ok while its last run is younger than factor x its refresh, else stale; no run = unknown. Pure."""
    t = _parse(as_of)
    if state is None:
        if t is None:
            state = "unknown"
        else:
            state = "stale" if (now - t).total_seconds() > factor * refresh_s else "ok"
    return {"key": key, "label": label, "refresh_s": refresh_s, "as_of": _iso(t) if t else None, "state": state,
            "reason": reason}


def _guard_last(delivery: Optional[dict]) -> Optional[str]:
    guards = ((delivery or {}).get("data") or {}).get("guards") or []
    seen = [g.get("seen_at") for g in guards if g.get("seen_at")]
    return max(seen) if seen else None


def rows(sampler_at: Any, digest_at: Any, delivery: Optional[dict], now: datetime, factor: float) -> list[dict]:
    d = (delivery or {}).get("data") or {}
    return [
        row("sampler", "Status sampler (every 5 min)", SAMPLER_S, sampler_at, now, factor),
        row("digest", "Daily health digest (OPS-12)", DAY_S, digest_at, now, factor),
        row("guards", "Nightly probes", DAY_S, _guard_last(delivery), now, factor),
        row("delivery", "Delivery summary (CI and deploys)", HOUR_S, d.get("repos_as_of"), now, factor,
            reason=d.get("repos_note")),
        row("schema_dump", "Nightly schema dump", DAY_S, None, now, factor, reason=SCHEMA_DUMP_REASON,
            state="not_measured"),
    ]


async def collect(pool: Any, delivery: Optional[dict], now: datetime, factor: float) -> list[dict]:
    async def one(sql: str) -> Any:
        try:
            return await pool.fetchval(sql)
        except Exception:  # noqa: BLE001 — a footer row: no time = "unknown", never a failed page
            return None

    return rows(await one(SAMPLER_SQL), await one(DIGEST_SQL), delivery, now, factor)
