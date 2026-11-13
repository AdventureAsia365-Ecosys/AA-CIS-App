"""AA-1048 — gated evaluation of a claim-instrument prompt version on the claims R1 fired on (S279 panel report,
root docs/audits/2026-11-13-S279-session/aa1048/panel-report.md section 5 C).

R1 (gpt-6-luna extraction, prompt v1) fired on 286 held-out claims of t2 and t9_blog: 71 stored violations and 215
stored ok. The blind panel found 5 of the 215 to be real promises (relabelled to violation by the tech lead) and
explained the rest: paraphrase or inference (66%), verbatim support missed (15%), permitted place background (14%).
This module asks one question of a new prompt version (services/ground_truth/prompts.py, v2 = B1-B5) before anyone
pays for a full R1: on a stratified subset of those fired claims, does it drop the false positives and keep the true
ones?

  python -m services.ground_truth.instrument_eval pool  --export <claims.jsonl> [--export ...] --labels <labels.jsonl>
      [--labels ...] [--relabels <relabels.json>] --votes <r1 log or JSON> --panel <merged.json> --out <pool.jsonl>
      $0, local: the fired claims with their group (tp, tp_relabel, fp_paraphrase, fp_verbatim, fp_background,
      fp_other, fp_disputed), and the B3 check (do the seed's "Place background" lines draw R1's fire?).
  python -m services.ground_truth.instrument_eval run --pool <pool.jsonl> [--prompt v2] [--mode entailment]
      [--candidate <model>@<connection>] [--trial N | --limit N] [--live --confirm-spend] [--cap USD] [--out <json>]
      DRY RUN by default: the subset, the plan and its cost estimate; no client, no call. A paid run needs --live,
      --confirm-spend and exactly one of --trial / --limit, within the settings below.

Settings (Platform > Settings; CIS migration 412; read after a refresh from the database before a paid run):
  cis.spend.instrument_eval_cap_usd           the run's hard cap (also never above r1.HARD_CAP_USD)
  cis.spend.instrument_eval_max_claims        the most claims --limit may take
  cis.spend.instrument_eval_trial_claims      the most claims --trial may take
  cis.quality.instrument_eval_fp_drop_min_pct go needs at least this share of the subset's false positives dropped
  cis.quality.instrument_eval_tp_keep_min_pct go needs at least this share of the subset's true positives kept

Subset (`select`): half the limit from the panel-ok false positives of the paraphrase, verbatim and background groups,
allocated in proportion to their sizes (largest remainder, so the raw subset rates estimate the population's); the
other half true positives: every relabelled claim first, then the stored violations in proportion per writer. Inside a
group the order is the claim hash (deterministic, unrelated to the text). The trial takes the first claims of the
subset in round-robin group order, so it holds a relabelled claim, a false positive and a stored violation.

Score: a false positive is dropped when the vote majority clears it (False), a true positive kept when the majority
flags it (True); an unverified claim (no majority) is neither (r1.majority, D7 "a miss is unverified"). The five
relabelled claims are in-sample for v2 (its B5 patterns were drawn from them), so TP-kept, recall, precision and the
verdict read the stored violations (group tp) only; the relabels are reported apart and never gated on. go only when
the run finished, the subset has false positives and stored violations, and both bars hold; a trial never gives a
verdict.

Calls: every call goes through r1.run (the Model Gateway on stage r1.STAGE, role judge, the hard cap gated on each
call's worst case, the ledger probe, one shared.llm_call_log row per reply with **billed_fields and the prompt version).
Nothing here writes a label. The output files carry counts, groups and hash prefixes only; the pool file holds real
text and stays in the workspace scratch folder (.tmp-session/), never in a repo.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from services.ground_truth import claims as cl
from services.ground_truth import panel as pn
from services.ground_truth import r1
from shared import settings

DEFAULT_PROMPT = "v2"
DEFAULT_MODE = "entailment"
DEFAULT_CANDIDATE = "gpt-6-luna-openai@openai-direct"
R1_INSTRUMENT = "gpt-6-luna-openai@openai-direct:extraction"
TP_GROUPS = ("tp_relabel", "tp")
FP_SAMPLED = ("fp_paraphrase", "fp_verbatim", "fp_background")
GROUPS = TP_GROUPS + FP_SAMPLED + ("fp_other", "fp_disputed")
ROUND_ROBIN = ("tp_relabel", "fp_paraphrase", "tp", "fp_verbatim", "fp_background")
FIRE_GROUP = {"paraphrase_or_inference": "fp_paraphrase", "none": "fp_verbatim",
              "background_knowledge": "fp_background"}
PLACE_BACKGROUND = "place background"
PREFILTER = "prefilter"


class EvalRefused(ValueError):
    """The step cannot run (inputs, gate or settings): no call is made."""


@dataclass(frozen=True)
class Limits:
    cap_usd: float
    max_claims: int
    trial_claims: int
    fp_drop_min: float          # a share, 0..1
    tp_keep_min: float          # a share, 0..1


def limits() -> Limits:
    """The evaluation's limits from the settings registry (the snapshot; `refresh_settings` re-reads it first)."""
    return Limits(cap_usd=min(float(settings.get("cis.spend.instrument_eval_cap_usd")), r1.HARD_CAP_USD),
                  max_claims=int(settings.get("cis.spend.instrument_eval_max_claims")),
                  trial_claims=int(settings.get("cis.spend.instrument_eval_trial_claims")),
                  fp_drop_min=float(settings.get("cis.quality.instrument_eval_fp_drop_min_pct")) / 100.0,
                  tp_keep_min=float(settings.get("cis.quality.instrument_eval_tp_keep_min_pct")) / 100.0)


