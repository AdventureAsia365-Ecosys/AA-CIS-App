"""services.acp_shared.atom_extraction — shared atom-extraction prompt/parse pieces.

Extracted from api/routers/v1_atoms.py (AA-475 — that router, the old platform-scope N2 atomize
endpoint, was deleted). These 4 pieces were never specific to that endpoint: they are the pure
prompt-build/parse/hash logic AA-299 proved, and `services/acp_produce/tenant_pipeline.py::
run_t5_atomize` (T5, the current owner_scope=tenant_id atomize pipeline) already depended on them
directly — moved here so T5 doesn't reach into a deleted router module.
"""
import hashlib
import re

SYSTEM_PROMPT = """You extract content atoms from a tour's source material for a travel \
marketing platform. An atom is one concrete, place-and-activity pair from the trip — not a \
summary, not a paraphrase, not an invented detail.

Extract concrete, verbatim-derived pairs only. If input is thin or empty, return an empty \
list — never invent content not present in the source text. `place` and `action` must each be \
a direct quote or minimal trim of the source material — do not add facts, names, numbers, or \
descriptive details that are not explicitly present in the input text, even if you know them \
to be true from general knowledge.

Example of what NOT to do: if the input says "visit a hillside temple", the atom must say \
only that — NOT "visit a 12th-century hillside temple famous for its hand-carved wooden \
gates", even though such details might be true of similar temples in general. Any fact not \
in the input text does not go in `place`/`action`, no matter how plausible or well-known.

AA-509 — `place`/`action` rules (adapted from Ms. Thư's aa-social-media extractor,
src/aa_social/stages/atoms.py):
- One entry per place-and-activity pair the text states. Do not invent, combine, or summarise
  across pairs.
- `place` is the place as the source names it. A walk between two towns is one place, written
  "A to B".
- Name the place. Never refer back to one: "the trail", "the village", "this small town" are
  not places — write what the text calls it.
- The named landmark the activity is about is the place, not the town it sits in: "visit
  Itsukushima Shrine on Miyajima Island" has `place` = "Itsukushima Shrine".
- `action` is what happens there, a short verb phrase in the infinitive ("walk", "travel by
  train", "explore", "eat dinner").
- Include getting from one place to another and logistics (transfers, check-ins, flights) —
  they are part of the record; `activity_type` below is what later separates transit/logistics
  from what gets ranked, not this extraction step.

AA-610 — `evidence` (adapted from Ms. Thư's `Atom.evidence`, aa-social-media models.py/
stages/atoms.py):
- `evidence` is the exact span of THIS day's source text the place/action pair came from —
  quoted character-for-character, never paraphrased, never summarised, never shortened by you.
- Copy the sentence(s) verbatim from the input. Do not merely restate place+action in your own
  words — that is not evidence, it is a second copy of the same two fields.
- If one sentence covers several atoms, each of those atoms may quote the same (or an
  overlapping) span — evidence is not required to be unique per atom.

Respond with ONLY a JSON object matching this exact contract:
{
  "atoms": [
    {
      "place": "the place, verbatim-derived",
      "action": "short verb phrase, verbatim-derived",
      "evidence": "the exact source sentence(s) this pair came from, quoted verbatim",
      "activity_type": "trek|bike|food|culture|stay|transit|other",
      "emotional_hook": "string or null",
      "visual_potential": 1,
      "persona_fit": ["string", "..."],
      "season_note": "string or null",
      "itinerary_day": 1
    }
  ]
}

visual_potential is an integer 1-3 (3 = strong photo/video potential). No prose outside the \
JSON object.

itinerary_day is the day number in the source itinerary this atom belongs to (e.g. "DAY 01" -> 1, \
"Day 12" -> 12). Return null if the source text has no clear day label for this moment, or the \
atom isn't tied to one specific day (e.g. general trip-wide information). Never guess a day number \
without clear support in the source text.

If the itinerary is thin, return FEW atoms. Never pad. Returning 3 honest atoms beats 10 \
invented ones."""


