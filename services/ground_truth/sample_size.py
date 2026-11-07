"""AA-1048 — how many labelled fires a gate needs before it may hold (critic F2: size CL-1 from a table, not "60
claims").

A promotion bar is stated in the metric it measures (root audit README "Gate honesty"):
  LLM instrument          precision >= 0.90, Wilson-95 lower bound >= 0.80, n_pos >= 20
  deterministic detector  precision >= 0.90, Wilson-95 lower bound >= 0.75, n_pos >= 15, labeller agreement >= 0.7
Precision and its lower bound are measured on the FLAGGED claims (every fire of the detector, on a claim of any
class), recall on n_pos (the labelled violations of the class). So a class needs (a) enough labelled fires for the
lower bound at the precision it shows and (b) enough labelled violations for recall. `min_flagged` answers (a);
`label_budget` turns (a) and (b) into a number of claims to label (of any class: a claim's class is only known once
it is labelled) from the fire rate and violation rate a first panel pass measures over all its labelled claims.

Pure: standard library only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

Z95 = 1.959963984540054
MAX_N = 5000


@dataclass(frozen=True)
class Bar:
    name: str
    precision: float
    lower_bound: float
    n_pos: int
    agreement: Optional[float] = None       # labeller agreement of the class the bar needs (None: not part of it)


BARS = (Bar("llm_instrument", 0.90, 0.80, 20), Bar("deterministic", 0.90, 0.75, 15, agreement=0.7))
# Observed precisions the table is printed for.
OBSERVED = (0.90, 0.92, 0.95, 0.98, 1.00)


def wilson_lower(successes: int, n: int, z: float = Z95) -> Optional[float]:
    """Wilson score interval lower bound of successes / n (None for n = 0)."""
    if n <= 0:
        return None
    if not 0 <= successes <= n:
        raise ValueError(f"successes {successes} not in 0..{n}")
    p = successes / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


def passes(tp: int, fired: int, n_pos: int, bar: Bar, agreement: Optional[float] = None) -> bool:
    """The bar on measured counts: precision, its lower bound, the number of labelled violations and, when the bar
    has one, the labeller agreement of the class (unknown agreement does not pass)."""
    if fired <= 0:
        return False
    if bar.agreement is not None and (agreement is None or agreement < bar.agreement):
        return False
    lb = wilson_lower(tp, fired)
    return tp / fired >= bar.precision and lb is not None and lb >= bar.lower_bound and n_pos >= bar.n_pos


def min_flagged(observed_precision: float, bar: Bar) -> Optional[int]:
    """The smallest number of labelled fires at which a detector showing `observed_precision` clears the bar's
    precision and lower bound. Conservative: true positives = floor(observed x n), so the measured precision is never
    better than the observed one (a ceiling would count 16 of 16 as "0.95"). None when it cannot (precision under the
    bar, or no n up to MAX_N)."""
    if observed_precision < bar.precision:
        return None
    for n in range(1, MAX_N + 1):
        tp = math.floor(observed_precision * n + 1e-9)
        lb = wilson_lower(tp, n)
        if tp / n >= bar.precision and lb is not None and lb >= bar.lower_bound:
            return n
    return None


def table(bars: Iterable[Bar] = BARS, observed: Iterable[float] = OBSERVED) -> list[dict]:
    rows = []
    for bar in bars:
        for p in observed:
            rows.append({"bar": bar.name, "precision_bar": bar.precision, "lb_bar": bar.lower_bound,
                         "n_pos_bar": bar.n_pos, "observed_precision": p, "min_flagged": min_flagged(p, bar)})
    return rows


def label_budget(*, fire_rate: float, violation_rate: float, observed_precision: float, bar: Bar) -> Optional[int]:
    """Claims to label (of any class) so that (a) the detector's fires reach `min_flagged` and (b) the labelled
    violations of the class reach the bar's n_pos, given the share of ALL labelled claims the detector fires on and
    the share that are violations of the class (both from a first panel pass, detectors.measure on the train split).
    None when the bar is out of reach (precision under the bar, or a rate of 0)."""
    need = min_flagged(observed_precision, bar)
    if need is None or fire_rate <= 0 or violation_rate <= 0:
        return None
    return max(math.ceil(need / fire_rate), math.ceil(bar.n_pos / violation_rate))


def budget_rows(rates: list[dict], *, budget: Optional[int] = None) -> list[dict]:
    """Per detector / class row of `rates` ({detector, class, kind: llm | deterministic (default), fire_rate,
    violation_rate, observed_precision or precision}; a rows file of `cli detectors` works as is): the claims to
    label and, with a total `budget`, whether the class can reach its bar ('stays warn' otherwise: say so up front,
    critic F2)."""
    out = []
    for r in rates:
        bar = BARS[0] if r.get("kind") == "llm" else BARS[1]
        observed = r.get("observed_precision", r.get("precision"))
        rates_ok = all(isinstance(v, (int, float)) for v in (r.get("fire_rate"), r.get("violation_rate"), observed))
        need = label_budget(fire_rate=float(r["fire_rate"]), violation_rate=float(r["violation_rate"]),
                            observed_precision=float(observed), bar=bar) if rates_ok else None
        status = "out of reach" if need is None else (
            "reachable" if budget is None or need <= budget else "stays warn (over the label budget)")
        out.append({**r, "bar": bar.name, "claims_to_label": need, "status": status})
    return out


def markdown(rows: list[dict]) -> str:
    """The sample-size table as Markdown (docs/calibration/aa1048-sample-size.md holds this output; a unit test keeps
    them equal)."""
    lines = ["| Bar | Precision bar | Lower-bound bar | n_pos bar | Observed precision | Labelled fires needed |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        need = "never" if r["min_flagged"] is None else str(r["min_flagged"])
        lines.append(f"| {r['bar']} | {r['precision_bar']:.2f} | {r['lb_bar']:.2f} | {r['n_pos_bar']} | "
                     f"{r['observed_precision']:.2f} | {need} |")
    return "\n".join(lines) + "\n"


__all__ = ["BARS", "Bar", "MAX_N", "OBSERVED", "Z95", "budget_rows", "label_budget", "markdown", "min_flagged",
           "passes", "table", "wilson_lower"]
