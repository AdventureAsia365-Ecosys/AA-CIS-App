"""AA-1077 — the extend repair: a too-short blog (F4 `length_short`) gets new paragraphs from its own untold seed facts
in its thinnest body sections, instead of a full rewrite.

AA-741 phase 3 (S276, root docs/audits/2026-11-10-S276-aa741-phase3/ §4.5.6): a short blog went to the full rewrite
(route table `F4_extreme_length: rewrite`). 18 rewrites fixed the length of 6 pieces, made 5 shorter or kept them the
same, and added a new hold-capable gate on 9 (F17 / F13 / F9 claims the regression guard then had to revert), for
about 1.02 USD in second writer calls. The extend repair keeps every passing section byte for byte and only adds:

- `plan_for` (pure, $0): runs only on a blog whose F4 failure is the SHORT side, with at least one body section and at
  least one seed fact sentence the post does not tell yet (`quality_gates.unused_source_facts`, the AA-788 F14 floor
  rewrite's source of facts). It returns the attempt's ledger with that F4 entry set aside, so the loop routes the
  other failures as before (a sentence repair for quoted findings; any gate that needs a full rewrite still gets
  one, and then no extend runs). None = the old path, unchanged.
- `extend` (one writer call through the gateway, `generate.extend_sections`, mode `length_extend`): the thinnest body
  sections (never the opening before the first heading, never the FAQ) each get ONE new paragraph, sized from the
  deficit to `length_contract.extend_target_chars()` (the length target, S278: floor + margin). Each paragraph is
  checked in code before it stands: no heading / list / question / tag / CTA, not more than twice the words asked,
  no specific the source lacks (`repair_contract.new_specifics`: a number, time, ordinal, position or pace word —
  the source-fidelity rule: never an activity, service or time the seed lacks), and a clean seam
  (`sentence_repair._integrity`: no new repeat, echo, orphan or misplaced moment). A paragraph that fails is
  dropped; the others stay. Then the attempt's deterministic fixes and EVERY T10 gate run on the whole text as for
  any attempt (F1 / F17 / F9 read the added text), and the AA-528 regression guard keeps attempt 1 when the added
  text broke a gate that passed (truth before length).

Round 2 (S278 paid trial: a short blog's repair round with 6 F17 cuts shrank it 3,957 → 3,546 chars and held it on
length): `plan_for` still routes attempt 2 (the F4 entry set aside), but the expand itself is planned by
`plan_after_repair` on the text the repair LEFT, after the sentence repair's cuts or after a full rewrite, whenever
that text is under the floor: its deficit includes the cut characters and the sections the repair shortened come
first (`hole_chars`, marked to the model "a repair cut text here"). A paragraph that names where its facts come from
(`hygiene.LEAK_RE`) is refused like one with a new specific.

Switch (S280, CIS 418): the setting `cis.t9.blog_length_extend` (off | on, Platform › Settings, maker-checker,
evidence-gated), read per piece for the piece's tenant (`mode(tenant_id)`): the tenant's value, else the platform
value, else the default off. A tenant value runs the paid trial on one audit tenant, through the normal tenant studio
flow (whose routes never pass a job's trial_flags); the platform value turns it on for everyone. Resolution order: a
job's trial flag `T9_LENGTH_EXTEND` (quality.trial_flags, one audit-tenant job) first, then the settings reader, whose
own first step is the env var `T9_LENGTH_EXTEND` (the old switch, now the definition's emergency override for one
release; scripts/aa1077/extend_trial.py sets it in its own process). Off = main's full rewrite, byte for byte (lesson
S272: a change of the writing loop that has not had its measured trial ships behind a switch whose default is main).

After the normalizer (S280, `length_recheck.lengthen_after_normalizer`): a loop-approved blog a MANDATORY normalizer cut
took under the floor gets one more `plan_after_repair` + `extend` pass on the stored text, with the same pinned switch,
then the final integrity pass and every gate again.

S281 (paid trial 5: a repair round shrank a blog 4,988 → 4,669 chars, the expand then added nothing: the writer left
both sections empty, "no fitting fact"). Two settings (CIS 422), platform + tenant, read once per piece with the switch
(`pin`) and only while it is on:
- `cis.t9.blog_extend_material` (source_facts | with_background, default source_facts = the repair above). With
  background, a paragraph may also give general background about a place, people, tradition or history the post or its
  source names (`background_names`: the seed's moment names), in the third person (writing policy: background about a
  named place is fine; never a tour activity, service, time, view or promise the source lacks). A plan no longer needs
  an untold fact. Each background paragraph gets three more code checks before it stands: no reader / group address
  (`_READER_RE`: "you", "we", "our": background is about the place, not what the trip does), no traveller, trip or
  service wording in the third person either (`_TRIP_RE`: travellers / visitors / the group as subject, trip / tour /
  "this walk", a guide, a meal, a tasting, a booking, what someone "can" do there), no name the post and its source
  lack (`repair_contract.new_names`), and no NEW F17 trip-fact or F18 rarity finding on the spliced text
  (`trip_facts.find_trip_fact_issues`, before vs after). F17 / F12 and every other gate still run on the whole text
  after the extend, as for any attempt.
- `cis.t9.blog_extend_shrink_sections` (int 0..6, default 3): when the repair round itself made the blog shorter and
  left it under the floor (`shrunk`: the text before the repair was longer), the plan asks up to this many sections
  (each another chance that a paragraph fits) instead of the one or two the deficit alone gives; the words asked are the
  same deficit shared between them. 0 = the deficit alone (the plan above).
"""
from __future__ import annotations