def refresh_settings() -> bool:
    """Re-read the approved settings from the database before a paid run (an ECS exec process has no refresher)."""
    import asyncio

    async def _go() -> bool:
        import asyncpg

        from shared.secrets import get_database_url

        pool = await asyncpg.create_pool(get_database_url().strip(), ssl="require", min_size=1, max_size=1)
        try:
            return await settings.ensure_fresh(pool)
        finally:
            await pool.close()
    try:
        return asyncio.run(_go())
    except Exception:                                        # noqa: BLE001 — the defaults / last snapshot stay
        return False


# ── pool ($0) ──────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PoolItem:
    item: r1.Item
    group: str

    def line(self) -> dict:
        return {**asdict(self.item), "evidence": list(self.item.evidence), "group": self.group}


def read_pool(text: str) -> list[PoolItem]:
    out = []
    for e in pn.read_jsonl(text):
        group = e.pop("group", None)
        if group not in GROUPS:
            raise EvalRefused(f"pool line {e.get('claim_hash', '?')[:12]}: unknown group {group!r}")
        e["evidence"] = tuple(e.get("evidence") or ())
        out.append(PoolItem(r1.Item(**e), group))
    return out


def _vote(c: str) -> Optional[bool]:
    return True if c == "1" else False if c == "0" else None


def _votes_obj(obj: Any, instrument: str) -> Optional[dict[str, list[Optional[bool]]]]:
    if isinstance(obj, dict) and obj.get("instrument") == instrument and isinstance(obj.get("votes"), dict):
        return {k: [_vote(c) for c in str(v)] for k, v in obj["votes"].items()}
    return None


def read_votes(text: str, instrument: str = R1_INSTRUMENT) -> dict[str, list[Optional[bool]]]:
    """hash prefix -> votes of `instrument`: from an R1 log ("VOTES {json}" lines, as the S278 R1 runner printed
    them; "1" flagged, "0" cleared, anything else no vote) or from one JSON object of the same shape."""
    try:
        whole = _votes_obj(json.loads(text), instrument)
    except ValueError:
        whole = None
    if whole is not None:
        return whole
    for line in text.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "VOTES":
            found = _votes_obj(json.loads(rest), instrument)
            if found is not None:
                return found
    raise EvalRefused(f"no votes of {instrument} in the votes file")


def panel_group(entry: Optional[dict]) -> str:
    """The false-positive group of a fired ok claim from the panel's merged verdict: the most common fire reason of
    its lenses (a tie or another reason: fp_other); a claim the panel majority did not call ok: fp_disputed."""
    if not entry:
        return "fp_other"
    if entry.get("majority") != "ok":
        return "fp_disputed"
    fires = Counter(v.get("fire") for v in (entry.get("votes") or {}).values() if isinstance(v, dict) and v.get("fire"))
    top = fires.most_common(2)
    if not top or (len(top) > 1 and top[0][1] == top[1][1]):
        return "fp_other"
    return FIRE_GROUP.get(top[0][0], "fp_other")


