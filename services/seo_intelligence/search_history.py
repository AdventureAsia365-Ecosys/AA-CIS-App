"""services/seo_intelligence/search_history.py — AA-1122 item 1: the monthly search volume history (CIS 450).

Every Google Ads volume answer DataForSEO sends carries ``monthly_searches``: the last 12 months of the keyword in the
asked location (``[{"year": 2026, "month": 10, "search_volume": 1300}, ...]``). Research used to keep only the 12-month
average (``acp_contract.search_demand.search_volume``) and replaced it on every refresh, so the season of a place was
paid for and dropped. This module keeps the months:

  - :func:`monthly_from_answer` / :func:`monthly_from_results` read them from a raw answer (the live
    ``search_volume`` endpoint, the Standard queue ``task_get`` and ``keywords_for_keywords`` share the row shape);
  - :class:`MonthlyBuffer` holds what a client parsed until the caller that stores the volumes writes them;
  - :func:`write` inserts them into ``acp_contract.search_volume_monthly`` with ON CONFLICT DO NOTHING: a month is
    stored once and never overwritten (a later purchase only adds the months it had not seen);
  - :func:`seasonality` / :func:`place_seasonality` read the season of a place in a market: its peak month and the
    year-over-year change (pure part + one read).

Writes never fail a purchase: the volumes are paid and stored by the research cache first; a refused history write is
logged (``search_history_write_failed``) and counted by the caller, and the next purchase of the keyword brings the
months again. $0: no DataForSEO call here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Sequence

import structlog

logger = structlog.get_logger()

Series = list[tuple[date, int | None]]

# The longest keyword the history table accepts (CIS 450 search_volume_monthly_keyword_check).
MAX_KEYWORD_CHARS = 200  # technical-constant: the column CHECK of CIS 450, not a policy value

_INSERT_SQL = """
    INSERT INTO acp_contract.search_volume_monthly (keyword, market, month, search_volume, source)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (keyword, market, month) DO NOTHING
"""

# The months of the keywords of a set of places in one market (the place's map: acp_contract.place_keyword, the
# buyable rows; a rule-dropped or an enforced Jev reject was never bought).
_PLACE_MONTHS_SQL = """
    SELECT k.subject AS place, h.keyword, h.month, h.search_volume
      FROM acp_contract.place_keyword k
      JOIN acp_contract.search_volume_monthly h ON h.keyword = k.keyword AND h.market = $2
     WHERE k.subject_kind = 'place' AND k.subject = ANY($1::text[])
       AND NOT coalesce(k.jev_mode, '') LIKE 'rule:%'
       AND NOT (k.jev_zone = 'reject' AND k.jev_mode = 'enforce')
"""

_COUNTS_SQL = """
    SELECT count(*) AS rows, count(DISTINCT (keyword, market)) AS keywords, min(month) AS first_month,
           max(month) AS last_month
      FROM acp_contract.search_volume_monthly
"""


def _month(item: Mapping) -> date | None:
    try:
        y, m = int(item.get("year")), int(item.get("month"))
    except (TypeError, ValueError):
        return None
    if not (1 <= m <= 12 and 2000 <= y <= 2100):
        return None
    return date(y, m, 1)


def _volume(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def series_of(row: Mapping) -> Series:
    """The (month, volume) pairs of one DataForSEO result row (``monthly_searches``), sorted by month, one per month.
    Pure. A row without the field (or with a bad entry) gives what it can, never an error."""
    out: dict[date, int | None] = {}
    for item in row.get("monthly_searches") or []:
        if not isinstance(item, Mapping):
            continue
        month = _month(item)
        if month is not None and month not in out:
            out[month] = _volume(item.get("search_volume"))
    return sorted(out.items())


def monthly_from_results(results: Iterable, keywords: Iterable[str] | None = None) -> dict[str, Series]:
    """keyword -> its months, for every result row of an answer that names a keyword (``keywords`` limits it to the
    asked ones). Pure."""
    wanted = set(keywords) if keywords is not None else None
    out: dict[str, Series] = {}
    for row in results or []:
        if not isinstance(row, Mapping):
            continue
        kw = row.get("keyword")
        if not isinstance(kw, str) or not kw or (wanted is not None and kw not in wanted):
            continue
        series = series_of(row)
        if series:
            out[kw] = series
    return out


def monthly_from_answer(data: Mapping, keywords: Iterable[str] | None = None) -> dict[str, Series]:
    """The months of a raw answer (``tasks[0].result``: search_volume live / task_get, keywords_for_keywords). Pure."""
    try:
        results = data["tasks"][0]["result"] or []
    except (KeyError, IndexError, TypeError):
        return {}
    return monthly_from_results(results, keywords)


class MonthlyBuffer:
    """What a DataForSEO client parsed, per (location code, keyword), until the caller that stores the volumes writes
    it (:func:`write`). One per client; the live volume path is one task at a time, so a pop never races a parse."""

    def __init__(self) -> None:
        self._by_loc: dict[int, dict[str, tuple[str, Series]]] = {}

    def keep(self, location_code: int | None, source: str, monthly: Mapping[str, Series]) -> None:
        if location_code is None or not monthly:
            return
        slot = self._by_loc.setdefault(int(location_code), {})
        for kw, series in monthly.items():
            slot[kw] = (source, list(series))

    def pop(self, location_code: int | None, keywords: Iterable[str] | None = None) -> dict[str, tuple[str, Series]]:
        """keyword -> (source, months) for ``keywords`` (None = all) of one location, removed from the buffer."""
        if location_code is None:
            return {}
        slot = self._by_loc.get(int(location_code)) or {}
        names = list(slot) if keywords is None else [k for k in keywords if k in slot]
        return {k: slot.pop(k) for k in names}

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_loc.values())


def with_history(history) -> dict:
    """``{"history": history}`` for a research cache ``put_volumes`` call when there are months to keep, else {} (a
    store double without the argument keeps working). Pure."""
    return {"history": history} if history else {}


def rows_for(market: str, history: Mapping[str, tuple[str, Series]]) -> list[tuple]:
    """The INSERT rows of one market's history. Pure. A keyword the table would refuse (too long) is left out."""
    rows = []
    for kw, (source, series) in sorted(history.items()):
        if not kw or len(kw) > MAX_KEYWORD_CHARS:
            continue
        for month, volume in series:
            rows.append((kw, market, month, volume, source))
    return rows


