"""AA-918 WM-16 — the L0 lexicon fast path: active `lexicon` rules join the AA-800 word layers of the T9 writer
(app ADR 0010 decisions 3, 4, 6; D11).

A lexicon rule bans a phrase (v1) or replaces it with neutral wording that adds nothing. It reaches the writer
BEFORE the gates, never as a text substitution after them:
  prompt   the phrase joins the brand pack's forbidden words, so the brand block's "Never use these words or
           phrases" line names it (brand_pack.render_brand_block); a replacement rule also gets "Write 'X' instead of
           'Y'" on the T9 WORDS line (signature_vocabulary.t9_words_line);
  gate     the same forbidden list feeds F2_lexicon (source F2_brand_forbidden): a phrase the writer still writes is a
           blocking, sentence-repairable finding, with the source exemption every forbidden word has (a phrase in the
           seed is never flagged).
The fact gates outrank it (decision 4): a learned rule never removes or relaxes a gate, it only adds a word.

Which rules: the `active` lexicon rules of the piece's writer (`t9_blog` / `t9_social`), the platform's plus the
tenant's own (`rules.rules_for`: never another tenant's), on the channels of its `applies_to` when it names any.
Precedence (decision 4): the tenant-authored brand rules win,
so a rule is skipped when its phrase is a signature word the tenant declared, or when it is in the CTA, the brand name
or the trip name (text the writer must keep verbatim: a ban there could never be repaired). A tenant rule wins over a
platform rule with the same phrase.

Activation is one admin click after a passing R0 replay (`replay.py`; no R2, no shadow: D11), rollback one click
(`rules.activate_l0` / `rules.rollback`). With no active rule the pack is returned unchanged, so the prompt is byte for
byte the pre-918 one (decision 12: release 0 is a no-op). A failure to read the rules writes no rule, never fails the
write.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

import structlog

from services.writing_memory.signals import writer_for_channel

logger = structlog.get_logger()

L0_WRITERS = ("t9_blog", "t9_social")      # the writers with an L0 reader (first target: T9 social, WM-16 scope 4)
PHRASE_MAX_WORDS = 6
_MASK_TOKEN_RE = re.compile(r"\[(?:N|PRICE|TIME|URL|NAME)\]")
_QUOTES = "\"'“”‘’`"
# Times of day a replacement may not add (new_specifics reads numbers; these are words).
_TIME_WORDS = ("dawn", "sunrise", "sunset", "dusk", "morning", "mornings", "afternoon", "evening", "evenings", "night",
               "nights", "overnight", "noon", "midday", "midnight", "daily", "weekly", "hourly")
_CAP_WORD_RE = re.compile(r"\b[A-Z][\w'’-]*")


def _word_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])")


def _has(text: Optional[str], phrase: str) -> bool:
    return bool(phrase) and _word_re(phrase).search((text or "").lower()) is not None


def phrase_of(rule: dict) -> str:
    """The phrase a lexicon rule bans: its rule_text (the drafter writes the phrase itself), quotes stripped."""
    return " ".join((rule.get("rule_text") or "").strip().strip(_QUOTES).split())


def phrase_problem(phrase: str) -> Optional[str]:
    """Why a phrase cannot be a forbidden word (None = it can): it must be real words, at most 6, with no mask token
    (a masked `[NAME]` never matches the writer's text)."""
    if not phrase or not re.search(r"[A-Za-z]", phrase):
        return "the rule has no phrase"
    if _MASK_TOKEN_RE.search(phrase):
        return "the phrase holds a masked token ([NAME], [N] …), which never matches written text"
    if len(phrase.split()) > PHRASE_MAX_WORDS:
        return f"a lexicon rule bans a phrase of at most {PHRASE_MAX_WORDS} words (this one is an instruction)"
    if len(phrase) < 3:
        return "the phrase is shorter than 3 characters"
    from services.writing_memory.lint import STOPWORDS
    if all(w.strip(_QUOTES + ".,;:!?").lower() in STOPWORDS for w in phrase.split()):
        return "the phrase is only function words (the, and, you …), which would block ordinary sentences"
    return None


def replacement_check(phrase: str, replacement: Optional[str]) -> dict:
    """ADR 0010 decision 3: a replacement may add no number, place, time, price or service. {ok, detail, added}.

    number / price   repair_contract.new_specifics (the F1 reading) + a mask token the phrase lacks;
    place / name     a capitalised word the phrase lacks (any position: fails closed on a capitalised first word);
    time             new_specifics' clock times + the time-of-day words above;
    service          the fact filter's promise words (included, free, private, transfer …) and the AA-800 service
                     nouns (lodge, hotel, meal, transfer …) the phrase does not have.
    A ban (no replacement) passes. A replacement that uses a forbidden word is `replacement_forbidden`'s check."""
    from services.acp_shared.signature_vocabulary import SERVICE_NOUNS
    from services.writing_memory.lint import lexicon_specifics

    rep = (replacement or "").strip()
    if not rep:
        return {"ok": True, "detail": "", "added": []}
    added: list[str] = []
    base = lexicon_specifics(phrase, rep)
    if not base["ok"]:
        added += [d.strip() for d in base["detail"].split(";") if d.strip()]
    low = phrase.lower()
    added += [f"name {w}" for w in dict.fromkeys(m.group(0) for m in _CAP_WORD_RE.finditer(rep))
              if not _has(low, w) and f"name {w}" not in added]
    added += [f"time '{w}'" for w in _TIME_WORDS if _has(rep, w) and not _has(low, w)]
    added += [f"service '{w}'" for w in SERVICE_NOUNS if _has(rep, w) and not _has(low, w)]
    added = list(dict.fromkeys(added))
    return {"ok": not added, "detail": "; ".join(added[:4]), "added": added}


def replacement_forbidden(phrase: str, replacement: Optional[str], forbidden: Iterable[str] = ()) -> Optional[str]:
    """The word a replacement may not use (None = none): its own phrase, a platform-truth or stock-superlative word
    (AA-800 layers 1-2, for every tenant), or one of `forbidden` (the tenant's own list and the other applied phrases
    of the same write). Otherwise the prompt says "Write 'X' instead of 'Y'" while the brand block says "Never use
    X", and F2_lexicon blocks what the writer was told to write (review round)."""
    from services.acp_shared.signature_vocabulary import PLATFORM_TRUTH, STOCK_SUPERLATIVES

    rep = (replacement or "").strip()
    if not rep:
        return None
    for w in (phrase, *PLATFORM_TRUTH, *STOCK_SUPERLATIVES, *forbidden):
        w = str(w or "").strip()
        if len(w) >= 2 and _has(rep, w):
            return w
    return None


def _drop_chained_replacements(applied: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped) of the applied entries so that no kept replacement uses another kept rule's phrase (A -> B
    while B is banned by another rule; the fixed words were checked per rule already). Drop every entry whose
    replacement hits another kept phrase until none does, then add back, in precedence order, each dropped entry that
    no longer conflicts (A -> B -> C with C banned keeps A once B is gone)."""
    kept, dropped = list(applied), []
    chained: list[dict] = []
    while True:
        phrases = {e["phrase"].lower() for e in kept}
        bad = [e for e in kept if replacement_forbidden(e["phrase"], e["replacement"],
                                                       phrases - {e["phrase"].lower()})]
        if not bad:
            break
        chained += bad
        kept = [e for e in kept if e not in bad]
    order = {id(e): i for i, e in enumerate(applied)}
    for e in sorted(chained, key=lambda x: order.get(id(x), 0)):
        phrases = {k["phrase"].lower() for k in kept}
        if replacement_forbidden(e["phrase"], e["replacement"], phrases) is None and not any(
                replacement_forbidden(k["phrase"], k["replacement"], [e["phrase"]]) for k in kept):
            kept.append(e)
        else:
            w = replacement_forbidden(e["phrase"], e["replacement"], phrases)
            dropped.append({**e, "reason": f"the replacement uses '{w}', which this write forbids" if w else
                            "another applied rule's replacement uses this phrase"})
    kept.sort(key=lambda x: order.get(id(x), 0))
    return kept, dropped


def select_rules(rules: Iterable[dict], *, pack: Optional[dict], cta: Optional[str] = None,
                 brand_name: Optional[str] = None, trip_name: Optional[str] = None,
                 channel: Optional[str] = None) -> tuple[list[dict], list[dict]]:
    """(applied, skipped) entries {rule_id, scope, phrase, replacement[, reason]} of the active lexicon rules. Tenant
    rules first, so a tenant rule wins over a platform rule with the same phrase. Skipped: not a lexicon rule, a bad
    phrase, a replacement that adds a specific (re-checked here too), a signature word the tenant declared (the
    tenant-authored brand rule wins, decision 4), or a phrase in the CTA / brand name / trip name (kept verbatim by
    the writer: a ban could never be repaired). Last, a replacement that uses a word this write forbids (its own
    phrase, a platform-truth or stock-superlative word, the tenant's list, another applied phrase) is skipped
    (`replacement_forbidden`; between applied rules, `_drop_chained_replacements`)."""
    from services.acp_shared.signature_vocabulary import signature_words

    # The tenant's declared words: the POSITIONING words it named (signature_words) and every phrase it listed itself
    # in `signature_words` (custom_validators; any phrase, not only a POSITIONING word — review round).
    declared = set(signature_words(pack)) | {str(w).lower().strip()
                                             for w in (pack or {}).get("signature_words") or [] if str(w).strip()}
    own = {str(w).lower().strip() for w in (pack or {}).get("forbidden_words") or []}
    applied: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    ordered = sorted(rules, key=lambda r: (r.get("scope") != "tenant", str(r.get("created_at") or ""),
                                           str(r.get("rule_id"))))
    for r in ordered:
        phrase = phrase_of(r)
        entry = {"rule_id": str(r.get("rule_id")), "scope": r.get("scope"), "phrase": phrase,
                 "replacement": (r.get("replacement") or None)}
        reason = None
        if r.get("kind") != "lexicon":
            reason = f"not a lexicon rule ({r.get('kind')})"
        elif channel and r.get("applies_to") and channel not in r["applies_to"]:
            reason = f"for {', '.join(r['applies_to'])} only"
        else:
            reason = phrase_problem(phrase)
        if reason is None and entry["replacement"] and not replacement_check(phrase, entry["replacement"])["ok"]:
            reason = "the replacement adds a specific"
        if reason is None and entry["replacement"]:
            word = replacement_forbidden(phrase, entry["replacement"], own)
            if word:
                reason = f"the replacement uses '{word}', which this write forbids"
        if reason is None and phrase.lower() in declared:
            reason = "a signature word this brand declared (the brand's own rule wins)"
        if reason is None:
            for label, text in (("CTA", cta), ("brand name", brand_name), ("trip name", trip_name)):
                if _has(text, phrase):
                    reason = f"in the {label}, which the writer keeps verbatim"
                    break
        if reason is None and phrase.lower() in seen:
            reason = "the same phrase as a rule already applied"
        if reason is not None:
            skipped.append({**entry, "reason": reason})
            continue
        seen.add(phrase.lower())
        if phrase.lower() in own and not entry["replacement"]:
            entry["already_forbidden"] = True       # the brand already bans it: recorded, the list is unchanged
        applied.append(entry)
    applied, conflicts = _drop_chained_replacements(applied)
    return applied, skipped + conflicts


def apply_to_pack(pack: Optional[dict], applied: list[dict]) -> Optional[dict]:
    """The pack the writer and the gates read: the tenant's forbidden words plus the applied phrases, and
    `learned_lexicon` (what the T9 WORDS line and the admin record read; `added` says whether the phrase was put on
    the list, False when the tenant already listed it). No applied rule: the same object, unchanged (release 0 is a
    no-op)."""
    if not applied:
        return pack
    base = dict(pack or {})
    words = [str(w) for w in base.get("forbidden_words") or []]
    have = {w.lower().strip() for w in words}
    learned = []
    for e in applied:
        added = e["phrase"].lower() not in have
        if added:
            words.append(e["phrase"])
            have.add(e["phrase"].lower())
        learned.append({**{k: e[k] for k in ("rule_id", "scope", "phrase", "replacement")}, "added": added})
    base["forbidden_words"] = words
    base["learned_lexicon"] = learned
    return base


def without_learned(pack: Optional[dict]) -> Optional[dict]:
    """The pack as the tenant wrote it: `apply_to_pack` undone (the phrases it added to forbidden_words removed,
    `learned_lexicon` dropped). R0's rule-off base reads it, so a piece written while an L0 rule was active does not
    count as rule-on. A pack without `learned_lexicon` is returned unchanged."""
    if not isinstance(pack, dict) or not pack.get("learned_lexicon"):
        return pack
    added = {str(e.get("phrase") or "").lower().strip() for e in pack["learned_lexicon"]
             if isinstance(e, dict) and e.get("added", True)}
    base = {k: v for k, v in pack.items() if k != "learned_lexicon"}
    base["forbidden_words"] = [w for w in pack.get("forbidden_words") or [] if str(w).lower().strip() not in added]
    return base


def activation_problem(rule: dict) -> Optional[tuple[str, str]]:
    """(code, why) when a rule may not be activated on the L0 path, None when it may (WM-16 scope 3: R0 + one admin
    click). The rule must be a lexicon rule of a T9 writer, a candidate or approved, with a passing lint, a usable
    phrase, a replacement that adds nothing, and an R0 result on its current text (`rule_sha`) with `l0_ok`."""
    from services.writing_memory.replay import rule_sha

    if rule.get("kind") != "lexicon":
        return "not_lexicon", "Only a lexicon rule takes the L0 path; other rules wait for a release (AA-919)"
    if rule.get("writer") not in L0_WRITERS:
        return "no_l0_reader", f"No L0 reader for writer {rule.get('writer')} yet (T9 blog and social only)"
    if rule.get("status") not in ("candidate", "approved"):
        return "not_activatable", f"A {rule.get('status')} rule cannot be activated"
    lint = rule.get("lint") if isinstance(rule.get("lint"), dict) else {}
    if not lint.get("ok"):
        return "lint", "The rule's lint did not pass"
    problem = phrase_problem(phrase_of(rule))
    if problem:
        return "phrase", problem[0].upper() + problem[1:]
    rep = replacement_check(phrase_of(rule), rule.get("replacement"))
    if not rep["ok"]:
        return "replacement_adds", f"The replacement adds {rep['detail']}"
    r0 = lint.get("r0") if isinstance(lint.get("r0"), dict) else None
    if r0 is None:
        return "r0_missing", "Run R0 on this rule first"
    if r0.get("rule_sha") != rule_sha(rule):
        return "r0_stale", "The rule changed after its R0 run; run R0 again"
    if not r0.get("l0_ok"):
        return "r0_failed", f"R0 did not pass: {r0.get('l0_reason') or 'no reason given'}"
    word = replacement_forbidden(phrase_of(rule), rule.get("replacement"))
    if word:
        return "replacement_forbidden", f"The replacement uses '{word}', which the writer is told never to use"
    return None


async def load_active(pool, writer: str, tenant_id) -> list[dict]:
    """The active lexicon rules one owner's writer gets (platform + that tenant's own). Any read failure (the CIS 280
    table missing, a DB error) is logged and gives no rule: the write goes on as before 918."""
    from services.writing_memory import rules as rules_mod

    if writer not in L0_WRITERS:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await rules_mod.rules_for(conn, writer, tenant_id, ("active",))
    except Exception as e:  # noqa: BLE001 — no learned rule is the pre-918 behaviour
        logger.warning("aa918_l0_rules_unavailable", writer=writer, error=f"{type(e).__name__}: {str(e)[:200]}")
        return []
    return [r for r in rows if r.get("kind") == "lexicon"]


async def l0_for_write(pool, *, tenant_id, channel: Optional[str], pack: Optional[dict], cta: Optional[str],
                       trip_name: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
    """(the pack with the learned lexicon, the record for writer_input) for one T9 write. The record is None when no
    active rule applies to this writer and owner, so the context and writer_input keep their pre-918 shape."""
    writer = writer_for_channel(channel)
    rules = await load_active(pool, writer, tenant_id)
    if not rules:
        return pack, None
    applied, skipped = select_rules(rules, pack=pack, cta=cta, brand_name=(pack or {}).get("brand_name"),
                                    trip_name=trip_name, channel=channel)
    if applied or skipped:
        logger.info("aa918_l0_applied", writer=writer, applied=[e["rule_id"] for e in applied],
                    skipped=[e["rule_id"] for e in skipped])
    return apply_to_pack(pack, applied), {"writer": writer, "applied": applied, "skipped": skipped}


__all__ = ["L0_WRITERS", "PHRASE_MAX_WORDS", "activation_problem", "apply_to_pack", "l0_for_write", "load_active",
           "phrase_of", "phrase_problem", "replacement_check", "replacement_forbidden", "select_rules",
           "without_learned"]
