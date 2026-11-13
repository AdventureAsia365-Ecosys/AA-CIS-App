"""AA-1049 W3 (root ADR 0020, S1 train) — the S1 control plane rewired in place, behind ``S1_CONTROL`` (default OFF).

The design (root audit 2026-11-06-S271 design-review.json, workflow s1 ``pick``, ``arch.roadmap[3]`` item "S1: ...")
keeps the S1 writer v2 and its code-built fact sheet and replaces the control half after it. This module is the $0
part of that train, built for the live A1 path and switched off until the tech lead turns it on (AA-1048 owns the
enforce switch of the final contract; this switch changes no publish rule):

(a) ``compose_page`` — code composes the contract fields and the writer's text is the narrative only: one day block
    per planned day, in plan order, with the heading ``Day N — Title`` numbered by code (the writer cannot add, merge,
    duplicate or renumber a day; a day it dropped stays missing, so validate's day check still fires); the writer's own
    fact line (distance / overnight / meals ...) is removed and the source day's verbatim fact lines are appended
    (``s1_writer_v2.source_fact_lines`` minus DO NOT USE); the SEO title is fitted at a word boundary; a meta still out
    of the policy band after the writer's meta call and its one retry becomes the code template (``meta_template``),
    accepted only when it passes the same code checks. The writer prompt is byte-identical (rule 7 still asks for a
    fact line; code replaces it): a prompt change is a paid A/B (W4/W5), and stored writer outputs stay valid replay
    cassettes.
(b) the S1 rows of the route table (``route_table.yaml`` section ``s1``, ``route_table.s1_route``): one row per S1
    code of the taxonomy (``taxonomy.yaml`` a1_codes) with the action repair_sentence / cut / regenerate_section /
    rewrite / hold; the lint (``route_table.s1_coverage_errors``) checks every S1 code has exactly one row.
(c) ``local_fix_node`` replaces flag_fix (and with it the ``_rerepair_meta`` loop) on the A1 v2 path: deterministic
    edits only (meta template / title fit / forbidden strip for ``regenerate_section`` rows, a sentence cut for ``cut``
    rows), the repair contract after the edits (a breach reverts the field), then the detectors that fired are run again
    on the changed text (the brand pre-audit here, validate and grounding in revalidate). What is left is held for
    review (``manual_check``): there is no second repair round (``graph._grounding_recheck`` skips its sentence
    re-repair when the switch is on). S277: the grounding node's sentence repair is a code cut too
    (``grounding.cut_and_recheck``, the ``cut`` rows of UNSUPPORTED_NUMBER / UNSUPPORTED_CLAIM): the train's path
    makes no ``s1_flag_fix`` call at all (the S276 smoke still made one, from that repair).
(d) the S1 brand judge stays off the live path whatever ``S1_JUDGE_LIVE`` says (D9:
    ``judge_node.s1_judge_off_live_path``).
(e) the class router (``route_after_judge``) replaces the score-only ``should_retry``: a ``rewrite`` row left on the
    draft gets ONE redraft (2 drafts in total), then the draft goes to the hitl exit; the saturated score only decides
    when no S1 code asks for a redraft.

Scope: an A1 admin rewrite whose draft the S1 writer v2 writes (``s1_writer_v2_live.applies`` / ``is_v2``). A tenant
(T2) rewrite, the Bedrock Batch seed and a legacy v1 draft never read the switch. Unset, ``graph`` or ANY other value =
main's path, byte for byte (tests/unit/test_aa1049_s1_train.py replays the main-recorded harness golden).
"""
from __future__ import annotations

import copy
import re
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()

CONTROL_ENV = "S1_CONTROL"
CONTRACT = "contract"
MAX_DRAFTS = 2              # the writer draft + one redraft for a `rewrite` row (design P2: "1 redraft")
REPORT_KEY = "s1_train"     # graph-state key of the train's report (compose / route / local_fix)


def mode() -> str:
    """``contract`` when the switch is on (exact value, case and spaces ignored), else ``graph`` (main)."""
    from services.quality import trial_flags
    raw = (trial_flags.get(CONTROL_ENV) or "").strip().lower()
    return CONTRACT if raw == CONTRACT else "graph"


def enabled() -> bool:
    return mode() == CONTRACT


def applies_to_write(state: dict) -> bool:
    """The writer call of this run composes its page by code: switch on and the S1 writer v2 writes it."""
    from . import s1_writer_v2_live
    return enabled() and s1_writer_v2_live.applies(state)


