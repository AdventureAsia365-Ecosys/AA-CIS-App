"""AA-790 (S294 D5, the $0 part of the TL-11 lift) — agreement of the offline tenant-voice rubric with the brand
labels of the AA-729 golden set, per writer, and what is missing to reach the bar. No model call, no DB, no write.

The bar (ADR 0020 D9, writing policy for a brand judge; the constants of ``services/quality/brand_agreement.py``,
nothing new here): Cohen's kappa >= 0.6 on >= 40 labelled pairs PER WRITER, with >= 5 labels of each class. Until a
writer meets it the rubric stays report-only (Writing Lab signal, Debate rank input); after it, a repair-only gate
that never holds.

Pairs. The instrument is ``services/acp_content_writing/voice_rubric.score`` (deterministic): it passes a text when it
would not block (``would_block`` False). The label is ``label.brand`` (``services/quality/brand_labels.py``, rubric
``aa1079-brand-v1``): strict = only ``fits`` passes, lenient = ``fits`` or ``partly``. FAIL (off-brand) is the
positive class, as in the brand-fit measure (AA-1079). The rubric was built on T9 posts; T2 tour pages are measured
the same way and reported apart (the T2 brand pack has no ``voice`` adjectives, so most T2 profiles compile neutral).

Label reliability. Each brand label carries the panel's per-lens votes (``voice``, ``audience``, ``skeptic``); the
mean pairwise kappa between lenses is the ceiling a rubric can reach against those labels, reported per writer.

Coverage. Per writer: labelled items (by source: ``agent_panel`` or ``tech_lead``), brand items in the fixture that
still have no brand label (the next panel pack), and how many pairs and how many of each class are still missing
after every unlabelled item is labelled (new golden samples needed).

    python -m services.quality.brand_voice_kappa --fixture <path or s3:key> [--out result.json]

Output: counts, kappas and sample ids (no text) between KAPPA-BEGIN / KAPPA-END on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import combinations
from typing import Any, Optional

from services.quality import brand_agreement as ba
from services.quality import brand_labels as bl

SCHEMA = "aa790-voice-kappa/1"
MODES = (("strict", False), ("lenient", True))
# T2 output fields that carry the tenant's prose (SEO fields and the name are left out: a title is not a voice sample).
T2_PROSE_FIELDS = ("subtitle", "summary", "highlights", "itineraries")


def _strings(v: Any, out: list[str]) -> None:
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        for x in v.values():
            _strings(x, out)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _strings(x, out)


def t2_text(output: Any) -> str:
    out: list[str] = []
    if isinstance(output, dict):
        for k in T2_PROSE_FIELDS:
            _strings(output.get(k), out)
    return "\n\n".join(s for s in out if s.strip())


def t2_pack(fixture: dict, sample: dict) -> dict:
    ref = (sample.get("input") or {}).get("brand_rules_ref") or ""
    rules = ((fixture.get("shared_inputs") or {}).get("brand_rules") or {}).get(ref) or {}
    return dict(rules) if isinstance(rules, dict) else {}


def scoring_input(fixture: dict, sample: dict, writer: str, channel: Optional[str]) -> dict:
    """text, brand pack, channel and CTA of one brand item, the way the voice rubric replay reads a piece."""
    if writer == "t2":
        pack = t2_pack(fixture, sample)
        return {"text": t2_text(sample.get("output")), "pack": pack, "channel": "tour", "cta": None,
                "validators": pack.get("custom_validators")}
    ctx = (sample.get("input") or {}).get("context") or {}
    out = sample.get("output") or {}
    return {"text": out.get("content_text") or "" if isinstance(out, dict) else "",
            "pack": ctx.get("brand_audience") or {}, "channel": channel, "cta": ctx.get("cta"), "validators": None}


def rubric_passes(inp: dict) -> tuple[bool, str]:
    """(passes, profile) of the voice rubric on one input: passes = it would not block."""
    from services.acp_content_writing import voice_rubric as vr

    rubric = vr.compile_profile(inp.get("pack"), vr.override_from_validators(inp.get("validators")))
    res = vr.score(inp.get("text") or "", rubric, channel=inp.get("channel"), cta=inp.get("cta"))
    return not res["would_block"], rubric.profile


def brand_items(fixture: dict) -> list[dict]:
    """[{sample_id, writer, channel, sample}] of every brand item (T2 / T9, never A1 or an injection trap)."""
    from services.golden_set.derive import writer_and_channel

    out = []
    for s in fixture.get("samples") or []:
        if bl.is_brand_item(s):
            writer, channel = writer_and_channel(s)
            out.append({"sample_id": s["sample_id"], "writer": writer, "channel": channel, "sample": s})
    return out


def lens_reliability(labels: list[dict], *, lenient: bool) -> dict:
    """Mean pairwise kappa between the panel's lenses (a lens pair counts only on items both voted on)."""
    votes: dict[str, dict[int, bool]] = {}
    for i, lab in enumerate(labels):
        for v in lab.get("votes") or []:
            if v.get("lens") and v.get("brand_fit") in bl.BRAND_FIT_VALUES:
                votes.setdefault(v["lens"], {})[i] = bl.label_passes(v, lenient=lenient)
    pairs = {}
    for a, b in combinations(sorted(votes), 2):
        common = sorted(set(votes[a]) & set(votes[b]))
        pairs[f"{a}~{b}"] = ba.agreement((votes[a][i], votes[b][i]) for i in common)
    kappas = [p["kappa"] for p in pairs.values() if p["kappa"] is not None]
    return {"pairs": {k: {"n": v["n"], "kappa": v["kappa"]} for k, v in pairs.items()},
            "mean_kappa": round(sum(kappas) / len(kappas), 4) if kappas else None}