import contextvars
import math
import re
from typing import Callable, Optional

import structlog

from services.acp_content_writing import length_contract as lc
from services.acp_content_writing.hygiene import LEAK_RE, _is_cta
from services.acp_content_writing.quality_gates import expand_ledger, unused_source_facts
from services.acp_content_writing.trip_facts import find_trip_fact_issues, parse_moments
from services.acp_shared.citation_tags import TAG_RE
from services.quality import registry as qr
from services.quality import settings_keys, trial_flags
from shared import settings

logger = structlog.get_logger()

EXTEND_ENV = "T9_LENGTH_EXTEND"
EXTEND_ON, EXTEND_OFF = "on", "off"
EXTEND_MODE = "length_extend"
SECTIONS_MAX = 3            # body sections that get a paragraph
FACTS_MAX = 8               # untold seed facts offered
WORDS_MIN, WORDS_MAX = 40, 160   # words asked per paragraph
WORDS_SLACK = 2.0           # a paragraph over this many times the words asked is refused
_HEADING_LINE_RE = re.compile(r"^#{1,6}\s", re.M)
_FAQ_TITLE_RE = re.compile(r"^#{1,6}\s*(?:faq\b|frequently asked)", re.I)
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]\s|\d+[.)]\s)", re.M)


EXTEND_SETTING = settings_keys.T9_BLOG_LENGTH_EXTEND   # "cis.t9.blog_length_extend" (CIS 418)
MATERIAL_SETTING = settings_keys.T9_BLOG_EXTEND_MATERIAL               # CIS 422 (S281)
SHRINK_SECTIONS_SETTING = settings_keys.T9_BLOG_EXTEND_SHRINK_SECTIONS  # CIS 422 (S281)
MATERIAL_FACTS, MATERIAL_BACKGROUND = "source_facts", "with_background"
# The options a piece runs with when the settings cannot be read: the repair before S281 (facts only, no shrink plan).
MAIN_OPTIONS = {"material": MATERIAL_FACTS, "shrink_sections": 0}
# S281 — a background paragraph is about the place, never about what the reader or the group does on the trip.
_READER_RE = re.compile(r"\b(?:you(?:['’](?:ll|re|ve|d))?|your|yours|yourself|yourselves|we(?:['’](?:ll|re|ve|d))?|"
                        r"our|ours|us)\b", re.I)
