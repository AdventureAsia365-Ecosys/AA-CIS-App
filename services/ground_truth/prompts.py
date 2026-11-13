"""AA-1048 — the prompt versions of the R1 claim instruments (services/ground_truth/r1.py).

A prompt version is the system text of each mode (entailment, extraction) plus the user templates the claim is put
into. The instrument id names its version ("<model>@<connection>:<mode>" for v1, "...:<mode>#v2" otherwise), and every
call records the version and the hash of its texts (`PromptVersion.tag`, written to shared.llm_call_log.prompt_version
through billed_fields and to quality_signal), so a measurement always says which prompt it measured.

  v1  the R1 prompt of S272-S278 (claim fragments against the aligned source lines; extraction lists clauses). R1 on
      gpt-6-luna extraction: precision 0.22-0.27, recall about 0.9 (root audit docs/audits/2026-11-13-S279-session/
      aa1048/panel-report.md). Kept selectable and the default, so the R1 records stay reproducible.
  v2  the S279 panel's changes B1-B5 (panel report section 5): judge whole sentences and allow paraphrase and one-step
      inference (B1), place background is allowed until it becomes a trip promise (B2), the seed's "Place background"
      lines support claims about the place (B3), headings / FAQ questions / CTAs / disclaimers / keyword titles are
      judged with the text that follows them (B4), and the confirmed B-promise patterns stay as positive examples so
      recall holds (B5). Entailment v2 reads the whole source and the text around the sentence, not only the aligned
      lines (support the instrument did not see was one cause of false fires).

Every example in these prompts uses invented places and phrasing (the golden-set text guard; nothing is copied from a
tour, a label or an audit). Pure: no DB, no network, no model call.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from services.ground_truth import policy

DEFAULT = "v1"

# ── v1 (unchanged texts of r1.py before S279) ──────────────────────────────────────────────────────────────────────

_RULES = (
    "A claim about THIS trip (an activity, a service, a meal, a time, a stay, a view, a number, a name, a day) must be "
    "stated by the source; otherwise it is unsupported.",
    "Background about a place the source names (history, geography, culture) is allowed, but it never promises the "
    "trip's activities, services, times or views.",
    "Reflection or general feeling that states no fact about the trip is voice, not a claim.",
    "Hypothetical framing ('you might', 'imagine') that names a concrete trip activity, service, time or view is a "
    "promise and needs the source.",
    "Suitability, safety, pace, rarity or uniqueness of the trip only as the source states it.",
)
ENTAILMENT_SYSTEM = (
    "You check ONE claim from a travel text against the SOURCE LINES the trip is built from.\n"
    + "\n".join(f"- {r}" for r in _RULES)
    + "\nAnswer 'supported' only when the source lines state what the claim says about the trip. Quote the source "
      "line you relied on (empty when unsupported). Reply with the JSON object only.")
EXTRACTION_SYSTEM = (
    "You read a travel TEXT and the SOURCE it was written from, and list every claim in the TEXT that the SOURCE "
    "does not support.\n" + "\n".join(f"- {r}" for r in _RULES)
    + "\nQuote each claim exactly as it appears in the TEXT (a sentence or a clause), name its class from the list "
      "given, and say why in one sentence. List nothing that the source supports. Reply with the JSON object only.")
V1_ENTAILMENT_USER = "SOURCE LINES:\n{lines}\n\nCLAIM ({field}):\n{claim}"
NO_LINE = "- (no source line shares a word with the claim)"
EXTRACTION_USER = "CLASSES: {classes}\n\nSOURCE:\n{source}\n\nTEXT ({field}):\n{text}"

# ── v2 (S279 panel B1-B5) ──────────────────────────────────────────────────────────────────────────────────────────

_V2_RULES = (
    # B1: whole sentences, paraphrase and one-step inference, with examples.
    "Judge the whole sentence the way a careful editor would, never fragment by fragment or word by word. A sentence "
    "is supported when the source states the same thing in other words (paraphrase) or when it follows from the "
    "source in one obvious step (one-step inference). Examples, all supported:\n"
    "  - source 'drive back to Varno on the last afternoon', sentence 'a round trip from Varno' (one step);\n"
    "  - source 'climb to the top of Mount Selk', sentence 'reach the summit of Mount Selk' (paraphrase);\n"
    "  - source '9 nights', sentence 'ten days on the road' (the same span);\n"
    "  - source 'Orvin, then Tessa', sentence 'south from Orvin to Tessa' when Tessa lies south of Orvin (where the "
    "named places are);\n"
    "  - tone and feeling words ('unhurried', 'a gentle start', 'memorable') that add no fact about the trip.",
    # B2: place background is allowed until it becomes a trip promise.
    "Background about a place the source names (its history, age, builder, architecture, nickname, region, position "
    "relative to another place) is allowed even when the source does not state it. It needs the source only when the "
    "sentence turns it into a promise about this trip: a view, an activity, a service, a time or a condition the "
    "traveller will meet.",
    # B3: the seed's place background lines are support for the place itself.
    "Lines listed under 'Place background' in the source are checked facts about the place itself. They support any "
    "sentence about that place (what it is, when it was built, what it holds or looks like), quoted or paraphrased. "
    "They do not support what this trip does there.",
    # B4: headings, FAQ questions, CTAs, disclaimers and keyword titles.
    "Headings, FAQ questions, calls to action, disclaimers and keyword titles state nothing on their own: judge them "
    "together with the text that follows them (AFTER), and treat them as supported when that text is supported.",
    # B5: the confirmed promise patterns stay unsupported (recall).
    "Still unsupported, the promises this check exists for (the source must state them):\n"
    "  - the character of a meal ('eat local', 'a home-cooked lunch') when the source names only a restaurant or a "
    "meal break;\n"
    "  - qualities of the guide ('a local who knows every lane and story') when the source says only that a guide "
    "leads;\n"
    "  - a group or social experience ('swap stories with the others on the tour') when the source speaks of one "
    "traveller or says nothing about other travellers;\n"
    "  - an arrival or crowd atmosphere ('Tessa is still quiet when you arrive', 'ahead of the crowds') the source "
    "does not give;\n"
    "  - an experience at a stop (a view, a light, something done or felt there) the source does not describe;\n"
    "  - a number, name, day, order or direction that contradicts the source.",
)
ENTAILMENT_SYSTEM_V2 = (
    "You check ONE sentence of a travel text against the SOURCE the trip is built from: is the reader promised "
    "anything about THIS trip that the source does not give?\n"
    + "\n".join(f"- {r}" for r in _V2_RULES)
    + "\nAnswer 'unsupported' only for such a promise or contradiction, and name the words that make it in 'why'. "
      "Otherwise answer 'supported' and quote the source line you relied on (empty when the sentence is voice, "
      "background or a heading). Reply with the JSON object only.")
EXTRACTION_SYSTEM_V2 = (
    "You read a travel TEXT and the SOURCE it was written from, and list every sentence of the TEXT that promises "
    "something about THIS trip that the SOURCE does not give.\n"
    + "\n".join(f"- {r}" for r in _V2_RULES)
    + "\nQuote each such sentence whole, exactly as it appears in the TEXT, name its class from the list given, and "
      "say why in one sentence. List nothing that is supported, background, voice or a heading. Reply with the JSON "
      "object only.")
V2_ENTAILMENT_USER = ("SOURCE:\n{source}\n\nTEXT AROUND THE SENTENCE ({field}):\nBEFORE: {before}\nSENTENCE: {claim}\n"
                      "AFTER: {after}")
NONE_TEXT = "(none)"


# ── user prompt builders (items are services.ground_truth.r1.Item; duck-typed to keep this module import-free) ──────


def v1_entailment_user(item: Any) -> str:
    lines = "\n".join(f"- {ln}" for ln in item.evidence) or NO_LINE
    return V1_ENTAILMENT_USER.format(lines=lines, field=item.field, claim=item.claim_text)


def extraction_user(items: list[Any]) -> str:
    first = items[0]
    classes = sorted({c.id for c in policy.policy_classes().values() if first.writer in c.writers})
    return EXTRACTION_USER.format(classes=", ".join(classes), source=first.source_text, field=first.field,
                                  text=first.field_text)


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def around(field_text: str, claim_text: str) -> tuple[str, str]:
    """(before, after) of the claim inside its field: the rest of its own line on each side, else the nearest
    non-empty line. Both empty when the claim is not found verbatim in the field text."""
    start = (field_text or "").find(claim_text or "\0")
    if start < 0 or not claim_text:
        return "", ""
    head, tail = field_text[:start], field_text[start + len(claim_text):]
    head_line, _, head_rest = head.rpartition("\n")
    before = head_rest.strip() or _first_line("\n".join(reversed(head_line.splitlines())))
    tail_line, _, tail_rest = tail.partition("\n")
    after = tail_line.strip() or _first_line(tail_rest)
    return before, after


def v2_entailment_user(item: Any) -> str:
    before, after = around(item.field_text, item.claim_text)
    return V2_ENTAILMENT_USER.format(source=item.source_text or NONE_TEXT, field=item.field,
                                     before=before or NONE_TEXT, claim=item.claim_text, after=after or NONE_TEXT)


# ── versions ───────────────────────────────────────────────────────────────────────────────────────────────────────


class UnknownPromptVersion(ValueError):
    """A prompt version that is not in VERSIONS."""


@dataclass(frozen=True)
class PromptVersion:
    id: str
    entailment_system: str
    extraction_system: str
    entailment_user: Callable[[Any], str]
    extraction_user: Callable[[list[Any]], str]
    templates: tuple[str, ...]

    def system(self, mode: str) -> str:
        return self.entailment_system if mode == "entailment" else self.extraction_system

    @property
    def sha(self) -> str:
        """sha256 of every text the version sends (systems and user templates), first 12 hex."""
        body = "\x1f".join((self.id, self.entailment_system, self.extraction_system, *self.templates))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]

    @property
    def tag(self) -> str:
        """What a call records as its prompt version: "aa1048-r1-<id>@<sha>"."""
        return f"aa1048-r1-{self.id}@{self.sha}"


VERSIONS: dict[str, PromptVersion] = {
    "v1": PromptVersion("v1", ENTAILMENT_SYSTEM, EXTRACTION_SYSTEM, v1_entailment_user, extraction_user,
                        (V1_ENTAILMENT_USER, NO_LINE, EXTRACTION_USER)),
    "v2": PromptVersion("v2", ENTAILMENT_SYSTEM_V2, EXTRACTION_SYSTEM_V2, v2_entailment_user, extraction_user,
                        (V2_ENTAILMENT_USER, NONE_TEXT, EXTRACTION_USER)),
}


def get(version: str) -> PromptVersion:
    if version not in VERSIONS:
        raise UnknownPromptVersion(f"unknown prompt version {version!r} (known: {', '.join(sorted(VERSIONS))})")
    return VERSIONS[version]


__all__ = ["DEFAULT", "ENTAILMENT_SYSTEM", "ENTAILMENT_SYSTEM_V2", "EXTRACTION_SYSTEM", "EXTRACTION_SYSTEM_V2",
           "PromptVersion", "UnknownPromptVersion", "VERSIONS", "around", "extraction_user", "get",
           "v1_entailment_user", "v2_entailment_user"]