def _preamble_parts(row: dict) -> list[str]:
    """TOUR/SUMMARY/HIGHLIGHTS lines shared by build_user_prompt() (whole-tour) and
    build_day_user_prompt() (AA-508, per-day) — factored out so the two never drift
    apart on what tour-level context the model sees."""
    parts = [f"TOUR: {row['name']}"]
    if row.get("aa_summary"):
        parts.append(f"SUMMARY: {row['aa_summary']}")
    if row.get("aa_highlights"):
        highlights = row["aa_highlights"]
        if isinstance(highlights, str):
            import json
            highlights = json.loads(highlights)
        if highlights:
            parts.append("HIGHLIGHTS:\n- " + "\n- ".join(str(h) for h in highlights))
    return parts


def build_user_prompt(row: dict) -> str:
    parts = _preamble_parts(row)
    if row.get("itinerary_source"):
        parts.append(f"ITINERARY:\n{row['itinerary_source']}")
    if row.get("inclusions"):
        parts.append(f"INCLUSIONS:\n{row['inclusions']}")
    if row.get("exclusions"):
        parts.append(f"EXCLUSIONS:\n{row['exclusions']}")
    return "\n\n".join(parts)


def build_day_user_prompt(row: dict, day_number: int, day_title: str, day_body: str) -> str:
    """AA-508 — per-day variant of build_user_prompt(). Same TOUR/SUMMARY/HIGHLIGHTS preamble for
    context, but ITINERARY is scoped to this one day instead of the whole trip, so run_t5_atomize()
    can call the model (and fingerprint/cache the result) per day instead of once for the whole
    tour. SYSTEM_PROMPT (what counts as an atom, how decompose works) is untouched by this — only
    what the model is shown changes, never what it's asked to do with it."""
    parts = _preamble_parts(row)
    parts.append(f"ITINERARY:\nDay {day_number} — {day_title}\n{day_body}")
    if row.get("inclusions"):
        parts.append(f"INCLUSIONS:\n{row['inclusions']}")
    if row.get("exclusions"):
        parts.append(f"EXCLUSIONS:\n{row['exclusions']}")
    return "\n\n".join(parts)


def source_hash(row: dict) -> str:
    """sha256 of build_user_prompt()'s own output (migration 084) — hashing the exact string
    sent to the model, rather than re-deriving the field concatenation, guarantees the hash can
    never drift out of sync with what was actually decomposed."""
    return hashlib.sha256(build_user_prompt(row).encode("utf-8")).hexdigest()