# S281 review — nor about the travellers, the trip or what it serves, in the third person either ("Visitors can take a
# boat trip...", "Travellers on this walk visit...", "A private guide leads..."): the people who come as the subject,
# the trip itself, a guide, a meal, a tasting or a booking, and what someone "can" do there. A background sentence that
# needs one of these words is the trip's, not the place's; it is refused (one paragraph less, never a trip claim).
_TRIP_RE = re.compile(
    r"\b(?:"
    r"travel(?:l)?ers?|visitors?|guests?|tourists?|walkers?|hikers?|trekkers?|cyclists?|participants?|"
    r"(?:the|a|each|every|your|our|small|private)\s+groups?|"
    r"trips?|tours?|touring|excursions?|itinerar(?:y|ies)|cruises?|packages?|"
    r"(?:this|today['’]s|the\s+day['’]s)\s+(?:walk|hike|trek|day|route|ride|journey|outing|visit|stop)|"
    r"guides?|guided|guiding|"
    r"lunch(?:es)?|dinners?|breakfasts?|brunch|picnics?|meals?|tastings?|sessions?|workshops?|"
    r"homestays?|overnight|transfers?|pick-?ups?|book(?:s|ed|ing|ings)?|reserv(?:e|ed|ation|ations)|tickets?|"
    r"(?:can|could|may|will|get\s+to)\s+(?:also\s+)?(?:visit|taste|enjoy|join|book|stay|ride|dine|sample|try|"
    r"explore|swim|kayak|meet|watch|see|hike|cycle|picnic|shop|buy|learn|take\s+part|take\s+a)"
    r")\b", re.I)


def mode(tenant_id: Optional[str] = None) -> str:
    """'on' / 'off' for a piece of `tenant_id`: the running job's trial flag when it set one (an audit-tenant trial
    job), else the setting cis.t9.blog_length_extend for the tenant (env override T9_LENGTH_EXTEND, then the tenant's
    value, then the platform value, then the default off; shared.settings, never the database). Anything but 'on' is
    off."""
    active = trial_flags.active()
    if EXTEND_ENV in active:
        raw = active[EXTEND_ENV]
    else:
        raw = settings.get(EXTEND_SETTING, str(tenant_id) if tenant_id else None)
    return EXTEND_ON if str(raw or "").strip().lower() == EXTEND_ON else EXTEND_OFF


# The piece's switch, pinned ONCE per piece by service.run_write_background (`pin`) so a setting change mid-piece
# never splits its repair rounds; the plans read it when the caller passes no `switch`. Cleared by `record`.
_PINNED: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("length_extend_pinned", default=None)
# S281 — the piece's options (`options`), pinned with the switch when it is on; read by the plans like the switch.
_PINNED_OPTS: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("length_extend_opts", default=None)


def options(tenant_id: Optional[str] = None) -> dict:
    """S281 (CIS 422) — {"material": source_facts | with_background, "shrink_sections": int >= 0} for a piece of
    `tenant_id` (tenant value, else platform value, else the default; shared.settings, never the database). A value
    that cannot be read gives MAIN_OPTIONS (the repair before S281), never an error in the writing loop."""
    tid = str(tenant_id) if tenant_id else None
    try:
        material = str(settings.get(MATERIAL_SETTING, tid) or "").strip().lower()
        shrink = int(settings.get(SHRINK_SECTIONS_SETTING, tid) or 0)
    except Exception as e:  # noqa: BLE001 — a settings failure keeps the repair as it was, never fails the piece
        logger.warning("t9_length_extend_options_unread", error=f"{type(e).__name__}: {str(e)[:160]}")
        return dict(MAIN_OPTIONS)
    return {"material": MATERIAL_BACKGROUND if material == MATERIAL_BACKGROUND else MATERIAL_FACTS,
            "shrink_sections": max(0, shrink)}


def mode_for(tenant_id: Optional[str], channel: Optional[str]) -> str:
    """`mode(tenant_id)` for a blog; off for any other channel (no settings read)."""
    return mode(tenant_id) if channel == "blog" else EXTEND_OFF


def pin(tenant_id: Optional[str], channel: Optional[str]) -> str:
    """Resolve the piece's switch (`mode_for`), pin it for the plans of this piece and return it."""
    resolved = mode_for(tenant_id, channel)
    _PINNED.set(resolved)
    _PINNED_OPTS.set(options(tenant_id) if resolved == EXTEND_ON else None)
    return resolved