def finals_from(label_lines: Iterable[dict], relabelled: Iterable[str] = ()) -> dict[str, dict]:
    """claim_hash -> {label, claim_class} from label lines (the Writing Lab claim browser or the cli.py finals), with
    the approved relabels applied (every one is a correction to violation; S279: the tech lead approved 5). Machine-rule
    labels (source prefilter) never count, as in R1's decision record (critic F14): R1's 286 fired claims leave out
    the 19 prefilter claims it also fired on."""
    out = {r["claim_hash"]: {"label": r.get("label"), "claim_class": r.get("claim_class")} for r in label_lines
           if r.get("claim_hash") and r.get("source") != PREFILTER}
    for h in relabelled:
        if h not in out:
            raise EvalRefused(f"relabelled claim {h[:12]} has no label line")
        out[h] = {**out[h], "label": "violation"}
    return out


def by_prefix(hashes: Iterable[str], keyed: dict[str, Any]) -> dict[str, Any]:
    """full hash -> value of `keyed` (keyed by a hash prefix of one length). Two hashes on one prefix are refused."""
    if not keyed:
        return {}
    n = len(next(iter(keyed)))
    seen: dict[str, str] = {}
    out = {}
    for h in hashes:
        p = h[:n]
        if p in seen and seen[p] != h:
            raise EvalRefused(f"two claims share the prefix {p}: use longer keys")
        seen[p] = h
        if p in keyed:
            out[h] = keyed[p]
    return out


def _group(item: r1.Item, panel: dict[str, dict], relabelled: set[str]) -> str:
    if item.label == "violation":
        return "tp_relabel" if item.claim_hash in relabelled else "tp"
    return panel_group(panel.get(item.claim_hash))


def build_pool(items: list[r1.Item], votes: dict[str, list], panel: dict[str, dict],
               relabelled: Iterable[str] = ()) -> list[PoolItem]:
    """The claims the instrument fired on (its vote majority, r1.majority), each with its group. `votes` and `panel`
    are keyed by full claim hash (by_prefix)."""
    rel = set(relabelled)
    return [PoolItem(i, _group(i, panel, rel)) for i in items if r1.majority(votes.get(i.claim_hash) or []) is True]


def pool_counts(pool: list[PoolItem]) -> dict:
    return {"fired": len(pool), "by_writer": dict(Counter(p.item.writer for p in pool)),
            "by_label": dict(Counter(p.item.label for p in pool)),
            "by_group": {g: n for g, n in sorted(Counter(p.group for p in pool).items())}}


# ── B3 check ($0) ──────────────────────────────────────────────────────────────────────────────────────────────────


def place_background_lines(source: str) -> set[str]:
    """The bullet lines of every "Place background ..." block of a T9 seed (the block ends at a blank or non-bullet
    line)."""
    out: set[str] = set()
    inside = False
    for raw in (source or "").splitlines():
        line = raw.strip()
        if line.lower().startswith(PLACE_BACKGROUND):
            inside = True
        elif inside and line.startswith("- "):
            out.add(line)
        else:
            inside = False
    return out


def best_line(claim_text: str, source: str) -> tuple[str, bool]:
    """The source line sharing the most content words with the claim (the first on a tie), and whether it holds
    every content word of the claim (the claim is covered near-verbatim by one line)."""
    want = cl._words(claim_text)
    best, best_n, best_words = "", 0, set()
    for raw in (source or "").splitlines():
        line = raw.strip()
        words = cl._words(line)
        n = len(want & words)
        if n > best_n:
            best, best_n, best_words = line, n, words
    return best, bool(want) and want <= best_words


def _rate(fired: int, n: int) -> dict:
    return {"n": n, "fired": fired, "fire_rate": round(fired / n, 4) if n else None}


def b3_check(items: list[r1.Item], votes: dict[str, list], pool: list[PoolItem]) -> dict:
    """Hypothesis B3 ("the seed's clause 'they say nothing about what this trip does' makes the instrument discount
    place background") against the stored R1 votes, at $0. R1 kept no raw fragments, so the check reads behaviour:
    among stored-ok claims of objects whose source has a Place background block, the fire rate when the claim's best
    source line is a Place background line versus another line, and the same for claims one line covers whole."""
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for i in items:
        pb = place_background_lines(i.source_text)
        verdict = r1.majority(votes.get(i.claim_hash) or [])
        if i.label != "ok" or not pb or verdict is None:
            continue
        line, covered = best_line(i.claim_text, i.source_text)
        side = "place_background" if line in pb else "other_line"
        for key in (f"best_{side}", f"covered_by_{side}" if covered else None):
            if key:
                tally[key][0] += verdict is True
                tally[key][1] += 1
    verbatim = [p for p in pool if p.group == "fp_verbatim"]
    in_pb = sum(1 for p in verbatim if best_line(p.item.claim_text, p.item.source_text)[0]
                in place_background_lines(p.item.source_text))
    rates = {k: _rate(*tally[k]) for k in ("best_place_background", "best_other_line", "covered_by_place_background",
                                           "covered_by_other_line")}
    a, b = rates["covered_by_place_background"]["fire_rate"], rates["covered_by_other_line"]["fire_rate"]
    return {"ok_claims_in_sources_with_place_background": rates,
            "fp_verbatim": {"n": len(verbatim), "best_line_is_place_background": in_pb},
            "covered_fire_rate_place_background_minus_other": round(a - b, 4) if a is not None and b is not None
            else None,
            "reading": ("consistent with B3: whole-line support from a Place background line draws more fire"
                        if a is not None and b is not None and a > b else "not shown by the stored votes")}


