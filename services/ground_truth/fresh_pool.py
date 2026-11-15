"""AA-1048 (S281) — a FRESH pool of R1-fired claims for the two-step instrument (services/ground_truth/two_step.py),
out of sample by construction, with the labelling sheet the agent panel fills. $0: no model call here.

Why. The S279 pool (the R1 fired claims of the held-out split) shaped every prompt since v2: the S279 panel read all its
false positives to write v2, and the v2, v3 and v4 runs read its claims. After three subsets it has 11 stored
violations left. A new measurement needs claims from pieces none of that touched.

  python -m services.ground_truth.fresh_pool eligible --export <claims.jsonl> [--export ...] --exclude-pool <pool.jsonl>
      [--exclude-pool ...] --exclude-run <report.json> [--exclude-run ...] [--split train|held_out|all]
      [--labels <finals.jsonl> ...] --out <eligible.jsonl> --manifest <eligible-manifest.json>
      The export lines whose piece (content_sha256) holds no claim of an excluded pool and no claim of an excluded run
      (a run report's hash prefixes are located through the pools and exports given; a prefix found nowhere is
      refused: it cannot be kept out). With --labels it also prints the plan of the R1 firing run on those claims
      (gpt-6-luna-openai extraction, prompt v1: the instrument whose false positives are being measured) on the
      labelled claims (scripts/aa1048/r1_calibrate.py). services/ground_truth/r1_fire.py fires the same instrument on
      a capped sample of the eligible claims whatever their label (paid, its own yes): its report is a --votes file.
  python -m services.ground_truth.fresh_pool sample --eligible <eligible.jsonl> --manifest <eligible-manifest.json>
      --votes <r1 report | votes log> [--instrument <id>] [--size N] --out-dir <dir>
      The claims the R1 majority fired on, at most N (cis.quality.claim_fresh_pool_sample_max), spread over the writers
      in proportion and ordered by sha256(salt + claim hash) with a salt taken from the eligible manifest (recorded).
      A fire report of r1_fire.py must be a full live run on this eligible file (a trial or another file is refused);
      the sample manifest records the votes file's digest and run.
      Writes candidates.jsonl (the claims), sheet.jsonl (one row per claim, numbered, blank answer fields),
      sources.jsonl (each piece's source once) and sample-manifest.json (counts, salt, hash prefixes; no text).
  python -m services.ground_truth.fresh_pool import --candidates <candidates.jsonl> --sample-manifest <json>
      --eligible-manifest <json> --sheet <filled.jsonl> [--sheet ...] [--labels <finals.jsonl> ...]
      --out <pool.jsonl> --manifest <fresh-manifest.json>
      Validates every sheet (each row number exactly once, allowed answers, a reason), takes the majority of the sheets
      per claim and writes the pool in instrument_eval's format: a violation is "tp", an ok claim "fp_<reason>"
      (paraphrase, verbatim, background, other). No panel majority, an "unsure" majority, or a stored label that
      disagrees with the panel leaves the claim out (counted). The manifest records the pool file's sha256, the runs and
      pieces kept out, the counts and the panel's step-1 answers (restatement / new claim, per hash prefix), so
      two_step.py can check that the pool is this pool and out of sample for the steps.

Every file but the manifests holds real tour text: --out / --out-dir must be outside a git work tree or ignored there
(scripts/aa1048/outpath.py), i.e. the workspace scratch folder (.tmp-session/). Panel brief:
docs/calibration/aa1048-two-step-panel-brief.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from services.ground_truth import instrument_eval as ev
from services.ground_truth import panel as pn
from services.ground_truth import prompts as pr
from services.ground_truth import r1
from shared import settings

FIRE_INSTRUMENT = ev.R1_INSTRUMENT                       # gpt-6-luna-openai@openai-direct:extraction (prompt v1)
FIRE_CANDIDATE = ("gpt-6-luna-openai", "openai-direct")
LABELS = ("violation", "ok", "unsure")
OK_REASONS = {"paraphrase": "fp_paraphrase", "verbatim": "fp_verbatim", "background": "fp_background",
              "other": "fp_other"}
STEP1 = ("restatement", "new_claim")
SHEET_FIELDS = ("label", "ok_reason", "step1", "reason")
FIRE_REPORT_KIND = "r1_fire"                             # services/ground_truth/r1_fire.py writes this report


class PoolRefused(ev.EvalRefused):
    """The step cannot run (inputs, sheet, settings): nothing is written."""


def sample_max() -> int:
    return int(settings.get("cis.quality.claim_fresh_pool_sample_max"))


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_jsonl(path: str, rows: Iterable[dict]) -> None:
    with open(ev._check_out(path), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def _write_json(path: str, obj: dict) -> None:
    with open(ev._check_out(path), "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def piece(line: dict) -> str:
    return str(line["content_sha256"]).lower()


# ── eligible ($0) ──────────────────────────────────────────────────────────────────────────────────────────────────


def shaped_pieces(pools: list[list[dict]], reports: list[str], exports: list[dict]) -> tuple[set[str], list[str]]:
    """(the pieces kept out, the run-id prefixes of the reports): every piece of an excluded pool, and the piece of
    every claim an excluded run voted on (its prefixes located in the pools and exports). A prefix found nowhere is
    refused."""
    out = {piece(p) for pool in pools for p in pool}
    known = [(str(e["claim_hash"]).lower(), piece(e)) for e in [*(p for pool in pools for p in pool), *exports]]
    runs = []
    for text in reports:
        run = ev.run_of(text)
        if run is None:
            raise PoolRefused("an excluded run must be a run report (with run_id), not a list of prefixes")
        runs.append(run)
        for prefix in sorted(ev.prefixes_from(text)):
            found = {pc for h, pc in known if h.startswith(prefix)}
            if not found:
                raise PoolRefused(f"run {run}: claim {prefix} is in no pool or export given, so its piece cannot be "
                                  "kept out (pass the pool that run read)")
            out |= found
    return out, sorted(set(runs))


def eligible(exports: list[dict], excluded: set[str], split: str) -> list[dict]:
    if split not in r1.SPLIT_CHOICES:
        raise PoolRefused(f"split must be one of {r1.SPLIT_CHOICES}")
    return [e for e in exports if piece(e) not in excluded and (split == "all" or r1.split_of_line(e) == split)]


def fire_plan(lines: list[dict], finals: dict[str, dict], split: str) -> Optional[dict]:
    """The R1 firing run's plan on these claims (the labelled ones: r1_calibrate reads only those), or None. Fit is
    read against the evaluation cap setting (cis.spend.instrument_eval_cap_usd); the firing run takes its own --cap."""
    items = r1.items_from(lines, finals, split=split)
    if not items:
        return None
    inst = r1.instruments([FIRE_CANDIDATE], ["extraction"])[0]
    return r1.plan(items, [inst], cap=ev.limits().cap_usd)


def _finals(paths: list[str]) -> dict[str, dict]:
    return ev.finals_from([ln for p in paths for ln in pn.read_jsonl(_read(p))]) if paths else {}


def cmd_eligible(a: argparse.Namespace) -> dict:
    exports = [ln for p in a.export for ln in pn.read_jsonl(_read(p))]
    pools = [pn.read_jsonl(_read(p)) for p in a.exclude_pool]
    reports = [_read(p) for p in a.exclude_run]
    if not pools and not reports:
        raise PoolRefused("name the pools and run reports that shaped the prompts (--exclude-pool / --exclude-run)")
    excluded, runs = shaped_pieces(pools, reports, exports)
    lines = eligible(exports, excluded, a.split)
    if not lines:
        raise PoolRefused("no claim is left on a piece outside the excluded pools and runs")
    _write_jsonl(a.out, lines)
    manifest = {"kind": "fresh_eligible", "issue": "AA-1048", "split": a.split, "excluded_runs": runs,
                "excluded_pools": [_sha(_read(p))[:16] for p in a.exclude_pool],
                "excluded_pieces": sorted(p[:16] for p in excluded), "eligible_sha256": _sha(_read(a.out)),
                "claims": len(lines), "pieces": len({piece(e) for e in lines}),
                "by_writer": dict(Counter(e["writer"] for e in lines))}
    _write_json(a.manifest, manifest)
    plan = fire_plan(lines, _finals(a.labels), a.split) if a.labels else None
    return {"ok": True, **{k: v for k, v in manifest.items() if k != "excluded_pieces"},
            "excluded_piece_count": len(excluded), "fire_plan": plan}


# ── sample ($0) ────────────────────────────────────────────────────────────────────────────────────────────────────


def read_fire_votes(text: str, instrument: str = FIRE_INSTRUMENT) -> dict[str, list[Optional[bool]]]:
    """hash (or prefix) -> votes of `instrument`: from scripts/aa1048/r1_calibrate.py's report ("votes": {instrument
    id: {claim_hash: [true|false|null, ...]}}) or from instrument_eval.read_votes' formats (an R1 log, a votes
    object)."""
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    votes = obj.get("votes") if isinstance(obj, dict) else None
    if isinstance(votes, dict) and isinstance(votes.get(instrument), dict):
        return {str(h): [v if isinstance(v, bool) else None for v in vs] for h, vs in votes[instrument].items()
                if isinstance(vs, list)}
    try:
        return ev.read_votes(text, instrument)
    except ev.EvalRefused as e:
        raise PoolRefused(str(e)) from e


def fire_report_check(text: str, manifest: dict) -> dict:
    """{} for a votes file that is not an r1_fire report; for one, its run and whether it was resumed. Refused unless
    it is a full live fire on the eligible file of `manifest` that ran to its end (a trial fires a few claims only; a
    dry run has no votes; a stopped run is partial: finish it with r1_fire --resume <checkpoint>; another file's votes
    would sample claims of another file). A resumed run's report is one run's report and is accepted."""
    try:
        obj = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(obj, dict) or obj.get("kind") != FIRE_REPORT_KIND:
        return {}
    if obj.get("mode") != "live" or obj.get("fire_kind") != "full":
        raise PoolRefused("the fire report is a dry run or a trial: sample from a full live fire (r1_fire.py --live "
                          "without --trial)")
    if obj.get("eligible_sha256") != manifest.get("eligible_sha256"):
        raise PoolRefused("the fire report fired another eligible file (eligible_sha256 differs)")
    if obj.get("stopped"):
        raise PoolRefused("the fire report stopped early (partial): finish the run with r1_fire --resume <its "
                          "checkpoint>, then sample from the resumed report")
    return {"votes_run": str(obj.get("run_id", ""))[:8], "votes_partial": False,
            "votes_resumed": bool(obj.get("resumed"))}


def salt_of(manifest: dict) -> str:
    return "aa1048-fresh:" + str(manifest.get("eligible_sha256", ""))[:16]


def order_key(salt: str, claim_hash: str) -> str:
    return hashlib.sha256(f"{salt}:{claim_hash}".encode("utf-8")).hexdigest()


def pick(fired: list[dict], size: int, salt: str) -> list[dict]:
    """At most `size` fired claims, spread over the writers in proportion (ev.allocate), each writer's claims in
    salted-hash order; the result in salted-hash order too."""
    by_writer: dict[str, list[dict]] = defaultdict(list)
    for e in sorted(fired, key=lambda e: order_key(salt, e["claim_hash"])):
        by_writer[e["writer"]].append(e)
    share = ev.allocate(size, {w: len(v) for w, v in by_writer.items()})
    out = [e for w in sorted(by_writer) for e in by_writer[w][:share[w]]]
    return sorted(out, key=lambda e: order_key(salt, e["claim_hash"]))


def sheet_row(n: int, e: dict) -> dict:
    before, after = pr.around(e.get("field_text") or e["claim_text"], e["claim_text"])
    return {"n": n, "piece": piece(e)[:16], "writer": e["writer"], "field": e["field"], "before": before,
            "sentence": e["claim_text"], "after": after, **{f: "" for f in SHEET_FIELDS}}


def cmd_sample(a: argparse.Namespace) -> dict:
    cap = sample_max()
    size = a.size if a.size is not None else cap
    if not 0 < size <= cap:
        raise PoolRefused(f"--size must be 1..{cap} (cis.quality.claim_fresh_pool_sample_max)")
    manifest = json.loads(_read(a.manifest))
    text = _read(a.eligible)
    if manifest.get("eligible_sha256") != _sha(text):
        raise PoolRefused("the eligible manifest is not the manifest of this eligible file (sha256 differs)")
    lines = pn.read_jsonl(text)
    votes_text = _read(a.votes)
    fire = fire_report_check(votes_text, manifest)
    votes = ev.by_prefix([e["claim_hash"] for e in lines], read_fire_votes(votes_text, a.instrument))
    if not votes:
        raise PoolRefused("the votes cover none of the eligible claims (fire the eligible claims first)")
    fired = [e for e in lines if r1.majority(votes.get(e["claim_hash"]) or []) is True]
    salt = salt_of(manifest)
    chosen = pick(fired, size, salt)
    if not chosen:
        raise PoolRefused("the R1 majority fired on none of the eligible claims")
    os.makedirs(a.out_dir, exist_ok=True)

    def out(name: str) -> str:
        return os.path.join(a.out_dir, name)
    _write_jsonl(out("candidates.jsonl"), ({**e, "n": n} for n, e in enumerate(chosen, 1)))
    _write_jsonl(out("sheet.jsonl"), (sheet_row(n, e) for n, e in enumerate(chosen, 1)))
    sources = {piece(e)[:16]: e.get("source_text") or "" for e in chosen}
    _write_jsonl(out("sources.jsonl"), ({"piece": k, "source": v} for k, v in sorted(sources.items())))
    sample = {"kind": "fresh_sample", "issue": "AA-1048", "salt": salt, "instrument": a.instrument,
              "eligible_sha256": manifest["eligible_sha256"], "votes_sha256": _sha(votes_text)[:16], **fire,
              "voted": len(votes), "fired": len(fired),
              "size": size, "claims": len(chosen), "pieces": len(sources),
              "by_writer": dict(Counter(e["writer"] for e in chosen)),
              "rows": {str(n): e["claim_hash"][:16] for n, e in enumerate(chosen, 1)},
              "candidates_sha256": _sha(_read(out("candidates.jsonl")))}
    _write_json(out("sample-manifest.json"), sample)
    return {"ok": True, **{k: v for k, v in sample.items() if k != "rows"}}


# ── import ($0) ────────────────────────────────────────────────────────────────────────────────────────────────────


def check_sheet(rows: list[dict], n_rows: int, name: str = "sheet") -> dict[int, dict]:
    """Row number -> answers. Refused unless each row 1..n_rows is there exactly once with an allowed label, an
    ok reason for an ok label, an allowed step-1 answer and a non-empty reason (the S272 lesson: a panel agent can
    hand in placeholders or broken rows, so every row is checked)."""
    problems, out = [], {}
    for r in rows:
        n = r.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= n_rows:
            problems.append(f"row {n!r}: not a row number 1..{n_rows}")
            continue
        if n in out:
            problems.append(f"row {n}: twice")
            continue
        label, why, step1 = r.get("label"), str(r.get("reason") or "").strip(), r.get("step1")
        if label not in LABELS:
            problems.append(f"row {n}: label {label!r} not in {LABELS}")
        if label == "ok" and r.get("ok_reason") not in OK_REASONS:
            problems.append(f"row {n}: an ok label needs ok_reason in {tuple(OK_REASONS)}")
        if step1 not in STEP1:
            problems.append(f"row {n}: step1 {step1!r} not in {STEP1}")
        if not why:
            problems.append(f"row {n}: no reason")
        out[n] = {"label": label, "ok_reason": r.get("ok_reason") or "", "step1": step1}
    missing = sorted(set(range(1, n_rows + 1)) - set(out))
    if missing:
        problems.append(f"rows missing: {missing[:10]}{' ...' if len(missing) > 10 else ''}")
    if problems:
        raise PoolRefused(f"{name}: {len(problems)} problem(s): " + "; ".join(problems[:8]))
    return out


def majority_of(values: list[str]) -> Optional[str]:
    """The value at least len(values) // 2 + 1 sheets gave, else None."""
    if not values:
        return None
    top, n = Counter(values).most_common(1)[0]
    return top if n >= len(values) // 2 + 1 else None