def record(context: dict, channel: Optional[str], switch: str) -> None:
    """A blog's `length_trace` (writer_input, admin-only) records the piece's switch; the pin is cleared."""
    if channel == "blog" and context.get("length_trace"):
        context["length_trace"]["extend"] = switch
    _PINNED.set(None)
    _PINNED_OPTS.set(None)


def _f4_short(gate_ledger: list) -> Optional[dict]:
    """The failed blocking F4_extreme_length entry of `gate_ledger` when it is the short side, else None."""
    for g in expand_ledger(gate_ledger or []):
        if g.get("gate") == qr.F4_EXTREME_LENGTH and not g.get("passed") and g.get("blocking", True):
            return g if lc.entry_side(g) == lc.SHORT else None
    return None


def sections(text: str) -> list[dict]:
    """The body sections of a blog: {"index", "heading", "start", "end" (insertion point: before a closing CTA
    paragraph, else the end of the section's text), "text", "chars"} for every heading before the FAQ. The opening
    before the first heading and the FAQ are never sections here."""
    heads = list(_HEADING_LINE_RE.finditer(text or ""))
    out: list[dict] = []
    for k, m in enumerate(heads):
        line_end = text.find("\n", m.start())
        line_end = len(text) if line_end < 0 else line_end
        heading = text[m.start():line_end].strip()
        if _FAQ_TITLE_RE.match(heading):
            break
        end = heads[k + 1].start() if k + 1 < len(heads) else len(text)
        body = text[line_end:end]
        stripped_end = line_end + len(body.rstrip())
        out.append({"index": len(out), "heading": heading.lstrip("#").strip(), "start": line_end,
                    "end": stripped_end, "text": body.strip(), "chars": lc.measure(body.strip())})
    return out


def _insert_at(text: str, sec: dict, cta: Optional[str]) -> int:
    """Where a new paragraph goes in `sec`: before its last paragraph when that one holds the CTA, else at its end."""
    body = text[sec["start"]:sec["end"]]
    paras = [m for m in re.finditer(r"[^\n]+(?:\n[^\n]+)*", body)]
    if paras and cta and (cta in paras[-1].group(0) or _is_cta(paras[-1].group(0), cta)):
        if len(paras) == 1:
            return -1
        return sec["start"] + paras[-1].start()
    return sec["end"]


def _switch(switch: Optional[str], tenant_id: Optional[str]) -> str:
    """The switch a plan reads: the caller's `switch`, else the piece's pinned one (`pin`), else `mode(tenant_id)`."""
    if switch is None:
        switch = _PINNED.get()
    return mode(tenant_id) if switch is None else switch


def _opts(opts: Optional[dict], tenant_id: Optional[str]) -> dict:
    """The options a plan reads: the caller's `opts`, else the piece's pinned ones (`pin`), else `options(tenant)`."""
    if opts is None:
        opts = _PINNED_OPTS.get()
    return {**MAIN_OPTIONS, **(options(tenant_id) if opts is None else opts)}


def background_names(source: str) -> list[str]:
    """S281 — the places the piece's source names as its moments (trip_facts.parse_moments on the seed half of the
    grounding text), in order, once each: what a background paragraph may be about (with what the post itself names)."""
    return list(dict.fromkeys(m.name for m in parse_moments(source or "") if m.name))


def _material(opts: dict, source: str, facts: list[str]) -> tuple[str, list[str]]:
    """(material, named) of a plan: with_background needs at least one name; else the plan stays on source facts."""
    if opts["material"] != MATERIAL_BACKGROUND:
        return MATERIAL_FACTS, []
    named = background_names(source)
    return (MATERIAL_BACKGROUND, named) if named else (MATERIAL_FACTS, [])


