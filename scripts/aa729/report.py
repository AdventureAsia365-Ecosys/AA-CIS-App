#!/usr/bin/env python3
"""AA-729 step 3 — render a score JSON (score.py) as the Markdown audit report. Local, $0, no DB.

  python3 -m scripts.aa729.report score-regenerate.json [--baseline older-score.json] [--out README.md]

`--baseline` adds a before/after section and refuses two runs on different fixture versions or modes
(Done-when line 3: before/after numbers on the same fixture version). The report goes to the private root
repo, `docs/audits/<date>-S<nnn>-aa729-baseline/`.
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
    lines = ["## Before / after", "", f"Before `{before['run_id']}` → after `{after['run_id']}`, fixture "
             f"{after.get('fixture_version')}, mode {after.get('mode')}.", ""]
    rows = []
    for k in _ordered({**bm, **am}):
        b, a = bm.get(k) or {}, am.get(k) or {}
        delta = (None if b.get("pass_rate") is None or a.get("pass_rate") is None
                 else a["pass_rate"] - b["pass_rate"])
        rows.append([k, _pct(b.get("pass_rate")), _pct(a.get("pass_rate")),
                     "–" if delta is None else f"{delta * 100:+.1f} pt",
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
    return lines


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
