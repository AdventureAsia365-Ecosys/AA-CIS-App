#!/usr/bin/env python3
"""AA-919 — READ-ONLY diagnosis of the R2 judge calibration (why judge_kappa < 0.6). $0: no model call, no write.

Successor of the session scripts `.tmp-session/S255/aa919/kappa_diag.py` and `S256/aa919/kappa_diag_s256.py` (root
workspace). The S256 one crashed on run 39096169 (`ValueError: could not convert string to float: "SEED: ..."`): it
reused the variable holding the judge SCORE for the judge SOURCE text, then called float() on it. The row parsing now
lives in `services.writing_memory.calibration` (`calibration_verdict`, `hidden_from_judge`), unit-tested, and the
source text has its own name.

Per run: the run row (verdict, stop, kappa, counts, cost), every CALIBRATION pair as r2.py stored it (sample, channel,
split, tenant, label verdict / source / grade, judge verdict / score / codes, the AA-729 baseline verdict on the same
frozen text, the source / output lengths the judge reads with the CURRENT image, panel-read fields the current judge
source lacks), then the report (`calibration.report`): recomputed kappa vs stored, confusion, recall per label,
kappa_max, leave-one-out range, score AUC, score-threshold counterfactuals, failure codes, per-stratum kappa (channel,
tenant, tier, split), and the disagreements with the judge's quoted claims. With >= 2 runs: the judge verdict per
sample across runs. Text excerpts (label reason, judge claims) are capped at 300 / 160 chars.

Run inside aa-cis-dev-worker (or -api) as cis_app (container DATABASE_URL), read-only session. The scripts/ folder is
not in the image: the file is exec()'d with no argv (root `.tmp-session/<S>/exec_svc.sh`). Arguments: prepend one
line to a copy, e.g. `import sys; sys.argv = ["kappa_diag", "39096169", "cc0f75cf"]`, or none for the newest 4 r2 runs
of --writer (default t9_social) that have a kappa.

  python3 kappa_diag.py [run-id-prefix ...] [--writer t9_social] [--limit 4]
"""
import argparse
import asyncio
import json
import os
import sys
from collections import Counter

for _p in (os.getcwd(), "/app"):
    if os.path.isdir(os.path.join(_p, "services")) and _p not in sys.path:
        sys.path.insert(0, _p)

from services.writing_memory import calibration as cal  # noqa: E402
from services.writing_memory import eval_checks as ec  # noqa: E402

HARD_FAIL_EXTRA = ("scope.persona_promise", "struct.leaked_instruction")


def short(s, n=300):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _app():
    """(source builder, output builder, judge_version, hard classes) from the image; None when not importable."""
    try:
        from services.quality.registry import hard_fact_classes
        from services.writing_memory import eval_writers as ew
        from services.writing_memory.eval_judge import judge_source_text, judge_version
        return judge_source_text, ew.output_text, judge_version, list(hard_fact_classes())
    except Exception as e:  # noqa: BLE001 — an old image: lengths are left out
        print(f"(app modules not importable: {type(e).__name__}: {e}; lengths are left out)")
        return None


def pair_record(r: dict, writer: str, shared: dict, app, base: dict) -> dict | None:
    """One calibration pair of a stored rejudge row (joined with its golden item), or None (no calibration)."""
    v = cal.calibration_verdict(r)
    if v is None:
        return None
    label = _j(r["label"]) or {}
    inp, out = _j(r["input"]) or {}, _j(r["control_output"]) or {}
    src_len = out_len = None
    judge_source = ""
    if app:
        src_fn, out_fn = app[0], app[1]
        try:
            judge_source = src_fn(writer, inp, shared)
            src_len, out_len = len(judge_source), len(out_fn(writer, out))
        except Exception as e:  # noqa: BLE001
            src_len = f"err {type(e).__name__}"
    ctx = inp.get("context") if isinstance(inp, dict) else None
    b = base.get(r["item_id"])
    item = {"channel": r["channel"], "tenant_id": r["tenant_id"], "tiers": list(r["tiers"] or []), "split": r["split"]}
    return {"sample": r["sample_id"], "label": r["label_verdict"], "source": r["label_source"],
            "grade": label.get("grade") if isinstance(label, dict) else None, "judge": v["verdict"],
            "score": v["score"], "codes": v["codes"], "claims": v["claims"],
            "base": b["verdict"] if b else None, "reason": label.get("reason") if isinstance(label, dict) else None,
            "hidden": cal.hidden_from_judge(ctx, judge_source) if app else {}, "channel": r["channel"],
            "split": r["split"], "tenant": r["tenant_id"], "strata": ec.item_strata(item),
            "src_len": src_len, "out_len": out_len, "cal": r["cal_kind"]}