def active(state: dict) -> bool:
    """The control plane of this run is the train's: switch on, an A1 draft of the S1 writer v2 (never a tenant
    rewrite or a batch seed)."""
    from . import s1_writer_v2_live
    return (enabled() and s1_writer_v2_live.is_v2(state) and not state.get("is_tenant_rewrite")
            and state.get("batch_text") is None)


def _report(state: dict, key: str, value) -> dict:
    rep = dict(state.get(REPORT_KEY) or {})
    rep[key] = value
    return rep


# ------------------------------------------------------------------------------------------- (a) code composition

_FACT_LABELS = (r"meals?|overnight|accommodation|hotel|lodge|distance|driving|drive|walking|walk|trekking|trek|"
                r"hiking|cycling|biking|altitude|elevation|ascent|descent")
# one "Label: value." clause ("Distance: 12.5 km.", "Meals included: breakfast."); a value never holds a sentence end
_FACT_CLAUSE = (rf"(?:{_FACT_LABELS})(?:\s+(?:time|distance|included|provided|duration|level|grade|gain))?\s*:"
                r"\s*(?:[^.\n]|\.(?=\d))*\.?")
_FACT_ONLY_LINE_RE = re.compile(rf"^\s*[-•*]?\s*(?:{_FACT_CLAUSE}\s*)+$", re.I)
# the same run of clauses at the end of a prose line ("... the chapel. Overnight: Velmora.")
_TRAILING_FACTS_RE = re.compile(rf"(?<=[.!?])\s+(?:{_FACT_CLAUSE}\s*)+$", re.I)


def strip_writer_fact_lines(body: str) -> tuple[str, int]:
    """The day body without the writer's own fact line(s): a line made only of "Label: value" clauses, or such a run
    at the end of a prose line. Returns (body, number of fact lines removed). Prose is never touched."""
    kept, removed = [], 0
    for line in str(body or "").splitlines():
        if _FACT_ONLY_LINE_RE.match(line):
            removed += 1
            continue
        m = _TRAILING_FACTS_RE.search(line)
        if m and line[:m.start()].strip():
            line, removed = line[:m.start()].rstrip(), removed + 1
        kept.append(line)
    return "\n".join(kept).strip(), removed


def _writer_days(items) -> list[tuple[int, int, dict]]:
    """(first day, last day, object) of each writer itinerary object with a readable day label, in writer order."""
    from .day_parse import day_range
    from .s1_writer_v2 import _DAY_RANGE_VALUE_RE
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        raw = str(it.get("day") if it.get("day") is not None else "").strip()
        m = _DAY_RANGE_VALUE_RE.match(raw)
        if m:
            first, last = day_range(int(m.group(1)), int(m.group(2)))
        else:
            d = re.match(r"^\s*(?:day\s*)?(\d{1,3})\s*$", raw, re.I)
            if not d:
                continue
            first = last = int(d.group(1))
        out.append((first, last, it))
    return out


def compose_itineraries(writer_items, tour: dict, facts: dict, plan: dict,
                        strip_facts: Optional[Callable[[str, list[str]], tuple[str, int]]] = None
                        ) -> tuple[Optional[str], dict]:
    """The canonical itinerary string composed by code from the writer's day objects and the day plan, or None when
    the writer gave no usable list (the page keeps what main assembled). Report: {days, missing, extra, duplicate,
    writer_fact_lines_removed, source_fact_lines, fact_lines}.

    ``strip_facts(body, source lines appended to this day)`` replaces ``strip_writer_fact_lines`` on the writer's day
    body (default: every writer fact line goes). The Master recompose (AA-1049, ``s1_recompose``) passes one that only
    removes a writer fact clause the day's appended source lines state again, so no fact is lost."""
    from .s1_writer_v2 import drop_do_not_use, serialize_days, source_fact_lines
    days = _writer_days(writer_items)
    if not days:
        return None, {"composed": False}
    planned = [d["day"] for d in plan["days"]]
    plan_by_day = {d["day"]: d for d in plan["days"]}
    planned_set = set(planned)
    by_day: dict[int, tuple[int, int, dict]] = {}
    duplicate = []
    for first, last, it in days:
        for n in range(first, last + 1):
            if n in by_day:
                duplicate.append(n)
            else:
                by_day[n] = (first, last, it)
    extra = sorted({n for n in by_day if n not in planned_set})
    fact_mode = "source" if plan["mode"] in ("source_days", "single_day") else "writer"
    blocks, emitted, missing = [], set(), []
    removed = appended = 0
    for n in planned:
        if n in emitted:
            continue
        hit = by_day.get(n)
        if hit is None:
            missing.append(n)
            continue
        first, last, it = hit
        span = [d for d in range(first, last + 1) if d in planned_set]
        # a range object keeps its range heading only when every day of it is planned and unwritten so far
        ranged = last > first and span == list(range(first, last + 1)) and not (set(span) & emitted)
        label = f"{first}-{last}" if ranged else str(n)
        emitted |= set(span) if ranged else {n}
        title = " ".join(str(it.get("title") or "").split())
        body = str(it.get("body") or "").strip()
        if fact_mode == "source":
            src = plan_by_day[n]["source"] if plan["mode"] == "source_days" else plan["days"][0]["source"]
            lines = drop_do_not_use(source_fact_lines(src), facts)
            body, k = strip_facts(body, lines) if strip_facts else strip_writer_fact_lines(body)
            removed += k
            appended += len(lines)
            body = "\n".join(x for x in [body, *lines] if x)
        blocks.append({"day": label, "title": title, "body": body})
    report = {"composed": True, "days": len(blocks), "missing": missing, "extra": extra,
              "duplicate": sorted(set(duplicate)), "fact_lines": fact_mode,
              "writer_fact_lines_removed": removed, "source_fact_lines": appended}
    return serialize_days(blocks), report


