"""AA-917 WM-12 scope 4 — the lint every drafted rule passes before it may become a candidate (app ADR 0010
decision 3: rules only constrain; decision 10: platform rows never hold tenant text).

  constraint_only       the rule cuts, bans, rephrases or formats. It is refused when it tells the writer to add,
                        mention, include, promise … something ("Include the lunch", "Mention free pick-up"); the same
                        verbs under a negation ("Do not mention …", "Never promise …") are a constraint and pass.
  lexicon_specifics     a lexicon "replace" rule's replacement adds no number, time, price, name or promise word
                        (repair_contract.new_specifics + the fact filter's promise words): "lunch" -> "lunch
                        included" fails.
  five_gram_overlap     a platform rule shares no 5-word run with any tenant text it may have learned from, and names
                        no tenant brand or forbidden word (a tenant rule: no other tenant's brand). A failure is a
                        `leak`: the detail quotes nothing and the store keeps the draft redacted. When the caller could
                        not load the corpus the check fails closed.
  predict               the rule explains >= 2 held-out edits of its cluster: one of its match_phrases is in the
                        masked text before the edit and gone after it.

Pure functions (the caller loads the corpora); `run_lint` returns {check: {ok, detail}} plus `ok` overall, the shape
stored in `shared.writing_rule.lint`.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from services.quality.repair_contract import new_names, new_specifics
from services.writing_memory import config as cfg
from services.writing_memory.masking import contains_phrase, mask, tokens

STOPWORDS = frozenset("""
a an the and or but nor of to in on at by for with from as is are was were be been being it its this that these those
than then so such very just also there here into onto over under about after before while when where which who whom
whose what how why all any each every some no not do does did done have has had having you your yours we our ours they
their them he she his her i me my mine us will would can could should may might must shall
""".split())

# Verbs that make a rule ADD content. A rule may still use them under a negation in the same clause.
_ADD_VERBS = (r"add|adds|adding|include|includes|including|mention|mentions|mentioning|say|state|states|stating|"
              r"highlight|highlights|emphasi[sz]e|emphasi[sz]es|promise|promises|offer|offers|list|lists|note|"
              r"describe|describes|feature|features|insert|inserts|introduce|introduces|cite|tell|always\s+use|"
              r"advertise|showcase|guarantee")
_ADD_RE = re.compile(rf"(?<![a-z])({_ADD_VERBS})(?![a-z])", re.I)
_NEGATION_RE = re.compile(r"(?<![a-z])(?:do\s+not|don['’]t|never|no|not|avoid|without|stop|refrain\s+from|instead\s+of|"
                          r"rather\s+than|unless|only\s+if|cut|remove|drop|delete)(?![a-z])", re.I)
_CLAUSE_SPLIT_RE = re.compile(r"[.;:!?]|,\s*(?:and|but|or)\s+|\s+(?:and\s+then|then)\s+", re.I)


def _check(ok: bool, detail: str = "") -> dict:
    return {"ok": bool(ok), "detail": detail}


def constraint_only(rule_text: str) -> dict:
    """Refuse a rule whose clause tells the writer to add something; negated verbs ("do not mention") pass."""
    for clause in _CLAUSE_SPLIT_RE.split(rule_text or ""):
        for m in _ADD_RE.finditer(clause):
            if not _NEGATION_RE.search(clause[:m.start()]):
                return _check(False, f"tells the writer to {m.group(1).lower()} something ('{clause.strip()[:80]}')")
    return _check(True)


def lexicon_specifics(phrase: str, replacement: Optional[str]) -> dict:
    """A lexicon replacement may add no specific: number / time (new_specifics), name, or promise word."""
    if not replacement:
        return _check(True)               # a ban
    from services.writing_memory.fact_filter import added_promises
    added = new_specifics(phrase, replacement) + [f"name {n}" for n in new_names(phrase, replacement)] + [
        f"promise '{w}'" for w in added_promises(phrase, replacement)]
    # the drafter writes masked text: a mask token the phrase does not have is a specific too ("[N] km", "[NAME]")
    added += [f"token {t}" for t in sorted(set(re.findall(r"\[(?:N|PRICE|TIME|URL|NAME)\]", replacement))
                                          - set(re.findall(r"\[(?:N|PRICE|TIME|URL|NAME)\]", phrase or "")))]
    return _check(not added, "; ".join(added[:3]))


def ngrams(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = tokens(text)
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def five_gram_overlap(rule_texts: Iterable[str], corpus_ngrams: set, names: Iterable[str]) -> dict:
    """No 5-word run of the rule (text, replacement, phrases) in the tenant corpora (both sides masked: the corpus
    n-grams come from `ngrams(mask(text))`), and no tenant brand / forbidden word in the raw rule text. Runs made only
    of mask tokens and stopwords are generic and do not count."""
    raw = [t for t in rule_texts if t]
    joined = " \n ".join(mask(t) for t in raw)
    # the corpus holds the n-gram tuples or their hash() (the miner keeps ints: a corpus of every tenant's text)
    hits = [g for g in ngrams(joined) if (g in corpus_ngrams or hash(g) in corpus_ngrams)
            and not all(w in STOPWORDS or w.startswith("[") for w in g)]
    # The detail never quotes the hit: it is stored on the rule row (lint), and a platform row holds no tenant text.
    # `leak` tells the store to redact the draft (rules.fit_to_store).
    if hits:
        return {**_check(False, f"shares {len(hits)} 5-word run(s) with tenant text"), "leak": True}
    low = " \n ".join(raw).lower()
    for name in names:
        n = (name or "").strip().lower()
        if len(n) >= 3 and re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", low):
            return {**_check(False, "names a tenant brand or forbidden word"), "leak": True}
    return _check(True)


def explains(match_phrases: Iterable[str], before: Optional[str], after: Optional[str]) -> bool:
    """One phrase of the rule is in the (masked) text before the edit and no longer in the text after it."""
    return any(contains_phrase(before, p) and not contains_phrase(after, p) for p in match_phrases if p)


def predict(match_phrases: list[str], held_out: list[dict]) -> dict:
    """PREDICT: the rule explains >= PREDICT_MIN_EXPLAINED held-out edits ({before_text, after_text})."""
    n = sum(1 for s in held_out if explains(match_phrases, s.get("before_text"), s.get("after_text")))
    ok = n >= cfg.PREDICT_MIN_EXPLAINED
    return {"ok": ok, "detail": f"explains {n} of {len(held_out)} held-out edits", "explained": n,
            "held_out": len(held_out)}


def shape(kind: str, rule_text: str, replacement: Optional[str], match_phrases: list[str]) -> dict:
    """The size rules of CIS 280, checked before the INSERT so a bad draft is a lint failure, not a 23514."""
    words = len((rule_text or "").split())
    if not (rule_text or "").strip():
        return _check(False, "empty rule")
    if kind == "preference_summary":
        sentences = len(re.findall(r"[.!?]+", rule_text))
        if sentences > cfg.SUMMARY_MAX_SENTENCES or words > 125:
            return _check(False, f"{sentences} sentences / {words} words")
    elif words > cfg.RULE_MAX_WORDS:
        return _check(False, f"{words} words (max {cfg.RULE_MAX_WORDS})")
    if len(rule_text) > cfg.RULE_MAX_CHARS:
        return _check(False, f"{len(rule_text)} characters (max {cfg.RULE_MAX_CHARS})")
    if replacement and kind != "lexicon":
        return _check(False, "a replacement on a non-lexicon rule")
    if replacement and len(replacement) > cfg.REPLACEMENT_MAX_CHARS:
        return _check(False, f"a {len(replacement)}-character replacement (max {cfg.REPLACEMENT_MAX_CHARS})")
    if not match_phrases or len(match_phrases) > cfg.MAX_MATCH_PHRASES:
        return _check(False, f"{len(match_phrases or [])} match phrases (1..{cfg.MAX_MATCH_PHRASES})")
    if sum(len(p) for p in match_phrases) > cfg.MATCH_PHRASES_MAX_CHARS:
        return _check(False, f"match phrases over {cfg.MATCH_PHRASES_MAX_CHARS} characters together")
    return _check(True)


def run_lint(*, kind: str, rule_text: str, replacement: Optional[str], match_phrases: list[str], scope: str,
             held_out: list[dict], corpus_ngrams: set, names: Iterable[str],
             corpus_unavailable: Optional[str] = None) -> dict:
    """Every check; `ok` is True only when all pass. `corpus_unavailable` (the parts the caller could not load) fails
    the overlap check closed: no rule passes it unchecked."""
    overlap = five_gram_overlap([rule_text, replacement or "", *match_phrases], corpus_ngrams, names)
    if corpus_unavailable and overlap["ok"]:
        overlap = _check(False, f"tenant corpus unavailable ({corpus_unavailable})")
    out = {
        "shape": shape(kind, rule_text, replacement, match_phrases),
        "constraint_only": constraint_only(rule_text),
        "lexicon_specifics": lexicon_specifics(rule_text if kind == "lexicon" else "", replacement)
        if kind == "lexicon" else _check(True),
        "five_gram_overlap": overlap,
        "predict": predict(match_phrases, held_out),
    }
    out["ok"] = all(v["ok"] for k, v in out.items() if isinstance(v, dict))
    out["scope"] = scope
    return out


__all__ = ["STOPWORDS", "constraint_only", "explains", "five_gram_overlap", "lexicon_specifics", "ngrams", "predict",
           "run_lint", "shape"]