def decide(answers: list[dict], stored: Optional[str]) -> tuple[Optional[str], str]:
    """(the pool group or None, why): violation -> tp, ok -> fp_<reason majority> (fp_other without one); no label
    majority, an unsure majority or a stored label that disagrees with the panel -> left out. The stored label (the
    adjudicated ground truth, when the claim has one) never decides alone: a claim needs both to agree."""
    label = majority_of([x["label"] for x in answers])
    if label is None:
        return None, "no_panel_majority"
    if label == "unsure":
        return None, "panel_unsure"
    if stored in ("violation", "ok") and stored != label:
        return None, "stored_label_disagrees"
    if label == "violation":
        return "tp", "violation"
    reason = majority_of([x["ok_reason"] for x in answers if x["label"] == "ok"])
    return OK_REASONS.get(reason or "other", "fp_other"), "ok"


def build(candidates: list[dict], sheets: list[dict[int, dict]], finals: dict[str, dict]) -> tuple[list, dict]:
    """(pool items, counts and the panel's step-1 answers per hash prefix)."""
    pool, left_out, groups, agree = [], Counter(), Counter(), Counter()
    step1: dict[str, str] = {}
    for e in candidates:
        n, h = e["n"], e["claim_hash"]
        answers = [s[n] for s in sheets]
        stored = (finals.get(h) or {}).get("label")
        group, why = decide(answers, stored)
        panel = majority_of([x["label"] for x in answers])
        if stored in ("violation", "ok") and panel in ("violation", "ok"):
            agree["agrees" if stored == panel else "disagrees"] += 1
        s1 = majority_of([x["step1"] for x in answers])
        if s1:
            step1[h[:16]] = s1
        if group is None:
            left_out[why] += 1
            continue
        groups[group] += 1
        label = "violation" if group == "tp" else "ok"
        cls = (finals.get(h) or {}).get("claim_class") or e.get("claim_class") or "unlabelled"
        item = r1.Item(h, e["writer"], cls, label, e.get("stratum") or "representative", e["content_sha256"],
                       e["field"], e["claim_text"], e.get("field_text") or e["claim_text"], e.get("source_text") or "",
                       tuple(e.get("evidence") or ()), r1.split_of_line(e))
        pool.append(ev.PoolItem(item, group))
    return pool, {"by_group": dict(sorted(groups.items())), "left_out": dict(sorted(left_out.items())),
                  "stored_label": dict(sorted(agree.items())), "step1_gold": step1,
                  "step1_gold_counts": dict(Counter(step1.values()))}