async def find_run(c, prefix):
    rows = await c.fetch("SELECT id::text AS id FROM shared.prompt_eval_runs WHERE id::text LIKE $1 || '%'", prefix)
    if len(rows) != 1:
        print(f"\n### run {prefix}: {len(rows)} matches, skipped")
        return None
    return rows[0]["id"]


async def diag(c, prefix, app, shared_by_set):
    rid = await find_run(c, prefix)
    if rid is None:
        return None
    run = await c.fetchrow(
        """SELECT id::text AS id, run_kind, writer, verdict, stop_reason, judge_kappa, judge_counts, cost_usd,
                  cost_cap_usd, release_id::text AS release_id, prompt_version, created_at, details
             FROM shared.prompt_eval_runs WHERE id = $1::uuid""", rid)
    det = _j(run["details"]) or {}
    print(f"\n{'=' * 110}\n### run {rid}  {run['run_kind']} / {run['writer']}  "
          f"created {run['created_at']:%Y-%m-%d %H:%M}  prompt_version {run['prompt_version']}")
    print(f"verdict={run['verdict']} stop={run['stop_reason']} phase={det.get('stopped_phase')} "
          f"kappa={run['judge_kappa']} counts={run['judge_counts']} cost=${float(run['cost_usd'] or 0):.4f} "
          f"cap=${float(run['cost_cap_usd'] or 0):.2f} release={run['release_id']}")
    keep = ("calibration_run_id", "control_mode", "goal", "candidate", "planned", "calibration_items", "kappa_pairs",
            "kappa_labels", "limit", "golden_set", "counts", "tune", "kappa_strata_below")
    print("details:", json.dumps({k: det.get(k) for k in keep if k in det}, default=str)[:1500])

    rows = await c.fetch(
        """SELECT r.item_id::text AS item_id, r.verdict, r.score, r.failure_codes, r.judge_model, r.judge_version,
                  r.details, g.sample_id, g.channel, g.split, g.tiers, g.label_verdict, g.label_source, g.label,
                  g.input, g.control_output, g.tenant_id::text AS tenant_id, g.set_id::text AS set_id
             FROM shared.eval_item_result r JOIN shared.eval_golden_item g ON g.item_id = r.item_id
            WHERE r.run_id = $1::uuid AND r.arm = 'control' AND r.mode = 'rejudge'
            ORDER BY g.sample_id""", rid)
    base = {}
    if det.get("calibration_run_id"):
        for b in await c.fetch(
                """SELECT item_id::text AS item_id, verdict FROM shared.eval_item_result
                    WHERE run_id = $1::uuid AND arm = 'control' AND mode = 'score_only'""", det["calibration_run_id"]):
            base[b["item_id"]] = b
    recs, jversions, jmodels = [], Counter(), Counter()
    print(f"\n{'sample':10} {'chan':8} {'split':7} {'label':5} {'src':12} {'grade':6} | {'judge':5} {'score':>5} "
          f"{'codes':44} | {'aa729':5} | src/out chars  cal")
    for row in rows:
        r = dict(row)
        cd = (_j(r["details"]) or {}).get("calibration") if isinstance(_j(r["details"]), dict) else None
        r["cal_kind"] = cd if isinstance(cd, str) else ("dict" if isinstance(cd, dict) else None)
        jversions[r["judge_version"]] += 1
        jmodels[r["judge_model"]] += 1
        x = pair_record(r, run["writer"], shared_by_set.get(r["set_id"]) or {}, app, base)
        if x is None:
            continue
        recs.append(x)
        mark = "  " if x["label"] == x["judge"] else "<>"
        print(f"{x['sample']:10} {str(x['channel'] or '-'):8} {x['split']:7} {x['label']:5} {x['source']:12} "
              f"{str(x['grade'] or '-'):6} | {x['judge']:5} {str(x['score']):>5} {','.join(x['codes'])[:44]:44} | "
              f"{str(x['base'] or '-'):5} | {x['src_len']}/{x['out_len']} {mark} {x['cal']}")
    print(f"\njudge_version per row: {dict(jversions)}  judge_model: {dict(jmodels)}"
          + (f"  (current image judge_version {app[2]()})" if app else ""))
    if not recs:
        print("no calibration pairs")
        return {"run": rid, "recs": recs}

    rep = cal.report(recs)
    stored = run["judge_kappa"]
    match = stored is not None and rep["kappa"] is not None and abs(float(stored) - rep["kappa"]) < 0.0015
    cm = rep["confusion"]
    print(f"\nKAPPA recomputed {rep['kappa']} vs stored {stored} on {rep['pairs']} pairs "
          f"(stored kappa_pairs {det.get('kappa_pairs')}) -> {'MATCH' if match else 'MISMATCH'}")
    print(f"confusion (label -> judge): pass->pass {cm['pass_pass']}  pass->fail {cm['pass_fail']}  "
          f"fail->fail {cm['fail_fail']}  fail->pass {cm['fail_pass']}")
    print(f"labels {rep['labels']}  recall pass {rep['pass_recall']} fail {rep['fail_recall']}  kappa_max "
          f"{rep['kappa_max']}  counts {rep['counts']}  leave-one-out {rep['loo_kappa']}  score AUC {rep['score_auc']}")
    print(f"pass iff score >= t: {rep['score_threshold_kappa']}")
    print(f"failure codes (all): {rep['codes']}\nfailure codes on PASS-labelled texts: {rep['codes_on_pass_labels']}")
    print("by label_source:", {s: f"{sum(1 for x in recs if x['source'] == s and x['label'] == x['judge'])}/"
                                  f"{sum(1 for x in recs if x['source'] == s)} agree"
                               for s in sorted({x['source'] for x in recs})})
    print("by grade:", {g: f"{sum(1 for x in recs if str(x['grade']) == g and x['label'] == x['judge'])}/"
                          f"{sum(1 for x in recs if str(x['grade']) == g)} agree"
                        for g in sorted({str(x['grade']) for x in recs})})
    bp = [(x["label"], x["base"]) for x in recs if x["base"] in ("pass", "fail")]
    if bp:
        print(f"AA-729 baseline score_only verdict vs label on the same texts: kappa {ec.cohen_kappa(bp)} on {len(bp)}")
    if app and app[3]:
        hard_set = set(app[3]) | set(HARD_FAIL_EXTRA)
        cf = [(x["label"], "fail" if set(x["codes"]) & hard_set else "pass") for x in recs]
        print(f"counterfactual: fail only on a HARD class -> kappa {ec.cohen_kappa(cf)}")
    print("\nKAPPA PER STRATUM (gate = the overall row; measurable = >= 10 pairs and >= 3 of each label):")
    for line in cal.strata_lines(rep["strata"]):
        print(line)
    if rep["strata_below"]:
        print(f"measurable strata under 0.6: {rep['strata_below']}")
    print("\nDISAGREEMENTS (label reason, judge codes, panel fields the current judge source lacks):")
    for x in recs:
        if x["label"] == x["judge"]:
            continue
        print(f"- {x['sample']} label {x['label']} ({x['source']}, {x['grade']}) judge {x['judge']} {x['score']} "
              f"{x['codes']} hidden-from-judge {x['hidden']}\n    reason: {short(x['reason'])}")
        for cl in x["claims"][:8]:
            print(f"    judge claim [{cl.get('class')}] {short(cl.get('claim'), 160)!r} -- {short(cl.get('why'), 160)}")
    return {"run": rid, "recs": recs}