# ── subset ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def allocate(quota: int, sizes: dict[str, int]) -> dict[str, int]:
    """`quota` spread over the groups in proportion to `sizes` (largest remainder, ties to the larger group, then the
    name), never more than a group has."""
    total = sum(sizes.values())
    quota = max(0, min(quota, total))
    if not quota:
        return {k: 0 for k in sizes}
    exact = {k: quota * n / total for k, n in sizes.items()}
    out = {k: math.floor(x) for k, x in exact.items()}
    for k in sorted(sizes, key=lambda k: (-(exact[k] - out[k]), -sizes[k], k))[:quota - sum(out.values())]:
        out[k] += 1
    return out


def _by_group(pool: list[PoolItem]) -> dict[str, list[PoolItem]]:
    groups: dict[str, list[PoolItem]] = defaultdict(list)
    for p in sorted(pool, key=lambda p: p.item.claim_hash):
        groups[p.group].append(p)
    return groups


def _round_robin(lists: list[list[PoolItem]]) -> list[PoolItem]:
    out: list[PoolItem] = []
    for k in range(max((len(x) for x in lists), default=0)):
        out += [x[k] for x in lists if k < len(x)]
    return out


def select(pool: list[PoolItem], limit: int) -> list[PoolItem]:
    """The stratified subset of at most `limit` claims (module doc), in round-robin group order."""
    groups = _by_group(pool)
    fp_quota = limit // 2
    fp = allocate(fp_quota, {g: len(groups[g]) for g in FP_SAMPLED})
    picked = {g: groups[g][:fp[g]] for g in FP_SAMPLED}
    picked["tp_relabel"] = groups["tp_relabel"][:limit - fp_quota]
    rest = limit - fp_quota - len(picked["tp_relabel"])
    by_writer: dict[str, list[PoolItem]] = defaultdict(list)
    for p in groups["tp"]:
        by_writer[p.item.writer].append(p)
    share = allocate(rest, {w: len(v) for w, v in by_writer.items()})
    picked["tp"] = _round_robin([by_writer[w][:share[w]] for w in sorted(by_writer)])
    return _round_robin([picked[g] for g in ROUND_ROBIN])


def composition(subset: list[PoolItem]) -> dict:
    return {"claims": len(subset), "by_group": {g: n for g, n in sorted(Counter(p.group for p in subset).items())},
            "by_writer": dict(Counter(p.item.writer for p in subset))}


# ── score ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def _share(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def _verdicts(subset: list[PoolItem], votes: dict[str, list]) -> dict[str, Counter]:
    """Per group: dropped / firing / unverified (false positives) or kept / lost / unverified (true positives)."""
    out: dict[str, Counter] = defaultdict(Counter)
    for p in subset:
        v = r1.majority(votes.get(p.item.claim_hash) or [])
        if p.group in TP_GROUPS:
            out[p.group]["kept" if v is True else "lost" if v is False else "unverified"] += 1
        else:
            out[p.group]["dropped" if v is False else "firing" if v is True else "unverified"] += 1
    return out


def decide(fp_drop: Optional[float], tp_keep: Optional[float], lim: Limits, *, trial: bool,
           stopped: Optional[str]) -> str:
    if trial:
        return "trial"
    if stopped or fp_drop is None or tp_keep is None:
        return "incomplete"
    return "go" if fp_drop >= lim.fp_drop_min and tp_keep >= lim.tp_keep_min else "no_go"


def _tp_block(c: Counter) -> dict:
    n = sum(c.values())
    return {"n": n, **c, "kept_share": _share(c["kept"], n)}


