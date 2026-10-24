"""AA-645 (S249) — offline replay ($0, no DB, no model call) of the flag_fix day-loss fix on the S249 shadow <=50.

Reads the three shadow artefacts (root `.tmp-session/s249/aa645/`): `wave50-shadow.json` (one row per tour: brand
audit codes, flag_fix fields, failure codes, plan / new / live days), `wave50-packet.json` (the blind-panel packet: the
source dict and the final new page of every tour) and `wave50-key.json` (packet page id -> tour + arm). It re-runs
PRODUCTION code on them:

(a) meal-line census + skip: `brand_audit_node.itinerary_claim_sentences` + `ITIN_MEAL_INVENTED` give the meal-claim
    sentences (the old matcher); `source_backed_meal_line` (the AA-645 fix) says which are copies of the source day's
    own meal line. A tour flagged ITINERARY_MEAL_TIME_INVENTED in the shadow whose every claim is now source-backed (and
    that has no unsupported clock time) is no longer flagged without asking Jev, so flag_fix does not rewrite its
    itinerary. Claims left over still go to Jev: the replay cannot ask Jev, so it counts them as still flagged (the
    shadow outcome). Caveat: the shadow does not keep the writer draft; the page read is the FINAL page, i.e. after
    flag_fix for the tours it changed (flag_fix kept the "Meals:" lines on those pages, so the lines are the writer's;
    a sentence flag_fix removed is not seen). On the tours whose flag_fix was reverted or not run the page IS the draft.
(b) days_kept: for every tour whose itinerary flag_fix rewrote, `repair_contract.check` on (draft, final). The draft is
    rebuilt as the final page plus one placeholder block per plan day the final page lacks (the plan is what the writer
    was told to write; the grounding call count of Rajasthan points at a 15-day draft, root panel50/rajasthan.json).
(c) publishable: `admin_pipeline._is_publishable` on the shadow row with `writer_version="v2"` and the replayed
    outcome (graph.hard_block_codes: ITINERARY_DAY_COUNT_MISMATCH is hard for an A1 v2 draft). The old gate (no
    writer_version) must reproduce the shadow's `publishable` on all 50 rows, or the replay stops.

Usage (repo root, the repo venv; importing the API module wants the unit-test env, and no AWS profile so the Sentry
DSN lookup fails fast instead of asking for an MFA code):
    env -u AWS_PROFILE AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null ENVIRONMENT=test \
        JWT_SECRET=x DATABASE_URL=postgresql://x:y@localhost/z \
        python -m scripts.aa645.dayloss_replay --dir <root>/.tmp-session/s249/aa645 [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MEAL = "ITINERARY_MEAL_TIME_INVENTED"
MISMATCH = "ITINERARY_DAY_COUNT_MISMATCH"


def load(dir_: Path) -> list[dict]:
    """One record per shadow tour: the shadow row, the packet source and the final new page."""
    shadow = json.loads((dir_ / "wave50-shadow.json").read_text())
    packet = json.loads((dir_ / "wave50-packet.json").read_text())
    key = json.loads((dir_ / "wave50-key.json").read_text())
    pages: dict[str, dict] = {}
    for group in packet["groups"]:
        for page in group["pages"]:
            k = key[page["page_id"]]
            if k["arm"] == "shadow_w":
                pages[k["sample_id"]] = {"source": group["source"], "page": page["page"]}
    out = []
    for row in shadow["tours"]:
        p = pages.get(row["tour_id"])
        if p is None:
            raise SystemExit(f"tour {row['tour_id']} has no shadow_w page in the packet")
        out.append({"row": row, **p})
    return out


def meal_census(source: dict, page: dict) -> dict:
    """Old vs new reading of the page's meal / clock-time claims (production functions)."""
    from services.content_generation import brand_audit_node as ba
    from services.content_generation import grounding as g

    itin = str(page.get("itineraries") or "")
    src_clocks = {ba._norm_clock(m.group(0)) for m in ba.ITIN_CLOCK_TIME.finditer(g.source_text(source))}
    clock_unsupported = [m.group(0) for m in ba.ITIN_CLOCK_TIME.finditer(itin)
                         if ba._norm_clock(m.group(0)) not in src_clocks]
    claims = [c for c in ba.itinerary_claim_sentences(itin) if ba.ITIN_MEAL_INVENTED.search(c["sentence"])]
    blocks = ba.source_day_meal_blocks(source, itin)
    backed = [c for c in claims if ba.source_backed_meal_line(c["sentence"], blocks.get(c["day"]))]
    left = [c for c in claims if c not in backed]
    lines = [c for c in claims
             if ba._MEAL_LINE_RE.search(c["sentence"]) or ba._PAGE_MEAL_CODE_RE.search(c["sentence"])]
    return {"claims": len(claims), "backed": len(backed), "lines": len(lines),
            "left": [f"Day {c['day']}: {c['sentence']}" for c in left],
            "clock_unsupported": clock_unsupported,
            "cleared_without_jev": bool(claims) and not left and not clock_unsupported}