async def write(conn_or_pool, market: str, history: Mapping[str, tuple[str, Series]]) -> int:
    """Insert one market's months (never overwrites a stored month). Returns the rows offered; 0 on any error, which is
    logged and never raised: the volumes are already stored and paid."""
    rows = rows_for(market, history)
    if not rows:
        return 0
    try:
        if hasattr(conn_or_pool, "acquire"):
            async with conn_or_pool.acquire() as conn:
                await conn.executemany(_INSERT_SQL, rows)
        else:
            await conn_or_pool.executemany(_INSERT_SQL, rows)
    except Exception as exc:  # noqa: BLE001 — history is a record of a paid answer, never a reason to fail the purchase
        logger.warning("search_history_write_failed", market=market, keywords=len(history), rows=len(rows),
                       error=str(exc)[:200])
        return 0
    return len(rows)


# ── reading the season ───────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Season:
    """The season of a place in one market. ``months`` = (month, volume) of the place's demand curve (per month the
    MAX over its keywords: close variants carry the same number, a sum would count one demand twice), oldest first.
    ``peak_month`` = the calendar month (1-12) of the highest volume of the latest 12 months; ``yoy_pct`` = the
    change of the latest 12 months against the 12 before, in percent, or of the latest month against the same month a
    year earlier when only that is known; None until a year more is stored."""
    months: list[tuple[date, int]]
    peak_month: int | None
    peak_volume: int | None
    yoy_pct: float | None


def _shift(d: date, months: int) -> date:
    m = d.year * 12 + (d.month - 1) + months
    return date(m // 12, m % 12 + 1, 1)


def seasonality(series_by_keyword: Mapping[str, Sequence[tuple[date, int | None]]]) -> Season:
    """The season of a place from its keywords' months. Pure."""
    curve: dict[date, int] = {}
    for series in series_by_keyword.values():
        for month, volume in series:
            if volume is None:
                continue
            curve[month] = max(curve.get(month, 0), int(volume))
    months = sorted(curve.items())
    if not months:
        return Season([], None, None, None)
    last = months[-1][0]
    window = [(m, v) for m, v in months if m > _shift(last, -12)]
    peak_m, peak_v = max(window, key=lambda mv: (mv[1], mv[0]))
    peak = (peak_m.month, peak_v) if peak_v > 0 else (None, None)
    prev = [(m, v) for m, v in months if _shift(last, -24) < m <= _shift(last, -12)]
    yoy = None
    if len(window) == 12 and len(prev) == 12:
        before = sum(v for _m, v in prev)
        if before > 0:
            yoy = round(100.0 * (sum(v for _m, v in window) - before) / before, 1)
    else:
        year_ago = curve.get(_shift(last, -12))
        if year_ago:
            yoy = round(100.0 * (curve[last] - year_ago) / year_ago, 1)
    return Season(months, peak[0], peak[1], yoy)


async def place_seasonality(pool, places: Sequence[str], market: str) -> dict[str, Season]:
    """place -> its season in ``market``, from its keyword map and the history (one read). A place with no stored
    month is left out."""
    if not places:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_PLACE_MONTHS_SQL, list(places), market)
    by_place: dict[str, dict[str, list]] = {}
    for r in rows:
        by_place.setdefault(r["place"], {}).setdefault(r["keyword"], []).append((r["month"], r["search_volume"]))
    return {p: seasonality(kws) for p, kws in by_place.items()}


async def counts(pool) -> dict:
    """Rows, (keyword, market) pairs and the first / last month stored (the Research tab's history line)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_COUNTS_SQL)
    return {"rows": int(row["rows"] or 0), "keywords": int(row["keywords"] or 0),
            "first_month": row["first_month"].isoformat() if row["first_month"] else None,
            "last_month": row["last_month"].isoformat() if row["last_month"] else None}


__all__ = ["MonthlyBuffer", "Season", "with_history", "monthly_from_answer", "monthly_from_results", "series_of", "rows_for", "write",
           "seasonality", "place_seasonality", "counts"]