def decide(agreement: dict) -> dict:
    """The D9 reading for one writer and mode (the tech lead decides): status, what the rubric may become, why."""
    n, fail, ok, k = agreement["n"], agreement["label_fail"], agreement["label_pass"], agreement["kappa"]
    if n < ba.GATE_MIN_PAIRS or fail < ba.MIN_PER_CLASS or ok < ba.MIN_PER_CLASS:
        return {"status": "insufficient_labels", "keep": "report_only",
                "why": f"{n} pairs ({fail} off-brand, {ok} on-brand); the bar is >= {ba.GATE_MIN_PAIRS} pairs with "
                       f">= {ba.MIN_PER_CLASS} of each class"}
    if k is None:
        return {"status": "no_signal", "keep": "report_only", "why": "one answer for every text: kappa undefined"}
    if k >= ba.GATE_KAPPA:
        return {"status": "calibrated", "keep": "repair_only_gate_allowed",
                "why": f"kappa {k} reaches {ba.GATE_KAPPA} on {n} pairs"}
    level = "moderate" if k >= ba.MODERATE_KAPPA else "weak"
    return {"status": f"not_calibrated_{level}", "keep": "report_only",
            "why": f"kappa {k} on {n} pairs is below {ba.GATE_KAPPA}"}


def coverage(items: list[dict], labelled: list[tuple[dict, dict]]) -> dict:
    """Per writer: what exists and what is missing to reach the bar."""
    out = {}
    for w in bl.BRAND_WRITERS:
        mine = [it for it in items if it["writer"] == w]
        labs = [lab for it, lab in labelled if it["writer"] == w]
        ids = {it["sample_id"] for it, _lab in labelled if it["writer"] == w}
        unlabelled = sorted(it["sample_id"] for it in mine if it["sample_id"] not in ids)
        by_source: dict[str, int] = {}
        by_fit: dict[str, int] = {}
        for lab in labs:
            by_source[lab["source"]] = by_source.get(lab["source"], 0) + 1
            by_fit[lab["brand_fit"]] = by_fit.get(lab["brand_fit"], 0) + 1
        reachable = len(labs) + len(unlabelled)
        classes = {}
        for mode, lenient in MODES:
            fail = sum(not bl.label_passes(lab, lenient=lenient) for lab in labs)
            classes[mode] = {"fail": fail, "pass": len(labs) - fail,
                             "fail_missing": max(0, ba.MIN_PER_CLASS - fail),
                             "pass_missing": max(0, ba.MIN_PER_CLASS - (len(labs) - fail))}
        out[w] = {"brand_items": len(mine), "labelled": len(labs), "by_source": by_source, "brand_fit": by_fit,
                  "human_labelled": by_source.get("tech_lead", 0), "unlabelled": len(unlabelled),
                  "unlabelled_ids": unlabelled, "pairs_missing": max(0, ba.GATE_MIN_PAIRS - len(labs)),
                  "pairs_missing_after_labelling_fixture": max(0, ba.GATE_MIN_PAIRS - reachable),
                  "classes": classes}
    return out