def score(subset: list[PoolItem], votes: dict[str, list], lim: Limits, *, trial: bool = False,
          stopped: Optional[str] = None) -> dict:
    """FP-dropped and TP-kept shares, precision and recall on the subset, per group, and the verdict (module doc).

    The relabelled claims (tp_relabel) are in-sample for a prompt whose B5 patterns were drawn from them, so TP-kept,
    recall, precision and the verdict read the stored violations (tp) only; the relabels are reported apart
    (`relabelled_in_sample`) and together (`true_positives_with_relabels`), never gated on."""
    per = _verdicts(subset, votes)
    fp = sum((per[g] for g in per if g not in TP_GROUPS), Counter())
    tp, rel = per.get("tp", Counter()), per.get("tp_relabel", Counter())
    n_fp = sum(fp.values())
    fp_drop, tp_keep = _share(fp["dropped"], n_fp), _share(tp["kept"], sum(tp.values()))
    return {"false_positives": {"n": n_fp, **fp, "dropped_share": fp_drop},
            "true_positives": {**_tp_block(tp), "sample": "out_of_sample", "groups": ["tp"]},
            "relabelled_in_sample": {**_tp_block(rel), "sample": "in_sample", "groups": ["tp_relabel"],
                                     "note": "the v2 B5 patterns were drawn from these claims; reported, not gated"},
            "true_positives_with_relabels": _tp_block(tp + rel),
            "precision": _share(tp["kept"], tp["kept"] + fp["firing"]), "recall": tp_keep,
            "by_group": {g: dict(c) for g, c in sorted(per.items())},
            "bars": {"fp_dropped_min": lim.fp_drop_min, "tp_kept_min": lim.tp_keep_min, "tp_kept_on": ["tp"]},
            "verdict": decide(fp_drop, tp_keep, lim, trial=trial, stopped=stopped)}


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _check_out(path: str) -> str:
    """The pool holds real text: --out must be outside a git work tree or git-ignored (scripts/aa1048/outpath.py,
    present in a checkout; the image has no scripts/ and never builds a pool)."""
    try:
        from scripts.aa1048.outpath import check_out
    except ImportError:                                      # pragma: no cover — the image
        return path
    try:
        return check_out(path)
    except ValueError as e:                                  # OutRefused
        raise EvalRefused(str(e)) from e


def cmd_pool(a: argparse.Namespace) -> dict:
    export = [ln for p in a.export for ln in pn.read_jsonl(_read(p))]
    labels = [ln for p in a.labels for ln in pn.read_jsonl(_read(p))]
    relabelled = [r["claim_hash"] for r in json.loads(_read(a.relabels))] if a.relabels else []
    items = r1.items_from(export, finals_from(labels, relabelled), split=a.split)
    hashes = [i.claim_hash for i in items]
    votes = by_prefix(hashes, read_votes(_read(a.votes), a.instrument))
    panel = by_prefix(hashes, {e["id"]: e for e in json.loads(_read(a.panel))})
    pool = build_pool([i for i in items if i.claim_hash in votes], votes, panel, relabelled)
    out = _check_out(a.out)
    with open(out, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(p.line(), ensure_ascii=False) + "\n" for p in pool)
    return {"ok": True, "items_with_votes": len(votes), "pool": pool_counts(pool), "b3": b3_check(items, votes, pool)}


def gate(a: argparse.Namespace, lim: Limits) -> int:
    """The subset size the run may take; EvalRefused when the flags or the settings refuse the run."""
    if a.trial is not None and a.limit is not None:
        raise EvalRefused("give --trial or --limit, not both")
    if a.live and a.trial is None and a.limit is None:
        raise EvalRefused("a paid run needs --trial N or --limit N")
    if a.trial is not None and not 0 < a.trial <= lim.trial_claims:
        raise EvalRefused(f"--trial must be 1..{lim.trial_claims} (cis.spend.instrument_eval_trial_claims)")
    if a.limit is not None and not 0 < a.limit <= lim.max_claims:
        raise EvalRefused(f"--limit must be 1..{lim.max_claims} (cis.spend.instrument_eval_max_claims)")
    if a.cap is not None and not 0 < a.cap <= lim.cap_usd:
        raise EvalRefused(f"--cap must be in (0, {lim.cap_usd}] USD (cis.spend.instrument_eval_cap_usd)")
    return a.trial if a.trial is not None else a.limit if a.limit is not None else lim.max_claims


def _instrument(a: argparse.Namespace) -> r1.Instrument:
    model, _, conn = a.candidate.partition("@")
    if not model or not conn:
        raise EvalRefused(f"a candidate is <model>@<connection>, got {a.candidate!r}")
    return r1.instruments([(model, conn)], [a.mode], prompt=a.prompt)[0]


