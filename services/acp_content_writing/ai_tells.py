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
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from services.acp_content_writing import hygiene
from services.acp_content_writing.hygiene import content_words, cta_norm, fold, sentence_spans, tidy_after_cut
from services.acp_shared.citation_tags import TAG

AI_TELLS_VERSION = "ai_tells.v1"

# ---------------------------------------------------------------- the brief (rendered into SYSTEM_PROMPT)

PROMPT_RULES = """- AI TELLS (an editor must not be able to tell a model wrote this piece):
  * Never set a claim up by denying another. No "X is not A. It is B.", "not A but B", "not because A, but
    because B", "X is more than A. It is B.", "These are not A." Say what the thing is, once, directly.
  * No verdict lines: a short sentence that comments on the piece or the moment instead of telling the reader
    something ("That's the opening.", "This is the section worth slowing down for.", "The geology is the thing
    itself."). Every sentence carries a fact from the content seed, a place, or what the reader does there.
  * No template openers: "There is a particular quality to ...", "X has a way of ...", "If there is one place
    ..., it is ...".
  * Never write genuinely, genuine, truly, precisely, crucially, importantly, notably, "earns its ...", "rewards
    those who ...", "worth slowing down for".
  * Short, varied sentences: most under 25 words, none over 40. Split a sentence that strings more than three
    clauses together, and never join two sentences with a semicolon.
  * Name a place once in full, then use the name. Never write an internal label such as "referred to throughout
    as" or "hereafter".
  * Say the call to action once. No heading and no lead-in sentence that repeats its words, and add nothing to
    it (no place name, no "today")."""

# Blog only (both format variants): the chosen angle is a brief, never a heading.
BLOG_HEADING_RULE = """- Every "## " heading names the place or what happens there. Never use the SELECTED ANGLE's name
  (or most of its words) as a heading or a title: it is an internal brief label, not copy."""

# Non-blog channels: the audience in the post, from the seed only (S251 panel: Terra LinkedIn 5.25 voice).
SOCIAL_AUDIENCE_RULE = """
AUDIENCE IN THE POST: write to the AUDIENCE of the BRAND block. Say what they see or do at this place, using
only what the CONTENT SEED states they see or do; when it states nothing of the kind, say why the place matters to
them in the seed's own facts. A post that only defines the place, like an encyclopedia entry, is not finished."""

# ---------------------------------------------------------------- context