def measure(fixture: dict, *, scorer=rubric_passes) -> dict:
    items = brand_items(fixture)
    labelled = [(it, lab) for it in items if (lab := bl.brand_label_of(it["sample"])) is not None]
    writers = {}
    for w in bl.BRAND_WRITERS:
        mine = [(it, lab) for it, lab in labelled if it["writer"] == w]
        scored, profiles, errors = [], {}, []
        for it, lab in mine:
            try:
                passed, profile = scorer(scoring_input(fixture, it["sample"], w, it["channel"]))
            except Exception as e:  # noqa: BLE001 — an item that cannot be scored is listed, never fatal
                errors.append({"sample_id": it["sample_id"], "error": type(e).__name__})
                continue
            profiles[profile] = profiles.get(profile, 0) + 1
            scored.append((it["sample_id"], passed, lab))
        modes = {}
        for mode, lenient in MODES:
            agr = ba.agreement((p, bl.label_passes(lab, lenient=lenient)) for _sid, p, lab in scored)
            modes[mode] = {"agreement": agr, "decision": decide(agr),
                           "lens_reliability": lens_reliability([lab for _it, lab in mine], lenient=lenient),
                           "disagreements": sorted(sid for sid, p, lab in scored
                                                   if p != bl.label_passes(lab, lenient=lenient))}
        writers[w] = {"scored": len(scored), "profiles": dict(sorted(profiles.items())), "errors": errors,
                      **modes}
    return {"schema": SCHEMA, "cost_usd": 0, "fixture_version": fixture.get("fixture_version"),
            "rubric": bl.BRAND_RUBRIC_VERSION,
            "bar": {"kappa": ba.GATE_KAPPA, "min_pairs": ba.GATE_MIN_PAIRS, "min_per_class": ba.MIN_PER_CLASS},
            "coverage": coverage(items, labelled), "writers": writers}


def report_lines(result: dict) -> list[str]:
    lines = [f"voice rubric vs brand labels, fixture {result['fixture_version']} (bar: kappa >= "
             f"{result['bar']['kappa']} on >= {result['bar']['min_pairs']} pairs, >= {result['bar']['min_per_class']} "
             "per class)", "",
             "| writer | labelled | human | unlabelled | pairs missing | after fixture | mode | n | kappa | "
             "lens kappa | status |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for w in bl.BRAND_WRITERS:
        c, r = result["coverage"][w], result["writers"][w]
        for mode, _lenient in MODES:
            m = r[mode]
            lines.append(f"| {w} | {c['labelled']} | {c['human_labelled']} | {c['unlabelled']} | {c['pairs_missing']} "
                         f"| {c['pairs_missing_after_labelling_fixture']} | {mode} | {m['agreement']['n']} | "
                         f"{m['agreement']['kappa']} | {m['lens_reliability']['mean_kappa']} | "
                         f"{m['decision']['status']} |")
    return lines


def _read_fixture(ref: str) -> dict:
    from services.golden_set.importer import _read

    data, _uri = _read(ref)
    return json.loads(data)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--fixture", required=True, help="the golden fixture (a local path or s3:<key>)")
    p.add_argument("--out")
    a = p.parse_args(argv)
    result = measure(_read_fixture(a.fixture))
    text = json.dumps(result, indent=2, sort_keys=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print("\n".join(report_lines(result)))
    print("KAPPA-BEGIN")
    print(text)
    print("KAPPA-END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
