"""AA-789 tier 1 — the seed-alignment checks, one per missed class (root PLAN.md §2).

Every check needs three keys: a dependency structure in the sentence (spaCy), a relation in the seed (`SeedGraph`) and
a closed trigger word. A missing key means abstain, so a check never fires on a guess. Each returns
`(class, reason)` pairs for one sentence; `find_candidates` runs them over a piece.

Classes 9 (paraphrased repeat, needs a local sentence encoder) and 11 (FAQ non-answer, the FAQ step) are later slices
(plan S6, S7) and have no check here.

Imports nothing from quality_gates (invariant I1): this layer cannot build a gate result. Routing is modes.py's.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from services.acp_content_writing.hygiene import fold, sentence_spans
from services.acp_content_writing.seed_align import nlp as sa_nlp
from services.acp_content_writing.seed_align.seed_graph import SeedGraph, norm, number_value

OPTIONAL_AS_PLANNED = "optional_as_planned"
PLANNED_AS_OPTIONAL = "planned_as_optional"
NEAR_AS_AT = "near_as_at"
DIFFICULTY_PROMISE = "difficulty_promise"
WORLD_SHARE = "world_share_unsupported"
SCOPELESS_SUPERLATIVE = "scopeless_superlative"
BRIEF_RECIPE_LEAK = "brief_recipe_leak"
NAMING_NOTE_LEAK = "naming_note_leak"
PARAPHRASED_REPEAT = "paraphrased_repeat"
ORPHAN_OPENER = "orphan_opener"
FAQ_NON_ANSWER = "faq_non_answer"
ALL_CLASSES = (OPTIONAL_AS_PLANNED, PLANNED_AS_OPTIONAL, NEAR_AS_AT, DIFFICULTY_PROMISE, WORLD_SHARE,
               SCOPELESS_SUPERLATIVE, BRIEF_RECIPE_LEAK, NAMING_NOTE_LEAK, PARAPHRASED_REPEAT, ORPHAN_OPENER,
               FAQ_NON_ANSWER)
# The classes with a check in this module.
BUILT_CLASSES = (OPTIONAL_AS_PLANNED, PLANNED_AS_OPTIONAL, NEAR_AS_AT, DIFFICULTY_PROMISE, WORLD_SHARE,
                 SCOPELESS_SUPERLATIVE, BRIEF_RECIPE_LEAK, NAMING_NOTE_LEAK, ORPHAN_OPENER)
# A fact check abstains on a sentence longer than this (the parse of a long sentence is not stable enough).
MAX_TOKENS = 40


@dataclass(frozen=True)
class Candidate:
    cls: str
    sentence: str
    reason: str
    start: int = 0


# ---------------------------------------------------------------- token helpers

def _lexicon(words) -> frozenset:
    """A closed word list in the form tokens are compared in (norm: 'evening' → 'even', 'riding' → 'rid')."""
    return frozenset(norm(w) for w in words)


def _lem(t) -> str:
    return norm(t.lemma_ or t.text)


# A noun the small model tags as a verb ('boat rides' → 'rides' VBZ, lemma 'rid'): never a clause verb.
_NOMINAL_DEPS = frozenset({"nsubj", "nsubjpass", "dobj", "pobj", "attr", "appos", "compound"})


def _misparsed_noun(t) -> bool:
    return t.pos_ == "VERB" and (t.lemma_ == "rid" or (t.dep_ in _NOMINAL_DEPS and t.tag_ in ("VBZ", "VBG")))


def _clause_verb(t):
    """The verb (or copula) that governs `t`."""
    cur = t
    for _ in range(12):
        if cur.pos_ in ("VERB", "AUX") and cur.dep_ not in ("aux", "auxpass") and not _misparsed_noun(cur):
            return cur
        if cur.head.i == cur.i:
            return cur
        cur = cur.head
    return cur


def _subject(v):
    for c in v.children:
        if c.dep_ in ("nsubj", "nsubjpass", "csubj", "csubjpass"):
            return c
    if v.dep_ in ("xcomp", "conj", "advcl", "ccomp") and v.head.i != v.i:
        return _subject(v.head)
    return None


def _chain(v) -> list:
    """`v` and the verbs it is coordinated with or a complement of, up to the clause root: a modal or hedge on any of
    them covers `v` ('families can walk the shore and take a boat')."""
    out = [v]
    cur = v
    while cur.dep_ in ("conj", "xcomp") and cur.head.i != cur.i:
        cur = cur.head
        out.append(cur)
    return out


def _has_neg(tokens: Iterable) -> bool:
    return any(t.dep_ == "neg" or t.lower_ in ("never", "no", "not", "without", "nothing", "none") for t in tokens)


def _span_text(tokens) -> str:
    return " ".join(t.text for t in tokens)


TRAVELLER = _lexicon({"family", "guest", "traveller", "traveler", "we", "you", "group", "child", "children", "kid",
                       "visitor", "everyone", "party", "parent", "couple", "i", "us"})
PLAN_SUBJECT = _lexicon({"day", "morning", "afternoon", "evening", "night", "itinerary", "plan", "programme",
                          "program", "trip", "schedule", "stay", "route"})
MODALS = frozenset({"can", "could", "may", "might", "would", "should"})


# ---------------------------------------------------------------- class 1: an optional activity told as planned

# Words that hedge the activity they govern: offered, conditional, chosen.
_HEDGE_WORDS = _lexicon({"optional", "optionally", "option", "if", "wish", "want", "choose", "choice", "prefer",
                          "available", "arrange", "request", "extra", "add", "depend", "free", "instead", "permit",
                          "possible", "possibility", "like", "unless", "whether", "weather", "perhaps", "maybe",
                          "chance", "opt", "offer", "cost", "fee", "pay"})


def _hedge_target(h):
    """The tokens an adjective / noun hedge is about ('with only horse riding optional' → 'horse riding'), or None."""
    if h.dep_ == "amod":
        return list(h.head.subtree)
    subj = next((c for c in h.children if c.dep_ in ("nsubj", "nsubjpass")), None)
    if subj is not None:
        return list(subj.subtree)
    if h.dep_ in ("acomp", "attr", "oprd"):
        s = _subject(h.head)
        if s is not None:
            return list(s.subtree)
    return None


def _hedged(tok, sent) -> bool:
    """Is `tok` inside the scope of a hedge (a modal or an offer word on its clause chain, an 'or' alternative)?"""
    v = _clause_verb(tok)
    chain = _chain(v)
    for cv in chain:
        for c in cv.children:
            if c.dep_ in ("aux", "auxpass") and c.lower_ in MODALS:
                return True
            if c.dep_ == "neg":
                return True
    covered = set()
    for cv in chain:
        for t in cv.subtree:
            covered.add(t.i)
    for h in sent:
        if h.i not in covered:
            continue
        if h.lower_ == "or" and h.dep_ == "cc":
            return True
        if _lem(h) not in _HEDGE_WORDS and h.lower_ not in _HEDGE_WORDS:
            continue
        if h.pos_ in ("ADJ", "NOUN"):
            target = _hedge_target(h)
            if target is not None and tok not in target:
                continue
        if h.pos_ == "ADV" and h.dep_ == "advmod" and tok not in list(h.head.subtree):
            continue
        return True
    return False


def check_optional_as_planned(sent, g: SeedGraph) -> list[tuple[str, str]]:
    if not g.optional_items or len(sent) > MAX_TOKENS:
        return []
    for item in g.optional_items:
        for t in sent:
            if _lem(t) not in item.anchors and norm(t.text) not in item.anchors:
                continue
            if t.pos_ not in ("NOUN", "PROPN", "VERB", "ADJ", "ADV"):
                continue
            if _hedged(t, sent):
                return []
            v = _clause_verb(t)
            # parse stability: a noun read as a verb on the way up ('boat rides' → 'rides' VBZ) → abstain
            if any(_misparsed_noun(a) for a in t.ancestors if a.i != v.i and (a.i - v.i) * (a.i - t.i) <= 0) or \
                    _misparsed_noun(t.head):
                return []
            subj = _subject(v)
            subj_toks = list(subj.subtree) if subj is not None else []
            if v.lemma_ == "be":
                continue
            if t in subj_toks:
                # the activity itself acts ('Boat rides offer a different register'): told as something that happens
                return [(OPTIONAL_AS_PLANNED, f"the optional {item.anchor} activity is told as part of the plan")]
            if subj is not None and (_lem(subj) in TRAVELLER or _lem(subj) in PLAN_SUBJECT):
                return [(OPTIONAL_AS_PLANNED, f"the optional {item.anchor} activity is told as part of the plan")]
    return []


# ---------------------------------------------------------------- class 2: a planned moment called optional

_VISIT_HEADS = _lexicon({"visit", "excursion", "trip", "tour", "detour", "outing", "day", "hike", "trek", "walk"})


def _flip_hedges(sent) -> list[tuple[Any, list]]:
    """(hedge, target tokens) of the explicit optionality forms: '<X> is optional / an optional extra', 'an optional
    <X>', 'optionally <verb>', '<X> is available on request', '<X> can be added / arranged', 'choose to <verb>',
    'skip <X>'."""
    out = []
    for h in sent:
        lem = h.lemma_.lower()
        if lem in ("optional", "extra") and h.pos_ in ("ADJ", "NOUN"):
            head = h.head if h.dep_ == "amod" else h
            if head.dep_ in ("attr", "acomp") or h.dep_ in ("acomp", "attr"):
                s = _subject(head.head if head.dep_ in ("attr", "acomp") else h.head)
                if s is not None:
                    out.append((h, list(s.subtree)))
            elif h.dep_ == "amod":
                out.append((h, list(h.head.subtree)))
        elif lem == "optionally" and h.dep_ == "advmod":
            out.append((h, [t for t in h.head.subtree if t.dep_ not in ("nsubj",)]))
        elif lem == "request" and h.dep_ == "pobj":
            s = _subject(_clause_verb(h))
            if s is not None:
                out.append((h, list(s.subtree)))
        elif lem in ("add", "arrange") and h.tag_ == "VBN" and any(c.dep_ == "auxpass" for c in h.children):
            s = _subject(h)
            if s is not None and _subject(h).dep_ == "nsubjpass":
                out.append((h, list(s.subtree)))
        elif lem == "choose":
            x = next((c for c in h.children if c.dep_ == "xcomp"), None)
            if x is not None:
                out.append((h, [t for t in x.subtree if t.dep_ != "aux"]))
        elif lem == "skip":
            o = next((c for c in h.children if c.dep_ == "dobj"), None)
            if o is not None:
                out.append((h, list(o.subtree)))
    return out


def _core(target: list) -> tuple[Any, set[str]]:
    """The head of a target phrase and the words that name what it is about: the head, its compounds and adjectives,
    its object, and the object of a 'to / at / of / on / into / up' preposition (never a possessive's owner or a
    'near' place)."""
    ids = {t.i for t in target}
    head = next((t for t in target if t.head.i not in ids or t.head.i == t.i), target[0])
    words = {_lem(head), norm(head.text)}

    def add(tok, depth=0):
        for c in tok.children:
            if c.dep_ in ("compound", "amod", "dobj", "nmod") and c.i in ids:
                words.update({_lem(c), norm(c.text)})
                add(c, depth + 1)
            elif c.dep_ in ("prep", "prt") and c.lower_ in ("to", "at", "of", "on", "into", "up", "in") and depth < 3:
                for p in c.children:
                    if p.dep_ == "pobj" and p.i in ids:
                        words.update({_lem(p), norm(p.text)})
                        add(p, depth + 1)
                    elif p.dep_ == "prep":
                        add(c, depth + 1)
            elif c.dep_ == "prep" and c.lower_ not in ("near", "after", "before", "during", "with", "for", "by"):
                continue
    add(head)
    return head, words


def check_planned_as_optional(sent, g: SeedGraph) -> list[tuple[str, str]]:
    if not g.moments or len(sent) > MAX_TOKENS:
        return []
    anchors = {a for it in g.optional_items for a in it.anchors}
    for h, target in _flip_hedges(sent):
        if not target or _has_neg([t for t in sent if t.i in range(max(0, h.i - 3), h.i)] + list(h.children)):
            continue
        head, core = _core(target)
        if core & anchors or _lem(head) in ("ride", "milk", "swim", "bath") and not core - {_lem(head)}:
            continue
        for m in g.moments:
            if not m.heads:
                continue  # the seed plans nothing for this moment (it only offers it)
            names = set(m.names)
            heads = set(m.heads)
            head_words = {_lem(head), norm(head.text)}
            rest = core - head_words
            hit = (head_words & names
                   or (head_words & heads and rest & (heads | names))
                   or ((head_words & _VISIT_HEADS or head_words & heads) and core & names))
            if hit:
                return [(PLANNED_AS_OPTIONAL, f"a planned moment ({m.name.split(' — ')[0][:60] or 'the visit'}) is "
                                              f"called optional")]
    return []


# ---------------------------------------------------------------- class 3: 'lunch at X' when the seed says near X

_EVENT_NOUNS = _lexicon({"lunch", "stop", "break", "pause", "picnic", "halt", "midday", "rest"})


def _names_place(tok_text: str, place_key: str) -> bool:
    return place_key and (fold(tok_text) == place_key or fold(tok_text) in place_key.split())


def check_near_as_at(sent, g: SeedGraph, context: str = "") -> list[tuple[str, str]]:
    rels = [r for r in g.near if r.place_key not in g.at_places]
    if not rels or len(sent) > MAX_TOKENS:
        return []
    text = fold(sent.text)
    for rel in rels:
        key = rel.place_key.split()[0]
        here_ok = key in fold(context) and not re.search(r"\bnear\b", text)
        if key not in text and not (here_ok and re.search(r"\bhere\b", text)):
            continue
        for t in sent:
            if _lem(t) not in _EVENT_NOUNS or t.pos_ not in ("NOUN", "PROPN", "ADJ"):
                continue
            # never in a time phrase: 'After lunch, …', 'before lunch'
            if t.head.lower_ in ("after", "before", "until", "during", "since") or any(
                    c.lower_ == "near" for c in t.subtree):
                continue
            ev_np = t if t.dep_ != "amod" and t.dep_ != "compound" else t.head
            v = _clause_verb(ev_np)
            if _has_neg(list(v.children)):
                continue
            subj = _subject(v)
            # who stops: the event is the subject, or a traveller / the route / a day stops
            if not (ev_np.dep_ in ("nsubj", "nsubjpass", "attr", "appos", "ROOT") or (
                    subj is not None and (_lem(subj) in TRAVELLER | PLAN_SUBJECT or _names_place(subj.text, key)
                                          or subj.lower_ in ("it", "this")))):
                continue
            # the link to the place: its own 'at / in' child, 'here', or the copula ('the stop is X', 'X is a stop')
            linked = False
            for c in ev_np.subtree:
                if c.lower_ in ("at", "in") and c.dep_ == "prep":
                    if any(_names_place(p.text, key) for p in c.subtree):
                        linked = True
                if c.lower_ == "here" and here_ok and c.head.lower_ not in ("near", "from"):
                    linked = True
            if v.lemma_ == "be" or v.lemma_ == "serve":
                other = [c for c in v.children if c.dep_ in ("nsubj", "attr", "acomp", "prep")]
                for c in other:
                    for p in c.subtree:
                        if _names_place(p.text, key) and not any(a.lower_ in ("near", "from", "outskirts", "of")
                                                                 for a in p.ancestors if a.i > c.i - 1 and a != p):
                            linked = True
                        if p.lower_ == "here" and here_ok:
                            linked = True
                        if p.lower_ in ("it",) and here_ok and p.dep_ == "nsubj":
                            linked = True
            if linked:
                return [(NEAR_AS_AT, f"the source has the {sorted(rel.events)[0]} near {rel.place}, the copy puts it "
                                     f"in or at {rel.place}")]
    return []


# ---------------------------------------------------------------- class 4: a difficulty promise the seed does not give

_EASE = _lexicon({"easy", "gentle", "effortless", "undemanding", "leisurely", "manageable", "relaxed", "unhurried",
                  "stroll"})
_HARD = _lexicon({"technical", "strenuous", "demanding", "difficult", "hard", "challenging", "exhausting",
                  "exhaustion", "rush", "rushed", "relentless", "tiring", "tire", "tired", "worn", "steep", "leave",
                  "struggle"})
_ACTIVITY = _lexicon({"walk", "hike", "trek", "climb", "crossing", "ride", "ascent", "descent", "trail", "route",
                      "stroll", "day", "pace", "journey", "path", "scramble", "paddle", "drive", "loop"})
_EQUIPMENT = _lexicon({"gear", "equipment", "boot", "shoe", "rope", "helmet", "pole", "kit", "permit"})


def check_difficulty_promise(sent, g: SeedGraph) -> list[tuple[str, str]]:
    if len(sent) > MAX_TOKENS:
        return []
    for t in sent:
        lem = _lem(t)
        word = lem if lem in _EASE | _HARD else norm(t.text) if norm(t.text) in _EASE | _HARD else None
        if word is None:
            continue
        if word in g.source_words or norm(t.text) in g.source_words:
            continue  # the seed gives it ('non-technical')
        v = _clause_verb(t)
        subj = _subject(v)
        scope = list(v.subtree)
        if subj is not None and _lem(subj) in _EQUIPMENT:
            continue
        if any(_lem(x) in _EQUIPMENT for x in scope):
            continue
        about = (subj is not None and (_lem(subj) in _ACTIVITY or any(_lem(x) in _ACTIVITY for x in subj.subtree))) \
            or (t.dep_ == "amod" and _lem(t.head) in _ACTIVITY) or _lem(t.head) in _ACTIVITY
        if word in _EASE:
            if about and not _has_neg(list(v.children)):
                return [(DIFFICULTY_PROMISE, f"a pace or ease promise ('{t.text}') the source does not give")]
            continue
        # a difficulty word: only its negation reassures ('not a technical climb', 'never rushed', 'without exhaustion')
        neg = _has_neg(list(v.children) + list(t.children) + ([subj] + list(subj.children) if subj is not None else [])
                       + ([t.head] if t.head.lower_ == "without" else [])) \
            or any(x.lemma_ in ("avoid", "rarely", "never") for x in [v] + list(v.children))
        if neg and (about or (subj is not None and _lem(subj) in TRAVELLER)):
            return [(DIFFICULTY_PROMISE, f"a reassurance ('{t.text}' denied) the source does not give")]
    return []


# ---------------------------------------------------------------- class 5: a share of the world the seed does not give

_PROPORTION = _lexicon({"share", "fraction", "portion", "proportion", "percent", "percentage", "half", "third",
                         "quarter", "most", "majority", "bulk", "%"})
_GLOBAL = _lexicon({"world", "planet", "earth", "globe"})


def _numbers(sent) -> list[float]:
    """Every number of a sentence, number words read as one run ('zero point four' → 0.4)."""
    out: list[float] = []
    run: list[str] = []
    for t in list(sent) + [None]:
        if t is not None and (t.like_num or (t.lower_ == "point" and run)):
            run.append(t.text)
            continue
        if run:
            vals = [number_value(" ".join(run))] + [number_value(w) for w in run if w.lower() != "point"]
            out += [v for v in vals if v is not None]
        run = []
    return out


def check_world_share(sent, g: SeedGraph) -> list[tuple[str, str]]:
    if len(sent) > MAX_TOKENS:
        return []
    for t in sent:
        if _lem(t) not in _PROPORTION and t.lower_ not in _PROPORTION:
            continue
        of = next((c for c in t.children if c.dep_ == "prep" and c.lower_ == "of"), None)
        if of is None:
            continue
        pobj = next((c for c in of.children if c.dep_ == "pobj"), None)
        if pobj is None or _lem(pobj) in _GLOBAL:
            continue  # 'most of the world' alone is not a share of a resource
        if not any(_lem(c) in _GLOBAL for c in pobj.subtree if c.dep_ in ("poss", "compound", "pobj")):
            continue
        # the figure as written: digits, or number words read together ('zero point four', 'seventy')
        words = [c.text for c in t.subtree if c.like_num or c.pos_ == "NUM" or c.lower_ == "point"]
        nums = [number_value(" ".join(words))] if words else []
        nums += [number_value(w) for w in words]
        nums = [n for n in nums if n is not None]
        if t.lower_ == "half":
            nums.append(50.0)
        sent_nums = _numbers(sent)
        if g.world_figures and (any(n in g.world_figures for n in nums) or any(n in g.world_figures
                                                                               for n in sent_nums)):
            continue  # the seed's own figure, stated as given
        reason = (f"a share of the world's {pobj.text} ({_span_text(list(t.subtree)[:8])[:60]}) the source does not "
                  f"give")
        return [(WORLD_SHARE, reason)]
    return []


# ---------------------------------------------------------------- class 6: a superlative the seed does not make

# Factual dimensions and practical superlatives ('best time', 'nearest town'); a contested case, never fired here.
_DIMENSION = _lexicon({"high", "low", "long", "short", "large", "big", "small", "deep", "wide", "tall", "old",
                        "young", "near", "close", "far", "easy", "quick", "fast", "cheap", "early", "late", "good",
                        "bad", "hot", "cold", "warm", "cool", "heavy", "light", "busy", "quiet", "great", "many",
                        "much", "few", "little", "last", "first", "next"})
_DERIV = (("ive", ""), ("ness", ""), ("ly", ""), ("al", ""), ("ic", ""))


_TRIP_SCOPE = _lexicon({"route", "trip", "day", "itinerary", "journey", "tour", "week", "stay", "programme"})


def _stem_forms(word: str) -> set[str]:
    out = {word}
    for suf, rep in _DERIV:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            out.add(word[: -len(suf)] + rep)
    out |= {w + "t" for w in list(out)}  # 'distinc' + t
    return out


def _backed(adj: str, g: SeedGraph) -> bool:
    forms = _stem_forms(adj)
    return any(forms & _stem_forms(s) for s in g.superlatives) or any(
        s.startswith(f[:6]) for f in forms for s in g.superlatives if len(f) >= 6)


def check_scopeless_superlative(sent, g: SeedGraph) -> list[tuple[str, str]]:
    if len(sent) > MAX_TOKENS:
        return []
    for t in sent:
        adj = None
        if t.tag_ == "JJS" and t.lemma_.lower() not in ("most", "least"):
            adj = t
        elif t.lower_ in ("most", "more") and t.head.pos_ == "ADJ" and t.dep_ == "advmod":
            adj = t.head
            if t.lower_ == "more" and not any(x.lower_ == "one" for x in sent[max(0, t.i - 4):t.i]):
                continue
        elif t.lower_ == "only" and t.dep_ == "amod" and t.head.pos_ == "NOUN":
            if any(_lem(x) in _GLOBAL for x in t.head.subtree) and "only" not in g.superlatives:
                return [(SCOPELESS_SUPERLATIVE, "a uniqueness claim ('the only … in the world') the source does not "
                                                "make")]
            continue
        if adj is None:
            continue
        noun = adj.head if adj.dep_ == "amod" else None
        # an idiom ('in the fullest sense') or a ranking inside the trip ('the route's most extraordinary encounter',
        # 'the best stop of the day'): not a claim about the world
        if noun is not None and (_lem(noun) in ("sense", "way", "extent", "degree") or any(
                c.dep_ == "poss" for c in noun.children) or any(
                p.dep_ == "pobj" and _lem(p) in _TRIP_SCOPE for c in noun.children if c.dep_ == "prep"
                for p in c.children)):
            continue
        word = _lem(adj)
        if word in _DIMENSION or norm(adj.text) in _DIMENSION or _backed(word, g):
            continue
        return [(SCOPELESS_SUPERLATIVE, f"a superlative ('{adj.text}') the source does not make")]
    # 'most of the world / the rest of the world has never …'
    for t in sent:
        if t.lower_ in ("most", "rest") and t.dep_ in ("nsubj", "nsubjpass"):
            of = next((c for c in t.children if c.lower_ == "of"), None)
            if of is not None and any(_lem(p) in _GLOBAL for p in of.subtree) and (
                    _has_neg(list(t.head.children)) or any(c.lower_ in ("rarely", "seldom", "hardly", "barely")
                                                          for c in t.head.children)):
                return [(SCOPELESS_SUPERLATIVE, "a rarity claim ('most of the world has never …')")]
    return []


# ---------------------------------------------------------------- class 7: writer-brief wording as copy

_META_NOUNS = _lexicon({"fact", "date", "year", "sentence", "paragraph", "detail", "moment", "line", "word"})
_RANGE_WORDS = {n: w for w, n in (("one", 1), ("two", 2), ("three", 3), ("four", 4), ("five", 5), ("six", 6),
                                  ("seven", 7), ("eight", 8), ("nine", 9), ("ten", 10), ("eleven", 11),
                                  ("twelve", 12), ("thirteen", 13), ("fourteen", 14), ("fifteen", 15),
                                  ("sixteen", 16), ("seventeen", 17), ("eighteen", 18))}


def check_brief_recipe_leak(sent, g: SeedGraph, kind: str = "prose") -> list[tuple[str, str]]:
    text = sent.text
    if kind == "heading":
        if any(t.lower_ in ("sentence", "sentences", "paragraph") for t in sent):
            return [(BRIEF_RECIPE_LEAK, "a heading names the writer's sentence budget")]
        return []
    ones = [t for t in sent if t.lower_ in ("one", "single") and t.dep_ in ("nummod", "amod", "det", "ROOT")
            and t.head.pos_ in ("NOUN", "PROPN")]
    heads = [t.head for t in ones]
    meta = [h for h in heads if _lem(h) in _META_NOUNS]
    if len(heads) >= 2 and len(meta) >= 2:
        return [(BRIEF_RECIPE_LEAK, "a 'one X, one Y, one Z' recipe from the writer brief")]
    for h in meta:
        if _lem(h) in ("sentence", "paragraph"):
            return [(BRIEF_RECIPE_LEAK, "the copy talks about its own sentence budget")]
        if _lem(h) == "fact" and any(c.lower_ == "the" for c in h.children) and any(
                c.dep_ == "relcl" for c in h.children):
            return [(BRIEF_RECIPE_LEAK, "'the one fact we have': the thin-moment note as copy")]
    low = fold(text)
    for a, b in g.audience_ranges:
        forms = [(str(a), str(b)), (_RANGE_WORDS.get(a, ""), _RANGE_WORDS.get(b, ""))]
        for x, y in forms:
            if x and y and re.search(rf"\b{x}\b\s*(?:-|–|to|and)\s*\b{y}\b", low):
                return [(BRIEF_RECIPE_LEAK, f"the brand's audience range ({a}-{b}) as copy")]
    for q in g.brief_quotes:
        if fold(q) in low and fold(q) not in fold(g.source_text):
            return [(BRIEF_RECIPE_LEAK, f"the brief's own wording ('{q}') as copy")]
    if g.voice_words and any(_lem(t) in ("voice", "tone", "register") for t in sent):
        hits = {_lem(t) for t in sent if t.pos_ == "ADJ"} & g.voice_words
        if len(hits) >= 2:
            return [(BRIEF_RECIPE_LEAK, "the brief's voice instruction as copy")]
    return []


# ---------------------------------------------------------------- class 8: the naming note as copy

_NAMING_META = _lexicon({"phonetic", "approximation", "connotation", "romanise", "romanize", "romanised",
                          "romanized", "transliterate", "transliteration", "spelling", "spell", "spelled", "spelt",
                          "variant", "international", "audience", "acceptable", "nickname", "correctly", "unfortunate",
                          "refer", "identify", "sufficient", "standard"})
_PRONOUNCE_Q = re.compile(r"\b(?:pronounce|say|spell|spelled|spelt|written|write)\b", re.I)


def check_naming_note_leak(sent, g: SeedGraph, *, names: set[str], kind: str = "prose",
                           question: str = "") -> list[tuple[str, str]]:
    if kind == "faq_a" and _PRONOUNCE_Q.search(question or ""):
        return []
    if sent[0].tag_ == "VB" or any(t.dep_ == "ROOT" and t.tag_ == "VB" and _subject(t) is None for t in sent):
        return []  # practical advice ('Check that Mörön is correctly spelled on your ticket')
    if not any(fold(t.text) in names or norm(t.text) in names for t in sent):
        return []
    # one word per token ('spelled' is the lemma 'spell', not two words)
    meta = {_lem(t) if _lem(t) in _NAMING_META else norm(t.text) for t in sent
            if _lem(t) in _NAMING_META or norm(t.text) in _NAMING_META}
    note = {_lem(t) for t in sent if _lem(t) in g.naming_words and t.pos_ in ("NOUN", "ADJ", "VERB", "ADV")
            and _lem(t) not in names and len(t.text) > 3} - {"name", "also", "use", "write", "know", "city", "call"}
    if len(meta | (note & _NAMING_META)) >= 2:
        return [(NAMING_NOTE_LEAK, "the seed's naming note told as copy")]
    return []


# ---------------------------------------------------------------- class 10: a section opener with no antecedent

_RELATIONAL = _lexicon({"access", "entry", "admission", "arrival", "departure", "approach", "entrance", "transfer",
                         "return", "descent", "ascent", "crossing"})


def check_orphan_opener(sent, heading_doc) -> list[tuple[str, str]]:
    if heading_doc is None:
        return []
    root = next((t for t in sent if t.dep_ == "ROOT"), None)
    if root is None:
        return []
    subj = _subject(root)
    if subj is None:
        return []
    plural_head = any(t.tag_ in ("NNS", "NNPS") for t in heading_doc)
    sub = list(subj.subtree)
    first = sent[0]
    if first.lower_ in ("they", "these", "those", "them") and first.i in {t.i for t in sub} and not plural_head:
        return [(ORPHAN_OPENER, f"the section opens on '{first.text}' with nothing it refers to")]
    if subj.dep_ in ("csubj", "csubjpass") or subj.tag_ in ("WP",):
        if any(t.lower_ in ("they", "them") for t in sub) and not plural_head:
            return [(ORPHAN_OPENER, "the section opens on a clause about 'they' with nothing it refers to")]
    if first.lower_ in ("this", "that") and first.dep_ == "nsubj" and root.lemma_ == "be" and len(sent) > 2 \
            and sent[2].lower_ in ("why", "how"):
        return [(ORPHAN_OPENER, f"the section opens on '{first.text} is why' with nothing it refers to")]
    if (subj.i == first.i and _lem(subj) in _RELATIONAL and subj.tag_ == "NN"
            and not any(c.dep_ in ("det", "poss", "prep", "compound", "amod") for c in subj.children)
            and _lem(subj) not in {_lem(t) for t in heading_doc}):
        return [(ORPHAN_OPENER, f"the section opens on a bare '{subj.text}' with nothing it belongs to")]
    return []


# ---------------------------------------------------------------- run over a piece

def _kinds(spans: list[dict]) -> list[dict]:
    return [s for s in spans if s["kind"] in ("prose", "bullet", "faq_a", "heading") and s["clean"].strip()]


def find_candidates(text: str, g: SeedGraph, *, names: Optional[set[str]] = None,
                    classes: Iterable[str] = BUILT_CLASSES) -> Optional[list[Candidate]]:
    """Every candidate of `classes` in `text`, one per sentence and class, or None when spaCy is not available."""
    classes = set(classes)
    spans = _kinds(sentence_spans(text or ""))
    if not spans:
        return []
    docs = sa_nlp.parse_many([s["clean"].lstrip("#").strip() for s in spans])
    if docs is None:
        return None
    names = names or set()
    out: list[Candidate] = []
    last_heading = None
    heading_open = False
    question = ""
    all_spans = sentence_spans(text or "")
    for i, (sp, doc) in enumerate(zip(spans, docs)):
        kind = sp["kind"]
        if kind == "heading":
            last_heading, heading_open = doc, True
            if BRIEF_RECIPE_LEAK in classes:
                out += [Candidate(c, sp["clean"], r, sp["start"]) for c, r in
                        check_brief_recipe_leak(doc, g, kind="heading")]
            continue
        # the question a sentence answers: the last FAQ question or question line of its section
        section = [s for s in all_spans if s["end"] <= sp["start"]]
        heads = [k for k, s in enumerate(section) if s["kind"] == "heading"]
        section = section[heads[-1] + 1:] if heads else section
        asked = [s for s in section if s["kind"] == "faq_q" or s["clean"].rstrip("*").rstrip().endswith("?")]
        question = asked[-1]["clean"] if asked else ""
        if question and kind == "prose":
            kind = "faq_a"
        found: list[tuple[str, str]] = []
        context = " ".join(s["clean"] for s in spans[max(0, i - 2):i])
        on = classes.__contains__
        if on(OPTIONAL_AS_PLANNED):
            found += check_optional_as_planned(doc, g)
        if on(PLANNED_AS_OPTIONAL):
            found += check_planned_as_optional(doc, g)
        if on(NEAR_AS_AT):
            found += check_near_as_at(doc, g, context=context + " " + sp["clean"])
        if on(DIFFICULTY_PROMISE):
            found += check_difficulty_promise(doc, g)
        if on(WORLD_SHARE):
            found += check_world_share(doc, g)
        if on(SCOPELESS_SUPERLATIVE):
            found += check_scopeless_superlative(doc, g)
        if on(BRIEF_RECIPE_LEAK):
            found += check_brief_recipe_leak(doc, g, kind=kind)
        if on(NAMING_NOTE_LEAK):
            found += check_naming_note_leak(doc, g, names=names, kind=kind, question=question)
        if on(ORPHAN_OPENER):
            found += check_orphan_opener(doc, last_heading if heading_open else None)
        heading_open = False
        out += [Candidate(c, sp["clean"], r, sp["start"]) for c, r in found]
    return out


__all__ = ["Candidate", "find_candidates", "ALL_CLASSES", "BUILT_CLASSES", "MAX_TOKENS",
           "OPTIONAL_AS_PLANNED", "PLANNED_AS_OPTIONAL", "NEAR_AS_AT", "DIFFICULTY_PROMISE", "WORLD_SHARE",
           "SCOPELESS_SUPERLATIVE", "BRIEF_RECIPE_LEAK", "NAMING_NOTE_LEAK", "PARAPHRASED_REPEAT", "ORPHAN_OPENER",
           "FAQ_NON_ANSWER"]
