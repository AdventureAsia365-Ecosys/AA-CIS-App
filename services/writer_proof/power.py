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
  4. The power of the pre-registered rule (decision.py), SE = sqrt(Var(d) / n), d signed so that positive = worse:
     non_inferior  declared when mean(d) + z_(1-alpha) SE <= margin. At a true difference of 0,
                   power = Phi(margin / SE - z_(1-alpha)); the n that reaches min_power is
                   ceil((z_(1-alpha) + z_power)^2 Var(d) / margin^2), and the smallest margin the sample can resolve
                   at min_power is (z_(1-alpha) + z_power) SE.
     improve       declared when mean(d) + z_(1-alpha) SE < 0 AND -mean(d) >= margin, i.e. mean(d) <= -max(z SE,
                   margin). At the pre-registered true improvement delta (power.effect_abs, > margin),
                   power = Phi((delta - max(z_(1-alpha) SE, margin)) / SE): at delta == margin it is at most 0.5.
                   The n that reaches min_power needs SE <= min(delta / (z_(1-alpha) + z_power),
                   (delta - margin) / z_power); the smallest true improvement the sample detects at min_power is
                   max((z_(1-alpha) + z_power) SE, margin + z_power SE).
     The noise allowance (k SE_noise of the A' arm) is unknown before the run: the margin stands for it, so both are
     upper bounds when the A' noise turns out larger.

The writer's spread is real in every W5 workflow: B is regenerated, A is the stored output (one earlier draw of the
same writer), so d also carries which sentences each draw wrote (S283 review). For a count metric the claim rates give
a reference minimum, `writer_sd_floor` = sqrt(2 x claims_per_item x Var(m)): the spread of d if each blog's sentences
were independent draws from the measured claims (Var(m) over every claim with rates; within-blog correlation only
widens it). A registered pre-registration states power.writer_sd (prereg.py); a stated value below the floor makes
the check inadequate (`writer_sd_below_floor`), so a paid run never rests on a writer spread the data already refutes.
`item_fail_rate` has no floor (None): its writer spread is stated, not derived.

`adequate` is power >= min_power at the pre-registered n, margin, rule and (improve) true effect, and the stated
writer_sd is not below the floor. An underpowered
pre-registration is refused by the runner's spend gate (a paid run that cannot decide is waste: the S271 lesson "an
eval refuses at $0 when it cannot give a valid verdict").

AA-1051 S283, a claim instrument judge (prereg judge.instrument, services/writer_proof/instrument.py): the chain
R1 fire -> two-step v7 has no single vote list. Its per-claim re-judge flag chance (`chain_rates`) is
  m = P(the fire majority does not clear it) x P(the two-step flags it | it reached the two-step),
from the fire report's votes (r1_fire --out: {"votes": {fire instrument id: {claim_hash: [...]}}}) and the two-step
gated report's step votes (two_step run --out: {"votes": {"classify": {hash16: [...]}, "judge": {hash16: [...]}}}):
P(two-step flags) = P(step 1 majority is not a restatement) x P(step 2 majority flags). A fired claim the two-step run
did not judge takes the mean of the judged ones (the gated subset is half stored violations, so this leans high: more
flags, more variance, a lower power). The rest of the check is unchanged.

    python -m services.writer_proof.power --r1-report <scratch>/r1-report.json --instrument <id> --prereg <id|path>
    python -m services.writer_proof.power --r1-report <fire-report.json> --two-step-report <ts-report.json> \\
        --prereg <id|path>                                          (a pre-registration judged by a claim instrument)
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


def chain_rates(fire_votes: dict[str, list], step_votes: dict[str, dict[str, list]]) -> list[tuple[str, float, int]]:
    """(claim hash, m, fire votes) per claim with >= 2 real fire votes, in hash order: the chance a re-judge by the
    chain R1 fire -> two-step flags it (module docstring). `step_votes`: {"classify": {hash16: votes}, "judge": {...}}
    keyed by the first 16 hex of the claim hash, as the two-step report writes them. Pure."""
    cls, jdg = step_votes.get("classify") or {}, step_votes.get("judge") or {}
    p_ts = {}
    for h16, v1 in cls.items():
        real1 = [v for v in v1 or [] if v is not None]
        real2 = [v for v in (jdg.get(h16) or []) if v is not None]
        if len(real1) < 2:
            continue
        routed = 1.0 - majority_prob(sum(1 for v in real1 if not v) / len(real1), len(real1))
        flags = majority_prob(sum(1 for v in real2 if v) / len(real2), len(real2)) if len(real2) >= 2 else None
        p_ts[h16] = (routed, flags)
    known = [f for _r, f in p_ts.values() if f is not None]
    mean_flag = sum(known) / len(known) if known else 1.0
    mean_ts = (sum(r * (mean_flag if f is None else f) for r, f in p_ts.values()) / len(p_ts)) if p_ts else 1.0
    out = []
    for h in sorted(fire_votes):
        real = [v for v in fire_votes[h] or [] if v is not None]
        if len(real) < 2:
            continue
        reach = 1.0 - majority_prob(sum(1 for v in real if not v) / len(real), len(real))
        r, f = p_ts.get(h[:16], (None, None))
        ts_flag = mean_ts if r is None else r * (mean_flag if f is None else f)
        out.append((h, reach * ts_flag, len(real)))
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