def meta_template(page: dict, tour: dict, facts: dict) -> Optional[str]:
    """A meta description composed by code from the page's own name and highlights and the source duration and country
    (no new fact): "<name>, <duration> in <country>: <h1>, <h2> and <h3>." The highlights are taken in page order; the
    largest set (then the earliest) whose sentence lands in the policy band wins. None when no variant lands in it."""
    from itertools import combinations

    from services.quality.s1_facts import field_text
    from .s1_writer_v2 import as_list
    from .seo_meta_utils import SEO_META_MAX, SEO_META_MIN

    name = " ".join(field_text(page.get("name")).split()).rstrip(" .")
    if not name:
        return None
    duration = " ".join(field_text(tour.get("duration")).split()).rstrip(" .")
    country = " ".join(field_text(tour.get("country")).split()).rstrip(" .")
    head = name
    if duration and duration.lower() not in name.lower() and not re.search(r"\d", name):
        head += f", {duration}"
    if country and country.lower() not in name.lower():
        head += f" in {country}"
    items = [" ".join(str(h).split()).rstrip(" .;,") for h in as_list(page.get("highlights"))]
    # words the page and the source write in lower case: a highlight opening with one of them reads as a list item
    # after the colon ("Lunch at a farm" -> "lunch at a farm"); a proper noun keeps its capital
    lower_words = set(re.findall(r"\b[a-z][a-z'-]*\b", " ".join(
        field_text(x) for x in (page.get("subtitle"), page.get("summary"), page.get("itineraries"),
                                tour.get("summary"), tour.get("description"), tour.get("itineraries")))))
    items = [_lower_first(h, lower_words) for h in items if h and not h.lower().startswith("optional")][:6]
    for k in range(len(items), 0, -1):
        for picked in combinations(items, k):
            tail = picked[0] if k == 1 else ", ".join(picked[:-1]) + " and " + picked[-1]
            text = f"{head}: {tail}."
            if SEO_META_MIN <= len(text) <= SEO_META_MAX:
                return text
    return None


def _lower_first(item: str, lower_words: set) -> str:
    first = item.split(" ", 1)[0]
    if first[:1].isupper() and first[1:].islower() and first.lower() in lower_words:
        return first.lower() + item[len(first):]
    return item


def compose_page(page: dict, writer_json: dict, tour: dict, facts: dict, plan: dict, forbidden: list[str],
                 meta_problems: list[str],
                 strip_facts: Optional[Callable[[str, list[str]], tuple[str, int]]] = None) -> tuple[dict, dict]:
    """(a) The page with its contract fields composed by code (see the module docstring) and the report.
    ``strip_facts``: see ``compose_itineraries`` (default None = the train's rule)."""
    from .s1_writer_v2 import meta_problems as check_meta
    from .seo_meta_utils import SEO_TITLE_MAX, fit_seo_title

    page = dict(page)
    itin, report = compose_itineraries((writer_json or {}).get("itineraries"), tour, facts, plan, strip_facts)
    if itin is not None:
        page["itineraries"] = itin
    title = str(page.get("seo_title") or "")
    if len(title) > SEO_TITLE_MAX:
        page["seo_title"] = fit_seo_title(title)
        report["seo_title"] = "fitted"
    report["meta"] = "writer"
    if meta_problems:
        tpl = meta_template(page, tour, facts)
        if tpl and not check_meta({"seo_title": page.get("seo_title"), "seo_meta": tpl}, tour, facts, forbidden):
            page["seo_meta"] = tpl
            report["meta"] = "template"
        else:
            report["meta"] = "writer_out_of_band"
    return page, report