def cmd_import(a: argparse.Namespace) -> dict:
    sample = json.loads(_read(a.sample_manifest))
    elig = json.loads(_read(a.eligible_manifest))
    text = _read(a.candidates)
    if sample.get("candidates_sha256") != _sha(text):
        raise PoolRefused("the sample manifest is not the manifest of this candidates file (sha256 differs)")
    if sample.get("eligible_sha256") != elig.get("eligible_sha256"):
        raise PoolRefused("the sample was not drawn from this eligible manifest")
    candidates = pn.read_jsonl(text)
    if [e.get("n") for e in candidates] != list(range(1, len(candidates) + 1)):
        raise PoolRefused("the candidates file is not numbered 1..N in order")
    sheets = [check_sheet(pn.read_jsonl(_read(p)), len(candidates), os.path.basename(p)) for p in a.sheet]
    pool, counts = build(candidates, sheets, _finals(a.labels))
    if not pool:
        raise PoolRefused("no claim has a decided label")
    _write_jsonl(a.out, (p.line() for p in pool))
    manifest = {"kind": "fresh_pool", "issue": "AA-1048", "pool_sha256": _sha(_read(a.out)),
                "excluded_runs": elig.get("excluded_runs") or [], "excluded_pools": elig.get("excluded_pools") or [],
                "excluded_pieces": elig.get("excluded_pieces") or [], "split": elig.get("split"),
                "eligible_sha256": elig.get("eligible_sha256"), "salt": sample.get("salt"),
                "candidates_sha256": sample.get("candidates_sha256"), "sheets": len(sheets),
                "claims": len(candidates), "pool": len(pool), **counts}
    _write_json(a.manifest, manifest)
    return {"ok": True, **{k: v for k, v in manifest.items() if k not in ("excluded_pieces", "step1_gold")}}


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m services.ground_truth.fresh_pool", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("eligible", help="$0: the claims on pieces no earlier round touched (+ the R1 firing plan)")
    e.add_argument("--export", action="append", required=True)
    e.add_argument("--exclude-pool", action="append", default=[])
    e.add_argument("--exclude-run", action="append", default=[])
    e.add_argument("--split", choices=r1.SPLIT_CHOICES, default="train")
    e.add_argument("--labels", action="append", default=[])
    e.add_argument("--out", required=True)
    e.add_argument("--manifest", required=True)
    s = sub.add_parser("sample", help="$0: the fired claims to label, the sheet and the sources")
    s.add_argument("--eligible", required=True)
    s.add_argument("--manifest", required=True)
    s.add_argument("--votes", required=True)
    s.add_argument("--instrument", default=FIRE_INSTRUMENT)
    s.add_argument("--size", type=int)
    s.add_argument("--out-dir", required=True)
    i = sub.add_parser("import", help="$0: the filled sheet(s) -> the fresh pool and its manifest")
    i.add_argument("--candidates", required=True)
    i.add_argument("--sample-manifest", required=True)
    i.add_argument("--eligible-manifest", required=True)
    i.add_argument("--sheet", action="append", required=True)
    i.add_argument("--labels", action="append", default=[])
    i.add_argument("--out", required=True)
    i.add_argument("--manifest", required=True)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    a = parser().parse_args(argv)
    run = {"eligible": cmd_eligible, "sample": cmd_sample, "import": cmd_import}[a.cmd]
    try:
        rep: dict[str, Any] = run(a)
    except (ev.EvalRefused, r1.R1Refused) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    print(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