# Words a negation or a verdict must not hold to be cut: they state a rule, an access fact or a service. ("It is not
# allowed to swim.", "Lunch is not included.", "The road is not open in winter, not in summer.")
_PROTECTED_RE = re.compile(
    r"\b(?:allow(?:ed|s)?|permit(?:ted|s)?|possible|suitable|safe|unsafe|included|includ(?:es|ing)|available|"
    r"required|requires?|recommended|open|closed|optional|guaranteed|offered|accessible|necessary|must|"
    r"forbidden|prohibited|banned|restricted|dangerous|risk)\b", re.I)
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
    ``ignore`` the brand-name words the integrity check skips (``hygiene.brand_words``)."""
    cta: Optional[str] = None
    angle_name: Optional[str] = None
    source: str = ""
    keep_words: frozenset = frozenset()
    ignore: frozenset = frozenset()


@dataclass(frozen=True)
class Edit:
    """One change: text[start:end] → ``text``. ``before`` is the sentence(s) it changes (for the log), ``olds`` the
    clean sentences it replaces and ``new`` the clean sentence it writes ('' for a cut), so a problem the old
    sentence already had is not read as new."""
    start: int
    end: int
    text: str
    kind: str
    before: str
    olds: tuple = ()
    new: str = ""


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
    if not m or not s[:m.start()].strip() or _protected(s) or _seed_fact(m.group(0), src):
        return s, []
    return s[:m.start()] + " because" + s[m.end():], ["not because … but because"]


# "is not a sequence of separate attractions but a coherent argument" → "is a coherent argument";
# "set not by a calendar but by the forest" → "set by the forest". Only after a copula (the frame "is not X but Y")
# or when X and Y open on the same preposition; "not only / just X but (also) Y" is ordinary English and stays.
_NOT_BUT_RE = re.compile(
    r"\b(?P<lead>(?:is|are|was|were|be|been|being)[ \t]+)?not[ \t]+"
    r"(?!only\b|just\b|merely\b|simply\b|because\b|that\b|even\b|least\b|much\b|many\b|enough\b|anything\b"
    r"|nothing\b|everything\b|all\b|to\b)"
    r"(?P<x>[^.,;:!?()\"“”\n]{1,70}?)[ \t]+but[ \t]+(?:rather[ \t]+)?(?!also\b|not\b|only\b)(?P<y>\w+)", re.I)
_PREPOSITIONS = frozenset("by for with in on at from through to into as about".split())
_DETERMINERS = frozenset("a an the this that these those its their his her our your one two three some each every "
                         "something someone".split())


def _not_but(s: str, src: frozenset) -> tuple[str, list[str]]:
    if _protected(s):
        return s, []
    done: list[str] = []
    for _ in range(3):
        hit = None
        for m in _NOT_BUT_RE.finditer(s):
            x_words = m.group("x").split()
            if not x_words or len(x_words) > 9 or _seed_fact(m.group("x"), src):
                continue
            y = m.group("y").lower()
            same_prep = x_words[0].lower() in _PREPOSITIONS and x_words[0].lower() == y
            copula = bool(m.group("lead")) and (y in _DETERMINERS or len(x_words) == 1)
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
    if (not m or _protected(m.group("subj") + " " + m.group("x")) or _DIGIT_RE.search(m.group("x"))
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


_WORD_RULES: tuple[Callable, ...] = (_labels, _not_because, _not_but, _not_tail, _inline_pair)


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
    if _protected(s1) or _DIGIT_RE.search(d.group("x")) or _seed_fact(d.group("x"), src):
        return None
    subj = d.group("subj").strip()
    if _subject_is_pronoun(subj):
        return ""
    verb = a.group("verb")
    plural = d.group("cop").lower() in ("are", "were")
    if verb.lower() in ("is", "are", "was", "were"):
        verb = d.group("cop").lower()
    elif plural:
        verb = _PLURAL_VERB.get(verb.lower(), verb)
    return f"{subj} {verb} {a.group('y')}"


def _lone_denial(s: str, src: frozenset) -> bool:
    d = _DENIAL_RE.match(_expand(s))
    if not d or "more than" in d.group(0).lower() or len(_words(s)) > 9:
        return False
    if _protected(s) or _DIGIT_RE.search(s) or _NAME_AFTER_FIRST_RE.search(s) or _seed_fact(d.group("x"), src):
        return False
    return bool(_LONE_SUBJ_RE.match(d.group("subj").strip()))


def _verdict(s: str, source_words: frozenset) -> bool:
    m = _VERDICT_RE.match(_expand(s))
    if not m or len(_words(s)) > 10:
        return False
    rest = m.group("rest")
    if not _VERDICT_REST_RE.match(rest.strip()):
        return False
    if _DIGIT_RE.search(rest) or _NAME_AFTER_FIRST_RE.search(" " + rest) or _PERSON_RE.search(rest) or _protected(s):
        return False
    concrete = [w for w in content_words(rest) if w not in _META_NOUNS]
    return not any(w in source_words for w in concrete)


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


def _source_words(ctx: TellContext) -> frozenset:
    return frozenset(content_words(ctx.source or ""))


def _sentence_edits(text: str, ctx: TellContext) -> list[Edit]:
    out = []
    src = _source_words(ctx)
    for sp in sentence_spans(text):
        if sp["kind"] not in ("prose", "bullet", "faq_a") or _is_cta(sp["clean"], ctx):
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


def _section_has_other_prose(spans: list[dict], i: int) -> bool:
    """True when the heading section of spans[i] keeps another prose sentence after it is cut (a cut never
    empties a section: tidy_after_cut would drop its heading)."""
    lo = i
    while lo > 0 and spans[lo - 1]["kind"] != "heading":
        lo -= 1
    hi = i
    while hi + 1 < len(spans) and spans[hi + 1]["kind"] != "heading":
        hi += 1
    return any(spans[k]["kind"] in ("prose", "bullet") for k in range(lo, hi + 1) if k != i)


def _structure_edits(text: str, ctx: TellContext) -> list[Edit]:
    spans = sentence_spans(text)
    prose = [i for i, sp in enumerate(spans) if sp["kind"] == "prose" and not sp["in_faq"]]
    first = _first_prose(spans)
    source_words = _source_words(ctx)
    out: list[Edit] = []
    used: set[int] = set()
    for n, i in enumerate(prose):
        if i in used:
            continue
        sp = spans[i]
        if _is_cta(sp["clean"], ctx):
            continue
        j = prose[n + 1] if n + 1 < len(prose) else None
        same_para = j is not None and "\n" not in text[sp["end"]:spans[j]["start"]]
        if same_para and j not in used and not _is_cta(spans[j]["clean"], ctx):
            joined = _pair(sp["clean"], spans[j]["clean"], source_words)
            if joined == "" and i != first:
                s, e = _deletion_end(text, sp, spans[j])
                out.append(Edit(s, e, "", "denial before its affirmation", sp["clean"]))
                used.update({i, j})
                continue
            if joined:
                tags = _tags(sp["text"] + " " + spans[j]["text"])
                out.append(Edit(sp["start"], spans[j]["end"], _with_tags(_cap(joined), tags),
                                "denial + affirmation joined", f"{sp['clean']} {spans[j]['clean']}",
                                (sp["clean"], spans[j]["clean"]), _cap(joined)))
                used.update({i, j})
                continue
        if i == first or not _section_has_other_prose(spans, i):
            continue
        kind = ("lone denial" if _lone_denial(sp["clean"], source_words)
                else "verdict line" if _verdict(sp["clean"], source_words) else None)
        if kind:
            nxt = spans[i + 1] if i + 1 < len(spans) else None
            s, e = _deletion_end(text, sp, nxt)
            out.append(Edit(s, e, "", kind, sp["clean"]))
            used.add(i)
    return out


def _heading_edits(text: str, ctx: TellContext) -> list[Edit]:
    spans = sentence_spans(text)
    angle_words = set(content_words(ctx.angle_name or "", ctx.ignore))
    out: list[Edit] = []
    for k, sp in enumerate(spans):
        if sp["kind"] == "heading" and not sp["in_faq"]:
            raw = sp["text"]
            title = _heading_title(raw)
            if _cta_heading(title, ctx.cta):
                out.append(Edit(sp["start"], sp["end"], "", "heading repeats the CTA", title))
                continue
            new = _angle_heading(title, angle_words)
            if new is not None:
                marks = raw[:len(raw) - len(raw.lstrip("#"))]
                out.append(Edit(sp["start"], sp["end"], f"{marks} {new}" if new else "", "angle label as heading",
                                title))
        elif sp["kind"] == "prose" and not sp["in_faq"] and _is_cta(sp["clean"], ctx):
            prev = next((spans[p] for p in range(k - 1, -1, -1) if spans[p]["kind"] in ("prose", "heading")), None)
            # only the literal CTA protects it here: a sentence that restates the CTA is CTA-like by design
            if prev is None or prev["kind"] != "prose" or hygiene.has_cta_literal(prev["clean"], ctx.cta):
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


def _accept(text: str, edits: list[Edit], ctx: TellContext) -> tuple[str, list[Edit], list[Edit]]:
    """Apply the edits all at once when the result is intact; else one at a time, keeping each edit that leaves
    no new integrity problem. Returns (text, kept, skipped)."""
    if not edits:
        return text, [], []
    base: list = []
    after = _apply(text, edits)
    if not _new_problems(text, after, edits, ctx, base):
        return after, list(edits), []
    kept: list[Edit] = []
    skipped: list[Edit] = []
    for e in sorted(edits, key=lambda e: e.start):
        trial = kept + [e]
        if _new_problems(text, _apply(text, trial), trial, ctx, base):
            skipped.append(e)
        else:
            kept = trial
    return _apply(text, kept), kept, skipped


_PASSES: tuple[Callable, ...] = (_sentence_edits, _structure_edits, _structure_edits, _heading_edits)


def rewrite(text: str, ctx: TellContext) -> tuple[str, list[str]]:
    """The deterministic AI-tell pass. Returns (text, log): one ``ai_tells: <kind>: '<sentence>'`` line per kept
    edit and one ``ai_tells: skipped <kind> (breaks the text): …`` line per edit the integrity check refused. Text
    with no tell comes back unchanged, byte for byte."""
    log: list[str] = []
    cur = text or ""
    for make in _PASSES:
        edits = make(cur, ctx)
        if not edits:
            continue
        cur, kept, skipped = _accept(cur, edits, ctx)
        log += [f"ai_tells: {e.kind}: '{e.before[:200]}'" for e in kept]
        log += [f"ai_tells: skipped {e.kind} (breaks the text): '{e.before[:200]}'" for e in skipped]
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


__all__ = ["AI_TELLS_VERSION", "BLOG_HEADING_RULE", "PROMPT_RULES", "SOCIAL_AUDIENCE_RULE", "TellContext", "census",
           "lead_focus", "residue", "rewrite", "seed_facts"]