# --------------------------------------------------------------------------------------------- (e) the class router

def enforced_codes(state: dict) -> list[str]:
    """The S1 codes on the draft that block Master (graph.hard_block_codes of this run)."""
    from .graph import hard_block_codes
    return sorted(set(state.get("failure_codes") or []) & set(hard_block_codes(state)))


def route_after_judge(state: dict) -> str:
    """retry / done / hitl for an A1 v2 draft (replaces the score-only should_retry when the switch is on): a
    ``rewrite`` row left among the enforced codes redrafts once while drafts remain (MAX_DRAFTS), then goes to the
    hitl exit; without one, the score decides as on main (it is saturated: in practice ``done``)."""
    from services.acp_content_writing import route_table as rt
    from .graph import min_quality
    rewrite = [c for c in enforced_codes(state) if rt.s1_route(c).action == rt.REWRITE]
    if rewrite:
        return "retry" if (state.get("retry_count", 0) + 1) < MAX_DRAFTS else "hitl"
    return "done" if state.get("quality_score", 0) >= min_quality() else "hitl"


# ------------------------------------------------------------------------------------------------ (c) local fix

def _cut_meal_sentences(generated: dict, tour: dict) -> tuple[dict, list[str]]:
    """``cut`` for ITINERARY_MEAL_TIME_INVENTED: delete each itinerary prose sentence whose meal / clock-time claim
    the source does not back (a meal line copied from the source day, a clock time the source states), when its day
    keeps at least one other sentence. Heading lines and the code fact lines are never cut."""
    from . import grounding as g
    from .brand_audit_node import (ITIN_CLOCK_TIME, ITIN_MEAL_INVENTED, _norm_clock, source_backed_meal_line,
                                   source_day_meal_blocks)
    itin = str(generated.get("itineraries") or "")
    if not itin:
        return generated, []
    blocks = source_day_meal_blocks(tour, itin)
    src_clocks = {_norm_clock(m.group(0)) for m in ITIN_CLOCK_TIME.finditer(g.source_text(tour))}
    out_lines, cut, day, day_sentences = [], [], None, {}
    lines = itin.splitlines()
    for ln in lines:                                   # prose sentences per day, to keep >= 1 per day
        m = g._GEN_DAY_NUM_RE.match(ln)
        if m and g._DAY_TITLE_RE.match(ln):
            day = int(m.group(1))
            continue
        if _FACT_ONLY_LINE_RE.match(ln):
            continue
        day_sentences[day] = day_sentences.get(day, 0) + len([s for s in g._SENT_SPLIT_RE.split(ln) if s.strip()])
    day = None
    for ln in lines:
        m = g._GEN_DAY_NUM_RE.match(ln)
        if (m and g._DAY_TITLE_RE.match(ln)) or _FACT_ONLY_LINE_RE.match(ln):
            if m and g._DAY_TITLE_RE.match(ln):
                day = int(m.group(1))
            out_lines.append(ln)                        # headings and fact lines are never cut
            continue
        kept = []
        for s in g._SENT_SPLIT_RE.split(ln):
            claim = ITIN_MEAL_INVENTED.search(s) or any(_norm_clock(c.group(0)) not in src_clocks
                                                       for c in ITIN_CLOCK_TIME.finditer(s))
            if (s.strip() and claim and not source_backed_meal_line(s, blocks.get(day))
                    and day_sentences.get(day, 0) > 1):
                cut.append(s.strip())
                day_sentences[day] -= 1
                continue
            kept.append(s)
        if ln.strip() and not " ".join(x for x in kept if x.strip()).strip():
            continue                                    # the whole line was the cut sentence(s)
        out_lines.append(" ".join(x.strip() for x in kept if x.strip()) if len(kept) != 1 else kept[0])
    if not cut:
        return generated, []
    return {**generated, "itineraries": "\n".join(out_lines)}, cut


def _cut_optional_highlights(generated: dict) -> tuple[dict, list[str]]:
    """``cut`` for HIGHLIGHTS_OPTIONAL_LANGUAGE: drop each highlight that opens with "Optional" while 3+ remain."""
    from .brand_audit_node import OPTIONAL_HIGHLIGHT
    items = generated.get("highlights")
    if not isinstance(items, list):
        return generated, []
    keep, cut = [], []
    for h in items:
        if OPTIONAL_HIGHLIGHT.match(str(h)) and len(items) - len(cut) > 3:
            cut.append(str(h))
        else:
            keep.append(h)
    return ({**generated, "highlights": keep}, cut) if cut else (generated, [])


