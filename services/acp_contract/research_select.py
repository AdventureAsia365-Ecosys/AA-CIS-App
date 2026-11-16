"""services/acp_contract/research_select.py — AA-1122: what segment research chooses inside a place, and the Jev part of
the research caps.

1. **Close variants (item 2).** Google Ads answers close variants ("tiger nest hike" / "tigers nest hike") with the
   volume of their whole group, the same number for each, and sending them in separate tasks does not split them
   (critic C5 of the S282 DFS audit; no clickstream is bought). So the keywords of one place whose volume is identical
   in every market it was measured in are ONE demand group (:func:`demand_groups`): their volumes are never added nor
   compared as if they were separate demand. The one SERP of a place is bought for the group with the most demand, and
   INSIDE the group the keyword is chosen by SERP presence (a variant whose SERP is already fresh in the store: nothing
   to buy), then by naturalness (the earliest in the place's map: the plain place name first, then the proposals in the
   order a traveller types them), then the fewest words, never by volume (:func:`best_keyword`).
2. **Jev questions under the caps (item 4).** The keyword gate asks Jev about each proposed keyword (paid). Before the
   questions, their worst case (``cis.spend.research_jev_ask_estimate_usd`` each) is reserved against the run budget's
   shared caps (the month cap, the automatic run cap: shared/research_budget.py); after, what Jev really cost is
   charged (:func:`reserve_jev`, :func:`charge_jev`).
"""
from __future__ import annotations

from shared.cost_guard import BudgetExceeded


def demand_groups(keywords: list[str], per_market: dict[str, dict[str, int]]) -> list[list[str]]:
    """The keywords grouped by demand: identical volume in every measured market and above 0 somewhere = one group; a
    keyword with no volume above 0 is its own group. Groups keep the map order of their first keyword. Pure."""
    groups: dict[object, list[str]] = {}
    for i, kw in enumerate(keywords):
        vols = per_market.get(kw) or {}
        key: object = tuple(sorted(vols.items())) if any(v > 0 for v in vols.values()) else ("solo", i)
        groups.setdefault(key, []).append(kw)
    return list(groups.values())


def best_keyword(place, best_volume: dict[str, int], *, serp_fresh: set[str] | frozenset = frozenset(),
                 per_market: dict[str, dict[str, int]] | None = None) -> str | None:
    """The keyword the one SERP of `place` is bought for (module docstring item 1), or None when none has a volume
    above 0. Without `per_market` every keyword is its own group (the pre-AA-1122 rule: the highest volume, ties by map
    order). Pure."""
    if not place.keywords:
        return None
    order = {kw: i for i, kw in enumerate(place.keywords)}
    groups = demand_groups(place.keywords, per_market) if per_market is not None else [[kw] for kw in place.keywords]
    top = max(groups, key=lambda g: (max(best_volume.get(kw, 0) for kw in g), -order[g[0]]))
    if max(best_volume.get(kw, 0) for kw in top) <= 0:
        return None
    return min(top, key=lambda kw: (kw not in serp_fresh, order[kw], len(kw.split())))


def serp_state(rows, wanted: list, serp_market: str) -> tuple[dict, dict, set, set]:
    """(best volume per keyword, keyword -> market -> volume, keywords with a fresh SERP, keywords whose SERP-market
    volume is known) from the store rows (_SERP_STATE_SQL) and this run's answers on the places. Pure."""
    best: dict[str, int] = {}
    per_market: dict[str, dict[str, int]] = {}
    fresh: set[str] = set()
    known: set[str] = set()
    for r in rows:
        best[r["keyword"]] = max(best.get(r["keyword"], 0), r["search_volume"] or 0)
        if r["search_volume"] is not None:
            per_market.setdefault(r["keyword"], {})[r["market"]] = r["search_volume"]
        if r["market"] == serp_market and r["serp_fresh"]:
            fresh.add(r["keyword"])
        if r["market"] == serp_market and r["volume_fresh"]:
            known.add(r["keyword"])
    for p in wanted:   # this run's answers too (a mock or a store write that has not landed)
        for kw, per in p.volumes.items():
            best[kw] = max(best.get(kw, 0), *[(v or 0) for v in per.values()], 0)
            per_market.setdefault(kw, {}).update({m: v for m, v in per.items() if v is not None})
            if serp_market in per:
                known.add(kw)
    return best, per_market, fresh, known


def _usd(value) -> float:
    """A cost figure as float (0.0 for anything that is not a number, e.g. a test double)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def jev_ask_estimate_usd() -> float:
    """The reserved worst-case price of one Jev keyword question (setting cis.spend.research_jev_ask_estimate_usd)."""
    from services.seo_intelligence.research_settings import JEV_ASK_ESTIMATE_USD
    from shared import settings
    return float(settings.get(JEV_ASK_ESTIMATE_USD))


def reserve_jev(llm_budget, questions: int, places: list, guard=None) -> tuple:
    """(the shared caps of `llm_budget`, whether the questions may be asked). A cap that cannot hold their worst case
    fails `places` (nothing of them is bought; they stay stale for the next run) and records the stop on `guard`."""
    caps = tuple(getattr(llm_budget, "shared_caps", ()) or ())
    try:
        for cap in caps:
            cap.check(questions * jev_ask_estimate_usd())
    except BudgetExceeded as exc:
        if guard is not None:
            guard.record(exc)
        for p in places:
            p.failed = True
        return caps, False
    return caps, True


def charge_jev(caps: tuple, decisions) -> None:
    """Charge what the Jev answers really cost to the shared caps."""
    cost = sum(_usd(getattr(dec, "cost_usd", 0.0)) for dec in decisions)
    for cap in caps:
        cap.charge(cost)


__all__ = ["demand_groups", "best_keyword", "serp_state", "jev_ask_estimate_usd", "reserve_jev", "charge_jev"]