def parse(argv):
    ap = argparse.ArgumentParser(description="AA-919 read-only kappa diagnosis ($0)")
    ap.add_argument("runs", nargs="*", help="run id prefixes (default: the newest r2 runs of --writer with a kappa)")
    ap.add_argument("--writer", default="t9_social", choices=("a1", "t2", "t9_blog", "t9_social"))
    ap.add_argument("--limit", type=int, default=4)
    return ap.parse_args(argv)


async def main(argv=None):
    import asyncpg

    a = parse(sys.argv[1:] if argv is None else argv)
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await c.execute("SET default_transaction_read_only = on")
        assert (await c.fetchval("SHOW default_transaction_read_only")) == "on"
        prefixes = a.runs or [r["id"][:8] for r in await c.fetch(
            "SELECT id::text AS id FROM shared.prompt_eval_runs WHERE run_kind = 'r2' AND writer = $1 "
            "AND judge_kappa IS NOT NULL ORDER BY created_at DESC LIMIT $2", a.writer, a.limit)]
        print("runs:", prefixes)
        for r in await c.fetch("SELECT name, version, status, created_at FROM shared.eval_golden_set "
                               "ORDER BY created_at DESC LIMIT 3"):
            print("golden set:", dict(r))
        app = _app()
        shared_by_set = {r["set_id"]: _j(r["shared_inputs"]) or {} for r in await c.fetch(
            "SELECT set_id::text AS set_id, shared_inputs FROM shared.eval_golden_set")}
        results = [await diag(c, p, app, shared_by_set) for p in prefixes]
        res = [r for r in results if r and r.get("recs")]
        if len(res) >= 2:
            print(f"\n{'=' * 110}\nCROSS-RUN judge verdict on the same calibration items:")
            for s in sorted({x["sample"] for r in res for x in r["recs"]}):
                cells = []
                for r in res:
                    x = next((y for y in r["recs"] if y["sample"] == s), None)
                    cells.append(f"{r['run'][:8]}={x['judge'] + '/' + str(x['score']) if x else '-'}")
                lab = next((y["label"] for r in res for y in r["recs"] if y["sample"] == s), "-")
                print(f"  {s:10} label {lab:5} " + "  ".join(cells))
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