def _subset(pool: list[PoolItem], a: argparse.Namespace, n: int, lim: Limits) -> list[PoolItem]:
    if a.trial is not None:
        return select(pool, lim.max_claims)[:n]
    return select(pool, n)


_caller: Optional[r1.Caller] = None                     # tests put a fake here; None = the Model Gateway
_probe: Callable[[Any], r1.SpendProbe] = r1.ledger_probe


def _live(subset: list[PoolItem], inst: r1.Instrument, a: argparse.Namespace, cap: float, lim: Limits) -> dict:
    run_id = str(uuid.uuid4())
    result = r1.run([p.item for p in subset], [inst], cap=cap, votes=a.votes, run_id=run_id, caller=_caller,
                    spend_probe=_probe(datetime.now(timezone.utc)))
    votes = result["votes"].get(inst.id) or {}
    return {"run_id": run_id, "spent_usd": result["spent_usd"], "ledger_usd": result["ledger_usd"],
            "calls": result["calls"], "stopped": result["stopped"], "caller_errors": result["caller_errors"],
            "identity_mismatches": result["identity_mismatches"],
            "score": score(subset, votes, lim, trial=a.trial is not None, stopped=result["stopped"]),
            "votes": {h[:16]: v for h, v in sorted(votes.items())}}


def cmd_run(a: argparse.Namespace) -> tuple[int, dict]:
    if a.live and not a.confirm_spend:
        raise EvalRefused("--live is paid: add --confirm-spend")
    fresh = refresh_settings() if a.live else None
    lim = limits()
    n = gate(a, lim)
    inst = _instrument(a)
    subset = _subset(read_pool(_read(a.pool)), a, n, lim)
    cap = a.cap if a.cap is not None else lim.cap_usd
    try:
        the_plan = r1.plan([p.item for p in subset], [inst], votes=a.votes, cap=cap)
    except r1.R1Refused as e:
        raise EvalRefused(str(e)) from e
    rep = {"mode": "live" if a.live else "dry_run", "instrument": inst.id,
           "prompt_version": inst.prompt_version.tag, "settings_refreshed": fresh, "limits": asdict(lim),
           "subset": composition(subset), "plan": the_plan}
    if not the_plan["fits_cap"]:
        return 3, {"ok": False, "refused": "the plan does not fit the cap", **rep}
    if not a.live:
        from services.golden_set import llm_guard

        llm_guard.install("aa1048_instrument_eval_dry_run")
        return 0, {"ok": True, **rep}
    live = _live(subset, inst, a, cap, lim)
    return (0 if not live["stopped"] else 3), {"ok": not live["stopped"], **rep, **live}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m services.ground_truth.instrument_eval",
                                 description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pool", help="$0: the fired claims with their groups, and the B3 check")
    p.add_argument("--export", action="append", required=True)
    p.add_argument("--labels", action="append", required=True)
    p.add_argument("--relabels")
    p.add_argument("--votes", required=True)
    p.add_argument("--instrument", default=R1_INSTRUMENT)
    p.add_argument("--panel", required=True)
    p.add_argument("--split", choices=r1.SPLIT_CHOICES, default="held_out")
    p.add_argument("--out", required=True)
    r = sub.add_parser("run", help="DRY RUN by default; --live --confirm-spend with --trial N or --limit N is paid")
    r.add_argument("--pool", required=True)
    r.add_argument("--prompt", default=DEFAULT_PROMPT)
    r.add_argument("--mode", choices=r1.MODES, default=DEFAULT_MODE)
    r.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    r.add_argument("--votes", type=int, default=r1.VOTES)
    r.add_argument("--trial", type=int)
    r.add_argument("--limit", type=int)
    r.add_argument("--cap", type=float)
    r.add_argument("--dry-run", action="store_true", help="the default; never paid")
    r.add_argument("--live", action="store_true", help="PAID: make the calls")
    r.add_argument("--confirm-spend", action="store_true", help="required with --live")
    r.add_argument("--out", help="write the report (counts, groups and hash prefixes only) here too")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "run" and a.dry_run and a.live:
        a.live = False                                      # --dry-run wins: never pay by accident
    try:
        rc, rep = (0, cmd_pool(a)) if a.cmd == "pool" else cmd_run(a)
    except (EvalRefused, r1.R1Refused) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    if getattr(a, "out", None) and a.cmd == "run":
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
