"""AA-984 — deterministic trim of an over-long blog into its F4 band.

S247 round 9, piece 32789658: the blog came back from its attempt-2 full rewrite at 14,576 chars (band
4,700–13,000, quality_gates._BLOG_MAX_CHARS) and was held on F4_extreme_length — F4 is not sentence-repairable,
so plan() returned None and no attempt 3 ran. The rewrite feedback carried no target size, and a second
rewrite would trade the length for new invented claims (S233: a full rewrite fixes F9 0/6).

`trim_to_band` runs on every blog attempt right before the gates (service.run_write_background, after every LLM
step and deterministic fix of the attempt). When the tag-free text is over the band's maximum it drops whole
paragraphs, lowest value first. Two thresholds (S248 review round 2): an untagged paragraph (no citation tag,
so no cited source fact) is dropped while the text is over BLOG_TRIM_TARGET_CHARS; a tagged one only while it is
still over the band's maximum — a text the untagged drops bring inside the band keeps every cited paragraph.

- droppable: a prose paragraph of a body section that is not the section's first paragraph (the one that
  introduces its moment), not in the FAQ, holding no CTA sentence, no list item or FAQ line, not a list lead-in
  (ending on ':'), not the only paragraph that names one of the trip's moments, not the only body paragraph
  that carries one of its citation ids (the trip's last stop, 'the National Institute for Zorig Chusum', is
  cited once and named unlike its itinerary label — S248 review on 32789658), and not followed in its section
  by a paragraph that opens on a pronoun ('It…', 'This is…');
- never the paragraph that says a moment's family access caveat: the trim runs after
  trip_facts.add_family_caveats, so a drop that makes trip_facts.missing_family_caveat report a moment it did
  not report before is skipped (it would reopen a blocking F17_trip_fact — S248 review, terra-family);
- order: an untagged paragraph (no citation tag, so no cited source fact) before a tagged one, then the longest
  section, then the latest paragraph of that section;
- each drop must leave the text clean at its seam (sentence_repair._integrity on the section heading and the
  paragraphs around the drop: no pronoun left without its referent, no new duplicate, echo or orphan line),
  never under the band's minimum, and never break a complete ending; a candidate that fails is skipped.

All or nothing: when the safe drops cannot bring the text under the band's maximum, the text is returned
unchanged and the old path runs (the attempt-1 full rewrite, now told the target size; a hold at attempt 2).
Whole paragraphs only — never a sentence-level prose trim (lesson S234 r3: orphaned pronouns)."""
from __future__ import annotations

import re
from typing import Optional

from services.acp_content_writing.hygiene import (_HEADING_RE, _is_cta, dangling_opener, sentence_spans,
                                                  tidy_after_cut)
from services.acp_content_writing.quality_gates import _length_bounds_for_channel, gate_complete
from services.acp_shared.citation_tags import TAG_RE, strip_citation_tags

# 500 under the 13,000 gate maximum: room for the gate's own measure and nothing after the gates adds text.
BLOG_TRIM_TARGET_CHARS = 12_500
# What an over-long blog's full rewrite is told to reach (service._repair_feedback).
REWRITE_TARGET_CHARS = 12_000
_LIST_LINE_RE = re.compile(r"^(?:[-*]\s|\d+[.)]\s)")


def measure(text: Optional[str]) -> int:
    """The length F4 reads: the tag-stripped text (quality_gates.run_quality_gates `length_check_text`)."""
    return len(strip_citation_tags(text or ""))


def _blocks(text: str, cta: Optional[str]) -> list[dict]:
    """The paragraphs of `text` (blank-line separated) with offsets: {"start", "end", "raw", "clean",
    "section", "pos", "droppable", "tags"}. `section` counts headings (0 = before the first one); `pos` is the
    paragraph's index among its section's prose paragraphs."""
    out: list[dict] = []
    section, in_faq, pos = 0, False, 0
    for m in re.finditer(r"[^\n]+(?:\n[ \t]*[^\s][^\n]*)*", text):
        raw = m.group(0)
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        if not lines:
            continue
        first = lines[0]
        if _HEADING_RE.match(first):
            section += 1
            pos = 0
            title = first.lstrip("#").strip().lower()
            in_faq = title.startswith(("faq", "frequently asked"))
            out.append({"start": m.start(), "end": m.end(), "raw": raw, "kind": "heading", "section": section,
                        "droppable": False})
            continue
        clean = " ".join(strip_citation_tags(raw, " ").split())
        special = (in_faq or any(_LIST_LINE_RE.match(ln) or ln.startswith(("**Q:", "A:", "#")) for ln in lines)
                   or clean.endswith(":"))
        cta_here = any(_is_cta(sp["clean"], cta) for sp in sentence_spans(raw)) or bool(cta and cta in raw)
        out.append({"start": m.start(), "end": m.end(), "raw": raw, "clean": clean, "kind": "para",
                    "faq": in_faq, "section": section, "pos": pos, "tags": len(TAG_RE.findall(raw)),
                    "ids": _tag_ids(raw),
                    "droppable": not special and not cta_here and pos > 0})
        pos += 1
    return out


def _tag_ids(raw: str) -> frozenset:
    """The citation ids a paragraph carries ('[R:a1]', '[R:a1, a2]' → {'a1', 'a2'})."""
    return frozenset(i for m in TAG_RE.finditer(raw) for i in re.split(r"[,;\s]+", m.group(1)) if i)


def _sole_cited(block: dict, blocks: list[dict]) -> bool:
    """Is `block` the only body paragraph (not the FAQ) carrying one of its citation ids? Dropping it would
    drop that source fact from the piece."""
    if not block["ids"]:
        return False
    others = set()
    for b in blocks:
        if b is not block and b["kind"] == "para" and not b["faq"]:
            others |= b["ids"]
    return bool(block["ids"] - others)