def writer_sd_floor(rates: list[tuple[str, float, int]], *, metric: str, claims_per_item: int) -> Optional[float]:
    """The reference minimum of the writer's spread of d for a count metric (module docstring): sqrt(2 x
    claims_per_item x the population variance of m over the claims). None for item_fail_rate or fewer than 2 claims.
    Pure."""
    if metric == "item_fail_rate" or len(rates) < 2:
        return None
    ms = [m for _, m, _ in rates]
    mbar = sum(ms) / len(ms)
    return math.sqrt(2 * claims_per_item * sum((m - mbar) ** 2 for m in ms) / len(ms))


def check(votes_by_claim: dict[str, list], *, n_items: int, margin_abs: float, metric: str, alpha: float = 0.05,
          min_power: float = 0.8, claims_per_item: int = 60, writer_sd: Optional[float] = None,
          rule: str = "non_inferior", effect_abs: Optional[float] = None,
          rates: Optional[list[tuple[str, float, int]]] = None) -> dict:
    """The power record of one pre-registered decision (`rule` non_inferior at a true difference of 0, or improve at
    the true improvement `effect_abs`). `rates` (chain_rates) replaces the rates of `votes_by_claim`. Pure."""
    if n_items < 1:
        raise PowerRefused("the pre-registration has no sample ids")
    if not margin_abs > 0:
        raise PowerRefused("margin_abs must be > 0")
    if rule not in ("non_inferior", "improve"):
        raise PowerRefused(f"no power rule for decision rule {rule!r}")
    if rule == "improve" and not (effect_abs is not None and effect_abs > 0):
        raise PowerRefused("an improve decision needs the true improvement it is powered for (power.effect_abs)")
    rates = claim_rates(votes_by_claim) if rates is None else rates
    if len(rates) < MIN_CLAIMS:
        raise PowerRefused(f"{len(rates)} claims with >= 2 real votes; >= {MIN_CLAIMS} needed for a variance")
    flips = sum(1 for _, m, _ in rates if 0 < m < 1)
    jv = judge_variance(rates, metric=metric, claims_per_item=claims_per_item)
    var_d = 2 * jv["var"] + float(writer_sd or 0.0) ** 2
    floor = writer_sd_floor(rates, metric=metric, claims_per_item=claims_per_item)
    below = writer_sd is not None and floor is not None and float(writer_sd) < floor - 1e-9
    nd = NormalDist()
    z_a, z_b = nd.inv_cdf(1 - alpha), nd.inv_cdf(min_power)
    se = math.sqrt(var_d / n_items)
    if rule == "non_inferior":
        effect = 0.0
        power = 1.0 if se == 0 else nd.cdf(margin_abs / se - z_a)
        n_needed: Optional[int] = 1 if var_d == 0 else math.ceil((z_a + z_b) ** 2 * var_d / margin_abs ** 2)
        resolvable = {"min_resolvable_margin": round((z_a + z_b) * se, 6)}
    else:
        effect = float(effect_abs)
        if se == 0:
            power = 1.0 if effect >= margin_abs else 0.0
        else:
            power = nd.cdf((effect - max(z_a * se, margin_abs)) / se)
        n_needed = _improve_n(var_d, effect, margin_abs, z_a, z_b)
        resolvable = {"min_detectable_effect": round(max((z_a + z_b) * se, margin_abs + z_b * se), 6)}
    return {"metric": metric, "rule": rule, "true_effect": effect, "claims": len(rates),
            "claims_not_unanimous": flips,
            "claims_per_item": claims_per_item, "pseudo_items": jv["pseudo_items"], "scaled": jv["scaled"],
            "judge_var_per_item": round(jv["var"], 6), "writer_sd": writer_sd,
            "writer_sd_floor": None if floor is None else round(floor, 6), "writer_sd_below_floor": below,
            "var_diff": round(var_d, 6), "n_items": n_items, "se_diff": round(se, 6), "margin_abs": margin_abs,
            "alpha": alpha, "power": round(power, 4), "min_power": min_power,
            "n_needed": n_needed, **resolvable,
            "adequate": power >= min_power and not below,
            "bound": "upper bound (judge noise only)" if writer_sd is None else "judge noise + assumed writer_sd"}


