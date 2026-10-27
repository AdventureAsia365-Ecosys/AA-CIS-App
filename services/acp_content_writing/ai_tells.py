"""AA-790 (S251) — the T9 writer-side AI-tell pass: what the writer is told, and what code fixes after it wrote.

The S251 paid T9 pilot (n = 20) scored AI-tells 4.08 (bar 5.5) with a 4-lens panel. The tells were the same on
every tenant: a claim set up by denying another ("is not only preserved. It remains in active use.", "not because
it was dramatic, but because it was real."), short verdict lines that comment on the piece ("That's the
opening.", "This is the section worth slowing down for."), template openers ("There is a particular quality to
…"), stock adverbs ("genuinely", "Crucially,"), long flat sentences, internal labels ("referred to throughout
as"), the angle's own label as an H2 and CTA lead-ins that repeat the CTA.

The 4-week gate freeze (S250 go/no-go 6b) allows no new gate, so this is a WRITER-side fix:

* ``PROMPT_RULES`` — the rules rendered into the T9 ``SYSTEM_PROMPT`` (prompts.py); the patterns below are the
  same ones, so the brief and the pass never drift.
* ``rewrite(text, ctx)`` — a deterministic pass the T9 loop runs on the writer's text before the gates
  (service.run_write_background, under the AA-790 step guard). It only deletes words, clauses or whole sentences,
  or joins a denial with the sentence that says what the thing is, so it can never add a claim the seed lacks.
  Every pass is checked with ``hygiene.integrity_problems`` (a new repeat, orphan, pronoun with no referent,
  fragment or broken paragraph): a pass that breaks the text falls back to one edit at a time, and an edit that
  breaks it is skipped.
* What the pass cannot fix in code (``earns its reputation``, ``has a way of``, ``There is a particular quality
  to …``) is the residue list ``hygiene.AI_TELL_LINE_RES``: the existing F15_voice "stock line" routing (one cheap
  sentence repair on attempt 1, never a hold).

Pure: no LLM, no DB. ``lead_focus`` (LinkedIn slate diversity) is here too because it reads the same seed lines.

Round 2 (S251 re-panel of the round-1 rewrite: AI-tells 4.15 → 5.12, bar 5.5; LinkedIn source fidelity 8.92 →
7.08): ``VISIT_DETAIL_RULE`` (every channel) and a new ``SOCIAL_AUDIENCE_RULE`` stop the writer inventing what a
visit shows; ``brand_voice_rule`` adds a PACE line for energetic brands and a RESTRAINT line for calm ones; the pass
also takes 'do' denials with a personifying verb ("does not shout about itself. It is …"), aphorism pairs ("is not a
ruin. It works."), "is not incidental" verdicts, verdict lead-ins before a colon ("The circle is complete: …") and
runs of three short sentences built the same way; the rest (internal labels, "is where the trip gets going", "worth
holding", invented visit logistics …) is residue in ``hygiene.AI_TELL_LINE_RES``.

Round 3 (S252 blind 3-arm panel of round 2: AI-tells 5.33, bar 5.5): the denial frames round 2 refused or missed are
joined or cut ("This is not a landscaped garden. It is a place …" → "This is a place …"; "The story does not begin
in A. It begins in B, …"; "…: not a relic, but a working institution"; staging tails "rather than staged", "not
just a view of them"; "offers something rarer than scenery: …"; "That single fact changes how you think …"), a
heading built from the angle brief's words loses that part, and more verdicts go to the residue list.

Round 4 (S252 blind 4-arm panel of round 3: AI-tells 5.22, bar 5.5; Terra Family brand voice 6.85) is prompt-side
only: ``PROMPT_RULES`` names the frames the writer itself still produced ('not as A but as B', 'A rather than B',
'earns its place', lists of three, template closers, label lines before the CTA) with ten DON'T / WRITE INSTEAD
pairs whose positive half is a <slot> filled from the seed; ``SELF_CHECK_RULE`` is a silent reread before output;
``family_presence_rule`` puts the family in every section for a family brand, from seed facts only. The pass is
unchanged (no new deletion), so ``AI_TELLS_VERSION`` stays v3.

Round 5 (S252 blind 5-arm panel of round 4: AI-tells 5.50, but Terra Family brand voice 7.0 → 5.5, "children never
appear concretely (only 'your family')") is prompt-side only and keeps every round-4 AI-tell change: ``FAMILY_RULE``
lets the children and the parents do what the seed says the group does, takes warmth from the seed's pace / effort /
rest cues, drops the one-sentence-per-section cap and gives three DON'T / WRITE INSTEAD pairs;
``FAMILY_SHORT_POST_RULE`` (plain-text posts) asks for one concrete family moment; ``VISIT_DETAIL_RULE`` says naming
who in the group does a seed moment is not a new fact (a FAMILY BRAND brief only). ``AI_TELLS_VERSION`` stays v3.

Round 7 (S254, the S252 7-arm panel: brand voice round 3 7.17 → round 6 6.75) is prompt-side, plus one residue
shape: ``ANGLE_BRIEF_RULE`` says the angle lines are a brief, never copy (the T8 brief's "an accessible, structured
stop" became the Terra LinkedIn closer in 20 of 42 posts); ``brand_focus_rule`` restates the style guide's own first
rules as a checklist met only from the seed (the judges' "nothing on pace, rest or comfort", "little about guiding
or privacy"), with ``TRAVELLER_RETURN_LINE`` for a brand that has no texture block; the ATMOSPHERE texture asks for
sentences of ordinary length (round 6 lengthened them); the "is a structured, accessible stop" verdict is residue in
``hygiene.AI_TELL_LINE_RES``. The pass is unchanged, so ``AI_TELLS_VERSION`` stays v3.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from services.acp_content_writing import hygiene
from services.acp_content_writing.hygiene import content_words, cta_norm, fold, sentence_spans, tidy_after_cut
from services.acp_shared.citation_tags import TAG

AI_TELLS_VERSION = "ai_tells.v3"  # v2: AA-790 round 2 (S251 re-panel); v3: round 3 (S252 re-panel)

# ---------------------------------------------------------------- the brief (rendered into SYSTEM_PROMPT)

PROMPT_RULES = """- AI TELLS (an editor must not be able to tell a model wrote this piece):
  * Never set a claim up by denying another. No "X is not A. It is B.", "X does not A. It B.", "not A but B",
    "not as A but as B", "A rather than B", "not because A, but because B", "X is more than A. It is B.",
    "These are not A.", "X is not incidental." Say what the thing is, once, directly. A denial stays only when
    the denial itself is a practical fact from the content seed ("Lunch is not included.").
  * No verdict lines: a short sentence that comments on the piece or the moment instead of telling the reader
    something ("That's the opening.", "This is the section worth slowing down for.", "The geology is the thing
    itself.", "X is where the trip gets going.", "This one fact is the key to understanding ...", "These
    numbers are worth holding.", "The circle is complete:", "A meal here is rarely just a meal.", "That is the
    shape of ...", "X earns its place.", "That fact matters.", "... is the point."). Every
    sentence carries a fact from the content seed, a place, or what the content seed says the reader does
    there.
  * No template openers or closers: "There is a particular quality to ...", "X has a way of ...", "If there is
    one place ..., it is ...", "X asks something of you before it gives anything back", "... is one that travels
    home with you", "the kind of ... that stays with them".
  * No lists of three for rhythm ("slow, unscripted, and rooted in ...", "calm, varied, and purposeful", "Two
    rivers, one road, a window onto ..."). List three things only when the content seed names three things;
    otherwise state the one fact that matters.
  * Never stack short sentences that open the same way ("The forts are still ... The shrines are still ...
    The mills are still ..."); say it once.
  * Never write genuinely, genuine, truly, precisely, crucially, importantly, notably, "earns its ...", "rewards
    those who ...", "worth slowing down for".
  * Short, varied sentences: most under 25 words, none over 40. Split a sentence that strings more than three
    clauses together, and never join two sentences with a semicolon.
  * Name a place once in full, then use the name. Never write an internal label such as "referred to throughout
    as" or "hereafter".
  * Never name your inputs: the words seed, content seed, angle, brief and atom never appear as words about this
    piece or where its facts come from ("What the seed says ...", "the angle here"). Never comment on your
    own text: no remark about a word, a sentence or the piece ("The hedge in that last sentence matters",
    "The word 'might' is doing honest work", "This is the argument that ...").
  * Say the call to action once. No heading and no lead-in sentence that repeats its words, and add nothing to
    it (no place name, no "today"). No label line before it ("This is one moment of the ...", "If a ... sounds
    like the right kind of journey, ...").
  * DON'T / WRITE INSTEAD (shapes an editor flagged in earlier pieces; <...> is a fact from the content seed,
    never an invented one):
    - DON'T "The fortress is not a relic. It is still a working seat of power."
      WRITE "The fortress <does what the content seed says it does>." (the second half alone, as a plain fact)
    - DON'T "Visitors see weaving as a lifelong discipline rather than a souvenir."
      WRITE "Weavers train for <the length of time the content seed gives>."
    - DON'T "This is the leg that earns its place." / "After a long ride, that fact matters."
      WRITE nothing: delete the line and put its fact in the sentence before ("After the ride, lunch is at
      <place>.").
    - DON'T "The steppe asks something of you before it gives anything back."
      WRITE an opening on the first fact: "<Day or place> starts with <what the content seed says happens>."
    - DON'T "slow, unscripted, and rooted in the valley" / "Two rivers, one road, a window onto village life".
      WRITE one fact: "The night is spent <where the content seed says>."
    - DON'T "The word 'usually' is worth holding on to." WRITE the hedge inside the fact: "The abbot usually <does
      what the content seed says>."
    - DON'T "the valley is still making up its mind" / "volcanic candour". WRITE what it is: "<Place> is <what the
      content seed says it is>."
    - DON'T "It is the kind of memory that stays with them long after the trip." WRITE nothing: delete the line,
      so the piece closes on its last fact with the call to action worked into it.
    - DON'T "that link is concrete and real" / "a grounded, accessible stop". WRITE the link itself: "<Who> <does
      what> at <place>."
    - DON'T "This is one moment of the <trip>. If a journey like this sounds right, message us." WRITE the call
      to action as the piece's own sentence, worked into the last fact, with no label line before it."""

# AA-790 round 4 (S252 4-arm panel: AI-tells 5.22, bar 5.5; the writer itself still produced the shapes above) — the
# last rule before the output line, so it is the last thing the writer reads.
SELF_CHECK_RULE = """- BEFORE YOU RETURN THE PIECE, reread it once, sentence by sentence. Make sure no sentence has
  the form "X is not A. It is B.", "not A but B", "not as A but as B", "A rather than B", "X is more than A. It
  is B." or "more than just A" (unless the denial is a practical fact from the content seed; a quantity from the
  seed, such as "more than 300 monks", stays word for word); no line judges instead of telling ("earns its place",
  "that matters", "is the point", "worth holding", "the shape of", "the kind of ... that"); no list of three that
  the content seed does not give; no opening or closing line about what a place asks, gives or leaves with the
  reader; no label line before the call to action. Rewrite any such sentence as the plain fact, or delete it.
  This check is silent: never mention it in the piece."""

# AA-790 round 2 (S251 re-panel: LinkedIn source fidelity 8.92 → 7.08) — every channel, blog included: the round-1
# AUDIENCE rule ("say what they see or do") pushed the writer to invent what a visit shows. Rendered into
# SYSTEM_PROMPT right after PROMPT_RULES.
VISIT_DETAIL_RULE = """- VISIT DETAIL: never say what a visit shows, includes or lets the reader watch, see or do,
  what is visible there, how the reader will react to it, or how a stop fits into a day (its pace, ease or
  logistics: "a chance to watch ...", "puts you right in the middle of ...", "slots easily into a day", "an easy
  part of the afternoon"), unless the content seed states it. Say who the stop is for and why its facts matter
  to them. When the brief has a FAMILY BRAND line, naming the children or the parents as the ones who see or do
  what the content seed says the group sees or does is not a new fact; the moment itself still comes only from
  the content seed."""

# Blog only (both format variants): the chosen angle is a brief, never a heading.
BLOG_HEADING_RULE = """- Every "## " heading names the place or what happens there. Never use the SELECTED ANGLE's name
  (or most of its words) as a heading or a title: it is an internal brief label, not copy."""

# Non-blog channels: the audience in the post (S251 panel: Terra LinkedIn 5.25 voice). Round 2: the round-1 text
# ("say what they see or do at this place") made the writer invent the visit ("a chance to watch the students at
# work", "slots easily into a day"); the audience stays, the visit content comes from the seed only.
SOCIAL_AUDIENCE_RULE = """
AUDIENCE IN THE POST: write to the AUDIENCE of the BRAND block. Name who they are (a family brand writes to the
family) and say why the CONTENT SEED's facts matter to them. Never describe what the visit shows, includes or lets
them watch or do, what they will notice or feel there, or how it fits into their day (pace, ease, logistics),
unless the CONTENT SEED says so. A post that only defines the place, like an encyclopedia entry, is not finished;
a post that invents the visit is wrong."""

# AA-790 round 2 (S251 re-panel: Trail Pulse voice 6.5 → 6.0, WildKind 7.5 → 6.75) — one line per brand energy,
# from the brand pack's own voice words, every channel.
_ENERGETIC_VOICE = frozenset("""energetic active dynamic lively upbeat playful bold adventurous spirited vibrant punchy
sporty high-energy""".split())
_CALM_VOICE = frozenset("""calm respectful thoughtful grounded discreet elegant measured quiet serene reflective
understated considered gentle contemplative""".split())
_ENERGETIC_RULE = """
PACE FOR THIS BRAND (its voice: {words}): short paragraphs of two or three sentences, written to the reader as
"you" (only what the CONTENT SEED states). Give a background fact (history, geology, dates, figures) one sentence
at most, then go back to the trip's own moments in the CONTENT SEED; never a paragraph of background facts."""
_CALM_RULE = """
RESTRAINT FOR THIS BRAND (its voice: {words}): state each fact and move on. No meta-commentary about the text
itself: never comment on a word, a sentence, a section or the piece ("The hedge in that last sentence
matters", "This is the argument that ...", "That detail is worth holding"), and never close a section on a
reflection about what it all means."""


_VOICE_WORD_RE = re.compile(r"[a-z-]+")
_VOICE_NEGATIONS = frozenset({"not", "never", "no", "nor"})


def _voice_words(entry: str) -> set[str]:
    """The voice words of one pack entry, without a word right after not / never / no / nor ('Calm, never
    playful' is calm, not playful; 'Not loud, not bold' is neither)."""
    words = _VOICE_WORD_RE.findall(entry.lower())
    return {w for k, w in enumerate(words) if k == 0 or words[k - 1] not in _VOICE_NEGATIONS}


def _pack_voice(brand_audience: Optional[dict]) -> list[str]:
    return [str(v) for v in (brand_audience or {}).get("voice") or [] if v]


def _energetic_entries(voice: list[str]) -> list[str]:
    """The voice entries that make a brand energetic: the PACE line's and the MOMENTUM texture's one test."""
    return [v for v in voice if _voice_words(v) & _ENERGETIC_VOICE]


def brand_voice_rule(brand_audience: Optional[dict]) -> str:
    """The PACE line for an energetic brand, the RESTRAINT line for a calm / respectful one ('' for neither; both
    when the voice has both kinds of word). Read from the brand pack's own ``voice`` words; a negated word ('never
    playful') does not count."""
    voice = _pack_voice(brand_audience)
    energetic = _energetic_entries(voice)
    calm = [v for v in voice if _voice_words(v) & _CALM_VOICE]
    return ((_ENERGETIC_RULE.format(words=", ".join(energetic)) if energetic else "")
            + (_CALM_RULE.format(words=", ".join(calm)) if calm else ""))


# AA-790 round 6 (S252 6-arm panel of round 5: AI-tells 5.58 kept, but non-family brand voice fell against round 3:
# Trail Pulse 6.83 → 6.0 "long middle stretches read like an encyclopedia", "the FAQ is formal brochure copy";
# Atlas & Hearth 8.2 → 7.4 "reference prose with little atmosphere"; WildKind 7.5 → 7.0 "encyclopedia more than
# observant nature writing; brand voice in one bolted-on line"). Rounds 4/5 removed the verdict, denial and aphorism
# lines that carried those voices ("Four days in.", "that matters", "the land is still deciding what it wants to
# be"), and the writer fell back to reference prose. BRAND TEXTURE says where the voice comes from instead, per
# brand type read from the pack: MOMENTUM (energetic: the PACE line's own test), ATMOSPHERE (luxury / cultural),
# OBSERVATION (nature / respectful). A family brand gets none (FAMILY_RULE carries its voice; round 5 fixed it); a
# pack with no texture signal gets none, so its prompt is byte-identical to round 5.
_ATMOSPHERE_VOICE = frozenset("""elegant atmospheric refined sophisticated cultured luxurious polished""".split())
_ATMOSPHERE_TYPE = frozenset("""luxury luxurious upscale high-end cultural culture heritage""".split())
_OBSERVATION_VOICE = frozenset("""nature-led observant respectful ethical responsible sustainable""".split())
_OBSERVATION_TYPE = frozenset("""nature nature-led wildlife conservation responsible eco eco-friendly ecological
sustainable""".split())

# Round 7: the same line also closes the BRAND FOCUS block of a brand with no texture (a family brand, a pack with
# no texture signal), so it is one constant; BRAND_TEXTURE_COMMON's bytes are unchanged.
TRAVELLER_RETURN_LINE = (
    """- Every factual paragraph comes back to the traveller within two sentences: after at most two sentences about
  what a place is (its history, geology, figures or other names), the next sentence puts the reader there, in a
  moment the CONTENT SEED gives (where they ride, walk, eat or sleep). A stretch that reads like a reference work
  or an encyclopedia entry is not finished. When the seed gives no moment for a place, keep its facts to two
  sentences and move on.""")
BRAND_TEXTURE_COMMON = "\n" + TRAVELLER_RETURN_LINE + """
- Texture never adds content and never needs a tell: SOURCE FIDELITY, VISIT DETAIL and the AI TELLS rules apply to
  every line. No verdict line, no aphorism, no praise, no denial set-up and no list of three to make the voice.
- In the DON'T / WRITE INSTEAD pairs below, <...> is a fact from the content seed, never an invented one."""
BRAND_TEXTURE_FAQ = """
- The FAQ answers are written in this voice too, never as formal brochure copy; each still answers with one fact
  from the CONTENT SEED or FACTS."""
BRAND_TEXTURE_MOMENTUM = """
- MOMENTUM: momentum comes from short, concrete action sentences built on what the CONTENT SEED says the reader
  does (riding, walking, swimming, climbing, paddling), written to "you", with the verb early. A short sentence
  carries an action or a fact, never a comment on the trip ("Four days in.", "That's the pace this route sets.",
  "This is the day the route announces itself."). Plain speech and contractions are welcome; no hype.
  - DON'T "The falls are a multi-tiered limestone system known for their turquoise pools." (a reference opener
    with no reader in it)
    WRITE "You ride <the distance or road the content seed gives> to <place>,
    then <do what the content seed says you do there>."
  - DON'T "After a long morning on the road, that matters."
    WRITE the action itself:
    "After <the morning's ride the content seed gives>, you <do what the content seed says comes next>.\""""
BRAND_TEXTURE_MOMENTUM_FAQ = """
  In the FAQ: "you", short sentences, the fact first."""
BRAND_TEXTURE_ATMOSPHERE = """
- ATMOSPHERE: atmosphere comes from the concrete sensory or material detail the CONTENT SEED or FACTS give (light,
  stone, wood, a river, silence, a meal, the time of day), told in calm, plainly built sentences of ordinary
  length (one fact or one moment each; the AI TELLS length limits apply) with the reader present in the moment.
  Only detail the seed gives: SOURCE FIDELITY forbids inventing what a place looks, sounds or feels like. Never
  reach for atmosphere with an aphorism ("The architecture is the devotion made structural.", "the land is still
  deciding what it wants to be"), with praise ("one of the most layered destinations in ...") or with a line about
  what a place asks of or gives the reader. When the seed gives no sensory detail, the calm comes from the sentence
  itself: one fact, fully and plainly told.
  - DON'T "The valley is one of the most layered destinations in the region."
    WRITE the seed's own detail, with the reader in it:
    "You reach <place> <at the time of day the content seed gives>,
    when <the light or stone the content seed names>."
  - DON'T "The river, also known by its local name, flows through the valley and joins a larger river downstream."
    (a reference entry)
    WRITE the river where the reader meets it:
    "You <stay or eat where the content seed says>, beside <the river the content seed names>.\""""
BRAND_TEXTURE_ATMOSPHERE_FAQ = """
  In the FAQ: the same calm, full sentences, the fact told plainly, no brochure phrasing ("is renowned for",
  "boasts")."""
BRAND_TEXTURE_OBSERVATION = """
- OBSERVATION: an observant, specific voice: name what the CONTENT SEED says is there (the habitat, the plants
  and animals, the village and the people who live there) in plain, exact words, and what the reader does among
  them. No drama words ("dramatic", "spectacular", "stunning") and no verdict about what a place means or drives
  ("The geology here drives everything visible above it.", "This stop gives the route its northernmost cultural
  depth."). Respect shows in what the sentence says: who lives or works there and what the seed says about how the
  visit happens; never a sighting or an encounter the seed does not give.
  - DON'T "The geology here drives everything visible above it."
    WRITE what the seed says is there, with the reader on the way:
    "On the way to <place>, you pass <the habitat the content seed names>."
  - DON'T "This stop gives the route its northernmost cultural depth."
    WRITE who lives there and what the seed says happens:
    "In <village>, <the people the content seed names> <do what the content seed says>,
    and you <join the moment the content seed gives>.\""""
BRAND_TEXTURE_OBSERVATION_FAQ = """
  In the FAQ: plain and exact, the fact the seed gives in the same observant words as the body."""
_TEXTURES = (("momentum", BRAND_TEXTURE_MOMENTUM, BRAND_TEXTURE_MOMENTUM_FAQ),
             ("atmosphere", BRAND_TEXTURE_ATMOSPHERE, BRAND_TEXTURE_ATMOSPHERE_FAQ),
             ("observation", BRAND_TEXTURE_OBSERVATION, BRAND_TEXTURE_OBSERVATION_FAQ))


def _texture_words(entry: str) -> set[str]:
    """The words of one pack entry that count as a texture signal: clause by clause, each clause up to its first
    not / never / no / nor ('Not a luxury brand' and 'Calm, never sporty' do not count the denied word)."""
    out: set[str] = set()
    for clause in _PACK_CLAUSE_RE.split(entry.lower()):
        for w in _VOICE_WORD_RE.findall(clause):
            if w in _VOICE_NEGATIONS:
                break
            out.add(w)
    return out


def brand_textures(brand_audience: Optional[dict]) -> dict[str, list[str]]:
    """{texture: the pack entries that signal it} for MOMENTUM (an energetic voice word, the same test as the PACE
    line), ATMOSPHERE (a luxury / cultural voice word or brand type) and OBSERVATION (a nature / respectful voice
    word or brand type). Empty for a family brand (``family_presence_rule``: FAMILY_RULE carries its voice) and for
    a pack with no signal. Read from the pack's words only, never from a tenant name."""
    pack = brand_audience or {}
    if family_presence_rule(pack):
        return {}
    voice = _pack_voice(pack)
    brand_type = str(pack.get("brand_type") or "")
    kind = [brand_type] if brand_type else []
    found = {
        "momentum": _energetic_entries(voice),
        "atmosphere": ([v for v in voice if _texture_words(v) & _ATMOSPHERE_VOICE]
                       + [t for t in kind if _texture_words(t) & _ATMOSPHERE_TYPE]),
        "observation": ([v for v in voice if _texture_words(v) & _OBSERVATION_VOICE]
                        + [t for t in kind if _texture_words(t) & _OBSERVATION_TYPE]),
    }
    return {k: v for k, v in found.items() if v}


def brand_texture_rule(brand_audience: Optional[dict], channel: Optional[str] = None) -> str:
    """The BRAND TEXTURE block for a pack with a texture signal (``brand_textures``), '' otherwise: one header
    naming the pack words it was read from, the common lines (back to the traveller within two sentences; texture
    never needs a tell), then one block per texture in a fixed order (MOMENTUM, ATMOSPHERE, OBSERVATION). A blog
    (``channel == "blog"``) also gets the FAQ line and each texture's FAQ sentence; other channels have no FAQ."""
    found = brand_textures(brand_audience)
    if not found:
        return ""
    words = list(dict.fromkeys(w for k, _, _ in _TEXTURES for w in found.get(k, [])))
    blog = channel == "blog"
    out = ("\nBRAND TEXTURE FOR THIS BRAND (from its pack: " + ", ".join(words) + "): the brand's voice comes from"
           " how each sentence is built and where the reader stands, never from a line that comments on the trip."
           + BRAND_TEXTURE_COMMON + (BRAND_TEXTURE_FAQ if blog else ""))
    for key, body, faq in _TEXTURES:
        if key in found:
            out += body + (faq if blog else "")
    return out


# AA-790 round 4 (S252 4-arm panel: Terra Family brand voice 6.85; "children show up once", "the child perspective
# stays generic", "long stretches read like a gazetteer with no family lens") — a family brand gets the family in
# every section, from the seed's own facts only. Separate from brand_voice_rule (PACE / RESTRAINT): a family brand
# may be neither energetic nor calm, and those two lines stay as they are.
# AA-790 round 5 (S252 5-arm panel of round 4: Terra Family brand voice 7.0 → 5.5, LinkedIn 6.92 → 5.08; "children
# never appear concretely (only 'your family')", "families appear only as an abstraction", "dry and institutional
# rather than warm", "no pace or comfort cues") — round 4 told the writer to speak to "your family" only and never
# give an activity to the children, one family sentence per section at most. Round 5: the children and the parents
# are part of the group, so they may do what the seed says the group does (not a new fact); warmth comes from the
# seed's own pace / effort / rest cues; no hard per-section cap. Still forbidden: child-specific services, prices,
# facilities, ages or activities the seed lacks, and what a child will feel or learn.
# 'family-run' / 'family owned since 1990' describe who owns the business, not who travels: never a trigger.
_FAMILY_OWNERSHIP = frozenset({"run", "owned", "operated", "led", "business", "businesses", "founded"})
_FAMILY_WORD_RE = re.compile(r"^(?:famil(?:y|ies)(?:-(?!(?:run|owned|operated|led|business|founded)$)[a-z]+)?"
                             r"|kids?|child(?:ren)?|parents?|(?:kid|child)-friendly)$")
FAMILY_RULE = """
FAMILY BRAND: this brand writes for families, so the piece reads warm and concrete: the children and the parents
are there, in the moments the CONTENT SEED and FACTS describe. SOURCE FIDELITY comes first: a family detail the
seed does not give is left out, never guessed.
- The children and the parents are part of the travelling group. When the CONTENT SEED or FACTS say the group
  sees, does, tastes, walks, rides or sleeps somewhere, name the children or the parents as the ones who do it
  ("the children taste ...", "you and the children walk ..."): that is the seed's own moment, not a new fact.
  Never give them a moment, a view or an activity the seed does not give the group. When the seed marks a moment
  optional, demanding or limited to some of the group, keep the seed's own words about who it is for and speak to
  the family as a whole there.
- Warmth comes from the pace, effort and rest cues the CONTENT SEED gives: the timing, the walking or driving
  time, a meal or rest stop, what is optional, a free or easy afternoon, where the night is. Use them when the seed
  gives them; never invent reassurance or comfort ("no rush", "easy for little legs", "nothing to worry about",
  "at their own pace").
- An age, an age band or "suitable for children" appears ONLY when the CONTENT SEED or FACTS state it. The
  AUDIENCE line's ages describe the brand's customers, never this trip.
- No child-specific service, price, facility or activity the seed does not give (a children's menu, a child rate,
  a family room, a kids' club). Never invent what children feel, notice, learn, understand or remember; never say
  a stop "works for every age" or is "easy to explain to younger travellers"; no school-trip framing ("a lesson",
  "a classroom").
- Bring the family into most sections, never as a formula: vary the words and where they sit in the sentence. Do
  not open section after section with "For children, ..." or "For families, ...", never write "tends to land
  differently", and do not let "your family" be the only sign of a family. The AI TELLS rules apply here too.
- A section whose seed facts give a family nothing to use states those facts plainly, without a made-up family
  line.
- DON'T / WRITE INSTEAD (<...> is a fact from the content seed, never an invented one):
  - DON'T "For families, this is a stop with a clear frame."
    WRITE the seed's moment with the children in it:
    "At <place>, the children <do what the content seed says the group does>."
  - DON'T "Children can watch a craft in progress." (the seed never says the group watches anyone work)
    WRITE only the moment the seed gives:
    "You and the children <do what the content seed says happens there>."
  - DON'T "This is a structured, organised stop on day six."
    WRITE the seed's own pace cue:
    "Day six <moves at the pace the content seed gives>, with <the meal or rest stop it names>.\""""
# AA-790 round 5 — non-blog plain-text channels (LinkedIn, Facebook, Instagram, TikTok, ads): a short post has no
# sections, so one concrete family moment carries it (S252 5-arm panel: Terra LinkedIn voice 6.92 → 5.08 on round 4).
FAMILY_SHORT_POST_CHANNELS = frozenset({"linkedin", "facebook", "instagram", "tiktok", "ads"})
FAMILY_SHORT_POST_RULE = """
- In this short post one concrete family moment from the CONTENT SEED is enough: the children or the parents in a
  moment the seed gives. Keep the post short."""


# AA-790 round 4 review: a negation up to four words back, in the same clause ('Adults-only luxury brand, not
# suitable for children'); 'by … family' / 'same family' / 'family for three generations' is ownership too ('Run by
# the same family for three generations').
_FAMILY_NEGATIONS = _VOICE_NEGATIONS | {"without", "adults-only", "adult-only"}
_FAMILY_SPAN_WORDS = frozenset({"generation", "generations", "decade", "decades", "years"})
_PACK_CLAUSE_RE = re.compile(r"[,;:.!?()]")


def _family_word_counts(words: list[str], k: int) -> bool:
    """words[k] names families or children as the brand's travellers: not negated (not / never / no / nor /
    without / adults-only up to four words back) and not ownership (a following 'run' / 'owned' / …; for a
    'family' word also 'by' up to three words back, 'same family', 'family for <n> generations / years')."""
    w = words[k]
    if not _FAMILY_WORD_RE.match(w):
        return False
    before = words[max(0, k - 4):k]
    if _FAMILY_NEGATIONS & set(before) or any(a in ("adult", "adults") and b == "only"
                                              for a, b in zip(before, before[1:])):
        return False
    after = words[k + 1:k + 4]
    if after and after[0] in _FAMILY_OWNERSHIP:
        return False
    if w.startswith("famil"):
        if "by" in words[max(0, k - 3):k] or (k and words[k - 1] == "same"):
            return False
        if after[:1] == ["for"] and _FAMILY_SPAN_WORDS & set(after[1:]):
            return False
    return True


def family_presence_rule(brand_audience: Optional[dict], channel: Optional[str] = None) -> str:
    """The FAMILY BRAND line for a pack whose voice or brand type names families or children ('Family-aware',
    'Premium family adventure travel brand'); '' otherwise. Round 5: a short plain-text post (``channel`` in
    ``FAMILY_SHORT_POST_CHANNELS``: LinkedIn, Facebook, Instagram, TikTok, ads) also gets
    ``FAMILY_SHORT_POST_RULE`` (one concrete family moment is enough); blog, email, landing page and no channel get
    ``FAMILY_RULE`` alone. The audience line alone never triggers it (a general
    brand can list 'couples and families'), a negated word ('not a family brand', 'Adults-only …, not suitable for
    children') does not count, and neither does ownership ('Family-run', 'family owned since 1990', 'Run by the
    same family for three generations'): that is who runs the business, not who travels. Read clause by clause, so
    a negation never reaches past a comma."""
    pack = brand_audience or {}
    for entry in [str(v) for v in pack.get("voice") or [] if v] + [str(pack.get("brand_type") or "")]:
        for clause in _PACK_CLAUSE_RE.split(entry.lower()):
            words = _VOICE_WORD_RE.findall(clause)
            if any(_family_word_counts(words, k) for k in range(len(words))):
                return FAMILY_RULE + (FAMILY_SHORT_POST_RULE if channel in FAMILY_SHORT_POST_CHANNELS else "")
    return ""


# AA-790 round 7 (S254; S252 7-arm panel: brand voice round 3 7.17 → round 6 6.75). Two causes the rounds before
# did not touch:
# * the T8 angle brief is copied as wording. 20 of 42 Terra LinkedIn posts of rounds 1-6 end on "… is a structured,
#   accessible stop", the angle's own words ("making it an accessible, structured stop") for a stop whose seed has
#   no practical fact; the judges call it "institutional". ANGLE_BRIEF_RULE follows the three angle lines.
# * the brand's own style-guide points are not covered ("nothing on pace, rest, comfort or safety", "says little
#   about guiding, privacy or stays", "little social feel"). The style guide sits in the brand block, hedged and
#   followed by the DON'T rules; nothing maps its points to the seed facts that can meet them. BRAND FOCUS restates
#   the guide's first rules (before its "Write like this:" / "Avoid:" parts) as a checklist, met only from the
#   seed; a brand with no texture block (a family brand, a no-signal pack) also gets TRAVELLER_RETURN_LINE there.
# 'content seed' in lower case, as SYSTEM_PROMPT writes it: the line sits above the "CONTENT SEED (" section, whose
# label stays the first upper-case "CONTENT SEED" of the prompt.
ANGLE_BRIEF_RULE = ("ANGLE BRIEF: the SELECTED ANGLE lines above are an internal brief, not copy. Follow their "
                    "focus and shape; never copy their phrasing into the piece (only the place names and facts "
                    "they share with the content seed); leave out any detail, reassurance or framing they ask for "
                    "that the content seed does not give.")

BRAND_FOCUS_HEAD = "\nBRAND FOCUS FOR THIS BRAND (the first rules of its style guide, in its own words):"
BRAND_FOCUS_USE = """
Cover each point wherever the CONTENT SEED or FACTS give you a fact for it, and check the piece against every point
before you finish. A point about the trip itself (its pace, rest, comfort, stays, meals, safety, guiding, privacy,
the group, the habitats or the people met) is met only by what the seed or FACTS give for it: a day number, a
walking or driving time, where the night is, a meal, what is optional, who leads, what is private. Use every such
fact the seed gives, in the brand's words, where it belongs in the piece. When the seed gives nothing for a point,
leave that point out: never a general line in its place ("a structured, accessible stop", "a comfortable pace
throughout", "expert guiding at every step"). SOURCE FIDELITY, VISIT DETAIL and the AI TELLS rules apply to every
line."""
# Not a style-guide point, so not in the list "in its own words": its own lead-in.
BRAND_FOCUS_TRAVELLER_HEAD = "\nOne more rule for this brand (not from its style guide):\n"
FOCUS_MAX_POINTS = 8
FOCUS_MAX_CHARS = 200
_FOCUS_BULLET_RE = re.compile(r"^(?:[-*•·]|\d{1,2}[.)])[ \t]*")
# A sentence ends at . ! ? followed by a word that does not start in lower case ('e.g. three per paragraph' stays one
# sentence), and never right after a common abbreviation ('e.g. Bhutan', 'St. Paul').
_FOCUS_SENTENCE_RE = re.compile(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bvs\.)(?<!\bSt\.)(?<!\bMt\.)(?<!\bapprox\.)"
                                r"(?<!\bincl\.)(?<=[.!?])[ \t]+(?=[^\sa-z])")
# A quoted span (straight or curly double quotes; an unclosed quote runs to the end of the line).
_FOCUS_QUOTE_RE = re.compile(r"[\"“]([^\"“”]*)(?:[\"”]|$)")
FOCUS_QUOTE_MIN_WORDS = 4  # a quoted span this long is an example sentence; 'Use "you" for the traveller.' is a rule
# Labels that open the style guide's examples or its don'ts, even with text after the colon ('Avoid: hype.').
_FOCUS_HEADER_LABEL_RE = re.compile(r"^(?:avoid|don['’]?t|do\s+not|never|examples?|write\s+like|not\s+like|instead)\b",
                                    re.I)


def _focus_header(text: str) -> bool:
    """A header starts the style guide's examples or its don'ts, and the focus points end there: a short label
    (at most five words, no full stop) before a colon with nothing after it ('Write like this:', 'Avoid:'), or a
    known example / don't label with text after it ('Avoid: hype, slang.', 'Examples: …', "Don't: …"). A
    'Label: rule' line such as 'Tone: warm and calm.' or 'Explain the pace: day lengths, rest.' is a point."""
    head, colon, rest = text.partition(":")
    if not colon or "." in head or not 0 < len(head.split()) <= 5:
        return False
    return not rest.strip() or bool(_FOCUS_HEADER_LABEL_RE.match(head.strip()))


def _focus_quotes_example(line: str) -> bool:
    """The line quotes an example sentence (a quoted span of ``FOCUS_QUOTE_MIN_WORDS`` words or more)."""
    return any(len(m.group(1).split()) >= FOCUS_QUOTE_MIN_WORDS for m in _FOCUS_QUOTE_RE.finditer(line))


def brand_focus_points(brand_audience: Optional[dict]) -> list[str]:
    """The style guide's first rules, one per line or sentence, up to the first header ('Write like this:',
    'Avoid:'; see ``_focus_header``). Bullets and numbers are dropped; a line that quotes an example sentence (four
    or more words in quotes) is skipped (the brand block keeps the examples, and they are never copy); at most
    ``FOCUS_MAX_POINTS`` points of at most ``FOCUS_MAX_CHARS`` characters and at least two words. Read from the pack
    only, never from a tenant name."""
    guide = str((brand_audience or {}).get("style_guide") or "")
    points: list[str] = []
    for raw in guide.splitlines():
        line = _FOCUS_BULLET_RE.sub("", raw.strip()).strip()
        if not line:
            continue
        if _focus_header(line):
            break
        if _focus_quotes_example(line):
            continue
        for sentence in _FOCUS_SENTENCE_RE.split(line):
            sentence = sentence.strip()
            if _focus_header(sentence):
                return points[:FOCUS_MAX_POINTS]
            if len(sentence.split()) >= 2 and len(sentence) <= FOCUS_MAX_CHARS:
                points.append(sentence if sentence[-1] in ".!?" else sentence + ".")
        if len(points) >= FOCUS_MAX_POINTS:
            break
    return points[:FOCUS_MAX_POINTS]


def brand_focus_rule(brand_audience: Optional[dict]) -> str:
    """The BRAND FOCUS block ('' when the style guide gives no point): the points, how to meet them from the seed
    only, and, for a brand with no BRAND TEXTURE block, ``TRAVELLER_RETURN_LINE`` (a texture block already has it)."""
    points = brand_focus_points(brand_audience)
    if not points:
        return ""
    out = BRAND_FOCUS_HEAD + "".join(f"\n- {p}" for p in points) + BRAND_FOCUS_USE
    if not brand_textures(brand_audience):
        out += BRAND_FOCUS_TRAVELLER_HEAD + TRAVELLER_RETURN_LINE
    return out

# ---------------------------------------------------------------- context

# Words a negation or a verdict must not hold to be cut: they state a rule, an access fact or a service. ("It is not
# allowed to swim.", "Lunch is not included.", "The road is not open in winter, not in summer.")
_PROTECTED_RE = re.compile(
    r"\b(?:allow(?:ed|s)?|permit(?:ted|s)?|possible|suitable|safe|unsafe|included|includ(?:es|ing)|available|"
    r"required|requires?|recommended|open|closed|optional|guaranteed|offered|accessible|necessary|must|"
    r"forbidden|prohibited|banned|restricted|dangerous|risk)\b", re.I)
# AA-790 review: what a family needs to know about effort, cost, payment or access. A denial, verdict or contrast
# whose own (denied / judged) part names one of these is a fact, not a set-up, and is never cut or folded
# ("It is not difficult.", "This is free.", "not reachable by car", "Bring cash for the market, not cards.").
# Checked on that part only, so a tell elsewhere in a sentence that mentions 'free time' is still fixed.
_PRACTICAL_RE = re.compile(
    r"\b(?:compulsory|mandatory|essential|obligatory|free|extra|charged?|charges|costs?|costly|fees?|paid|payable|"
    r"prices?|priced|refundable|bookable|booked|reservations?|reachable|cash|cards?|easy|easier|difficult|difficulty|"
    r"difficulties|hard|"
    r"strenuous|steep|tiring|demanding|physical|far|long|short|deep|shallow|cold|hot|crowded|busy|quiet|noisy|"
    r"cheap|cheaper|expensive|affordable|wheelchair|strollers?|prams?|toilets?|tipping"
    # S252 ship criterion: an amenity a room has or lacks ('something better than air conditioning: …')
    r"|air[ \t-]+condition(?:ing|ed|er|ers)?|wi-?fi)\b", re.I)
_DIGIT_RE = re.compile(r"\d")
_TAG_RE = re.compile(TAG)  # the shared citation-tag definition (AA-912)
_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][\w'’-]*")
# A capitalised word after the first one: a name. 'I' and the start of a quoted sentence are not.
_NAME_AFTER_FIRST_RE = re.compile(r"(?<=\s)(?!I\b)[A-ZÀ-Þ][a-zà-öø-ÿ]")
# Nouns a verdict line uses about the piece itself; they never make it concrete.
_META_NOUNS = frozenset("""day days trip trips journey journeys route routes section sections stop stops moment moments
part parts opening start point points kind sort type thing things place places story stories experience way""".split())
_PRONOUN_SUBJECTS = frozenset({"it", "this", "that", "these", "those", "they", "such"})


@dataclass(frozen=True)
class TellContext:
    """What the pass needs about one piece. ``source`` is the seed's own lines plus the Facts (a verdict line that
    names a seed fact is not cut); ``keep_words`` are the brand's signature words (folded), never stripped;
    ``ignore`` the brand-name words the integrity check skips (``hygiene.brand_words``); ``angle_text`` the whole
    chosen angle (name, why it works, formula fit, best final style: round 3, a heading lifted from the brief)."""
    cta: Optional[str] = None
    angle_name: Optional[str] = None
    source: str = ""
    keep_words: frozenset = frozenset()
    ignore: frozenset = frozenset()
    angle_text: str = ""


def angle_text(angle: Optional[dict]) -> str:
    """The chosen angle's own words (name, why it works, formula fit, best final style) for ``TellContext``."""
    return " ".join(str((angle or {}).get(k) or "") for k in ("name", "why_it_works", "formula_fit",
                                                               "best_final_style")).strip()


@dataclass(frozen=True)
class Edit:
    """One change: text[start:end] → ``text``. ``before`` is the sentence(s) it changes (for the log), ``olds`` the
    clean sentences it replaces and ``new`` the clean sentence it writes ('' for a cut), so a problem the old
    sentence already had is not read as new, and the fact check (``_fact_losers``) knows what the edit removed."""
    start: int
    end: int
    text: str
    kind: str
    before: str
    olds: tuple = ()
    new: str = ""
    heading: bool = False  # a heading label: only its seed words count in the fact check


def _words(s: str) -> list[str]:
    return _WORD_RE.findall(s)


def _tags(raw: str) -> list[str]:
    return list(dict.fromkeys(_TAG_RE.findall(raw or "")))


def _with_tags(sentence: str, tags: list[str]) -> str:
    return sentence + (" " + " ".join(tags) if tags else "")


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _protected(s: str) -> bool:
    return bool(_PROTECTED_RE.search(s))


def _practical(part: str) -> bool:
    return bool(_PRACTICAL_RE.search(part))


# Round-3 review: who a thing is (not) for is an audience limit, a fact a family plans by ('This is not a walk for
# small children.'). A denied part that names people is never cut or folded into a join, by any rule: the pairs
# (_pair, _denied_ok), the lone denial, 'not X but Y', ', not X.' and 'X is not A; it is B' (second review).
_AUDIENCE_RE = re.compile(
    rf"\b(?:{hygiene._PERSON_NOUNS}|toddlers?|infants?|bab(?:y|ies)|beginners?|novices?|first-timers?|seniors?"
    r"|elderly|older[ \t]+(?:people|visitors|walkers)|wheelchair[ \t]+users?|non-swimmers?|swimmers?|walkers?"
    r"|climbers?|riders?|passengers?|pedestrians?|cyclists?|motorists?|drivers?"
    # third review: 'not a trek for people with vertigo', 'for anyone with …', 'not for vegetarians', 'for the unfit'
    r"|people|persons?|anyone|those|visitors?|vegetarians?|vegans?|the[ \t]+(?:unfit|faint-hearted))\b", re.I)


def _audience(part: str) -> bool:
    return bool(_AUDIENCE_RE.search(part))


def _seed_fact(denied: str, src: frozenset) -> bool:
    """The denied part is the seed's own statement ('the herds are not primarily raised for meat'): a contrast
    whose other half the seed states is allowed (prompts.py SOURCE FIDELITY) and is a fact, so it stays."""
    words = [w for w in content_words(denied) if w not in _META_NOUNS]
    return bool(words) and sum(w in src for w in words) >= 0.6 * len(words)


_VOWEL_SOUND_EXCEPTIONS_A = ("uni", "use", "usu", "uti", "eur", "one", "once")   # 'a unique', 'a one-day'
_VOWEL_SOUND_EXCEPTIONS_AN = ("hour", "honest", "honour", "honor", "heir")       # 'an hour'


def _article_for(word: str) -> str:
    w = word.lower()
    if w.startswith(_VOWEL_SOUND_EXCEPTIONS_AN):
        return "an"
    if w.startswith(_VOWEL_SOUND_EXCEPTIONS_A):
        return "a"
    return "an" if w[:1] in "aeiou" else "a"


_ARTICLE_BEFORE_RE = re.compile(r"\b([Aa]n?)\s+$")


def _drop_span(s: str, start: int, end: int) -> str:
    """``s`` without s[start:end] (a word and its trailing space): the article before it is made to agree with the
    word after it ('a genuine encounter' → 'an encounter'), and a cut at the sentence start capitalises the rest."""
    head, tail = s[:start], s[end:]
    m = _ARTICLE_BEFORE_RE.search(head)
    nxt = _WORD_RE.match(tail)
    if m and nxt:
        art = _article_for(nxt.group(0))
        art = art.capitalize() if m.group(1)[0].isupper() else art
        head = head[:m.start(1)] + art + head[m.end(1):]
    if not head.strip():
        tail = _cap(tail)
    return head + tail


# ---------------------------------------------------------------- word / clause rules (one sentence)

# Sentence-initial stock adverbs ("Crucially, the institute …") and the same word set off by commas mid-sentence.
_OPENER_ADVERB_RE = re.compile(r"^(?:Crucially|Importantly|Notably|Tellingly|Interestingly|Ultimately|Indeed|"
                               r"Undeniably|Admittedly|Fundamentally),[ \t]+(?=\S)")
_MID_ADVERB_RE = re.compile(r",[ \t]*(?:crucially|importantly|notably|tellingly|interestingly|ultimately|indeed|"
                            r"undeniably|fundamentally),[ \t]*", re.I)
# Intensifiers that add nothing ('genuinely unusual', 'genuine depth', 'sits precisely at'). Never before a
# number ('precisely 50 metres' is a measurement).
_STOCK_WORD_RE = re.compile(r"\b(?:genuinely|genuine|truly|precisely|undeniably)[ \t]+(?=[A-Za-z])", re.I)


def _stock_words(s: str, ctx: TellContext) -> tuple[str, list[str]]:
    done: list[str] = []
    m = _OPENER_ADVERB_RE.match(s)
    if m and fold(m.group(0).rstrip(", \t")) not in ctx.keep_words:
        done.append(m.group(0).rstrip(", \t"))
        s = _cap(s[m.end():])
    s2 = _MID_ADVERB_RE.sub(", ", s)
    if s2 != s:
        done.append("mid-sentence adverb")
        s = s2
    while True:
        m = next((x for x in _STOCK_WORD_RE.finditer(s) if fold(x.group(0).strip()) not in ctx.keep_words), None)
        if not m:
            break
        done.append(m.group(0).strip())
        s = _drop_span(s, m.start(), m.end())
    return s, done


# ", referred to throughout as the Khaling National Handloom" / "(hereafter the Institute)": an internal label.
_LABEL_RE = re.compile(
    r",?[ \t]*(?:(?:hereafter|henceforth)[ \t]+(?:referred[ \t]+to[ \t]+as|called|known[ \t]+as)"
    r"|(?:referred[ \t]+to|called|known)[ \t]+(?:throughout|hereafter|henceforth|below|from[ \t]+here[ \t]+on"
    r"|in[ \t]+this[ \t]+(?:piece|post|article|guide|section))[ \t]+as)[ \t]+[^,.;:!?()\n]{1,80}", re.I)
_LABEL_PAREN_RE = re.compile(r"[ \t]*\((?:hereafter|henceforth|referred[ \t]+to[ \t]+(?:here|below|throughout)"
                             r"[ \t]+as)[^()\n]{1,80}\)", re.I)


def _labels(s: str, src: frozenset) -> tuple[str, list[str]]:
    out = _LABEL_PAREN_RE.sub("", _LABEL_RE.sub("", s))
    return out, (["internal label"] if out != s else [])


# "…offers, not because it is remote, but because of what it represents." → "…offers because of what it represents."
_NOT_BECAUSE_RE = re.compile(r",?[ \t]+not[ \t]+because[ \t]+[^.;!?\n]{1,140}?,?[ \t]+but[ \t]+because\b", re.I)


def _not_because(s: str, src: frozenset) -> tuple[str, list[str]]:
    m = _NOT_BECAUSE_RE.search(s)
    if (not m or not s[:m.start()].strip() or _protected(s) or _practical(m.group(0))
            or _seed_fact(m.group(0), src)):
        return s, []
    return s[:m.start()] + " because" + s[m.end():], ["not because … but because"]


# "is not a sequence of separate attractions but a coherent argument" → "is a coherent argument";
# "set not by a calendar but by the forest" → "set by the forest". Only after a copula (the frame "is not X but Y")
# or when X and Y open on the same preposition; "not only / just X but (also) Y" is ordinary English and stays.
_NOT_BUT_RE = re.compile(
    r"\b(?P<lead>(?:is|are|was|were|be|been|being)[ \t]+)?not[ \t]+"
    r"(?!only\b|just\b|merely\b|simply\b|because\b|that\b|even\b|least\b|much\b|many\b|enough\b|anything\b"
    r"|nothing\b|everything\b|all\b|to\b)"
    r"(?P<x>[^.,;:!?()\"“”\n]{1,70}?)(?P<comma>,)?[ \t]+but[ \t]+(?:rather[ \t]+)?(?!also\b|not\b|only\b)(?P<y>\w+)",
    re.I)
_PREPOSITIONS = frozenset("by for with in on at from through to into as about".split())
# AA-790 review: "is not difficult but it is long" / "is not paved but the jeeps manage it fine": after "but" a new
# clause starts, so cutting "not X but" leaves "is it is long". A subject pronoun, a finite verb or an object
# pronoun in Y's phrase (before any relative word) marks that clause; the copula path then never fires.
_SUBJECT_PRONOUNS = frozenset("i it they we you he she there".split())
_CLAUSE_MARKERS = frozenset("""i it they we you he she them us him her there here is are was were be has have had do
does did can could will would may might must should shall cannot can't won't don't doesn't didn't isn't aren't
wasn't weren't""".split())
_RELATIVE_WORDS = frozenset("that which who whom whose where when while".split())
_WH_WORDS = frozenset("what how why whether".split())
_DEGREE_WORDS = frozenset("very quite rather more far much too so fairly surprisingly".split())
_Y_PHRASE_RE = re.compile(r"[^.,;:!?()\n]*")
_DETERMINERS = frozenset("a an the this that these those its their his her our your one two three some each every "
                         "something someone".split())


def _y_phrase(s: str, start: int) -> list[str]:
    """Y's words from ``start`` to the next punctuation, cut at the first relative word ('a monastery where monks
    still live' → ['a', 'monastery']) or at a wh-clause a preposition opens ('an argument about what this corner
    is' → ['an', 'argument', 'about']; 'the monks know what it is' is not cut)."""
    out: list[str] = []
    for w in _Y_PHRASE_RE.match(s, start).group(0).split():
        w = w.lower().strip("\"'“”‘’")
        if w in _RELATIVE_WORDS or (w in _WH_WORDS and out and out[-1] in _PREPOSITIONS):
            break
        out.append(w)
    return out


def _parallel_copula(x_words: list[str], y_words: list[str]) -> bool:
    """'is not A but B' is folded to 'is B' only when B is the same kind of phrase as A and no clause starts after
    'but': two noun phrases ('not a museum but a working monastery'), or a one-word complement and a short one
    ('not difficult but long', 'not grey but very green')."""
    if not y_words or y_words[0] in _SUBJECT_PRONOUNS or _CLAUSE_MARKERS & set(y_words):
        return False
    x0, y0 = x_words[0].lower(), y_words[0]
    if y0 in _DETERMINERS:
        return x0 in _DETERMINERS
    if len(x_words) != 1 or x0 in _DETERMINERS:
        return False
    if len(y_words) == 1:
        return True
    return len(y_words) == 2 and (y0 in _DEGREE_WORDS or y0.endswith("ly"))


def _y_ends_sentence(s: str, start: int) -> bool:
    """Y's phrase runs to the end of the sentence (only closing punctuation after it) and is short: after ', but'
    a new clause often starts ('is not a museum, but the monks welcome visitors'), so the comma form is folded only
    when Y is the sentence's last phrase ('…: not a relic, but a working institution.')."""
    m = _Y_PHRASE_RE.match(s, start)
    return len(m.group(0).split()) <= 5 and not s[m.end():].strip(" \t.!?\"'”’)")


# A thing presented as staged or dead: the only nouns a colon 'not X, but Y' may fold although the seed uses the word
# (it uses it as a plain noun: 'the sacred relic of …'). Never when the seed itself denies it ('is not a relic').
_STAGED_NOUN_X_RE = re.compile(
    r"^[ \t]*(?:(?:a|an|the|any|some)[ \t]+)?(?:mere[ \t]+|dead[ \t]+|static[ \t]+)?"
    r"(?P<n>relics?|museum[ \t]+pieces?|backdrops?|stage[ \t]+sets?|set[ \t]+pieces?|theme[ \t]+parks?"
    r"|curiosit(?:y|ies)|showpieces?)[ \t]*$", re.I)


def _staged_noun_x(x: str, src: frozenset) -> bool:
    m = _STAGED_NOUN_X_RE.match(x)
    return bool(m) and not _seed_denies(m.group("n"), src)


def _seed_denies(noun: str, src: frozenset) -> bool:
    """The source text itself denies ``noun`` ('is not a relic', 'no relics', 'never a museum piece')."""
    text = getattr(src, "text", "")
    stem = re.escape(noun.split()[0][:5].lower())  # relic(s), museu(m), curio(sity / sities)
    return bool(text) and bool(re.search(rf"\b(?:not|no|never|nor)[ \t]+(?:[\w'’-]+[ \t]+){{0,3}}?{stem}", text, re.I))


def _not_but(s: str, src: frozenset) -> tuple[str, list[str]]:
    if _protected(s):
        return s, []
    done: list[str] = []
    for _ in range(3):
        hit = None
        for m in _NOT_BUT_RE.finditer(s):
            x_words = m.group("x").split()
            # AA-790 round 3: the frame may also follow a colon ('what a dzong is: not a relic, but a working
            # institution'); with a comma before 'but', only when Y ends the sentence (_y_ends_sentence). In the colon
            # frame a one-word X the seed uses elsewhere ('the sacred relic of …') is not the seed's contrast.
            colon = not m.group("lead") and s[:m.start()].rstrip().endswith(":")
            if not x_words or len(x_words) > 9:
                continue
            # Round-3 review: the seed-fact guard holds in the colon frame too ('Avoid the monsoon months; April is
            # …' keeps '…: not in the monsoon, but in April.'). The one exception is a staged-thing noun the seed
            # uses as a plain noun and never denies ('houses the sacred relic of …' vs '…: not a relic, but …').
            if _seed_fact(m.group("x"), src) and not (colon and _staged_noun_x(m.group("x"), src)):
                continue
            # third review: a limit ('not the only route up, but the shortest') is a fact in every frame
            if _practical(m.group("x")) or _audience(m.group("x")) or _LIMIT_RE.search(m.group("x")):
                continue
            y = m.group("y").lower()
            # third review: after ', but' or ': not' only the copula path folds, plus the role frame 'not as X, but
            # as Y' (the panel's 'not as cultural artefacts, but as living practice'). Any other same-preposition
            # contrast there is a place or time choice ('Dinner is not at the lodge, but at a farmhouse.', ': not on
            # Sundays, but on Saturdays'); the bare 'set not by a calendar but by the forest' form still folds.
            same_prep = (x_words[0].lower() in _PREPOSITIONS and x_words[0].lower() == y
                         and (y == "as" or not (m.group("comma") or colon)))
            copula = (bool(m.group("lead")) or colon) and _parallel_copula(x_words, _y_phrase(s, m.start("y")))
            if m.group("comma") and not _y_ends_sentence(s, m.start("y")):
                continue
            if same_prep or copula:
                hit = m
                break
        if hit is None:
            break
        keep = hit.group("lead") or ""
        s = s[:hit.start()] + keep + s[hit.start("y"):]
        done.append("not X but Y")
    return s, done


# "…is active, not archival." / "…a government enterprise, not a heritage attraction." → the claim alone.
_NOT_TAIL_RE = re.compile(
    r",[ \t]+not[ \t]+(?!only\b|just\b|merely\b|simply\b|because\b|even\b|least\b|always\b|necessarily\b|far\b"
    r"|long\b|until\b|before\b|after\b|yet\b|once\b|to\b)(?P<x>[^.,;:!?()\"“”\n]{1,90}?)"
    r"(?P<end>[ \t]*[.!?]+[\"'”’)]*)$", re.I)


def _not_tail(s: str, src: frozenset) -> tuple[str, list[str]]:
    m = _NOT_TAIL_RE.search(s)
    if (not m or len(m.group("x").split()) > 12 or _protected(s) or _DIGIT_RE.search(m.group("x"))
            or _practical(s) or _audience(m.group("x")) or _LIMIT_RE.search(m.group("x"))
            or _seed_fact(m.group("x"), src)):
        return s, []
    return s[:m.start()] + m.group("end").strip(), [", not X"]


# One sentence: "This is not a performance; it is an offer that …" → "This is an offer that …".
_INLINE_PAIR_RE = re.compile(
    r"^(?P<subj>[^;:\n]{1,90}?)[ \t]+(?P<cop>is|are|was|were)[ \t]+not[ \t]+(?:only[ \t]+|just[ \t]+|simply[ \t]+"
    r"|merely[ \t]+)?(?P<x>[^.;:!?\n]{1,80}?)[ \t]*[;:,][ \t]+(?:it|this|that|they)(?:[ \t]+(?P<cop2>is|are|was|were)"
    r"|['’](?P<c2>s|re))[ \t]+(?!not\b)(?P<y>.+)$", re.I)


def _inline_pair(s: str, src: frozenset) -> tuple[str, list[str]]:
    m = _INLINE_PAIR_RE.match(s)
    if (not m or _NOT_ONLY_RE.search(m.group(0)[:m.start("x")])  # fifth review: 'is not just A; it is B' asserts A
            or _protected(m.group("subj") + " " + m.group("x")) or _DIGIT_RE.search(m.group("x"))
            or _practical(m.group("x")) or _audience(m.group("x")) or _LIMIT_RE.search(m.group("x"))
            or _seed_fact(m.group("x"), src)):
        return s, []
    cop = m.group("cop2") or {"s": "is", "re": "are"}[m.group("c2").lower()]
    if m.group("cop").lower() in ("are", "were") and cop.lower() in ("is", "was"):
        cop = m.group("cop")  # the subject's own number
    return f"{m.group('subj')} {cop} {m.group('y')}", ["X is not A; it is B"]


# "X; the Y …" → two sentences when the second clause opens on its own subject (a long flat sentence).
_SEMI_RE = re.compile(r";[ \t]+(?=(?:the|it|its|this|these|those|they|their|a|an|each|both|he|she|his|her|there|"
                      r"many|some|most|its)\b)", re.I)


def _semicolons(s: str) -> tuple[str, list[str]]:
    cuts = [m for m in _SEMI_RE.finditer(s)
            if len(_words(s[:m.start()].split(";")[-1])) >= 6 and len(_words(s[m.end():].split(";")[0])) >= 6
            and s[:m.start()].count("(") <= s[:m.start()].count(")")]
    if not cuts:
        return s, []
    out, pos = [], 0
    for m in cuts:
        out.append(s[pos:m.start()] + ". " + s[m.end()].upper())
        pos = m.end() + 1
    out.append(s[pos:])
    return "".join(out), ["semicolon split"] * len(cuts)


# AA-790 round 2 — a verdict lead-in before a colon: "The circle is complete: the bridges … are repaired by …",
# "The thread that ties this section of the trip together is this: …" → the statement after the colon. The
# generic "<subject> is this:" form fires only when the subject talks about the piece or names an abstraction
# (one of _LEAD_IN_NOUNS, or a piece-meta noun: section, piece, 'part of the journey'): "The rule at the temple
# is this: …" keeps its subject. Round-2 review: the broad _META_NOUNS (way, place, day, route, stop …) cut
# informative subjects ("The only way in is this: …", "The best place to stay is this: …"), so they are not
# used here, and a subject that holds a superlative / 'only', a rule, access or practical word, a digit or a
# name is a fact and is never cut.
_LEAD_IN_RE = re.compile(
    r"^(?P<lead>The[ \t]+circle[ \t]+(?:closes|is[ \t]+complete)|Here[ \t]+is[ \t]+the[ \t]+thing"
    r"|(?P<subj>(?:The|This|That)[ \t][^:;,.!?\n]{1,110}?)[ \t]+is[ \t]+this):[ \t]+(?P<rest>[a-zA-Z][^\n]+)$")
_LEAD_IN_NOUNS = frozenset("""point result answer logic lesson upshot difference truth idea convergence argument
pattern thread takeaway""".split())
_PIECE_META_NOUNS = frozenset("section sections piece post article paragraph".split())
_PART_OF_RE = re.compile(r"\bparts?[ \t]+of[ \t]+(?:the|this|our|your)[ \t]+(?:journey|trip|route|day|story)\b", re.I)
_LEAD_IN_FACT_WORDS = frozenset("""only best safest easiest quickest fastest cheapest shortest main first last
nearest closest""".split())


_FINITE_RE = re.compile(r"\b(?:is|are|was|were|has|have|had|can|could|will|would|may|might|must|does|do|did)\b", re.I)


def _lead_in(s: str, src: frozenset) -> tuple[str, list[str]]:
    m = _LEAD_IN_RE.match(s)
    # what is left must be a sentence of its own: 'the bridges … are repaired …', never a bare noun phrase
    # ('a river and the towns along it, fed by …' stays, and goes to the F15 repair as residue)
    if not m or len(_words(m.group("rest"))) < 5 or not _FINITE_RE.search(m.group("rest").split(",")[0]):
        return s, []
    subj = m.group("subj")
    if subj is not None:
        subj_words = {w.lower() for w in _words(subj)}
        if not (subj_words & (_LEAD_IN_NOUNS | _PIECE_META_NOUNS) or _PART_OF_RE.search(subj)):
            return s, []
        if (subj_words & _LEAD_IN_FACT_WORDS or _protected(subj) or _practical(subj) or _DIGIT_RE.search(subj)
                or _NAME_AFTER_FIRST_RE.search(subj)):
            return s, []
    return _cap(m.group("rest")), ["verdict lead-in"]


# AA-790 round 3 (S252 re-panel of round 2: AI-tells 5.33, bar 5.5) — a contrast tail whose denied half is a
# staging word: "… is direct rather than staged.", "… a colour produced by the limestone rather than by any trick of
# the light.", "… a long-standing condition rather than an incidental detail.", "… to move through a place, not just
# pass through it." Closed lists, because a 'rather than' tail is often a fact ("by bus rather than by boat", "lunch
# at a local home rather than a restaurant"): STAGING words deny that a thing is put on for show, which no seed
# states as a fact; SPECTATOR words follow 'not just / merely / only / simply' ('not just a view of them'), where
# the asserted half (a view, a glimpse) carries no fact either. Only at the sentence end; the main clause stays.
# Round-3 review: only words that say a thing is put on for show. Itinerary nouns (show, performance,
# demonstration, spectacle) left the list: 'see the dance rehearsal rather than the full performance', 'watched from
# a platform rather than used in shows' are expectation and welfare facts. Between the article and the word only a
# closed modifier ('a mere backdrop', 'carefully staged'): never a verb ('rather than used as a backdrop') or a
# free word ('the festival performances').
_STAGING = (r"staged|scripted|choreographed|stage-managed|museum[ \t]+pieces?|backdrops?|stage[ \t]+sets?"
            r"|incidental[ \t]+(?:details?|features?)|tricks?[ \t]+of[ \t]+the[ \t]+light")
_STAGING_MODIFIERS = r"mere|purely|carefully|artfully|deliberately|elaborately|neatly|painted|pretty"
_SPECTATOR = (r"views?|glimpses?|snapshots?|photos?|photographs?|postcards?|backdrops?"
              r"|pass(?:ing)?[ \t]+through(?:[ \t]+(?:it|them))?")
# S252 ship criterion: the tail names nothing. Before the spectator word only a closed modifier ('a quick glimpse'),
# never a free word ('not just a rim view' names the rim); after it only 'of it / of them' ('not just glimpses of
# the leopards', 'not merely a backdrop of the hearth' name the thing seen, a fact the cut would lose).
_SPECTATOR_MODIFIERS = r"quick|brief|passing|fleeting|distant|pretty|nice|good|mere|few|single"
# Starts at the keyword after a blank (a lookbehind, never a leading blank run: CodeQL py/polynomial-redos).
_STAGING_TAIL_RE = re.compile(
    r"(?<=[ \t])(?:(?:rather[ \t]+than|instead[ \t]+of|not)[ \t]+(?:(?:by|as)[ \t]+)?(?:(?:a|an|any|the|some)[ \t]+)?"
    rf"(?!(?:just|merely|only|simply)\b)(?:(?:{_STAGING_MODIFIERS})[ \t]+)?(?:{_STAGING})\b"
    r"|not[ \t]+(?:just|merely|only|simply)[ \t]+(?:(?:a|an|the|any)[ \t]+)?"
    rf"(?:(?:{_SPECTATOR_MODIFIERS})[ \t]+)?(?:{_SPECTATOR}))"
    r"(?:[ \t]+of[ \t]+(?:it|them))?"
    r"(?P<end>[ \t]*[.!?]+[\"'”’)]*)$", re.I)


def _staging_tail(s: str, src: frozenset) -> tuple[str, list[str]]:
    m = _STAGING_TAIL_RE.search(s)
    if not m:
        return s, []
    head = s[:m.start()].rstrip(" \t")
    part = m.group(0)
    # a bare 'not' tail only after a comma: 'The dance is not staged.' would read 'The dance is.'
    if part[:3].lower() == "not" and not head.endswith(","):
        return s, []
    head = head.rstrip(",").rstrip()
    # the tail only: 'Excursions are included, and the contact is direct rather than staged.' keeps its rule word
    if (len(_words(head)) < 4 or _protected(part) or _practical(part) or _DIGIT_RE.search(part)
            or _seed_fact(part, src)):
        return s, []
    return head + m.group("end").strip(), ["staging contrast tail"]


# "… the Tsaatan settlements offer something rarer than scenery: a way of life …" / "… offers something specific:
# the chance to …" / "… offers something that no guided commentary can substitute: a person's own account …" — a
# build-up before a colon. The verb keeps what follows the colon as its object: "offer a way of life …".
# S252 ship criterion: the 'than X' comparand goes with the lead, so it may only be a word that names no fact:
# 'something better than air conditioning', 'richer than trekking', 'more relaxed than the trek', 'different than
# the main highway' name an amenity, an activity, an effort level or a road the reader plans by, and stay.
_SOMETHING_COMPARANDS = (r"scenery|sightseeing|views?|glimpses?|photos?|photographs?|postcards?|snapshots?"
                         r"|pretty[ \t]+(?:views?|pictures?)")
_SOMETHING_RE = re.compile(
    r"(?<=[ \t])(?:offers?|offered|gives?|provides?|holds?|brings?|adds?|carries|reveals?|shows?)[ \t]+"
    r"(?P<lead>something[ \t]+(?:(?:rarer|richer|deeper|better|more[ \t]+[\w-]+|other|specific|concrete|particular"
    r"|different|rare|special|unusual|valuable)(?:[ \t]+than[ \t]+(?:(?:a|an|the|just|mere|any)[ \t]+)?"
    rf"(?:{_SOMETHING_COMPARANDS}))?"
    r"|that[ \t]+no[ \t]+(?:[\w'’-]+[ \t]+){1,4}?(?:can|could|will)(?:[ \t]+not|not)?[ \t]+"
    r"(?:substitute|replicate|replace|match|rival)(?:[ \t]+for)?)"
    r"[ \t]*:[ \t]+)(?=(?:a|an|the|its|their|his|her|our|your)\b)", re.I)


def _something_lead(s: str, src: frozenset) -> tuple[str, list[str]]:
    m = _SOMETHING_RE.search(s)
    if not m or _practical(m.group("lead")) or _seed_fact(m.group("lead"), src):
        return s, []
    return s[:m.start("lead")] + s[m.end("lead"):], ["'something …:' build-up"]


_WORD_RULES: tuple[Callable, ...] = (_labels, _not_because, _not_but, _not_tail, _inline_pair, _lead_in,
                                     _staging_tail, _something_lead)


def _rewrite_sentence(clean: str, ctx: TellContext, src: frozenset, *, prose: bool) -> tuple[str, list[str]]:
    s, kinds = clean, []
    s, k = _stock_words(s, ctx)
    kinds += [f"stock word '{w}'" for w in k]
    for rule in _WORD_RULES:
        s, k = rule(s, src)
        kinds += k
    if prose:
        s, k = _semicolons(s)
        kinds += k
    return " ".join(s.split()), kinds


# ---------------------------------------------------------------- sentence rules (pairs, lone denials, verdicts)

_CONTRACTIONS = (
    (re.compile(r"\b(That|This|It|What|There|Here)['’]s\b"), r"\1 is"),
    (re.compile(r"\b(They|These|Those|We|You)['’]re\b"), r"\1 are"),
    (re.compile(r"\b(is|are|was|were)n['’]t\b", re.I), r"\1 not"),
    (re.compile(r"\b(does|do|did)n['’]t\b", re.I), r"\1 not"),
)


def _expand(s: str) -> str:
    for rx, rep in _CONTRACTIONS:
        s = rx.sub(rep, s)
    return s


_DENIAL_RE = re.compile(
    r"^(?P<subj>[^;:!?\n]{1,90}?)[ \t]+(?P<cop>is|are|was|were)[ \t]+(?:not[ \t]+(?:only[ \t]+|just[ \t]+|simply[ \t]+"
    r"|merely[ \t]+)?|more[ \t]+than[ \t]+)(?P<x>[^.;:!?\n]{1,90}?)[ \t]*[.!]$", re.I)
_AFFIRM_RE = re.compile(
    r"^(?P<pro>It|This|That|They|These|Those)[ \t]+(?P<verb>is|are|was|were|remains|remained|stays|stayed|becomes"
    r"|became)[ \t]+(?!not\b)(?P<y>.+)$")
_PLURAL_VERB = {"remains": "remain", "stays": "stay", "becomes": "become"}
_LONE_SUBJ_RE = re.compile(r"^(?:this|that|these|those|it|they|such[ \t]+\w+|what[ \t][^,]{1,40}?)$", re.I)
_VERDICT_RE = re.compile(r"^(?:This|That)(?:[ \t]+(?:kind|sort|type)[ \t]+of[ \t]+\w+)?[ \t]+(?:is|was)[ \t]+"
                         r"(?P<rest>[^?]+?)[.!]$")
_PERSON_RE = re.compile(r"\b(?:you|your|yours|we|our|us)\b", re.I)
# What a verdict's complement opens on: the piece talking about itself ("the opening", "the section worth …", "what
# considered pacing looks like", "part of what makes …", "a good second day") or a one-word judgement ("structure",
# "deliberate"). "This is an offer that depends on the evening." / "This is a working monastery." are not verdicts.
_VERDICT_REST_RE = re.compile(
    r"^(?:the|what|how|why|where|part[ \t]+of|(?:a|an)[ \t]+(?:good|great|real|different|rare|special|useful|fair"
    r"|proper|telling|welcome|whole|fitting)\b|[\w-]+$)", re.I)


def _subject_is_pronoun(subj: str) -> bool:
    return subj.strip().lower() in _PRONOUN_SUBJECTS


def _pair(s1: str, s2: str, src: frozenset) -> Optional[str]:
    """The joined sentence for a denial + affirmation pair, '' to drop the denial only, None when not a pair."""
    d, a = _DENIAL_RE.match(_expand(s1)), _AFFIRM_RE.match(_expand(s2))
    if not d or not a or len(_words(s1)) > 22:
        return None
    # second review: a limit in the denied part ('This is not the only path to the monastery.') says other
    # routes exist, a fact; it is never cut or joined away (the round-2 pairs had the same guard in _denied_ok)
    if (_protected(s1) or _practical(d.group("x")) or _audience(d.group("x")) or _LIMIT_RE.search(d.group("x"))
            or _DIGIT_RE.search(d.group("x")) or _seed_fact(d.group("x"), src)):
        return None
    if _NOT_ONLY_RE.search(d.group(0)):
        # 'This is not just a glacier. It is the valley's only source of water.' asserts the glacier: never cut on its
        # own (the next 'It' then pointed at the sentence before) and never folded into a join (fifth review)
        return KEEP_PAIR
    subj = d.group("subj").strip()
    if _subject_is_pronoun(subj):
        joined = _demonstrative_join(subj, a.group("pro"), a.group("verb"), " " + a.group("y"), copula=True)
        # second review: a join that would be a short verdict-shaped line ('This is the tail of the monsoon.', 'This is
        # the halfway point.') is a correction of which thing it is, not a reframe: both sentences stay (KEEP_PAIR).
        # Joined, the verdict rule cut it on the next pass and both facts were lost.
        return KEEP_PAIR if joined and _verdict_shape(joined) else joined
    verb = a.group("verb")
    plural = d.group("cop").lower() in ("are", "were")
    if verb.lower() in ("is", "are", "was", "were"):
        # the affirmation's tense, the subject's number (round 2 review: 'The bridge is not the original. It was
        # rebuilt.' read 'The bridge is rebuilt.')
        past = verb.lower() in ("was", "were")
        verb = ("were" if plural else "was") if past else ("are" if plural else "is")
    elif plural:
        verb = _PLURAL_VERB.get(verb.lower(), verb)
    return f"{subj} {verb} {a.group('y')}"


_DEMONSTRATIVES = {"this": False, "that": False, "these": True, "those": True}  # → plural
_PRONOUN_PLURAL = {"it": False, "this": False, "that": False, "they": True, "these": True, "those": True}


def _demonstrative_join(subj: str, pro: str, verb: str, y: str, *, copula: bool) -> str:
    """AA-790 round 3 — a demonstrative denial + an affirmation on another pronoun: "This is not a landscaped
    memorial. It is a place where …" → "This is a place where …". Cutting the denial (round 1) left the paragraph
    opening on "It is …" with no referent, so the integrity check refused it and the pair stayed (S252 re-panel).
    The joined sentence keeps the denial's own subject, so its referent is unchanged. '' (cut the denial, as
    before) for the same pronoun twice ('It is not … It is …' cut = join), for 'it' / 'they' / 'such' subjects, or
    when the two pronouns differ in number."""
    s, p = subj.lower(), pro.lower()
    if s not in _DEMONSTRATIVES or s == p or _DEMONSTRATIVES[s] != _PRONOUN_PLURAL.get(p, not _DEMONSTRATIVES[s]):
        return ""
    if copula and verb.lower() in ("is", "are", "was", "were"):
        past = verb.lower() in ("was", "were")
        verb = ("were" if _DEMONSTRATIVES[s] else "was") if past else ("are" if _DEMONSTRATIVES[s] else "is")
    return f"{subj} {verb}{y}"  # ``y`` carries its own end punctuation


# AA-790 round 3: a passive denial states what happened ('They were not saved.' about monasteries destroyed in a
# purge): a fact, never a lone-denial cut. The denied part opens on a past participle.
_PARTICIPLES = frozenset("""built made done known seen taken given kept held lost left found born sold shown spared
rebuilt destroyed""".split())


def _is_participle(word: str) -> bool:
    w = word.lower()
    return w in _PARTICIPLES or (len(w) > 4 and w.endswith("ed"))


def _lone_denial(s: str, src: frozenset) -> bool:
    d = _DENIAL_RE.match(_expand(s))
    if not d or "more than" in d.group(0).lower() or len(_words(s)) > 9 or _NOT_ONLY_RE.search(d.group(0)):
        return False  # fifth review: 'This is not just a glacier.' asserts the glacier
    x_words = d.group("x").split()
    if x_words and _is_participle(x_words[0]):
        return False
    if (_protected(s) or _practical(d.group("x")) or _audience(d.group("x")) or _LIMIT_RE.search(d.group("x"))
            or _DIGIT_RE.search(s)
            or _NAME_AFTER_FIRST_RE.search(s) or _seed_fact(d.group("x"), src)):
        return False
    return bool(_LONE_SUBJ_RE.match(d.group("subj").strip()))


def _verdict_shape(s: str) -> bool:
    """The sentence has a verdict line's shape and length ('This is the halfway point.', 'This is deliberate.'),
    whatever its words: ``_verdict`` without the seed and name checks."""
    m = _VERDICT_RE.match(_expand(s))
    return bool(m) and len(_words(s)) <= 10 and bool(_VERDICT_REST_RE.match(m.group("rest").strip()))


# Third review: 'This is the coldest part of the trek.' / 'This is the wettest month.' / 'This is the only day off.'
# rank a thing on a measurable scale (a practical fact a reader plans by), not the piece judging itself. A closed list
# of -est forms (never 'forest', 'interest', 'harvest'), and most / least + a word that is not a closed evaluative
# judgement ('the most rewarding part' is still a verdict line; 'the most exposed stretch' is a fact).
_SUPERLATIVES = """best worst highest lowest longest shortest hardest easiest coldest hottest warmest coolest wettest
driest busiest quietest calmest oldest newest youngest biggest largest smallest steepest deepest shallowest fastest
quickest slowest nearest closest furthest farthest safest cheapest latest earliest toughest windiest sunniest
rainiest muddiest narrowest widest emptiest fullest"""
_EVALUATIVE = """important striking telling memorable rewarding interesting remarkable significant meaningful beautiful
moving powerful revealing compelling evocative satisfying special impressive affecting atmospheric extraordinary
fascinating intriguing"""
_SUPERLATIVE_RE = re.compile(
    rf"\b(?:{'|'.join(_SUPERLATIVES.split())}|(?:most|least)[ \t]+(?!(?:{'|'.join(_EVALUATIVE.split())})\b)[a-z-]+)\b",
    re.I)


def _superlative_fact(rest: str) -> bool:
    return bool(_SUPERLATIVE_RE.search(rest))


def _verdict(s: str, source_words: frozenset) -> bool:
    m = _VERDICT_RE.match(_expand(s))
    if not m or len(_words(s)) > 10:
        return False
    rest = m.group("rest")
    if not _VERDICT_REST_RE.match(rest.strip()):
        return False
    if (_DIGIT_RE.search(rest) or _NAME_AFTER_FIRST_RE.search(" " + rest) or _PERSON_RE.search(rest) or _protected(s)
            or _practical(rest) or _LIMIT_RE.search(rest) or _superlative_fact(rest)):
        return False
    concrete = [w for w in content_words(rest) if w not in _META_NOUNS]
    return not any(w in source_words for w in concrete)


# ---------------------------------------------------------------- round 2 (S251 re-panel): more denial shapes

# "The steppe does not announce its scale. It builds." / "The crater does not announce itself from the road. It is
# simply there, …" — a denial with 'do'. Only verbs that personify the place or comment on how it presents
# itself: a 'do' denial is often a fact ("The ferry does not run on Sundays.", "The bus does not stop at the falls.",
# "The herders do not live in fixed villages."), so the verb list is closed (precision first).
_DO_DENIAL_RE = re.compile(
    r"^(?P<subj>[^;:!?\n]{1,90}?)[ \t]+(?P<aux>does|do|did)[ \t]+not[ \t]+(?P<verb>[a-z]+)"
    r"(?P<x>[^.;:!?\n]{0,90}?)[ \t]*[.!]$")
_PERFORMATIVE_VERBS = frozenset("""announce advertise shout perform pretend position present frame stage showcase
market sell rush hurry compete try insist beg reveal hide explain impose feel exist""".split())
# Round-2 review: with any object these verbs state a plain fact ("This shop does not sell alcohol.", "The troupe does
# not perform in winter.", "The workshop does not sell to visitors."); only the reflexive form ("does not sell
# itself", "does not market itself") is a tell.
_TRANSACTIONAL_VERBS = frozenset("sell market perform explain compete".split())
# The affirmation after a denial, any verb: 'It builds.', 'It works.', 'They rise from the rock.' Only
# 'It' / 'They': 'This means …' / 'That changes …' point at the whole statement, not at its subject.
_AFFIRM_VERB_RE = re.compile(r"^(?P<pro>It|They)[ \t]+(?P<verb>[a-z]+)(?P<y>(?:[ \t,].*)?)[.!]$")
# What a 'do' denial may deny: the subject itself ('does not announce itself', 'does not shout about itself', 'does
# not advertise its age'), nothing after the verb, or a manner adverb. 'The guide does not explain the ritual. It is
# …' has an object the next 'It' may point at, so it is never joined. Round-2 review: a preposition alone is not
# enough: "do not perform in the rainy season", "does not sell to visitors" are a season or an audience the join
# would drop.
_DO_X_OK_RE = re.compile(r"^(?:$|[ \t]+(?:(?:about|to|for|with|in|on|at|from|by)[ \t]+(?:itself|themselves)"
                         r"|itself|themselves|its|their|his|her|much|anything|everything|loudly|openly|quickly|easily"
                         r"|immediately|all)\b)")
_DO_X_REFLEXIVE_RE = re.compile(r"^[ \t]+(?:(?:about|to|for|with|in|on|at|from|by)[ \t]+)?(?:itself|themselves)\b")
# A lone denial may also frame its object: "does not present the village as a stage", "does not position the town as
# a backdrop".
_FRAMING_VERBS = frozenset("present position frame stage showcase".split())
_FRAMING_AS_RE = re.compile(r"^[ \t]+(?:the|this|that|these|those|its|their|a|an)[ \t]+[\w'’-]+(?:[ \t]+[\w'’-]+){0,2}"
                            r"[ \t]+as[ \t]+(?:a|an)[ \t]")
_NOT_A_VERB = frozenset("""is are was were be been has have had does do did can could will would may might must shall
should not also still never only just remains remained stays stayed becomes became seems seemed""".split())
# A word in Y that points back to what the denial named ('they rise from it' after 'above the ridge'): the join
# would leave it with no referent.
_BACK_REFERENCE = frozenset("it its itself them they their theirs".split())
_SINGULAR_PRONOUNS = frozenset({"it", "this", "that"})


def _lemmas(verb: str) -> frozenset:
    """The base forms ``verb`` may have: 'hides' → {hides, hide, hid}, 'rushes' → {rushes, rushe, rush}, 'carries' →
    {…, carry}. Round-2 review: one stem stripped '-es' from verbs that end in -e ('hides' → 'hid', 'stages' →
    'stag'), so the same-verb guard missed 'does not hide …. It hides …'. Compared as sets: a candidate that is not a
    word matches nothing."""
    v = verb.lower()
    out = {v}
    if v.endswith("ies") and len(v) > 4:
        out.add(v[:-3] + "y")
    if v.endswith("es") and len(v) > 4:
        out.add(v[:-2])
    if v.endswith("s") and not v.endswith("ss") and len(v) > 3:
        out.add(v[:-1])
    return frozenset(out)


def _do_x_ok(verb: str, x: str, *, lone: bool = False) -> bool:
    """The denied part after a 'do' denial's verb may go: see _DO_X_OK_RE / _TRANSACTIONAL_VERBS / _FRAMING_AS_RE."""
    lemmas = _lemmas(verb)
    if lemmas & _TRANSACTIONAL_VERBS:
        return bool(_DO_X_REFLEXIVE_RE.match(x))
    return bool(_DO_X_OK_RE.match(x)) or (lone and bool(lemmas & _FRAMING_VERBS) and bool(_FRAMING_AS_RE.match(x)))


# A denial that states a limit ('does not reveal itself until the last bend') is a fact, not a set-up.
_LIMIT_RE = re.compile(r"\b(?:until|unless|except|before|after|only|without|till)\b", re.I)


def _denied_ok(part: str, s1: str, src: frozenset) -> bool:
    """The denied part may be dropped: no rule / access / practical / limit word, no digit, no name, not the seed's
    own."""
    return not (_protected(s1) or _practical(part) or _audience(part) or _LIMIT_RE.search(part)
                or _DIGIT_RE.search(part) or _NAME_AFTER_FIRST_RE.search(" " + part) or _seed_fact(part, src))


def _verb_in_seed(verb: str, src: frozenset) -> bool:
    """A bare affirmation whose verb the seed uses ('It floods.') is a fact: the pair is left alone."""
    return any(set(content_words(v)) & src for v in _lemmas(verb))


_NOT_ONLY_RE = re.compile(r"\bnot[ \t]+(?:only|just|simply|merely)\b", re.I)
CUT_BOTH = "\x00cut both"  # _pair2: both sentences go (an aphorism pair: 'X is not a ruin. It works.')
KEEP_PAIR = "\x00keep pair"  # _pair: a pair, but both sentences stay (no join, no lone-denial cut)
_CUT_BOTH_KIND = "aphorism pair (denial + bare verb)"


def _pair2(s1: str, s2: str, src: frozenset) -> Optional[str]:
    """Round 2 denial + affirmation pairs the round-1 ``_pair`` does not take: a 'do' denial with a personifying
    verb, or a copula denial of a noun phrase followed by a short non-copula affirmation. Returns the joined
    sentence, '' to drop the denial only (pronoun subject), ``CUT_BOTH`` for a bare aphorism pair ('It
    works.' / 'It builds.': joining would leave 'The temple works.'), or None."""
    e1, e2 = _expand(s1), _expand(s2)
    if len(_words(s1)) > 14 or len(_words(s2)) > 14:
        return None
    a_cop, a_verb = _AFFIRM_RE.match(e2), _AFFIRM_VERB_RE.match(e2)
    d = _DO_DENIAL_RE.match(e1)
    if d:
        if not _lemmas(d.group("verb")) & _PERFORMATIVE_VERBS:
            return None
        if not _do_x_ok(d.group("verb"), d.group("x")) or not _denied_ok(d.group("verb") + d.group("x"), s1, src):
            return None
        # 'It' / 'They' only: 'The town does not hurry. That is the appeal.' is about the statement, not the town
        a = ((a_cop if a_cop and a_cop.group("pro") in ("It", "They") else None)
             or (a_verb if a_verb and a_verb.group("verb") not in _NOT_A_VERB else None))
        if not a:
            return None
        pro = a.group("pro").lower()
        aux = d.group("aux")
        if (aux == "does" and pro not in _SINGULAR_PRONOUNS) or (aux == "do" and pro in _SINGULAR_PRONOUNS):
            return None
        verb, y = a.group("verb"), a.group("y")
        if a is a_verb and _lemmas(verb) & _lemmas(d.group("verb")):
            return None  # 'does not run on Sundays. It runs daily otherwise.' is a restriction, not a set-up
        if _BACK_REFERENCE & {w.lower() for w in _words(y)}:
            return None
        subj = d.group("subj").strip()
        if _subject_is_pronoun(subj):
            return ""
        if a is a_verb and not _words(y):
            return None if _verb_in_seed(verb, src) else CUT_BOTH
        if verb.lower() in ("is", "are", "was", "were"):
            past = verb.lower() in ("was", "were")
            plural = aux == "do" or (aux == "did" and pro not in _SINGULAR_PRONOUNS)
            verb = ("were" if plural else "was") if past else ("are" if plural else "is")
        return f"{subj} {verb}{y}." if a is a_verb else f"{subj} {verb} {y}"
    d = _DENIAL_RE.match(e1)
    if not d or "more than" in d.group(0).lower() or not a_verb or a_verb.group("verb") in _NOT_A_VERB:
        return None
    if _NOT_ONLY_RE.search(d.group(0)):
        return None  # 'is not only a temple. It hosts weddings.': 'not only X' asserts X, the join would drop it
    x_words = d.group("x").split()
    if not x_words or x_words[0].lower() not in ("a", "an", "the") or not _denied_ok(d.group("x"), s1, src):
        return None
    if len(_words(s2)) > 4 or _BACK_REFERENCE & {w.lower() for w in _words(a_verb.group("y"))}:
        return None
    plural = d.group("cop").lower() in ("are", "were")
    if plural != (a_verb.group("pro").lower() not in _SINGULAR_PRONOUNS):
        return None
    if _subject_is_pronoun(d.group("subj").strip()):
        return ""
    if not _words(a_verb.group("y")):
        return None if _verb_in_seed(a_verb.group("verb"), src) else CUT_BOTH
    return f"{d.group('subj').strip()} {a_verb.group('verb')}{a_verb.group('y')}."


def _pronoun_verb_pair(s1: str, s2: str, src: frozenset) -> Optional[str]:
    """AA-790 round 3 — a demonstrative copula denial of a noun phrase + an 'It' / 'They' affirmation with any
    verb and any length: "This is not a single dramatic revelation. It builds across several days, …" → "This
    builds across several days, …". The lone-denial cut left "It builds …" with no referent (refused by the
    integrity check). The denied part goes only (round-2 guards: a/an/the noun phrase, no rule / practical /
    limit word, digit, name or seed fact; never 'not only'); the affirmation is kept whole."""
    e1, e2 = _expand(s1), _expand(s2)
    d, a = _DENIAL_RE.match(e1), _AFFIRM_VERB_RE.match(e2)
    if not d or not a or "more than" in d.group(0).lower() or _NOT_ONLY_RE.search(d.group(0)):
        return None
    if len(_words(s1)) > 12 or a.group("verb") in _NOT_A_VERB or not _words(a.group("y")):
        return None
    x_words = d.group("x").split()
    if not x_words or x_words[0].lower() not in ("a", "an", "the") or not _denied_ok(d.group("x"), s1, src):
        return None
    return _demonstrative_join(d.group("subj").strip(), a.group("pro"), a.group("verb"), a.group("y") + ".",
                               copula=False) or None


# "Bhutan's cultural story does not begin in Thimphu. It begins in the east, where …" — the same verb moved from
# one place to another. Only for an abstract, narrative subject (story, history, heritage …): "The trek does not
# begin in Paro. It begins in Haa." is logistics and is never touched; the verb list is closed.
_NARRATIVE_NOUNS = frozenset("""story stories history histories tale tales narrative narratives heritage significance
meaning appeal interest character identity roots""".split())
_RELOCATE_VERBS = frozenset("begin start end lie live rest".split())
_RELOCATE_X_RE = re.compile(r"^[ \t]+(?P<prep>in|at|on|with|from)[ \t]+[^,;:!?.\n]{1,60}$")


def _relocation_pair(s1: str, s2: str, src: frozenset) -> Optional[str]:
    """The joined sentence ('<subject> begins in the east, …'), or None. The affirmation must repeat the verb and
    open on the same preposition; the denied place may be a name (the point of the frame), but no rule / practical
    / limit word, no digit and not the seed's own words."""
    e1, e2 = _expand(s1), _expand(s2)
    d, a = _DO_DENIAL_RE.match(e1), _AFFIRM_VERB_RE.match(e2)
    if not d or not a or len(_words(s1)) > 14:
        return None
    lemmas = _lemmas(d.group("verb"))
    if not lemmas & _RELOCATE_VERBS or not _lemmas(a.group("verb")) & lemmas:
        return None
    subj_words = _words(d.group("subj"))
    if not subj_words or subj_words[-1].lower() not in _NARRATIVE_NOUNS:
        return None
    mx = _RELOCATE_X_RE.match(d.group("x"))
    if not mx or not a.group("y").lower().startswith(f" {mx.group('prep').lower()} "):
        return None
    aux, pro = d.group("aux"), a.group("pro")
    if (aux == "does" and pro != "It") or (aux == "do" and pro != "They"):
        return None
    part = d.group("verb") + d.group("x")
    if (_protected(s1) or _practical(part) or _LIMIT_RE.search(part) or _DIGIT_RE.search(part)
            or _seed_fact(part, src) or _BACK_REFERENCE & {w.lower() for w in _words(a.group("y"))}):
        return None
    return f"{d.group('subj').strip()} {a.group('verb')}{a.group('y')}."


# "This kind of visit does not present the village as a stage." — a lone 'do' denial with a pronoun or
# 'This (kind of) N' subject and a personifying verb.
_LONE_DO_SUBJ_RE = re.compile(r"^(?:this|that|these|those|it|they|(?:this|that|these|those)(?:[ \t]+(?:kind|sort|type)"
                              r"[ \t]+of)?[ \t]+[a-z]+)$", re.I)


def _lone_do_denial(s: str, src: frozenset) -> bool:
    """Round-2 review: the denied part is checked as in the pair path (``_do_x_ok``, plus the framing 'as a …'
    form), so 'This shop does not sell alcohol.' / 'It does not perform for tourists.' stay."""
    d = _DO_DENIAL_RE.match(_expand(s))
    if not d or len(_words(s)) > 14 or not _lemmas(d.group("verb")) & _PERFORMATIVE_VERBS:
        return False
    if not _do_x_ok(d.group("verb"), d.group("x"), lone=True):
        return False
    return bool(_LONE_DO_SUBJ_RE.match(d.group("subj").strip())) and _denied_ok(d.group("verb") + d.group("x"), s,
                                                                                 src)


# "The order of the stops is not incidental." / "The overlap of two cultures here is not accidental." / "The
# continuity is deliberate." — a verdict on a noun subject: the sentence says nothing but that the thing was meant.
_MEANT_RE = re.compile(
    r"^(?:The|This|That|These|Those|Such|Its|Their)[ \t][^,;:!?\n]{1,60}?[ \t]+(?:is|are|was|were)[ \t]+"
    r"(?:not[ \t]+(?:incidental|accidental|arbitrary|random|a[ \t]+coincidence|an[ \t]+accident|by[ \t]+chance)"
    # AA-790 round 3: 'The position is not incidental to its significance.'
    r"(?:[ \t]+to[ \t]+(?:its|their|the)[ \t]+(?:significance|meaning|importance|story|character|appeal|identity"
    r"|history|role))?"
    r"|no[ \t]+(?:accident|coincidence)|(?:entirely[ \t]+|quite[ \t]+|fully[ \t]+)?(?:deliberate|intentional))[.!]$")


# Round-2 review: a count or an order of days, or a rest day, is pace / schedule information ('The slow pace of the
# first two days is deliberate.', 'The rest day is deliberate.'), like a digit. 'the two cultures' is not.
_SCHEDULE_RE = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|first|second|third|fourth|fifth|last|both"
    r"|half)[ \t]+(?:[a-z]+[ \t]+)?(?:days?|nights?|mornings?|afternoons?|evenings?|weeks?|hours?|stages?|legs?)\b"
    r"|\b(?:rest|free|spare|buffer)[ \t]+(?:days?|mornings?|afternoons?|evenings?)\b|\bacclimati[sz]", re.I)


def _meant_verdict(s: str, src: frozenset) -> bool:
    """A name ('The rest day in Leh is deliberate.', as in _verdict) or a schedule phrase keeps the sentence."""
    return (bool(_MEANT_RE.match(_expand(s))) and len(_words(s)) <= 12 and not _DIGIT_RE.search(s)
            and not _NAME_AFTER_FIRST_RE.search(s) and not _SCHEDULE_RE.search(s)
            and not _protected(s) and not _practical(s) and not _seed_fact(s, src))


# AA-790 round 3 — "That single fact changes how you think about the country." A whole-sentence verdict on the
# sentence before it, about the reader's view ('how you / families / a visit …'); "That fact changes how the
# festival is scheduled." is a fact and stays. Never the first sentence or a section's only one (as every cut).
_CHANGES_VERDICT_RE = re.compile(
    r"^(?:That|This)[ \t]+(?:(?:single|one|simple|small|last)[ \t]+)?(?:fact|detail|number|figure|statistic|point"
    r"|distinction|context|contrast)[ \t]+(?:alone[ \t]+)?(?:changes|reshapes|reframes|shifts|alters|transforms"
    r"|upends)[ \t]+(?:how|what|the[ \t]+way)[ \t]+(?:you|we|a[ \t]+family|families|travell?ers|visitors|people"
    r"|children|a[ \t]+visit|the[ \t]+visit)\b[^.!?\n]{0,80}[.!]$")


def _changes_verdict(s: str) -> bool:
    return (bool(_CHANGES_VERDICT_RE.match(s)) and len(_words(s)) <= 16 and not _DIGIT_RE.search(s)
            and not _protected(s) and not _practical(s))


# "The forts are still guarded. The shrines are still lit. The mills are still turning." — three or more short
# sentences in one paragraph built the same way (same opener, same verb and the word after it). Joined into one
# sentence: nothing is cut, nothing added.
_ANAPHORA_MAX_WORDS = 8


def _anaphora_key(s: str) -> Optional[tuple[str, str, str]]:
    w = _words(s)
    if not 4 <= len(w) <= _ANAPHORA_MAX_WORDS or not s.rstrip().endswith(".") or _DIGIT_RE.search(s):
        return None
    if w[0] not in ("The", "Each", "Every", "Its", "Their", "Some", "Many", "No"):
        return None
    return w[0], w[2].lower(), w[3].lower()


def _lower_first(s: str) -> str:
    """'The temples …' → 'the temples …' when the first word is the shared opener (never a name)."""
    return s[:1].lower() + s[1:]


def _anaphora_edits(text: str, ctx: TellContext, written: frozenset = frozenset()) -> list[Edit]:
    spans = sentence_spans(text)
    out: list[Edit] = []
    i = 0
    while i < len(spans):
        sp = spans[i]
        key = (_anaphora_key(sp["clean"]) if sp["kind"] == "prose" and not sp["in_faq"] and sp["clean"] not in written
               else None)
        j = i
        while (key and j + 1 < len(spans) and spans[j + 1]["kind"] == "prose" and not spans[j + 1]["in_faq"]
               and spans[j + 1]["clean"] not in written
               and "\n" not in text[spans[j]["end"]:spans[j + 1]["start"]]
               and _anaphora_key(spans[j + 1]["clean"]) == key and not _is_cta(spans[j + 1]["clean"], ctx)):
            j += 1
        if key and i == _first_prose(spans):
            i = j + 1  # a run that opens the piece is its hook: none of it is joined (round-2 review)
            continue
        if key and j - i >= 2 and not _is_cta(sp["clean"], ctx):
            run = [s["clean"].rstrip().rstrip(".") for s in spans[i:j + 1]]
            joined = ", ".join([run[0]] + [_lower_first(r) for r in run[1:-1]]) + " and " + _lower_first(run[-1]) + "."
            tags = _tags(" ".join(s["text"] for s in spans[i:j + 1]))
            out.append(Edit(sp["start"], spans[j]["end"], _with_tags(joined, tags), "anaphora run joined",
                            " ".join(s["clean"] for s in spans[i:j + 1]), tuple(s["clean"] for s in spans[i:j + 1]),
                            joined))
            i = j + 1
            continue
        i += 1
    return out


# ---------------------------------------------------------------- headings and the CTA

def _heading_title(raw: str) -> str:
    return raw.lstrip("#").strip()


def _angle_heading(title: str, angle_words: set) -> Optional[str]:
    """The heading without the angle's label ('' to drop it), or None when it does not carry the label."""
    if len(angle_words) < 2:
        return None
    parts = [p.strip() for p in title.split(":")]

    def carries(p: str) -> bool:
        return len(set(content_words(p)) & angle_words) >= max(2, round(0.6 * len(angle_words)))
    hit = [carries(p) for p in parts]
    if not any(hit):
        return None
    kept = [p for p, h in zip(parts, hit) if not h and p]
    return ": ".join(kept)


# AA-790 round 3 — "## Northern Mongolia and the Logic of Convergence": the angle's best final style said "the idea
# of convergence … as the organising logic"; the writer lifted two brief words into a heading and the angle-name
# check (_angle_heading) did not see it (the angle's NAME was different). A heading part whose words are the brief's
# and not the seed's is a brief label: the part goes, the place stays.
_AND_THE_RE = re.compile(r"(?<![ \t])[ \t]+and[ \t]+(?=the[ \t])", re.I)  # starts where a blank run starts


def _brief_heading(title: str, brief_words: set, src_words: frozenset) -> Optional[str]:
    """The heading without its brief-label part ('Northern Mongolia and the Logic of Convergence' → 'Northern
    Mongolia'), or None. A part (split at ':' and at ' and the ') is a brief label when at least two of its content
    words are not in the seed + Facts and every such word is in the chosen angle's text. Never empties a heading."""
    if not brief_words:
        return None
    parts, cut = [], False
    for part in title.split(":"):
        kept = []
        for sub in _AND_THE_RE.split(part.strip()):
            novel = [w for w in content_words(sub) if w not in src_words]
            if len(novel) >= 2 and all(w in brief_words for w in novel):
                cut = True
                continue
            kept.append(sub)
        if kept:
            parts.append(" and ".join(kept))
    return ": ".join(parts) if cut and parts else None


def _cta_heading(title: str, cta: Optional[str]) -> bool:
    t, c = cta_norm(title), cta_norm(cta)
    return bool(t) and bool(c) and len(t.split()) >= 2 and t in c


_CLAUSE_SPLIT_RE = re.compile(r"(,[ \t]+and[ \t]+|;[ \t]+)")


def _cta_restating_clause(sentence: str, cta: Optional[str], ignore: frozenset) -> Optional[str]:
    """The sentence without its last clause when that clause restates the CTA ('…, and our travel designers are
    ready to shape an itinerary…' before 'Speak with our travel designers to shape your private journey.')."""
    cta_words = set(content_words(cta or "", ignore))
    if len(cta_words) < 3:
        return None
    parts = _CLAUSE_SPLIT_RE.split(sentence)
    if len(parts) < 3:
        return None
    last = parts[-1]
    shared = set(content_words(last, ignore)) & cta_words
    if len(shared) < 3 or len(shared) < 0.5 * len(cta_words):
        return None
    head = "".join(parts[:-2]).rstrip(" ,;")
    end = re.search(r"[.!?]+[\"'”’)]*$", sentence)
    return head + (end.group(0) if end else ".")


# ---------------------------------------------------------------- edits

def _is_cta(sentence: str, ctx: TellContext) -> bool:
    return bool(ctx.cta) and (hygiene.has_cta_literal(sentence, ctx.cta) or hygiene._is_cta(sentence, ctx.cta))


def _first_prose(spans: list[dict]) -> Optional[int]:
    return next((i for i, sp in enumerate(spans) if sp["kind"] == "prose" and not sp["in_faq"]), None)


class _SourceWords(frozenset):
    """The seed + Facts content words (a frozenset, as every rule reads it) that also carry the source text, for the
    one rule that must see the seed's own wording (``_seed_denies``)."""
    text: str = ""

    def __new__(cls, text: str):
        obj = super().__new__(cls, content_words(text or ""))
        obj.text = text or ""
        return obj


def _source_words(ctx: TellContext) -> frozenset:
    return _SourceWords(ctx.source or "")


def _sentence_edits(text: str, ctx: TellContext, written: frozenset = frozenset()) -> list[Edit]:
    out = []
    src = _source_words(ctx)
    for sp in sentence_spans(text):
        if sp["kind"] not in ("prose", "bullet", "faq_a") or _is_cta(sp["clean"], ctx) or sp["clean"] in written:
            continue
        new, kinds = _rewrite_sentence(sp["clean"], ctx, src, prose=sp["kind"] == "prose" and not sp["in_faq"])
        if kinds and new != sp["clean"] and len(_words(new)) >= 3:
            out.append(Edit(sp["start"], sp["end"], _with_tags(new, _tags(sp["text"])), "; ".join(kinds),
                            sp["clean"], (sp["clean"],), new))
    return out


def _deletion_end(text: str, sp: dict, nxt: Optional[dict]) -> tuple[int, int]:
    """[start, end) to cut a sentence: its trailing spaces when another sentence follows on the line, else the
    spaces before it."""
    start, end = sp["start"], sp["end"]
    if nxt is not None and "\n" not in text[end:nxt["start"]]:
        return start, nxt["start"]
    while start > 0 and text[start - 1] in " \t":
        start -= 1
    return start, end


def _section_has_other_prose(spans: list[dict], i: int, also: Optional[int] = None) -> bool:
    """True when the heading section of spans[i] keeps another prose sentence after it (and spans[also], when
    given) is cut (a cut never empties a section: tidy_after_cut would drop its heading)."""
    lo = i
    while lo > 0 and spans[lo - 1]["kind"] != "heading":
        lo -= 1
    hi = i
    while hi + 1 < len(spans) and spans[hi + 1]["kind"] != "heading":
        hi += 1
    return any(spans[k]["kind"] in ("prose", "bullet") for k in range(lo, hi + 1) if k not in (i, also))


def _structure_edits(text: str, ctx: TellContext, written: frozenset = frozenset()) -> list[Edit]:
    """Joins and cuts of whole sentences. ``written`` holds the sentences an earlier pass of this rewrite wrote:
    none of them is ever cut or used as the denial of a pair (second review: the second structure pass cut the
    demonstrative join 'This is the tail of the monsoon.' as a verdict line, so both facts of the pair went). A
    written sentence may still be the affirmation a join keeps word for word ('It is a meeting with people who …,
    something rare.' after the stock-word pass dropped 'genuinely')."""
    spans = sentence_spans(text)
    prose = [i for i, sp in enumerate(spans) if sp["kind"] == "prose" and not sp["in_faq"]]
    first = _first_prose(spans)
    source_words = _source_words(ctx)
    out: list[Edit] = []
    used: set[int] = set()
    for n, i in enumerate(prose):
        if i in used or spans[i]["clean"] in written:
            continue
        sp = spans[i]
        if _is_cta(sp["clean"], ctx):
            continue
        j = prose[n + 1] if n + 1 < len(prose) else None
        same_para = j is not None and "\n" not in text[sp["end"]:spans[j]["start"]]
        if same_para and j not in used and not _is_cta(spans[j]["clean"], ctx):
            joined = _pair(sp["clean"], spans[j]["clean"], source_words)
            join_kind = "denial + affirmation joined"
            if joined is None:  # AA-790 round 2: 'do' denials and short verb affirmations
                joined = _pair2(sp["clean"], spans[j]["clean"], source_words)
                join_kind = "denial + verb affirmation joined"
            if not joined:  # AA-790 round 3: a moved narrative verb, a demonstrative + a long verb affirmation
                for fn, kind in ((_relocation_pair, "denial + relocation joined"),
                                 (_pronoun_verb_pair, "denial + verb affirmation joined")):
                    alt = fn(sp["clean"], spans[j]["clean"], source_words)
                    if alt:
                        joined, join_kind = alt, kind
                        break
            if joined == KEEP_PAIR:
                used.update({i, j})
                continue
            if joined == CUT_BOTH:
                if i != first and spans[j]["clean"] not in written and _section_has_other_prose(spans, i, also=j):
                    nxt = spans[j + 1] if j + 1 < len(spans) else None
                    s, _ = _deletion_end(text, sp, spans[j])
                    _, e = _deletion_end(text, spans[j], nxt)
                    out.append(Edit(s, e, "", _CUT_BOTH_KIND,
                                    f"{sp['clean']} {spans[j]['clean']}", (sp["clean"], spans[j]["clean"])))
                    used.update({i, j})
                continue
            if joined == "" and i != first:
                s, e = _deletion_end(text, sp, spans[j])
                out.append(Edit(s, e, "", "denial before its affirmation", sp["clean"], (sp["clean"],)))
                used.update({i, j})
                continue
            if joined:
                tags = _tags(sp["text"] + " " + spans[j]["text"])
                out.append(Edit(sp["start"], spans[j]["end"], _with_tags(_cap(joined), tags),
                                join_kind, f"{sp['clean']} {spans[j]['clean']}",
                                (sp["clean"], spans[j]["clean"]), _cap(joined)))
                used.update({i, j})
                continue
        if i == first or not _section_has_other_prose(spans, i):
            continue
        kind = ("lone denial" if _lone_denial(sp["clean"], source_words) or _lone_do_denial(sp["clean"], source_words)
                else "verdict line" if (_verdict(sp["clean"], source_words) or _meant_verdict(sp["clean"], source_words)
                                        or _changes_verdict(sp["clean"]))
                else None)
        if kind:
            nxt = spans[i + 1] if i + 1 < len(spans) else None
            s, e = _deletion_end(text, sp, nxt)
            out.append(Edit(s, e, "", kind, sp["clean"], (sp["clean"],)))
            used.add(i)
    return out


def _heading_edits(text: str, ctx: TellContext, written: frozenset = frozenset()) -> list[Edit]:
    spans = sentence_spans(text)
    angle_words = set(content_words(ctx.angle_name or "", ctx.ignore))
    brief_words = set(content_words(" ".join(t for t in (ctx.angle_name, ctx.angle_text) if t), ctx.ignore))
    src_words = _source_words(ctx)
    out: list[Edit] = []
    for k, sp in enumerate(spans):
        if sp["kind"] == "heading" and not sp["in_faq"]:
            raw = sp["text"]
            title = _heading_title(raw)
            if _cta_heading(title, ctx.cta):
                out.append(Edit(sp["start"], sp["end"], "", "heading repeats the CTA", title, (title,), heading=True))
                continue
            marks = raw[:len(raw) - len(raw.lstrip("#"))]
            new = _angle_heading(title, angle_words)
            if new is not None:
                out.append(Edit(sp["start"], sp["end"], f"{marks} {new}" if new else "", "angle label as heading",
                                title, (title,), new, heading=True))
                continue
            new = _brief_heading(title, brief_words, src_words)
            if new:
                out.append(Edit(sp["start"], sp["end"], f"{marks} {new}", "brief words as heading", title, (title,),
                                new, heading=True))
        elif sp["kind"] == "prose" and not sp["in_faq"] and _is_cta(sp["clean"], ctx):
            prev = next((spans[p] for p in range(k - 1, -1, -1) if spans[p]["kind"] in ("prose", "heading")), None)
            # only the literal CTA protects it here: a sentence that restates the CTA is CTA-like by design
            if (prev is None or prev["kind"] != "prose" or prev["clean"] in written
                    or hygiene.has_cta_literal(prev["clean"], ctx.cta)):
                continue
            cut = _cta_restating_clause(prev["clean"], ctx.cta, ctx.ignore)
            if cut and len(_words(cut)) >= 5:
                out.append(Edit(prev["start"], prev["end"], _with_tags(cut, _tags(prev["text"])),
                                "clause restates the CTA", prev["clean"], (prev["clean"],), cut))
    return out


def _apply(text: str, edits: list[Edit]) -> str:
    out, pos = [], 0
    for e in sorted(edits, key=lambda e: e.start):
        if e.start < pos:
            continue  # overlapping edit: the earlier one wins
        out.append(text[pos:e.start])
        out.append(e.text)
        pos = e.end
    out.append(text[pos:])
    return tidy_after_cut("".join(out))


def _quoted(problem: str) -> tuple[str, str]:
    """(why, sentence) of an integrity problem "<why>: '<sentence>'" (no regex: linear on any input)."""
    why, sep, rest = problem.partition(": '")
    return (why, rest[:-1]) if sep and rest.endswith("'") else ("", "")


def _new_problems(before: str, after: str, edits: list[Edit], ctx: TellContext, base: list) -> list[str]:
    """``hygiene.integrity_problems(before, after)`` without the ones an edited sentence already had under its old
    words: the check keys a sentence by its text, so dropping 'genuinely' from a sentence that already opened on
    'This' with no referent reads as a new dangling opener (S251 pilot d99c621c). ``base`` is filled once with
    the problems ``before`` has on its own (``integrity_problems('', before)``)."""
    found = hygiene.integrity_problems(before, after, ctx.ignore, ctx.cta)
    if not found:
        return []
    if not base:
        base.append(set(hygiene.integrity_problems("", before, ctx.ignore, ctx.cta)))
    known = base[0]
    out = []
    for p in found:
        why, quoted = _quoted(p)
        olds = [o for e in edits if e.new and why and quoted == e.new[:200] for o in e.olds]
        if any(f"{why}: '{o[:200]}'" in known for o in olds):
            continue
        out.append(p)
    return out


def _cut_both_subject(e: Edit) -> Optional[str]:
    """The head noun of the denial's subject of an aphorism-pair cut ('The old forge is not a relic. It works.' →
    'forge'), or None (a pronoun, a stop word or a generic word)."""
    if e.kind != _CUT_BOTH_KIND or not e.olds:
        return None
    ex = _expand(e.olds[0])
    d = _DO_DENIAL_RE.match(ex) or _DENIAL_RE.match(ex)
    words = _words(d.group("subj")) if d else []
    head = hygiene.fold(words[-1]).strip("'’") if words else ""
    if len(head) <= 2 or head in hygiene._STOP or head in _PRONOUN_SUBJECTS or head in hygiene._FACT_GENERIC:
        return None
    return head


def _edit_facts_lost(e: Edit, after_forms, seed: frozenset) -> list[str]:
    lost = hygiene.missing_facts(list(e.olds or (e.before,)), e.new, after_forms, seed, heading=e.heading)
    # fifth review: an aphorism-pair cut takes the denial's subject out with both sentences ('The old forge is not a
    # relic. It works.'); it stands only while the text still names that subject elsewhere
    head = _cut_both_subject(e)
    if head and head not in lost and not any(f in after_forms for f in hygiene._forms(head)):
        lost = lost + [head]
    return lost


def _fact_losers(edits: list[Edit], after: str, seed: frozenset) -> dict[int, list[str]]:
    """The fact-preservation invariant (round 3, S252; ``hygiene.missing_facts``): {index in ``edits``: the facts
    it drops} for every edit whose removed words hold a fact token the text ``after`` all edits no longer carries.
    One ``word_forms`` of ``after`` plus each edit's own words: linear in the text."""
    forms = hygiene.word_forms(after)
    out = {}
    for k, e in enumerate(edits):
        lost = _edit_facts_lost(e, forms, seed)
        if lost:
            out[k] = lost
    return out


class _FormsAfter:
    """The word forms of the text after one more edit, read from running counts (``in`` only): the one-at-a-time
    path checks each edit in O(its own words), so the whole check stays linear in the text."""

    def __init__(self, counts: dict, minus: dict, plus: dict):
        self.counts, self.minus, self.plus = counts, minus, plus

    def __contains__(self, form: str) -> bool:
        return self.counts.get(form, 0) - self.minus.get(form, 0) + self.plus.get(form, 0) > 0


def _accept(text: str, edits: list[Edit], ctx: TellContext,
            seed: frozenset = frozenset()) -> tuple[str, list[Edit], list[tuple[Edit, str]]]:
    """Apply the edits all at once when the result is intact and keeps every fact; else one at a time, keeping each
    edit that leaves no new integrity problem and drops no fact (``_fact_losers``: the arbiter over every rule, so
    no chain of rules can delete a fact). Returns (text, kept, [(skipped edit, why)])."""
    if not edits:
        return text, [], []
    base: list = []
    after = _apply(text, edits)
    if not _fact_losers(edits, after, seed) and not _new_problems(text, after, edits, ctx, base):
        return after, list(edits), []
    kept: list[Edit] = []
    skipped: list[tuple[Edit, str]] = []
    counts = hygiene._form_counts([text])  # the kept text's word forms, kept current edit by edit
    for e in sorted(edits, key=lambda e: e.start):
        minus, plus = hygiene._form_counts(list(e.olds or (e.before,))), hygiene._form_counts([e.new])
        lost = _edit_facts_lost(e, _FormsAfter(counts, minus, plus), seed)
        if lost:
            skipped.append((e, f"drops a fact: {', '.join(lost[:6])}"))
            continue
        trial = kept + [e]
        if _new_problems(text, _apply(text, trial), trial, ctx, base):
            skipped.append((e, "breaks the text"))
            continue
        kept = trial
        for f, n in minus.items():
            counts[f] = counts.get(f, 0) - n
        for f, n in plus.items():
            counts[f] = counts.get(f, 0) + n
    return _apply(text, kept), kept, skipped


_PASSES: tuple[Callable, ...] = (_sentence_edits, _structure_edits, _structure_edits, _anaphora_edits, _heading_edits)


def rewrite(text: str, ctx: TellContext) -> tuple[str, list[str]]:
    """The deterministic AI-tell pass. Returns (text, log): one ``ai_tells: <kind>: '<sentence>'`` line per kept
    edit and one ``ai_tells: skipped <kind> (<why>): …`` line per edit refused: it breaks the text (the integrity
    check) or drops a fact (round 3, S252: the fact-preservation invariant, ``_fact_losers``; every rule proposes,
    the invariant decides). Text with no tell comes back unchanged, byte for byte."""
    log: list[str] = []
    cur = text or ""
    seed = hygiene.source_forms(ctx.source)
    written: set[str] = set()  # the sentences a pass wrote: no later pass acts on them (second review)
    for make in _PASSES:
        edits = make(cur, ctx, frozenset(written))
        if not edits:
            continue
        cur, kept, skipped = _accept(cur, edits, ctx, seed)
        # third review: a semicolon split writes two sentences in one edit; each is a written sentence of its own
        written.update(e.new for e in kept if e.new)
        written.update(sp["clean"] for e in kept if e.new for sp in sentence_spans(e.new))
        log += [f"ai_tells: {e.kind}: '{e.before[:200]}'" for e in kept]
        log += [f"ai_tells: skipped {e.kind} ({why}): '{e.before[:200]}'" for e, why in skipped]
    log = list(dict.fromkeys(log))  # a refused edit is proposed again by the second structure pass
    return (cur, log) if cur != (text or "") else (text, [ln for ln in log if "skipped" in ln])


def residue(text: str) -> list[tuple[str, str]]:
    """The tells code does not rewrite (``hygiene.AI_TELL_LINE_RES``): [(phrase, sentence)]. F15_voice reads the
    same list through ``hygiene.template_lines``."""
    return [(p, s) for p, s in hygiene.template_lines(text) if any(rx.search(s) for rx in hygiene.AI_TELL_LINE_RES)]


def census(text: str, ctx: TellContext) -> dict:
    """Counts for the eval script and the tests: what ``rewrite`` would change, by kind, plus the residue and the
    sentence lengths (prose, FAQ out)."""
    _, log = rewrite(text, ctx)
    kinds: dict[str, int] = {}
    for line in log:
        if "skipped" in line:
            continue
        k = line.split(": ", 2)[1]
        k = "stock word" if k.startswith("stock word") else k
        for part in k.split("; "):
            part = "stock word" if part.startswith("stock word") else part
            kinds[part] = kinds.get(part, 0) + 1
    prose = [sp["clean"] for sp in sentence_spans(text or "") if sp["kind"] == "prose" and not sp["in_faq"]]
    lengths = [len(_words(s)) for s in prose]
    return {"version": AI_TELLS_VERSION, "fixable": kinds, "fixable_total": sum(kinds.values()),
            "residue": len(residue(text)), "sentences": len(lengths),
            "mean_words": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
            "over_35_words": sum(n > 35 for n in lengths)}


# ---------------------------------------------------------------- LinkedIn slate diversity

_SOURCE_LINE_RE = re.compile(r"^Source itinerary:[ \t]*(.+)$", re.M)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])[ \t]+(?=[A-Z])")


