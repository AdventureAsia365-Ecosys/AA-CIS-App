"""AA-1048 — mutation suites per writer (root audit W2: "about 50 clean texts x 8 classes and code-injected MUTANTS
strata"; proof: "mutation recall >= 0.95 on structural / number / inclusion / meal classes and >= 0.8 on names /
service words; false findings <= 5% of clean sentences").

A suite is built from invented trips only (made-up places, lodges and activities: never text from the DB, an audit or
a prompt, so the public repo and the golden-set text guard stay clean). For each writer it makes N clean texts whose
every sentence states a fact of its own synthetic source, then injects one defect of each of the 8 classes into each
text. The deterministic detectors (detectors.py) run on every clean sentence and every mutant sentence against the
trip's source: recall per class = mutants on which at least one detector fires; false findings = clean sentences on
which any non-tripwire detector fires (tripwires only route to the claim engine, critic F7, and are reported apart).

Deterministic: the same seed gives the same suite. Pure: no DB, no network, no model call.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from services.ground_truth import detectors as det

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
# class -> the recall bar of the proof
RECALL_BARS = {"number": 0.95, "structural": 0.95, "inclusion": 0.95, "meal": 0.95, "clock": 0.95,
               "optional": 0.8, "name": 0.8, "service_word": 0.8}
CLASSES = tuple(RECALL_BARS)
# the policy class each mutation class stands for (taxonomy ids, through the policy seam's vocabulary)
POLICY_CLASS = {"number": "fact.number", "structural": "struct.day_structure", "inclusion": "fact.inclusion",
                "meal": "fact.inclusion", "clock": "other.f7", "optional": "other.f6", "name": "fact.name",
                "service_word": "fact.inclusion"}
FALSE_FINDING_BAR = 0.05
DEFAULT_TEXTS = 50

# Invented vocabulary (no real place, venue or brand).
_PLACES = ("Velmora", "Ostrin Valley", "Brannoch Lake", "Kalvey Pass", "Dunmere", "Sarrow Ridge", "Toleth Bay",
           "Hessaly Gorge", "Morrin Falls", "Quillan Steppe", "Varnek Island", "Ellowen Forest")
_UNUSED_PLACES = ("Dravenmoor", "Castellan Reach", "Wyndholt", "Orrinvale", "Brisk Harrow", "Pellamy Sound")
_LODGES = ("Harbour Lodge", "Ridge House", "Fernway Camp", "Old Mill Guesthouse", "Cedar Rest")
_WALKS = ("walk", "hike", "cycle")
_OPTIONALS = ("kayak on the lake", "ride a canoe to the far shore", "join a pottery class", "take a sunrise boat trip")
_MEALS = ("dinner", "lunch")


@dataclass(frozen=True)
class Trip:
    places: tuple[str, ...]
    days: int
    km: int
    mode: str
    lodge: str
    meal: str
    optional: str
    absent_meal: str

    def source(self, writer: str = "a1") -> str:
        """The source the writer reads: an S1 / T2 day-by-day itinerary, or for T9 a seed of moment blocks in the
        T9 seed layout ('<place> — <action>' label, 'Source itinerary:' and 'Day N of the trip' lines) that the T10
        detectors parse."""
        if writer.startswith("t9"):
            return self._seed()
        lines = [f"Day 1: Arrive in {self.places[0]} and transfer to the {self.lodge}. {self.meal.capitalize()} "
                 f"included."]
        lines.append(f"Day 2: {self.mode.capitalize()} {self.km} km to {self.places[1]}. "
                     f"Optional: {self.optional}.")
        for d in range(3, self.days + 1):
            lines.append(f"Day {d}: Continue to {self.places[min(d - 1, len(self.places) - 1)]}.")
        return "\n".join(lines)

    def _seed(self) -> str:
        blocks = [f"{self.places[0]} — arrive\nSource itinerary: Arrive in {self.places[0]} and transfer to the "
                  f"{self.lodge}. {self.meal.capitalize()} included.\nDay 1 of the trip",
                  f"{self.places[1]} — {self.mode}\nSource itinerary: {self.mode.capitalize()} {self.km} km to "
                  f"{self.places[1]}. Optional activities include {self.optional}.\nDay 2 of the trip"]
        for d in range(3, self.days + 1):
            place = self.places[min(d - 1, len(self.places) - 1)]
            blocks.append(f"{place} — visit\nSource itinerary: Continue to {place}.\nDay {d} of the trip")
        return "\n\n".join(blocks)


def make_trip(rng: random.Random) -> Trip:
    places = tuple(rng.sample(_PLACES, 4))
    meal = rng.choice(_MEALS)
    absent = "breakfast" if meal != "breakfast" else "lunch"
    return Trip(places=places, days=rng.randint(3, 5), km=rng.randint(6, 24), mode=rng.choice(_WALKS),
                lodge=rng.choice(_LODGES), meal=meal, optional=rng.choice(_OPTIONALS), absent_meal=absent)


# Clean sentences: every one states only what its trip's source states (or nothing factual).
_CLEAN = {
    "a1": (lambda t: f"The journey begins in {t.places[0]}, with a transfer to the {t.lodge}.",
           lambda t: f"On the second day you {t.mode} {t.km} km to {t.places[1]}.",
           lambda t: f"The route then continues to {t.places[2]}.",
           lambda t: "Each stage leaves time to take in the landscape."),
    "t2": (lambda t: f"Arrive in {t.places[0]} and settle in at the {t.lodge}.",
           lambda t: f"Day 2 brings a {t.km} km {t.mode} to {t.places[1]}.",
           lambda t: f"From there the trip continues to {t.places[2]}.",
           lambda t: "The pace stays relaxed from start to finish."),
    "t9_blog": (lambda t: f"{t.places[0]} is where this trip starts, at the {t.lodge}.",
                lambda t: f"The second day is a {t.km} km {t.mode} to {t.places[1]}.",
                lambda t: f"Later the itinerary moves on to {t.places[2]}.",
                lambda t: "Slow travel rewards attention to small details."),
    "t9_social": (lambda t: f"Start in {t.places[0]} with a stay at the {t.lodge}.",
                  lambda t: f"Then a {t.km} km {t.mode} to {t.places[1]}.",
                  lambda t: f"Next stop: {t.places[2]}.",
                  lambda t: "Good trips leave room to look around."),
}

# One defect per class: (trip, rng) -> the mutated sentence.
_MUTANTS: dict[str, Callable[[Trip, random.Random], str]] = {
    "number": lambda t, r: f"On the second day you {t.mode} {t.km * 3 + r.randint(5, 9)} km to {t.places[1]}.",
    "structural": lambda t, r: f"Day {t.days + r.randint(2, 4)} ends in {t.places[1]}.",
    "inclusion": lambda t, r: r.choice(("A private guide and airport transfers are included.",
                                        "Entrance fees and permits are included throughout.")),
    "meal": lambda t, r: f"{t.absent_meal.capitalize()} is included at the {t.lodge} every day.",
    "clock": lambda t, r: f"The group leaves {t.places[0]} at {r.randint(5, 7)}:{r.choice(('00', '15', '30'))} am.",
    "optional": lambda t, r: f"On day 2 you will {t.optional}.",
    "name": lambda t, r: f"On the second day you {t.mode} {t.km} km to {r.choice(_UNUSED_PLACES)}.",
    "service_word": lambda t, r: r.choice((f"You stay in a luxury lodge in {t.places[0]}.",
                                           f"Nights are spent in a boutique hotel in {t.places[0]}.")),
}


@dataclass(frozen=True)
class Case:
    writer: str
    text_id: int
    kind: str              # clean | <mutation class>
    sentence: str
    source: str
    scope: str


def build_suite(writers: Iterable[str] = WRITERS, n_texts: int = DEFAULT_TEXTS, seed: int = 1048) -> list[Case]:
    cases: list[Case] = []
    for writer in writers:
        rng = random.Random(f"{seed}:{writer}")
        for i in range(n_texts):
            trip = make_trip(rng)
            src = trip.source(writer)
            for make in _CLEAN[writer]:
                cases.append(Case(writer, i, "clean", make(trip), src, trip.places[1]))
            for cls in CLASSES:
                cases.append(Case(writer, i, cls, _MUTANTS[cls](trip, rng), src, trip.places[1]))
    return cases


def run_suite(cases: list[Case], detectors: Optional[Iterable[det.Detector]] = None) -> dict:
    """Recall per writer and class, the detectors that caught each class, and the false-finding rate on clean
    sentences (tripwires reported apart)."""
    detectors = tuple(detectors or det.DETECTORS)
    holding = tuple(d for d in detectors if d.kind != "tripwire")
    caught: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    by_detector: dict[str, Counter] = defaultdict(Counter)
    clean_fp: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    clean_by_detector: Counter = Counter()
    tripwire_clean: Counter = Counter()
    for i, c in enumerate(cases):
        claim = det.ClaimInput(f"m{i}", c.writer, "body", c.sentence, c.source, scope_name=c.scope)
        hits = set()
        for d in detectors:
            try:
                if d.fn(claim):
                    hits.add(d.name)
            except Exception:              # noqa: BLE001 — a crashing detector catches nothing
                continue
        hold_hits = hits & {d.name for d in holding}
        if c.kind == "clean":
            clean_fp[c.writer][1] += 1
            clean_fp[c.writer][0] += bool(hold_hits)
            clean_by_detector.update(hold_hits)
            tripwire_clean.update(hits - hold_hits)
            continue
        caught[(c.writer, c.kind)][1] += 1
        caught[(c.writer, c.kind)][0] += bool(hold_hits)
        by_detector[c.kind].update(hold_hits)
    recall = {}
    for (writer, cls), (hit, n) in sorted(caught.items()):
        r = hit / n if n else None
        recall.setdefault(writer, {})[cls] = {"caught": hit, "mutants": n, "recall": round(r, 4) if n else None,
                                              "bar": RECALL_BARS[cls],
                                              "passes": r is not None and r >= RECALL_BARS[cls]}
    false = {w: {"flagged": f, "clean": n, "rate": round(f / n, 4) if n else None,
                 "passes": n > 0 and f / n <= FALSE_FINDING_BAR} for w, (f, n) in sorted(clean_fp.items())}
    return {"recall": recall, "false_findings": false,
            "caught_by": {k: dict(v.most_common()) for k, v in sorted(by_detector.items())},
            "clean_flagged_by": dict(clean_by_detector.most_common()),
            "tripwires_on_clean": dict(tripwire_clean.most_common()),
            "cases": len(cases)}


def summary_lines(report: dict) -> list[str]:
    lines = []
    for writer, classes in report["recall"].items():
        cells = ", ".join(f"{c} {v['recall']:.2f}{'' if v['passes'] else ' (under ' + str(v['bar']) + ')'}"
                          for c, v in classes.items())
        ff = report["false_findings"].get(writer) or {}
        lines.append(f"{writer}: {cells}; false findings {ff.get('rate')}")
    return lines


__all__ = ["CLASSES", "Case", "DEFAULT_TEXTS", "FALSE_FINDING_BAR", "POLICY_CLASS", "RECALL_BARS", "Trip",
           "WRITERS", "build_suite", "make_trip", "run_suite", "summary_lines"]