def _improve_n(var_d: float, effect: float, margin: float, z_a: float, z_b: float) -> Optional[int]:
    """The n at which the improve rule reaches the target power at the true improvement `effect`; None when no n can
    (effect <= margin: the rule then passes at most half the time)."""
    if effect <= margin:
        return None
    if var_d == 0:
        return 1
    se_max = effect / (z_a + z_b)
    if z_b > 0:
        se_max = min(se_max, (effect - margin) / z_b)
    return max(1, math.ceil(var_d / se_max ** 2))


def check_prereg(pre: Any, votes_by_claim: dict[str, list],
                 rates: Optional[list[tuple[str, float, int]]] = None) -> dict:
    """`check` with the pre-registration's own n, margin, metric, alpha and power settings (n: the sample ids, or the
    sample rule's n before the rule is resolved)."""
    from services.writer_proof.prereg import WORKFLOWS
    doc = pre.doc
    pw = doc.get("power") or {}
    writer = WORKFLOWS[doc["workflow"]]["writer"]
    n_items = len(doc.get("sample_ids") or []) or int((doc.get("sample") or {}).get("n") or 0)
    return check(votes_by_claim, n_items=n_items, margin_abs=float(doc["decision"]
                 ["margin_abs"]), metric=doc["decision"]["metric"], alpha=float(doc["decision"]["alpha"]),
                 min_power=float(pw.get("min_power", 0.8)),
                 claims_per_item=int(pw.get("claims_per_item") or DEFAULT_CLAIMS_PER_ITEM[writer]),
                 writer_sd=pw.get("writer_sd"), rule=doc["decision"]["rule"],
                 effect_abs=None if pw.get("effect_abs") is None else float(pw["effect_abs"]), rates=rates)


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


def step_votes_of(report: dict) -> dict[str, dict[str, list]]:
    """The step votes of a two-step report (two_step run --out: {"votes": {"classify": {...}, "judge": {...}}})."""
    votes = report.get("votes") or {}
    if not isinstance(votes, dict) or not isinstance(votes.get("classify"), dict) or not votes["classify"]:
        raise PowerRefused("the two-step report has no step votes (write it with two_step run --out)")
    return {"classify": votes["classify"], "judge": votes.get("judge") or {}}


def prereg_power(pre: Any, r1_report: dict, *, instrument: Optional[str] = None,
                 two_step_report: Optional[dict] = None) -> dict:
    """`check_prereg` from the report files: with a claim instrument judge (judge.instrument) the chain rates of the
    fire report and the two-step report, else the R1 votes of `instrument`."""
    if (pre.doc.get("judge") or {}).get("instrument"):
        if two_step_report is None:
            raise PowerRefused("a claim instrument judge needs the two-step gated report too (--two-step-report)")
        from services.writer_proof.instrument import fire_instrument
        rates = chain_rates(votes_of(r1_report, fire_instrument().id), step_votes_of(two_step_report))
        return check_prereg(pre, {}, rates=rates)
    return check_prereg(pre, votes_of(r1_report, instrument))


def main(argv: Optional[list[str]] = None) -> int:
    from services.writer_proof import prereg as pr
    ap = argparse.ArgumentParser(prog="python -m services.writer_proof.power", description=__doc__.split("\n")[0])
    ap.add_argument("--r1-report", required=True, help="the R1 report JSON with votes (r1_calibrate --out)")
    ap.add_argument("--instrument", help="instrument id (default: the report's recommended one)")
    ap.add_argument("--two-step-report", help="the two-step gated report with step votes (a claim instrument judge)")
    ap.add_argument("--prereg", required=True, help="pre-registration id or path")
    a = ap.parse_args(argv)
    try:
        pre = pr.load(a.prereg)
        with open(a.r1_report, encoding="utf-8") as f:
            report = json.load(f)
        ts_report = None
        if a.two_step_report:
            with open(a.two_step_report, encoding="utf-8") as f:
                ts_report = json.load(f)
        out = prereg_power(pre, report, instrument=a.instrument, two_step_report=ts_report)
    except (pr.PreregRefused, PowerRefused, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    print(json.dumps({"ok": out["adequate"], "prereg": pre.id, "sha256": pre.sha256, "power": out}, indent=1))
    return 0 if out["adequate"] else 1


if __name__ == "__main__":
    sys.exit(main())
