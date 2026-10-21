#!/usr/bin/env python3
"""AA-729 step 3 — render a score JSON (score.py) as the Markdown audit report. Local, $0, no DB.

  python3 -m scripts.aa729.report score-regenerate.json [--baseline older-score.json] [--out README.md]

`--baseline` adds a before/after section and refuses two runs on different fixture versions or modes
(Done-when line 3: before/after numbers on the same fixture version). AA-925: a stratum either run lists in
`invalid_strata` (a hard detector failed open after one retry) is marked INVALID and gets no delta; the
fitter revert criterion (s1_flag_fix calls per S1 sample, S1 review rate, T2/T3 rewrites) is printed.
The report goes to the private root repo, `docs/audits/<date>-S<nnn>-aa729-baseline/`.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any, Optional

STRATUM_ORDER = ("s1", "t2", "t9_blog", "t9_linkedin", "injection", "all")


def _pct(x: Optional[float]) -> str:
    return "–" if x is None else f"{x * 100:.1f}%"


def _usd(x: Optional[float], digits: int = 4) -> str:
    return "–" if x is None else f"${x:.{digits}f}"


def _cell(v: Any) -> str:
    return str("–" if v is None or v == "" else v).replace("|", "\\|").replace("\n", " ")


def _table(head: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out


def _ordered(metrics: dict) -> list[str]:
    return [k for k in STRATUM_ORDER if k in metrics] + sorted(k for k in metrics if k not in STRATUM_ORDER)


def _label_cell(m: dict, mode: str) -> str:
    if mode == "score-only":
        d = m.get("disagreement") or {}
        if not d.get("n"):
            return "no comparable label"
        return (f"{_pct(d.get('rate'))} of {d['n']} "
                f"(false pass {d.get('false_pass', 0)}, false fail {d.get('false_fail', 0)})")
    by = m.get("pass_rate_by_frozen_label") or {}
    parts = [f"label {k}: {_pct(v.get('pass_rate'))} of {v.get('n')}" for k, v in by.items() if v.get("n")]
    return "; ".join(parts) or "no labelled sample"


def _sample_note(r: dict) -> str:
    if r.get("error"):
        return r["error"]
    v = r.get("verdict") or {}
    for key in ("first_failure", "held_reason", "status"):
        if v.get(key) and not v.get("passed"):
            return f"{key}: {v[key]}"
    if v.get("blocking_failures"):
        return "blocking: " + ", ".join(v["blocking_failures"][:4])
    if v.get("failure_codes"):
        return "codes: " + ", ".join(v["failure_codes"][:4])
    inj = r.get("injection") or {}
    if inj.get("violations"):
        return "; ".join(inj["violations"])
    return ""


def render(score: dict, *, baseline: Optional[dict] = None, title: Optional[str] = None) -> str:
    if score.get("schema") != "aa729-score":
        raise ValueError("not an aa729 score file")
    mode = score["mode"]
    rows = score.get("samples") or []
    metrics = score.get("metrics") or {}
    totals = score.get("totals") or {}
    lines = [f"# {title or 'AA-729 golden set — ' + mode + ' run'}", ""]
    lines += _table(["", ""], [
        ["Run id", f"`{score['run_id']}`"], ["Mode", mode], ["Fixture version", score.get("fixture_version")],
        ["Started / updated (UTC)", f"{score.get('started_at')} / {score.get('updated_at')}"],
        ["Samples scored / planned", f"{len(rows)} / {len(score.get('planned') or [])}"],
        ["Cost cap", _usd(score.get("cap_usd"), 2)],
        ["Stopped early", score.get("stopped") or "no"],
        ["Final", "yes" if score.get("final") else "no (checkpoint)"],
    ])
    lines += ["", "## Cost", "",
              f"Total **{_usd(totals.get('cost_usd_llm_call_log'))}** from "
              f"`{totals.get('cost_source', 'llm_call_log')}`"
              " (writer, judge, Jev and embedding rows of this run). Check it against the provider dashboards"
              " before quoting it (lessons).", ""]
    lines += _table(["Check", "Value"], [
        ["In-process LLM spend (LLMClient)", _usd(totals.get("local_llm_usd"))],
        ["LLM calls made / logged", f"{totals.get('local_llm_calls')} / {totals.get('logged_llm_rows')}"],
        ["Calls with no log row", totals.get("unlogged_llm_calls")],
    ])
    by_stage = totals.get("rows_minus_calls_by_stage")
    if by_stage is not None:  # AA-927: signed reconciliation (S245 results do not have it)
        calls = totals.get("local_llm_calls_by_stage") or {}
        logged = totals.get("logged_llm_rows_by_stage") or {}
        reqs = totals.get("provider_requests_by_stage") or {}
        lines += ["", "Calls made in-process vs `llm_call_log` rows, per stage (LLM rows only: no Jev, no "
                  "embedding, no gateway-attempt rows). A non-zero diff means a call without its row "
                  "(negative) or a double row (positive).", ""]
        lines += _table(["Stage", "Calls made", "Provider requests", "Rows logged", "Rows − calls"],
                        [[st, calls.get(st, 0), reqs.get(st, calls.get(st, 0)), logged.get(st, 0), d]
                         for st, d in sorted(by_stage.items())])
        lines += [""]
        lines += _table(["Cost check", "Value"], [
            ["LLM spend in-process / logged", f"{_usd(totals.get('local_llm_usd'))} / "
                                              f"{_usd(totals.get('logged_llm_usd'))}"],
            ["Logged − in-process", _usd(totals.get("cost_diff_usd"))],
            ["Gateway-attempt rows (billed, discarded by the gateway)",
             f"{totals.get('gateway_attempt_rows', 0)} ({_usd(totals.get('gateway_attempt_usd'))})"],
            ["Embedding responses / rows",
             f"{totals.get('local_embed_calls', 0)} / {totals.get('logged_embed_rows', 0)}"],
        ])
    invalid = score.get("invalid_strata") or {}
    lines += ["", "## Degraded detectors (AA-925)", ""]
    deg_rows = []
    for k in sorted({r["stratum"] for r in rows}):
        counts: Counter = Counter()
        retried = 0
        for r in rows:
            if r["stratum"] != k:
                continue
            counts.update((r.get("verdict") or {}).get("degraded") or {})
            retried += bool(r.get("degraded_retry"))
        deg_rows.append([k, "INVALID (" + ", ".join(invalid[k]) + ")" if k in invalid else "valid",
                         ", ".join(f"{n} {c}" for n, c in sorted(counts.items())) or "none", retried])
    lines += _table(["Stratum", "Validity", "Degraded entries", "Samples retried"], deg_rows)
    lines += ["", "## Metrics per stratum", ""]
    label_head = "Judge vs label disagreement" if mode == "score-only" else "Pass-rate by frozen label"
    lines += _table(
        ["Stratum", "n", "Errors", "Pass-rate", "Labelled", label_head, "Jev ran", "Jev agrees with label",
         "Writer calls / sample", "Cost / sample", "Cost"],
        [[k, m.get("n"), m.get("errors"), _pct(m.get("pass_rate")), m.get("labelled"), _label_cell(m, mode),
          (m.get("jev") or {}).get("ran"),
          f"{_pct((m.get('jev') or {}).get('agree_with_label'))} of {(m.get('jev') or {}).get('label_n', 0)}",
          m.get("writer_calls_per_sample"), _usd(m.get("cost_per_sample_usd")), _usd(m.get("cost_usd"))]
         for k, m in ((k, metrics[k]) for k in _ordered(metrics))])
    inj = metrics.get("injection") or {}
    if inj:
        lines += ["", "## Prompt injection", ""]
        if "injection_defended_rate" in inj:
            lines.append(f"Defended (no canary, injected link, markup or prompt leak in the new output): "
                         f"**{_pct(inj['injection_defended_rate'])}** of {inj.get('scored')}.")
        if "injection_caught_rate" in inj:
            lines.append(f"Compromised outputs failed by the gates / judges: **{_pct(inj['injection_caught_rate'])}**"
                         f" of {inj.get('scored')}.")
    src = Counter((r["stratum"], r.get("label_source") or "?") for r in rows)
    lines += ["", "## Label provenance", ""]
    lines += _table(["Stratum", "Label source", "Samples"], [[a, b, n] for (a, b), n in sorted(src.items())])
    if baseline is not None:
        lines += ["", *before_after(baseline, score)]
    lines += ["", "## Per sample", ""]
    lines += _table(["Sample", "Stratum", "Group", "Passed", "Label (source)", "Jev problem", "Writer calls",
                     "Cost", "Note"],
                    [[r["sample_id"], r["stratum"], r.get("group"), (r.get("verdict") or {}).get("passed"),
                      f"{r.get('label_verdict') or '–'} ({r.get('label_source')})"
                      + (" post-edit" if r.get("post_edit") else ""),
                      (r.get("jev") or {}).get("problem"), (r.get("calls") or {}).get("writer", 0),
                      _usd(r.get("cost_usd")), _sample_note(r)[:160]]
                     for r in sorted(rows, key=lambda r: r["sample_id"])])
    lines += ["", "## How to read this", "",
              "- **score-only** judges the frozen outputs: *disagreement* compares the verdict with the label of the"
              " same text (post-edit samples and unlabelled ones are left out).",
              "- **regenerate** writes each sample again from its frozen input: pass-rate, writer calls and cost"
              " describe the current writer; the label describes the old output, so it is shown as pass-rate per"
              " frozen label, not as disagreement.",
              "- *Jev* counts only questions that judge the written text; *problem* = any decided verdict in the"
              " question's problem zone.",
              "- Before/after numbers are only valid on the same fixture version and mode."]
    return "\n".join(lines) + "\n"


def before_after(before: dict, after: dict) -> list[str]:
    if before.get("fixture_version") != after.get("fixture_version"):
        raise ValueError(f"fixture versions differ: {before.get('fixture_version')} vs {after.get('fixture_version')}")
    if before.get("mode") != after.get("mode"):
        raise ValueError(f"modes differ: {before.get('mode')} vs {after.get('mode')}")
    bm, am = before.get("metrics") or {}, after.get("metrics") or {}
    invalid = set(before.get("invalid_strata") or {}) | set(after.get("invalid_strata") or {})
    if invalid:
        invalid.add("all")   # the "all" row mixes every stratum, an invalid one included
    lines = ["## Before / after", "", f"Before `{before['run_id']}` → after `{after['run_id']}`, fixture "
             f"{after.get('fixture_version')}, mode {after.get('mode')}.", ""]
    if invalid:
        lines += [f"INVALID strata (a hard detector failed open after one retry, AA-925): "
                  f"{', '.join(sorted(invalid))} — no delta is computed for them.", ""]
    rows = []
    for k in _ordered({**bm, **am}):
        b, a = bm.get(k) or {}, am.get(k) or {}
        delta = (None if b.get("pass_rate") is None or a.get("pass_rate") is None or k in invalid
                 else a["pass_rate"] - b["pass_rate"])
        rows.append([k, _pct(b.get("pass_rate")), _pct(a.get("pass_rate")),
                     ("INVALID" if k in invalid else "–") if delta is None else f"{delta * 100:+.1f} pt",
                     _pct((b.get("disagreement") or {}).get("rate")), _pct((a.get("disagreement") or {}).get("rate")),
                     _usd(b.get("cost_per_sample_usd")), _usd(a.get("cost_per_sample_usd")),
                     b.get("writer_calls_per_sample"), a.get("writer_calls_per_sample")])
    lines += _table(["Stratum", "Pass before", "Pass after", "Δ", "Disagree before", "Disagree after",
                     "Cost/sample before", "Cost/sample after", "Writer calls before", "Writer calls after"], rows)
    bs = {r["sample_id"]: (r.get("verdict") or {}).get("passed") for r in before.get("samples") or []}
    flips = [(r["sample_id"], bs[r["sample_id"]], (r.get("verdict") or {}).get("passed"))
             for r in after.get("samples") or [] if r["sample_id"] in bs
             and bs[r["sample_id"]] != (r.get("verdict") or {}).get("passed")]
    lines += ["", f"Samples whose verdict changed: {len(flips)}"]
    if flips:
        lines += [""] + _table(["Sample", "Before", "After"], [list(f) for f in sorted(flips)])
    lines += ["", *revert_criterion(before, after, invalid)]
    return lines


def _stage_calls(run: dict, stratum: str, stage: str) -> tuple[Optional[float], int]:
    rows = [r for r in run.get("samples") or [] if r["stratum"] == stratum and not r.get("error")]
    if not rows:
        return None, 0
    return sum(int((r.get("calls_by_stage") or {}).get(stage, 0)) for r in rows) / len(rows), len(rows)


def _review_rate(run: dict, stratum: str) -> Optional[float]:
    rows = [r for r in run.get("samples") or [] if r["stratum"] == stratum and not r.get("error")]
    return None if not rows else sum(not (r.get("verdict") or {}).get("passed") for r in rows) / len(rows)


def revert_criterion(before: dict, after: dict, invalid: set) -> list[str]:
    """AA-925 — the fitter revert criterion (PR body): the AA-740 mid-clause cut is gone, so a too-long meta
    costs an LLM repair. Revise the fitter part if s1_flag_fix calls per S1 sample or the S1 review rate move
    past the baseline plus the A' noise (score-only A vs A'). Regenerate runs only (score-only calls no writer)."""
    out = ["## Fitter revert criterion (AA-925)", ""]
    rows = []
    for stratum, stage in (("s1", "s1_flag_fix"), ("t2", "t2_flag_fix"), ("t2", "t2_generate")):
        (b, nb), (a, na) = _stage_calls(before, stratum, stage), _stage_calls(after, stratum, stage)
        rows.append([stratum, f"{stage} calls / sample", "–" if b is None else f"{b:.2f} (n={nb})",
                     "–" if a is None else f"{a:.2f} (n={na})",
                     "INVALID" if stratum in invalid else ("–" if a is None or b is None else f"{a - b:+.2f}")])
    for stratum in ("s1", "t2"):
        b, a = _review_rate(before, stratum), _review_rate(after, stratum)
        delta = "–" if a is None or b is None else f"{(a - b) * 100:+.1f} pt"
        rows.append([stratum, "review rate (not passed)", _pct(b), _pct(a), "INVALID" if stratum in invalid else delta])
    out += _table(["Stratum", "Measure", "Before", "After", "Δ"], rows)
    out += ["", "Revise the fitter part of AA-925 when a Δ is past the baseline + A' noise "
            "(regenerate mode only; score-only calls no writer)."]
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("score")
    ap.add_argument("--baseline")
    ap.add_argument("--title")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    with open(a.score, encoding="utf-8") as f:
        score = json.load(f)
    baseline = None
    if a.baseline:
        with open(a.baseline, encoding="utf-8") as f:
            baseline = json.load(f)
    text = render(score, baseline=baseline, title=a.title)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {a.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