def normalise(text: str) -> str:
    """AA-508 — same normalisation aa-social-media's own `_normalise()` uses (models.py) before
    hashing: lowercase, every run of non-alphanumeric characters collapsed to one space, stripped.
    Exported (not `_normalise`) — content_hash_atom_id() and any test/consumer that needs to
    reproduce a hash input by hand both need it."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def content_hash_atom_id(
    owner_scope: str, tour_id: str, day_number: int, place: str, action: str,
) -> str:
    """An Atom's identity, derived from what it is rather than when it arrived — AA-508/AA-509,
    mirrors aa-social-media's own atom_id() (src/aa_social/models.py):

        atom_id = sha256(f"{owner_scope}|{trip_code}|{day}|{normalise(place)}|{normalise(action)}")

    AA-509 build prompt reverted to this literal formula (place/action, not the combined `text`
    AA-508 used as a stand-in before T5 decompose could produce place/action separately —
    docs/claude_audit/AA-509-step0-schema-matching-investigation.md mục 4, Hướng A now chosen).
    Argument order matches the literal formula (owner_scope, tour_id/trip_code, day, place,
    action) — NOT AA-508's original (tour_id, owner_scope, day, text) order; every call site
    updated accordingly.

    `owner_scope` (the rewriting tenant's id, or 'platform') stays in the hash — AA-508's own
    addition, kept per the build prompt's explicit instruction ("giữ nguyên"): the reference repo
    has no multi-tenant concept, but `tour_atoms.atom_id` here is a single GLOBAL primary key
    shared by every tenant that has ever rewritten a given `tour_id` — without owner_scope, two
    tenants' differently-worded rewrites of the same tour_id/day that happened to normalise to
    the same place/action would collide on that one PK and silently overwrite each other's row.

    `owner_scope`/`tour_id` are used verbatim (not normalised), same as the reference formula
    treats `trip_code`; `day_number`/`place`/`action` are normalised the same way normalise() does.
    """
    parts = (owner_scope, str(tour_id), str(day_number), normalise(place), normalise(action))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def derive_atom_text(place: str, action: str) -> str:
    """AA-509 — `tour_atoms.text` stays a real, populated column (NOT dropped): it is read
    directly by score_distinctiveness(), T9's content_seed, N7 research H2 titles,
    slot_runner.py, and acp_angle_gate/service.py (grep-confirmed before this change, see
    implementation notes Decision 1) — rewriting every one of those to read place+action
    separately was real, unrequested blast radius, not this task's ask (Hướng A only asks T5's
    decompose OUTPUT to split place/action). This derives the same combined view T5 used to get
    straight from the LLM, now computed from the two fields it actually returns."""
    place = (place or "").strip()
    action = (action or "").strip()
    if not place and not action:
        return ""
    if not action:
        return place
    if not place:
        return action
    return f"{place} — {action}"


def checkable_evidence(evidence: str, day_body: str) -> str | None:
    """AA-610 — port of Ms. Thư's `_checkable_evidence()` (aa-social-media stages/atoms.py):
    `evidence` is only trustworthy as a `said` signal if it is genuinely a verbatim quote from
    the day's own source text, not the model's paraphrase of place+action (which would just be
    a second, redundant copy of those two fields with no new signal). Checked by normalised
    substring containment (whitespace-insensitive — a multi-line quote's exact newlines/spacing
    are not the point, its wording is) rather than exact `in`, since models routinely
    reflow whitespace when quoting.

    Returns the ORIGINAL (non-normalised) `evidence` string when it checks out — never the
    normalised form, so `said`'s LENGTH() keeps measuring what was actually quoted, not a
    lowercased/collapsed stand-in. Returns the whole `day_body` (the reference repo's own
    fallback) when `evidence` fails the check — never None/empty, so a Segment whose Atom
    quoted badly still gets *some* said signal for that day rather than 0, and never silently
    swallows a day's text because one atom mis-quoted it.
    """
    if not evidence or not evidence.strip():
        return day_body
    haystack = re.sub(r"\s+", " ", (day_body or "")).strip().lower()
    needle = re.sub(r"\s+", " ", evidence).strip().lower()
    if needle and needle in haystack:
        return evidence
    return day_body


def day_fingerprint(day_title: str, day_body: str, model: str) -> str:
    """What one day's atomize reading depends on (AA-508) — mirrors aa-social-media's own
    _fingerprint() (src/aa_social/stages/atoms.py): the day's own text, the decompose instruction
    (SYSTEM_PROMPT), and the model. Change any of the three and the fingerprint changes — a
    matching fingerprint means run_t5_atomize() would get the exact same reading calling the model
    again, so it doesn't."""
    joined = "\n\0".join((day_title or "", day_body or "", SYSTEM_PROMPT, model))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def strip_json_fence(text: str) -> str:
    """invoke_claude() responses sometimes wrap the JSON in a ```json ... ``` fence despite
    SYSTEM_PROMPT saying "No prose outside the JSON object" (observed live, AA-305 inline-path
    test) — strip it before json.loads()."""
    match = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text.strip(), re.DOTALL)
    return match.group(1) if match else text