def seed_facts(seed: Optional[str]) -> list[str]:
    """The seed's own fact lines, in order: the sentences of every 'Source itinerary:' line, then the 'Place
    background' bullets (atom_seed.compose_seed's line contract). 'Other trips' lines are never facts of this trip."""
    own, _ = hygiene.split_seed(seed)
    facts: list[str] = []
    for m in _SOURCE_LINE_RE.finditer(own):
        facts += [s.strip() for s in _SENT_SPLIT_RE.split(m.group(1)) if len(_words(s)) >= 5]
    in_bg = False
    for line in own.splitlines():
        if line.startswith("Place background"):
            in_bg = True
            continue
        if in_bg and line.startswith("- "):
            if len(_words(line)) >= 5:
                facts.append(line[2:].strip())
            continue
        in_bg = False
    return list(dict.fromkeys(facts))


def lead_focus(seed: Optional[str], index: int) -> Optional[str]:
    """The seed fact the ``index``-th write of one request leads with (0 = the first write: free choice, None).
    Repeated writes on one request share one atom and one angle; without this the S251 pilot's six Terra LinkedIn
    posts all opened on 'Years of practice on a single craft'. Deterministic, so writes started together still
    differ."""
    facts = seed_facts(seed)
    if index <= 0 or len(facts) < 2:
        return None
    return facts[index % len(facts)]


__all__ = ["AI_TELLS_VERSION", "ANGLE_BRIEF_RULE", "BLOG_HEADING_RULE", "PROMPT_RULES", "SOCIAL_AUDIENCE_RULE",
           "TRAVELLER_RETURN_LINE", "VISIT_DETAIL_RULE", "TellContext", "angle_text", "brand_focus_points",
           "brand_focus_rule", "brand_voice_rule", "census", "lead_focus", "residue", "rewrite", "seed_facts"]