def draft_from_plan(final_itin: str, plan_days: list[int]) -> str:
    """The final page plus a placeholder "Day N —" block for each plan day it lacks (see (b))."""
    from services.quality.repair_contract import day_headings

    have = set(day_headings(final_itin))
    extra = [f"Day {d} — (draft day, not stored in the shadow)\nThe writer's text for this day." for d in plan_days
             if d not in have]
    return "\n\n".join([final_itin.rstrip()] + extra) if extra else final_itin


def replay(records: list[dict]) -> dict:
    from api.routers.admin_pipeline import _is_publishable
    from services.quality import repair_contract

    rows, mismatched_old_gate = [], []
    for rec in records:
        r, page, source = rec["row"], rec["page"], rec["source"]
        audit_codes = set((r.get("brand_audit") or {}).get("codes") or [])
        ff_fields = set((r.get("flag_fix") or {}).get("fields") or [])
        census = meal_census(source, page)
        base = {"quality_score": r["quality_score"], "brand_audit_status": (r.get("brand_audit") or {}).get("status"),
                "fix_pass_applied": bool((r.get("flag_fix") or {}).get("applied")),
                "failure_codes": list(r.get("failure_codes") or [])}
        if bool(_is_publishable(base)) != bool(r["publishable"]):
            mismatched_old_gate.append(r["name"])

        flagged_meal = MEAL in audit_codes
        skip_itin = flagged_meal and census["cleared_without_jev"]
        other_codes = audit_codes - {MEAL}
        skip_all = skip_itin and not other_codes and ff_fields <= {"itineraries"}

        # (b) days_kept on the itinerary rewrite flag_fix made (old audit) / would still make (new audit)
        plan = (r.get("days") or {}).get("plan") or []
        final_itin = str(page.get("itineraries") or "")
        draft = draft_from_plan(final_itin, plan)
        res = repair_contract.check({"itineraries": draft}, {"itineraries": final_itin}, allowed={"itineraries"},
                                    names=False)
        days_viol = [v for v in res.violations if v["invariant"] == "days_kept"]
        rewrote_itin = "itineraries" in ff_fields
        reverted_old = rewrote_itin and bool(days_viol)
        reverted_new = reverted_old and not skip_itin

        # (c) the replayed outcome: a reverted rewrite keeps the draft's (plan's) days; a skipped rewrite never
        # touched them. The day-count code then fires only when the final day list still misses a plan day.
        days_kept_at_plan = reverted_old or (skip_itin and rewrote_itin and bool(days_viol))
        codes = list(base["failure_codes"])
        if days_kept_at_plan:
            codes = [c for c in codes if c != MISMATCH]
        new_result = {**base, "failure_codes": codes, "writer_version": "v2"}
        worst = {**base, "writer_version": "v2"}           # if the writer itself had dropped the days
        rows.append({
            "name": r["name"], "country": r.get("country"), "plan_days": len(plan),
            "final_days": len((r.get("days") or {}).get("new") or []),
            "meal_flagged": flagged_meal, "flag_fix_fields": sorted(ff_fields),
            "meal_claims": census["claims"], "meal_backed": census["backed"], "meal_lines": census["lines"],
            "meal_left": census["left"],
            "clock_unsupported": census["clock_unsupported"],
            "a_skip_itinerary_rewrite": skip_itin, "a_skip_flag_fix": skip_all,
            "b_days_kept_reverts_old": reverted_old, "b_days_kept_reverts_new": reverted_new,
            "b_detail": days_viol[0]["detail"] if days_viol else None,
            "c_publishable_old": bool(r["publishable"]),
            "c_publishable_new": bool(_is_publishable(new_result)),
            "c_publishable_if_writer_dropped": bool(_is_publishable(worst)),
            "days_published": len(plan) if days_kept_at_plan else len((r.get("days") or {}).get("new") or []),
        })
    if mismatched_old_gate:
        raise SystemExit(f"old publish gate does not reproduce the shadow on: {mismatched_old_gate}")
    return {"rows": rows, "summary": summarise(rows)}


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    flagged = [x for x in rows if x["meal_flagged"]]
    rewrote = [x for x in rows if "itineraries" in x["flag_fix_fields"]]
    return {
        "tours": n,
        "meal_flagged_old": len(flagged),
        "meal_claim_sentences": sum(x["meal_claims"] for x in rows),
        "meal_claim_sentences_source_backed": sum(x["meal_backed"] for x in rows),
        "meal_line_sentences": sum(x["meal_lines"] for x in rows),     # "Meals: …" / "(B, L)" lines among the claims
        "a_skip_itinerary_rewrite": sum(x["a_skip_itinerary_rewrite"] for x in rows),
        "a_skip_flag_fix_entirely": sum(x["a_skip_flag_fix"] for x in rows),
        "a_still_to_jev": sum(1 for x in flagged if not x["a_skip_itinerary_rewrite"]),
        "flag_fix_rewrote_itinerary_old": len(rewrote),
        "flag_fix_rewrites_itinerary_new": sum(1 for x in rewrote if not x["a_skip_itinerary_rewrite"]),
        "b_days_kept_reverts_old_audit": sum(x["b_days_kept_reverts_old"] for x in rows),
        "b_days_kept_reverts_new_audit": sum(x["b_days_kept_reverts_new"] for x in rows),
        "c_non_publishable_old": sum(not x["c_publishable_old"] for x in rows),
        "c_non_publishable_new": sum(not x["c_publishable_new"] for x in rows),
        "c_non_publishable_if_writer_dropped": sum(not x["c_publishable_if_writer_dropped"] for x in rows),
        "published_with_fewer_days_than_plan_new": [x["name"] for x in rows
                                                    if x["c_publishable_new"] and x["days_published"] < x["plan_days"]],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dir", required=True, type=Path, help="folder with wave50-shadow/packet/key.json")
    ap.add_argument("--json", type=Path, help="write the per-tour rows + summary here")
    args = ap.parse_args(argv)
    out = replay(load(args.dir))
    s = out["summary"]
    for k, v in s.items():
        print(f"{k}: {v}")
    print("\nper tour (meal-flagged or itinerary rewritten or day mismatch):")
    for x in out["rows"]:
        if x["meal_flagged"] or x["flag_fix_fields"] or x["plan_days"] != x["final_days"]:
            print(f"- {x['name'][:40]:40} claims {x['meal_claims']:2} backed {x['meal_backed']:2} "
                  f"skip_itin={x['a_skip_itinerary_rewrite']!s:5} days_kept={x['b_days_kept_reverts_new']!s:5} "
                  f"pub {x['c_publishable_old']}->{x['c_publishable_new']} days_published {x['days_published']}"
                  f"/{x['plan_days']}" + (f"  left: {x['meal_left'][:2]}" if x["meal_left"] else ""))
    if args.json:
        args.json.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