def plan_for(content_text: str, gate_ledger: list, *, channel: Optional[str], source: str,
             tenant_id: Optional[str] = None, switch: Optional[str] = None,
             opts: Optional[dict] = None) -> Optional[dict]:
    """The extend plan of one attempt, or None (the old path): {"ledger": the ledger with the short F4 entry passed,
    "only_length": no other blocking failure, "facts": the untold seed facts, "sections": the chosen sections with
    the words to add, "deficit_chars", "target_chars"}. Runs when `_switch` is on."""
    if channel != "blog" or _switch(switch, tenant_id) != EXTEND_ON:
        return None
    f4 = _f4_short(gate_ledger)
    if f4 is None:
        return None
    target = lc.extend_target_chars()
    deficit = target - lc.measure(content_text)
    body = sections(content_text)
    if deficit <= 0 or not body:
        return None
    facts = unused_source_facts(source or "", content_text, limit=FACTS_MAX)
    material, named = _material(_opts(opts, tenant_id), source, facts)
    if not facts and material != MATERIAL_BACKGROUND:
        return None
    ledger = [({**g, "passed": True, "violations": [], "set_aside": EXTEND_MODE}
               if g.get("gate") == qr.F4_EXTREME_LENGTH else g) for g in gate_ledger or []]
    others = [g for g in expand_ledger(ledger) if not g.get("passed") and g.get("blocking", True)]
    return {"ledger": ledger, "only_length": not others, "facts": facts,
            "sections": _choose(body, deficit, facts, by_facts=material == MATERIAL_FACTS),
            "deficit_chars": deficit, "target_chars": target,
            **({"material": material, "named": named} if material == MATERIAL_BACKGROUND else {})}