def _sole_namer(block: dict, blocks: list[dict], moments: Optional[list]) -> bool:
    """Is `block` the only body paragraph naming one of the moments it names?"""
    if not moments:
        return False
    from services.acp_content_writing.trip_facts import moments_named  # lazy: a T9 module
    named = moments_named(block["clean"], moments)
    others = [b for b in blocks if b is not block and b["kind"] == "para" and not b["faq"]]
    return any(not any(m in moments_named(b["clean"], moments) for b in others) for m in named)


def _seam(block: dict, blocks: list[dict]) -> tuple[str, str]:
    """(before, after) of the drop around its seam: the section heading, the paragraph before, the dropped one
    and the paragraph after (same section). A paragraph drop can only break what meets at the seam (a new
    near-duplicate, echo or orphan line between the two neighbours, an opener that lost its referent): a
    repeat between distant sentences existed before. integrity_problems is quadratic in the sentences, so it
    reads this window, not the whole 14k-char text (≈ 3 s per call on the full text)."""
    k = blocks.index(block)
    head = next((b["raw"] for b in reversed(blocks[:k]) if b["kind"] == "heading"), None)
    prev = blocks[k - 1]["raw"] if k > 0 and blocks[k - 1]["kind"] == "para" else None
    nxt = blocks[k + 1] if k + 1 < len(blocks) else None
    nxt_raw = nxt["raw"] if nxt is not None and nxt["kind"] == "para" and nxt["section"] == block["section"] else None
    kept = [p for p in (head, prev, nxt_raw) if p]
    with_block = [p for p in (head, prev, block["raw"], nxt_raw) if p]
    return "\n\n".join(with_block), "\n\n".join(kept)


def _orphans_next(block: dict, blocks: list[dict]) -> bool:
    """Does the paragraph right after `block` in its section open on a pronoun ('It…', 'This is…') that the
    dropped paragraph may have been its referent for?"""
    k = blocks.index(block)
    nxt = blocks[k + 1] if k + 1 < len(blocks) else None
    if nxt is None or nxt["kind"] != "para" or nxt["section"] != block["section"]:
        return False
    first = next((sp["clean"] for sp in sentence_spans(nxt["raw"])), "")
    return dangling_opener(first)


def _caveat_misses(text: str, trip_ctx) -> frozenset:
    """The moments trip_facts.missing_family_caveat reports for `text` (empty without a trip context)."""
    if trip_ctx is None:
        return frozenset()
    from services.acp_content_writing.trip_facts import missing_family_caveat  # lazy: a T9 module
    return frozenset(m["moment"] for m in missing_family_caveat(text, trip_ctx.moments, trip_ctx.audience,
                                                                 trip_ctx.cta))


def trim_to_band(text: str, *, channel: Optional[str], cta: Optional[str] = None,
                 ignore: frozenset = frozenset(), moments: Optional[list] = None,
                 trip_ctx=None) -> tuple[str, list[str]]:
    """(text, log): an over-long blog trimmed into its band by dropping whole low-value paragraphs (untagged
    ones down to BLOG_TRIM_TARGET_CHARS, tagged ones only while over the band's maximum — module docstring),
    or the text unchanged and [] — any other channel, a blog inside its band, or one the safe drops cannot
    bring under the band's maximum. `trip_ctx` (trip_facts.TripFactContext) guards
    the family access caveats."""
    if channel != "blog" or not text:
        return text, []
    from services.acp_content_writing.sentence_repair import _integrity  # lazy: sentence_repair imports hygiene
    lo, hi = _length_bounds_for_channel("blog")
    before = measure(text)
    if before <= hi:
        return text, []
    cur, dropped, skipped = text, [], set()
    caveats_missing = _caveat_misses(cur, trip_ctx)
    while measure(cur) > BLOG_TRIM_TARGET_CHARS:
        blocks = _blocks(cur, cta)
        over_band = measure(cur) > hi
        # an untagged paragraph down to the target; a tagged one only while still over the band (review round 2)
        cands = [b for b in blocks if b["droppable"] and b["clean"] not in skipped and (over_band or not b["tags"])]
        if not cands:
            break
        sec_len: dict[int, int] = {}
        for b in blocks:
            if b["kind"] == "para":
                sec_len[b["section"]] = sec_len.get(b["section"], 0) + measure(b["raw"])
        b = min(cands, key=lambda c: (c["tags"] > 0, -sec_len[c["section"]], -c["pos"]))
        trial = tidy_after_cut(cur[:b["start"]] + cur[b["end"]:])
        if (_sole_namer(b, blocks, moments) or _sole_cited(b, blocks) or _orphans_next(b, blocks)
                or measure(trial) < lo
                or (gate_complete(cur)["passed"] and not gate_complete(trial)["passed"])
                or _integrity(*_seam(b, blocks), ignore, cta, moments)
                or _caveat_misses(trial, trip_ctx) - caveats_missing):
            skipped.add(b["clean"])
            continue
        cur = trial
        dropped.append(b["clean"])
    after = measure(cur)
    if not dropped or after > hi:
        return text, []
    return cur, [f"trimmed {len(dropped)} paragraph(s) to fit the blog length band ({before:,} → {after:,} chars, "
                 f"max {hi:,}): " + " | ".join(f"'{d[:80]}'" for d in dropped)]


__all__ = ["BLOG_TRIM_TARGET_CHARS", "REWRITE_TARGET_CHARS", "measure", "trim_to_band"]