def _regenerate_sections(state: dict, generated: dict) -> tuple[dict, list[str]]:
    """``regenerate_section``: the deterministic section steps (forbidden strip, title fit, meta fit then the meta
    template) on a copy of the page."""
    from services.quality.s1_facts import build_fact_table
    from .forbidden_words import all_forbidden
    from .graph import _apply_forbidden_strip, _apply_seo_meta_fit
    from .s1_writer_v2 import meta_problems, with_agency_fields
    from .seo_meta_utils import SEO_TITLE_MAX, fit_seo_title

    out = dict(generated)
    done = []
    if _apply_forbidden_strip(out, state):
        done.append("forbidden_strip")
    title = str(out.get("seo_title") or "")
    if len(title) > SEO_TITLE_MAX:
        out["seo_title"] = fit_seo_title(title)
        done.append("seo_title_fit")
    if _apply_seo_meta_fit(out, state):
        done.append("seo_meta_fit")
    tour = with_agency_fields(state.get("tour") or {}, state.get("tour_agency"))
    facts = build_fact_table(tour)
    forbidden = all_forbidden(state.get("brand_forbidden_words"))
    meta = {"seo_title": out.get("seo_title"), "seo_meta": out.get("seo_meta")}
    if meta_problems(meta, tour, facts, forbidden):
        tpl = meta_template(out, tour, facts)
        if tpl and not meta_problems({**meta, "seo_meta": tpl}, tour, facts, forbidden):
            out["seo_meta"] = tpl
            done.append("seo_meta_template")
    return out, done


def local_fix_node(state: dict) -> dict:
    """(c) The S1 local fix (replaces flag_fix on the train's path). No model call. See the module docstring."""
    from services.acp_content_writing import route_table as rt
    from services.quality import elephant_policy
    from .brand_audit_node import derive_status, pre_audit_checks
    from .graph import _contract_after_edit

    generated = state.get("generated") or {}
    codes = list(dict.fromkeys(list(state.get("failure_codes") or []) + list(state.get("brand_audit_codes") or [])))
    routes = {c: rt.s1_route(c).action for c in codes}
    report = {"routes": routes, "edits": {}, "left": []}
    passthrough = {**state, "fix_pass_applied": False, "fix_pass_fields": [],
                   REPORT_KEY: _report(state, "local_fix", report)}
    if not generated or not codes:
        return passthrough
    # D8: an elephant-riding tour is kept as it is for a human; no edit of any kind.
    if elephant_policy.POLICY_CODE in codes and elephant_policy.scan(generated, state.get("tour")):
        report["left"] = sorted(codes)
        report["policy_keep"] = True
        return passthrough
    before = copy.deepcopy(generated)
    after = dict(generated)
    actions = set(routes.values())
    if rt.REGENERATE_SECTION in actions:
        after, done = _regenerate_sections(state, after)
        if done:
            report["edits"]["regenerate_section"] = done
    if rt.CUT in actions:
        if "ITINERARY_MEAL_TIME_INVENTED" in codes:
            after, cut = _cut_meal_sentences(after, state.get("tour") or {})
            if cut:
                report["edits"]["cut_itineraries"] = len(cut)
        if "HIGHLIGHTS_OPTIONAL_LANGUAGE" in codes:
            after, cut = _cut_optional_highlights(after)
            if cut:
                report["edits"]["cut_highlights"] = len(cut)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if not changed:
        report["left"] = sorted(codes)
        out = passthrough
        if state.get("brand_audit_status") == "flagged":
            # main's flag_fix would have rewritten these fields; nothing here can: held for review, never "fixed"
            out = {**out, "brand_audit_status": "manual_check"}
        return out
    s = {**state, "generated": after}
    s = _contract_after_edit(s, before, detector="s1_local_fix")   # a breach reverts the field (in place)
    after = s["generated"]
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    # Re-run the brand pre-audit that fired (validate and grounding re-run in revalidate on the changed text).
    pre = pre_audit_checks(after, state.get("tour"), tenant_id=state.get("tenant_id"), is_tenant_rewrite=False)
    status = derive_status(pre, None)
    left = sorted(set(pre))
    if left and status == "flagged":
        status = "manual_check"        # a code no deterministic step clears: review, never "fixed"
    report.update(left=left, changed_fields=changed)
    logger.info("s1_local_fix_done", changed=changed, left=left, edits=report["edits"])
    return {**s, "fix_pass_applied": bool(changed), "fix_pass_fields": changed,
            "brand_audit_codes": pre, "brand_audit_status": status,
            REPORT_KEY: _report(state, "local_fix", report)}