def _choose(body: list[dict], deficit: int, facts: list[str], holes: Optional[dict] = None, *,
            min_sections: int = 0, by_facts: bool = True) -> list[dict]:
    """The sections that get a paragraph, each with the words to add. Round 2: the sections a repair made shorter
    (`holes`: section index -> chars lost) come first, largest loss first, so a cut sentence's place is filled; then
    the thinnest. One section per WORDS_MAX words needed (at least two past half of it), within what exists.
    S281: `min_sections` (the shrink setting, after a repair that shrank the blog) asks at least that many sections
    (past SECTIONS_MAX too), the same words shared between them; `by_facts` False (background material) no longer caps
    the sections at the number of untold facts. Both at their defaults = the plan before S281."""
    holes = holes or {}
    words_needed = math.ceil(deficit / lc.CHARS_PER_WORD)
    wanted = max(math.ceil(words_needed / WORDS_MAX), 2 if words_needed > WORDS_MAX // 2 else 1, min_sections)
    k = min(max(SECTIONS_MAX, min_sections), len(body), len(facts) if by_facts else len(body), wanted)
    per = min(WORDS_MAX, max(WORDS_MIN, math.ceil(words_needed / k)))
    order = sorted(body, key=lambda s: (-holes.get(s["index"], 0), s["chars"], s["index"]))
    return [{**s, "words": per, **({"hole_chars": holes[s["index"]]} if holes.get(s["index"]) else {})}
            for s in sorted(order[:k], key=lambda s: s["index"])]


def _holes(before: Optional[str], after: str) -> dict:
    """{section index in `after`: chars it lost against the section with the same heading in `before`} (> 0 only)."""
    if not before:
        return {}
    was = {s["heading"]: s["chars"] for s in sections(before)}
    return {s["index"]: was[s["heading"]] - s["chars"] for s in sections(after)
            if s["heading"] in was and was[s["heading"]] > s["chars"]}


def plan_after_repair(content_text: str, *, before: Optional[str], channel: Optional[str],
                      source: str, tenant_id: Optional[str] = None,
                      switch: Optional[str] = None, opts: Optional[dict] = None,
                      shrunk: Optional[bool] = None) -> tuple[Optional[dict], str]:
    """AA-1077 round 2 — the expand step of a repair round: (plan, reason). A blog repair round (the sentence repair
    of cut-type fixes, or the full rewrite) that leaves the text UNDER the F4 floor is never accepted as it is: the
    expand runs on the text the repair left, sized from ITS deficit to the target (the cuts' lost characters included:
    S278 73e6a233 went 3,957 → 3,546 chars after 6 F17 cuts and was held on length), with the sections the repair
    made shorter first. reason: 'expand' with a plan, else why not ('off', 'not_blog', 'at_or_over_floor',
    'no_sections', 'no_untold_facts': no untold fact and, with background material, no name either).
    S281: `shrunk` (default: `before` was longer than the text now) asks the shrink setting's sections; the material
    setting adds background (`_material`). `opts` = the piece's options (default: pinned, else read)."""
    if channel != "blog":
        return None, "not_blog"
    if _switch(switch, tenant_id) != EXTEND_ON:
        return None, "off"
    now = lc.measure(content_text)
    if now >= lc.floor_chars():
        return None, "at_or_over_floor"
    body = sections(content_text)
    if not body:
        return None, "no_sections"
    facts = unused_source_facts(source or "", content_text, limit=FACTS_MAX)
    o = _opts(opts, tenant_id)
    material, named = _material(o, source, facts)
    if not facts and material != MATERIAL_BACKGROUND:
        return None, "no_untold_facts"
    if shrunk is None:
        shrunk = before is not None and lc.measure(before) > now
    target = lc.extend_target_chars()
    deficit = target - now
    holes = _holes(before, content_text)
    secs = _choose(body, deficit, facts, holes, min_sections=o["shrink_sections"] if shrunk else 0,
                   by_facts=material == MATERIAL_FACTS)
    plan = {"facts": facts, "sections": secs, "deficit_chars": deficit, "target_chars": target,
            "hole_chars": sum(holes.values())}
    if shrunk and o["shrink_sections"]:
        plan["shrunk"] = True
    if material == MATERIAL_BACKGROUND:
        plan.update(material=material, named=named)
    return plan, "expand"


def _refusal(add: str, asked: int, *, cta: Optional[str], source: str, trip_ctx,
             known: Optional[str] = None) -> Optional[str]:
    """Why a paragraph is refused, else None. `known` (S281, background material only: the post's text) adds the
    background checks: no reader / group address and no name the post and its source lack."""
    from services.quality.repair_contract import new_specifics  # lazy: repair_contract imports hygiene
    if not add.strip():
        return "empty (no fitting fact)"
    if _HEADING_LINE_RE.search(add) or _LIST_LINE_RE.search(add) or "?" in add or TAG_RE.search(add):
        return "not plain prose (heading, list, question or tag)"
    if cta and (cta.strip() and cta.strip() in add or _is_cta(add, cta)):
        return "repeats the call to action"
    n = lc.words(add)
    if n > asked * WORDS_SLACK:
        return f"{n} words for {asked} asked"
    leak = LEAK_RE.search(add)
    if leak:   # round 2 — never a paragraph that says where its facts come from (F13_leak would hold the piece)
        return f"names where facts come from ('{leak.group(0)}')"
    added = new_specifics("", add, trip_ctx=trip_ctx, source=source)
    if added:
        return "new specifics: " + "; ".join(added[:3])
    if known is not None:
        return _background_refusal(add, source=source, known=known)
    return None


def _background_refusal(add: str, *, source: str, known: str) -> Optional[str]:
    """S281 — the background-material checks of a paragraph: third person, about the place, no new name."""
    from services.quality.repair_contract import new_names  # lazy: repair_contract imports hygiene
    reader = _READER_RE.search(add)
    if reader:   # S281 — background is about the place, never what the reader or the group does
        return f"addresses the reader or the group ('{reader.group(0)}'): background is third person"
    trip = _TRIP_RE.search(add)
    if trip:   # S281 review — the travellers, the trip or a service as the subject is a trip claim, not background
        return f"speaks of the trip, its travellers or a service ('{trip.group(0)}'): background is about the place"
    names = new_names(known, add, source)
    if names:
        return "names what neither the post nor its source names: " + ", ".join(names[:3])
    return None


def _new_trip_findings(cur: str, trial: str, trip_ctx) -> list[str]:
    """S281 — the F17 trip-fact / F18 rarity findings the spliced text has and the text before it had not."""
    if trip_ctx is None:
        return []
    was, now = find_trip_fact_issues(cur, trip_ctx), find_trip_fact_issues(trial, trip_ctx)
    return [f for k in ("trip_fact", "rarity") for f in now[k] if f not in was[k]]


def extend(content_text: str, plan: dict, *, call: Callable[..., tuple[list[str], float, Optional[str]]],
           source: str, cta: Optional[str] = None, ignore: frozenset = frozenset(), moments: Optional[list] = None,
           trip_ctx=None, voice: Optional[str] = None, **call_kw) -> tuple[str, float, dict]:
    """(text, cost, log entry): `content_text` with the accepted paragraphs spliced in. `call` is
    generate.extend_sections (injected for tests). A call error keeps the text as it was (the gates then hold the
    piece on its length, as before), never a second call."""
    from services.acp_content_writing.sentence_repair import _integrity  # lazy: sentence_repair imports hygiene
    before = lc.measure(content_text)
    entry: dict = {"sections": len(plan["sections"]), "facts_offered": len(plan["facts"]),
                   "words_asked": sum(s["words"] for s in plan["sections"]), "chars_before": before,
                   "target_chars": plan["target_chars"], "added": 0, "refused": [], "model": None}
    background = plan.get("material") == MATERIAL_BACKGROUND
    if background:   # S281 — recorded only off the default material, so a source_facts entry keeps its shape
        entry["material"] = MATERIAL_BACKGROUND
    if plan.get("shrunk"):
        entry["shrunk"] = True
    asked = [{"heading": s["heading"], "text": s["text"], "words": s["words"],
              **({"hole_chars": s["hole_chars"]} if s.get("hole_chars") else {})} for s in plan["sections"]]
    if background:
        call_kw = {**call_kw, "background": plan.get("named") or []}
    try:
        adds, cost, model = call(sections=asked, facts=plan["facts"], voice=voice, **call_kw)
    except Exception as e:  # noqa: BLE001 — an extend failure never fails the job; the old outcome stands
        entry.update(error=f"{type(e).__name__}: {str(e)[:160]}", chars_after=before)
        return content_text, 0.0, entry
    entry["model"] = model
    cur = content_text
    # splice from the last section to the first, so earlier offsets stay valid; each paragraph checked on its own
    for sec, raw in sorted(zip(plan["sections"], adds), key=lambda p: -p[0]["index"]):
        why = _refusal(str(raw or ""), sec["words"], cta=cta, source=source, trip_ctx=trip_ctx,
                       known=cur if background else None)
        add = " ".join(str(raw or "").split())
        live = next((s for s in sections(cur) if s["index"] == sec["index"]), None)
        at = _insert_at(cur, live, cta) if live is not None and why is None else -1
        if why is None and at < 0:
            why = "no safe place in the section"
        if why is None:
            rest = cur[at:].lstrip("\n")
            trial = cur[:at].rstrip("\n") + "\n\n" + add + ("\n\n" + rest if rest else cur[at:])
            problems = _integrity(cur, trial, ignore, cta, moments)
            trip = _new_trip_findings(cur, trial, trip_ctx) if background and not problems else []
            if problems:
                why = "seam: " + "; ".join(problems[:2])
            elif trip:
                why = "trip fact: " + "; ".join(trip[:2])
            else:
                cur = trial
                entry["added"] += 1
                continue
        entry["refused"].append({"section": sec["index"], "why": why[:200]})
    entry["chars_after"] = lc.measure(cur)
    entry["reached_floor"] = entry["chars_after"] >= lc.floor_chars()
    return cur, float(cost or 0.0), entry


__all__ = ["EXTEND_ENV", "EXTEND_SETTING", "EXTEND_ON", "EXTEND_OFF", "EXTEND_MODE", "SECTIONS_MAX", "FACTS_MAX",
           "MATERIAL_SETTING", "SHRINK_SECTIONS_SETTING", "MATERIAL_FACTS", "MATERIAL_BACKGROUND", "MAIN_OPTIONS",
           "mode", "mode_for", "options", "pin", "record", "sections", "background_names", "plan_for",
           "plan_after_repair", "extend"]
