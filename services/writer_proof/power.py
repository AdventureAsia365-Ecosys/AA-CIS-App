"""AA-1051 — the power check of a pre-registration from the R1 vote variance ($0; S271 critic fix F12: "binary
per-item paired criterion on about 14 tune and 4-6 holdout pairs ... a minimum detectable effect of about 0.7-0.9").

Before a paid proof, check that its fixed sample can tell "no worse" from "worse by more than the margin", using what
the R1 calibration already measured: the claim instrument's own disagreement with itself.

  1. Per labelled claim with >= 2 real votes (r1 report `votes[instrument][claim_hash]`), q = the share of votes that
     flagged it, and m(q) = P(majority of V votes flags it) = P(Binomial(V, q) >= V // 2 + 1): the chance a re-judge
     of the same text flags it. Its re-judge variance is m(1 - m).
  2. Claims are grouped into pseudo-items of `claims_per_item` claims (by claim hash order: no data-driven grouping).
     Per pseudo-item, the variance of the primary metric under a re-judge:
        fail_claims_per_item / fail_classes_per_item   sum of m(1 - m)            (a count)
        item_fail_rate                                 P(1 - P), P = 1 - prod(1 - m)   (any claim flagged)
     The judge-noise variance of one item's metric (var_j) is their mean.
  3. One paired difference d = B - A of one item, both judged independently, has Var(d) = 2 var_j + writer_sd^2
     (writer_sd: the pre-registration's assumption for the writer's own run-to-run spread; null = 0, so the result is
     an UPPER bound on power: the judge noise alone).
  4. The decision (decision.py) declares non_inferior when mean(d) + z_(1-alpha) SE <= margin, SE = sqrt(Var(d) / n).
     If the true difference is 0, power = Phi(margin / SE - z_(1-alpha)); the n that reaches min_power is
     ceil((z_(1-alpha) + z_power)^2 Var(d) / margin^2), and the smallest margin the sample can resolve at min_power is
     (z_(1-alpha) + z_power) SE.

`adequate` is power >= min_power at the pre-registered n and margin. An underpowered pre-registration is refused by the
runner's spend gate (a paid run that cannot decide is waste: the S271 lesson "an eval refuses at $0 when it cannot
give a valid verdict").

    python -m services.writer_proof.power --r1-report <scratch>/r1-report.json --instrument <id> --prereg <id|path>
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from statistics import NormalDist
from typing import Any, Optional

DEFAULT_CLAIMS_PER_ITEM = {"t9_blog": 60, "t9_social": 12, "a1": 68, "t2": 59}   # split_plan.CLAIMS_PER_OBJECT (S273)
MIN_CLAIMS = 20                     # fewer real-vote claims: the variance estimate is not worth a decision


class PowerRefused(ValueError):
    """The votes cannot support a power estimate (no instrument, too few claims with two real votes)."""


def majority_prob(q: float, votes: int) -> float:
    """P(Binomial(votes, q) >= votes // 2 + 1)."""
    need = votes // 2 + 1
    return sum(math.comb(votes, k) * q ** k * (1 - q) ** (votes - k) for k in range(need, votes + 1))


def claim_rates(votes_by_claim: dict[str, list]) -> list[tuple[str, float, int]]:
    """(claim hash, m, votes) per claim with >= 2 real votes, in hash order."""
    out = []
    for h in sorted(votes_by_claim):
        real = [v for v in votes_by_claim[h] or [] if v is not None]
        if len(real) < 2:
            continue
        q = sum(1 for v in real if v) / len(real)
        out.append((h, majority_prob(q, len(real)), len(real)))
    return out


def judge_variance(rates: list[tuple[str, float, int]], *, metric: str, claims_per_item: int) -> dict:
    """var_j of one item's metric under a re-judge, from pseudo-items of `claims_per_item` claims."""
    if claims_per_item < 1:
        raise PowerRefused("claims_per_item must be >= 1")
    ms = [m for _, m, _ in rates]
    groups = [ms[i:i + claims_per_item] for i in range(0, len(ms), claims_per_item)]
    full = [g for g in groups if len(g) == claims_per_item]
    if not full:                      # fewer claims than one item: scale the per-claim variance up
        per_claim = sum(m * (1 - m) for m in ms) / len(ms)
        if metric == "item_fail_rate":
            mbar = sum(ms) / len(ms)
            p = 1 - (1 - mbar) ** claims_per_item
            return {"var": p * (1 - p), "pseudo_items": 0, "scaled": True}
        return {"var": per_claim * claims_per_item, "pseudo_items": 0, "scaled": True}
    vals = []
    for g in full:
        if metric == "item_fail_rate":
            p = 1 - math.prod(1 - m for m in g)
            vals.append(p * (1 - p))
        else:
            vals.append(sum(m * (1 - m) for m in g))
    return {"var": sum(vals) / len(vals), "pseudo_items": len(full), "scaled": False}


def check(votes_by_claim: dict[str, list], *, n_items: int, margin_abs: float, metric: str, alpha: float = 0.05,
          min_power: float = 0.8, claims_per_item: int = 60, writer_sd: Optional[float] = None) -> dict:
    """The power record of one pre-registered decision. Pure."""
    if n_items < 1:
        raise PowerRefused("the pre-registration has no sample ids")
    if not margin_abs > 0:
        raise PowerRefused("margin_abs must be > 0")
    rates = claim_rates(votes_by_claim)
    if len(rates) < MIN_CLAIMS:
        raise PowerRefused(f"{len(rates)} claims with >= 2 real votes; >= {MIN_CLAIMS} needed for a variance")
    flips = sum(1 for _, m, _ in rates if 0 < m < 1)
    jv = judge_variance(rates, metric=metric, claims_per_item=claims_per_item)
    var_d = 2 * jv["var"] + float(writer_sd or 0.0) ** 2
    nd = NormalDist()
    z_a, z_b = nd.inv_cdf(1 - alpha), nd.inv_cdf(min_power)
    se = math.sqrt(var_d / n_items)
    power = 1.0 if se == 0 else nd.cdf(margin_abs / se - z_a)
    n_needed = 1 if var_d == 0 else math.ceil((z_a + z_b) ** 2 * var_d / margin_abs ** 2)
    return {"metric": metric, "claims": len(rates), "claims_not_unanimous": flips,
            "claims_per_item": claims_per_item, "pseudo_items": jv["pseudo_items"], "scaled": jv["scaled"],
            "judge_var_per_item": round(jv["var"], 6), "writer_sd": writer_sd,
            "var_diff": round(var_d, 6), "n_items": n_items, "se_diff": round(se, 6), "margin_abs": margin_abs,
            "alpha": alpha, "power_at_zero": round(power, 4), "min_power": min_power,
            "n_needed": n_needed, "min_resolvable_margin": round((z_a + z_b) * se, 6),
            "adequate": power >= min_power,
            "bound": "upper bound (judge noise only)" if writer_sd is None else "judge noise + assumed writer_sd"}


def check_prereg(pre: Any, votes_by_claim: dict[str, list]) -> dict:
    """`check` with the pre-registration's own n, margin, metric, alpha and power settings."""
    from services.writer_proof.prereg import WORKFLOWS
    doc = pre.doc
    pw = doc.get("power") or {}
    writer = WORKFLOWS[doc["workflow"]]["writer"]
    return check(votes_by_claim, n_items=len(doc.get("sample_ids") or []), margin_abs=float(doc["decision"]
                 ["margin_abs"]), metric=doc["decision"]["metric"], alpha=float(doc["decision"]["alpha"]),
                 min_power=float(pw.get("min_power", 0.8)),
                 claims_per_item=int(pw.get("claims_per_item") or DEFAULT_CLAIMS_PER_ITEM[writer]),
                 writer_sd=pw.get("writer_sd"))


def votes_of(report: dict, instrument: Optional[str]) -> dict[str, list]:
    """The votes of one instrument in an R1 report file (scripts/aa1048/r1_calibrate.py --out: {"votes": {...}})."""
    votes = report.get("votes") or {}
    if not votes:
        raise PowerRefused("the R1 report has no votes (write it with r1_calibrate --out)")
    if instrument is None:
        instrument = report.get("recommended") or (sorted(votes)[0] if len(votes) == 1 else None)
    if instrument not in votes:
        raise PowerRefused(f"instrument {instrument!r} not in the report ({', '.join(sorted(votes))})")
    return votes[instrument]


def main(argv: Optional[list[str]] = None) -> int:
    from services.writer_proof import prereg as pr
    ap = argparse.ArgumentParser(prog="python -m services.writer_proof.power", description=__doc__.split("\n")[0])
    ap.add_argument("--r1-report", required=True, help="the R1 report JSON with votes (r1_calibrate --out)")
    ap.add_argument("--instrument", help="instrument id (default: the report's recommended one)")
    ap.add_argument("--prereg", required=True, help="pre-registration id or path")
    a = ap.parse_args(argv)
    try:
        pre = pr.load(a.prereg)
        with open(a.r1_report, encoding="utf-8") as f:
            report = json.load(f)
        out = check_prereg(pre, votes_of(report, a.instrument))
    except (pr.PreregRefused, PowerRefused, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    print(json.dumps({"ok": out["adequate"], "prereg": pre.id, "sha256": pre.sha256, "power": out}, indent=1))
    return 0 if out["adequate"] else 1


if __name__ == "__main__":
    sys.exit(main())
