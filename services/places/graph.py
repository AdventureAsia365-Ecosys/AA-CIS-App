"""AA-712 — tour graph builders (pure). Port of AA-TripPlanner-Web backend/extraction/tour_graph.py
(AA-673, @ 6523816); rows are keyed by source_id (the catalog tour identity) instead of tour_id.

  stop      a (source, day) and where the traveller is: the overnight destination, else the
            destination with the most places that day (ties by name). AA-847: a LINE destination (river,
            road, trail: geo_precision 'line') is never a stop — a river day with no overnight takes its other
            place, and "a night on the Mekong" (an overnight on a line) falls back to the day's main point place;
            the line itself is drawn as the day's stretch (geo.tour_segment)
  node      a destination with at least one stop
  edge      A -> B when a tour goes from stop A to a different stop B next (days without a stop are
            skipped over); weighted by the number of tours taking that step
  leg       every span [day_from, day_to] (day_to > day_from) of one tour whose first and last days
            have a stop — the unit an advisor can sell
  junction  two destinations within `junction_km` (default 150), both directions

No model call, no cost. Persisted as one versioned build (services/places/store.py swap_graph).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from services.places.guards import km as _km_raw

DEFAULT_JUNCTION_KM = 150.0
INTENSITY_ORDER = ["leisurely", "moderate", "active", "strenuous"]


@dataclass(frozen=True)
class Stop:
    source_id: str
    day_index: int
    destination_id: object
    source: str  # overnight | component


@dataclass
class Graph:
    stops: list[Stop] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    legs: list[dict] = field(default_factory=list)
    junctions: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


LINE = "line"


def is_line(dests: dict, destination_id) -> bool:
    return (dests.get(destination_id) or {}).get("precision") == LINE


def main_component_destinations(places: Iterable[dict], dests: dict) -> dict[tuple, object]:
    """(source_id, day) -> the non-line destination with the most places that day, ties by name."""
    count: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    for p in places:
        if p.get("destination_id") is not None and p["destination_id"] in dests \
                and not is_line(dests, p["destination_id"]):
            count[(p["source_id"], p["day_index"])][p["destination_id"]] += 1
    return {k: sorted(v.items(), key=lambda kv: (-kv[1], dests[kv[0]]["name"]))[0][0] for k, v in count.items()}


def stop_rows(days: Iterable[dict], places: list[dict], dests: dict) -> list[dict]:
    """Rows for choose_stops from geo.tour_day + geo.tour_place rows of live sources."""
    main = main_component_destinations(places, dests)
    return [{"source_id": d["source_id"], "day_index": d["day_index"],
             # a night "on the Mekong" (overnight on a line): not a stop, the day's main point place is
             "overnight_destination_id": (None if is_line(dests, d.get("overnight_destination_id"))
                                          else d.get("overnight_destination_id")),
             "component_destination_id": main.get((d["source_id"], d["day_index"]))}
            for d in sorted(days, key=lambda r: (r["source_id"], r["day_index"]))]


def choose_stops(rows: Iterable[dict]) -> list[Stop]:
    """One stop per (source, day): the overnight destination, else the main place destination."""
    out = []
    for r in rows:
        if r["overnight_destination_id"] is not None:
            out.append(Stop(r["source_id"], r["day_index"], r["overnight_destination_id"], "overnight"))
        elif r.get("component_destination_id") is not None:
            out.append(Stop(r["source_id"], r["day_index"], r["component_destination_id"], "component"))
    return out


def _by_source(stops: Iterable[Stop]) -> dict[str, list[Stop]]:
    tours: dict[str, list[Stop]] = defaultdict(list)
    for s in stops:
        tours[s.source_id].append(s)
    for seq in tours.values():
        seq.sort(key=lambda s: s.day_index)
    return tours


def _km(dests: dict, a, b) -> float:
    da, db = dests[a], dests[b]
    return _km_raw(da["lat"], da["lng"], db["lat"], db["lng"])


def build_edges(stops: Iterable[Stop], dests: dict) -> list[dict]:
    steps: dict[tuple, set] = defaultdict(set)
    for source_id, seq in _by_source(stops).items():
        for prev, nxt in zip(seq, seq[1:]):
            if prev.destination_id != nxt.destination_id:
                steps[(prev.destination_id, nxt.destination_id)].add(source_id)
    return [{"from_destination_id": a, "to_destination_id": b, "tour_count": len(t),
             "source_ids": sorted(t), "km": round(_km(dests, a, b), 1)}
            for (a, b), t in sorted(steps.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))]


def _dedupe_consecutive(values: Iterable) -> list:
    out: list = []
    for v in values:
        if not out or out[-1] != v:
            out.append(v)
    return out


def build_legs(stops: Iterable[Stop], dests: dict, places: Iterable[dict]) -> list[dict]:
    """Every span between two stop days of the same tour (day_to > day_from)."""
    acts: dict[tuple, set] = defaultdict(set)  # (source, day) -> activities
    for p in places:
        acts[(p["source_id"], p["day_index"])].add(p["activity"])
    legs = []
    for source_id, seq in _by_source(stops).items():
        for i in range(len(seq)):
            for j in range(i + 1, len(seq)):
                span = seq[i:j + 1]
                day_from, day_to = span[0].day_index, span[-1].day_index
                dest_ids = _dedupe_consecutive(s.destination_id for s in span)
                countries = list(dict.fromkeys(dests[d]["country"] for d in dest_ids))
                activities = sorted(set().union(*(acts.get((source_id, d), set())
                                                  for d in range(day_from, day_to + 1))))
                legs.append({"source_id": source_id, "day_from": day_from, "day_to": day_to,
                             "days": day_to - day_from + 1,
                             "start_destination_id": span[0].destination_id,
                             "end_destination_id": span[-1].destination_id,
                             "destination_ids": dest_ids, "countries": countries,
                             "activities": activities})
    return legs


def build_junctions(dest_ids: Iterable, dests: dict, max_km: float) -> list[dict]:
    """Destination pairs within `max_km`, both directions."""
    ids = sorted(set(dest_ids), key=str)
    out = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            dist = _km(dests, a, b)
            if dist <= max_km:
                cross = dests[a]["country"] != dests[b]["country"]
                out.append({"destination_a": a, "destination_b": b, "km": round(dist, 1), "cross_border": cross})
                out.append({"destination_a": b, "destination_b": a, "km": round(dist, 1), "cross_border": cross})
    return out


def build_nodes(stops: Iterable[Stop], dests: dict, places: Iterable[dict]) -> list[dict]:
    tours: dict = defaultdict(set)
    count: dict = defaultdict(int)
    for s in stops:
        tours[s.destination_id].add(s.source_id)
        count[s.destination_id] += 1
    acts: dict = defaultdict(set)
    levels: dict = defaultdict(set)
    months: dict = defaultdict(set)
    for p in places:
        d = p.get("destination_id")
        if d in tours:
            acts[d].add(p["activity"])
            if p.get("intensity_level") in INTENSITY_ORDER:
                levels[d].add(p["intensity_level"])
            months[d].update(p.get("season_months") or [])
    nodes = []
    for d in sorted(tours, key=str):
        lv = sorted(levels[d], key=INTENSITY_ORDER.index)
        nodes.append({"destination_id": d, "name": dests[d]["name"], "country": dests[d]["country"],
                      "lat": dests[d]["lat"], "lng": dests[d]["lng"], "source_ids": sorted(tours[d]),
                      "stop_count": count[d], "activities": sorted(acts[d]),
                      "intensity_min": lv[0] if lv else None, "intensity_max": lv[-1] if lv else None,
                      "season_months": sorted(months[d])})
    return nodes


def _per_country(g: Graph, dests: dict) -> dict:
    stats: dict = defaultdict(lambda: {"nodes": 0, "edges": 0, "legs": 0, "junctions": 0})
    for n in g.nodes:
        stats[n["country"]]["nodes"] += 1
    for e in g.edges:
        stats[dests[e["from_destination_id"]]["country"]]["edges"] += 1
    for leg in g.legs:
        for c in leg["countries"]:
            stats[c]["legs"] += 1
    for j in g.junctions:
        stats[dests[j["destination_a"]]["country"]]["junctions"] += 1  # directed rows
    return dict(sorted(stats.items()))


def build_graph(rows: Iterable[dict], dests: dict, places: list[dict],
                junction_km: float = DEFAULT_JUNCTION_KM) -> Graph:
    stops = [s for s in choose_stops(rows) if s.destination_id in dests and not is_line(dests, s.destination_id)]
    g = Graph(stops=stops)
    g.nodes = build_nodes(stops, dests, places)
    g.edges = build_edges(stops, dests)
    g.legs = build_legs(stops, dests, places)
    g.junctions = build_junctions((n["destination_id"] for n in g.nodes), dests, junction_km)
    g.stats = {"tours": len({s.source_id for s in stops}), "stops": len(stops),
               "stops_from_components": sum(1 for s in stops if s.source == "component"),
               "nodes": len(g.nodes), "edges": len(g.edges), "legs": len(g.legs),
               "junction_pairs": len(g.junctions) // 2,
               "cross_border_pairs": sum(1 for j in g.junctions if j["cross_border"]) // 2,
               "per_country": _per_country(g, dests)}
    return g
