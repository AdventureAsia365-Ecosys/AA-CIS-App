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
  v3  S280: v2's evaluation (run 3018008d) dropped 48% of the sampled false positives, under the 60% bar; most of
      the rest are paraphrase. v3 replaces B1 with a restatement test (find the source statements, set aside wording
      that adds no checkable fact, fire only on a fact the instrument can name: P1), lets a trip-wide fact support
      one day (P2), widens B2 to crafts and customs (P2b), treats a label made of source words and an inferred day
      start as paraphrase (P3), and adds an unstated order and a number bound to the wrong thing to B5. Same user
      prompts as v2. The claims of v2's evaluation subset shaped v3, so they are in-sample for it: instrument_eval
      --exclude keeps them out of v3's subset.
  v4  S280: v3's evaluation (run 33835b87) dropped 68% of the sampled false positives (pass) but kept 85% of the
      stored violations (bar 90%). v4 keeps v3's restatement test and adds pace / effort / suitability to the facts,
      a guard against a restatement that adds a new specific (or joins two source facts into one), two one-step
      readings of the source's order, a visited place's own lasting features as background, P3 for sentences, and
      P4 for sentences about the text itself. Shaped on the v2 and v3 runs: both are excluded from its evaluation
      (PromptVersion.shaped_on, checked by instrument_eval before a live run).

Every example in these prompts uses invented places and phrasing (the golden-set text guard). The B5 examples name the
pattern classes of the five claims the tech lead relabelled to violation in S279, in wording of their own: checked
against the evaluation pool before S279 shipped, no example shares a three-word sequence (with a content word) with any
pool claim, and none shares a content word with a relabelled claim other than 'before'. The relabelled claims
still shaped the patterns, so they are in-sample for v2: instrument_eval scores them apart and gates on the stored
violations only. Pure: no DB, no network, no model call.
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
    "  - source 'come back to Varno at the end', sentence 'a round trip from Varno' (one step);\n"
    "  - source 'hike up Mount Selk', sentence 'walk to the highest point of Mount Selk' (paraphrase);\n"
    "  - source '9 nights', sentence 'ten days in all' (the same span);\n"
    "  - source 'Orvin, then Tessa', sentence 'south from Orvin to Tessa' when Tessa lies south of Orvin (where the "
    "named places are);\n"
    "  - tone and feeling words ('unhurried', 'restful', 'memorable') that add no fact about the trip.",
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
    # B5: the confirmed promise patterns stay unsupported (recall). The examples name each pattern in wording of
    # their own: no example shares a sentence frame or a distinctive phrase with a claim of the evaluation pool.
    "Still unsupported, the promises this check exists for (the source must state them):\n"
    "  - the character of a meal ('a family-style dinner', 'recipes handed down for generations') when the source "
    "names only a restaurant or a meal break;\n"
    "  - qualities of the guide ('a guide who grew up in the valley') when the source says only that a guide "
    "leads;\n"
    "  - a group or social experience ('new friends by the end of the week') when the source speaks of one "
    "traveller or says nothing about other travellers;\n"
    "  - an arrival or crowd atmosphere ('the plaza is deserted for us', 'before the coach parties') the source "
    "does not give;\n"
    "  - an experience at a stop (scenery seen, a sound heard, a mood felt there) the source does not describe;\n"
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

# ── v3 (S280: paraphrase, the cause of most v2 false fires) ────────────────────────────────────────────────────────
# v2 dropped 12 of 25 sampled false positives; 10 of the 13 it still fired on restate the source in other words with
# a style word, a viewpoint, a framing verb, a trip-wide fact placed on one day, a descriptive label or the writer's
# reading of the facts (S279 run 3018008d + the panel's reasons). v3 replaces B1 with a restatement test that sets
# that wording aside and fires only on a fact it can name (P1), adds trip-wide facts (P2), descriptive labels and day
# headings (P3), and widens B2 to crafts and customs (P2b). B3 and B4 are v2's texts unchanged. The B5 list keeps
# every v2 item and adds an unstated order and a number bound to the wrong thing (the two stored violations v2 lost).
# All examples are invented (Karsk, Brellin, Ostra): no example shares a three-word sequence with a
# content word with any claim of the evaluation pool (checked before S280 shipped; the pool stays in .tmp-session/).

_V3_RULES = (
    # P1 (replaces B1): the restatement test.
    "Judge the whole sentence the way a careful editor would, never fragment by fragment or word by word, in three "
    "steps.\n"
    "  1. Find the source statements the sentence restates. Look through the WHOLE source (overview, highlights, "
    "every day, inclusions, place background), not only the lines next to it.\n"
    "  2. Set aside wording that adds no checkable fact: synonyms and summaries; images and figures of speech ('a "
    "wall of peaks' for a mountain view); the traveller's point of view ('from the boat', 'underfoot'); emphasis "
    "('entirely', 'truly', 'simply'); framing verbs ('is built around', 'centres on', 'belongs to', 'ties together'); "
    "and the writer's reading of what the source facts mean for the reader. Wording about the trip's meals, guide, "
    "group, crowds or what the traveller will see, hear or feel at a stop is NOT style: it is a fact (list below).\n"
    "  3. What is left are the sentence's facts. The sentence is supported when the source states each of them in any "
    "words, or when each follows from the source in one obvious step.\n"
    "Answer 'unsupported' only when you can name a concrete fact left after step 2 that the whole source does not "
    "give. If you cannot name one, the sentence is supported. Examples, all supported:\n"
    "  - source 'the ride lasts one to two hours, depending on the riders', sentence 'the riders alone decide how "
    "long the ride lasts' (paraphrase with emphasis);\n"
    "  - source 'olive groves grow along the road to Ostra', sentence 'olive groves roll past the window on the drive "
    "to Ostra' (point of view);\n"
    "  - source 'the cable car up Karsk hill and the hilltop fort', sentence 'the cable car lifts you "
    "toward the fort, so the two make one trip' (the writer's reading; the fort stands on the hilltop);\n"
    "  - source 'the afternoon is open for a swim, a nap or a stroll', sentence 'some guests swim while others rest' "
    "(the source's own options as choices; no option added);\n"
    "  - source 'the canoe trip runs through Brellin marsh, home to herons and otters', sentence 'the canoe trip is "
    "planned around the herons and otters of Brellin marsh' (a framing verb; no sighting promised).",
    # P2 (new): trip-wide facts support one day.
    "A fact the source states for the whole trip or the whole route (beech woods beside the path, guesthouses of one "
    "kind at every stop, vineyards along the road) supports the same fact on one day or at one stop, unless the "
    "source says something else for that one day.",
    # P2b (B2 widened): place background, crafts and customs.
    "Background about a place the source names (its history, age, builder, architecture, nickname, region, position "
    "relative to another place, how it is usually described or compared) is allowed even when the source does not "
    "state it, and so is background about a craft, food or custom the source names (how it is made, how long it has "
    "belonged to the region). A sentence that joins such background to a visit the source gives is supported. It "
    "needs the source only when the background becomes a promise about this trip: a view, an activity, a service, a "
    "time or a condition the traveller will meet.",
    _V2_RULES[2],                                                        # B3 unchanged
    _V2_RULES[3],                                                        # B4 unchanged
    # P3 (new): descriptive labels and day headings.
    "A label made only of source words for something the source describes is a paraphrase, with capitals or "
    "without: 'the Karsk Valley Lodge' when the source says the nights are at a lodge in the Karsk valley. A name "
    "with a word the source does not give ('the Karsk Royal Lodge') is a new name and unsupported. A day heading that "
    "names where a day begins and ends is supported when the start is where the trip already is (the last "
    "night's stop, the arrival town, or the town the trip returns to at the end) and the end and the means of travel "
    "are in the source.",
    # B5 extended: the promises this check exists for.
    "Still unsupported, the promises this check exists for (the source must state them):\n"
    "  - the character of a meal ('a family-style dinner', 'recipes handed down for generations') when the source "
    "names only a restaurant or a meal break;\n"
    "  - qualities of the guide ('a guide who grew up in the valley') when the source says only that a guide "
    "leads;\n"
    "  - a group or social experience ('new friends by the end of the week') when the source speaks of one "
    "traveller or says nothing about other travellers;\n"
    "  - an arrival or crowd atmosphere ('the plaza is deserted for us', 'before the coach parties') the source "
    "does not give;\n"
    "  - an experience at a stop (scenery seen, a sound heard, a mood felt there) the source does not describe;\n"
    "  - an order, sequence or schedule the source does not give ('the ferry comes first and the market second' when "
    "the source only names both);\n"
    "  - a number or measure bound to the wrong thing: read 'it', 'there' and 'this' as the thing named just BEFORE; "
    "a size, height, date or count the source gives for another place, or not at all, is unsupported;\n"
    "  - a number, name, day, order or direction that contradicts the source.",
)
ENTAILMENT_SYSTEM_V3 = (
    "You check ONE sentence of a travel text against the SOURCE the trip is built from: is the reader promised "
    "anything about THIS trip that the source does not give?\n"
    + "\n".join(f"- {r}" for r in _V3_RULES)
    + "\nAnswer 'unsupported' only for such a promise or contradiction, and in 'why' name the fact that the source "
      "does not give. Otherwise answer 'supported' and quote the source line you relied on (empty when the sentence "
      "is voice, background or a heading). Reply with the JSON object only.")
EXTRACTION_SYSTEM_V3 = (
    "You read a travel TEXT and the SOURCE it was written from, and list every sentence of the TEXT that promises "
    "something about THIS trip that the SOURCE does not give.\n"
    + "\n".join(f"- {r}" for r in _V3_RULES)
    + "\nQuote each such sentence whole, exactly as it appears in the TEXT, name its class from the list given, and "
      "in 'why' name the fact that the source does not give. List nothing that is supported, background, voice or a "
      "heading. Reply with the JSON object only.")

# ── v4 (S280: v3's recall losses and its remaining false fires) ───────────────────────────────────────────────────
# v3 (run 33835b87) dropped 17 of 25 sampled false positives (bar 60%: pass) but kept 17 of 20 stored violations (bar
# 90%: fail). Two of the three it lost are pace promises ('moves slowly', 'at a slow pace'): P1 set aside "the writer's
# reading" and its recall guard had no pace item (v1's "pace ... only as the source states it" was dropped in v2).
# The third joins two source facts into one new activity (a ride offered 'by canoe or motor boat' plus canoeing on a
# lake, written as one canoe trip). Its 8 remaining false fires: a purpose given to a source fact ('by design'), a
# part of the day that follows from the source's order, a day said to end at its last listed stop, a sentence about
# the article itself, a trip start that P3 allowed in headings only, and three lasting features of a visited monument
# (inlay, a ridge, half-submerged floors) that read as "scenery seen". v4 keeps v3's restatement test and adds:
# pace, effort and suitability to the facts (P1 step 2, B5); the new-specific guard (P1); two one-step examples (P1);
# a visited place's own lasting features as background (P2b, B5); sentences in P3; and P4 for text about the text.
# P2, B3 and B4 are v3's strings unchanged. Every example is invented (Karsk, Brellin, Ostra, Velm, Tarro), and the
# S280 leak check found no three-word sequence with a content word shared with any claim of the evaluation pool.

_V4_P1 = (
    "Judge the whole sentence the way a careful editor would, never fragment by fragment or word by word, in three "
    "steps.\n"
    "  1. Find the source statements the sentence restates. Look through the WHOLE source (overview, highlights, "
    "every day, inclusions, place background), not only the lines next to it.\n"
    "  2. Set aside wording that adds no checkable fact: synonyms and summaries; images and figures of speech ('a "
    "wall of peaks' for a mountain view); the traveller's point of view ('from the boat', 'underfoot'); emphasis "
    "('entirely', 'truly', 'simply'); framing verbs ('is built around', 'centres on', 'belongs to', 'ties together'); "
    "a purpose given to a fact the source states ('kept brief on purpose' for a visit the source calls brief); and "
    "the writer's reading of what the source facts mean for the reader. Wording about the trip's meals, guide, group, "
    "crowds, pace, effort, comfort, safety or who it suits, or about what the traveller will see, hear or feel at a "
    "stop, is NOT style: it is a fact (list below).\n"
    "  3. What is left are the sentence's facts. The sentence is supported when the source states each of them in any "
    "words, or when each follows from the source in one obvious step.\n"
    "A restatement must not add a new specific: a number, name, time, service, activity or view that the whole "
    "source does not give and that does not follow from it in one obvious step. Joining two source facts into one "
    "new specific adds one ('kayaking through the reed beds of Velm' when the source has the reed beds 'by kayak or "
    "launch' and the kayaking on Velm lake), and so do firming one side of an 'or' the source leaves open and "
    "stretching a fact to days or nights the source gives to something else.\n"
    "Answer 'unsupported' only when you can name a concrete fact left after step 2 that the whole source does not "
    "give. If you cannot name one, the sentence is supported. Examples, all supported:\n"
    "  - source 'the ride lasts one to two hours, depending on the riders', sentence 'the riders alone decide how "
    "long the ride lasts' (paraphrase with emphasis);\n"
    "  - source 'olive groves grow along the road to Ostra', sentence 'olive groves roll past the window on the drive "
    "to Ostra' (point of view);\n"
    "  - source 'the cable car up Karsk hill and the hilltop fort', sentence 'the cable car lifts you "
    "toward the fort, so the two make one trip' (the writer's reading; the fort stands on the hilltop);\n"
    "  - source 'the afternoon is open for a swim, a nap or a stroll', sentence 'some guests swim while others rest' "
    "(the source's own options as choices; no option added);\n"
    "  - source 'the canoe trip runs through Brellin marsh, home to herons and otters', sentence 'the canoe trip is "
    "planned around the herons and otters of Brellin marsh' (a framing verb; no sighting promised);\n"
    "  - source 'lunch at the farm, then Tarro abbey' for one day, sentence 'Tarro abbey fills the hours after the "
    "meal, so it is an afternoon stop' (the afternoon follows from the source's order: what comes after lunch is "
    "afternoon; a clock time would be new);\n"
    "  - source 'day 3: the harbour market of Ostra after dark' and nothing after it that day, sentence 'day 3 "
    "closes at the harbour market' (the last thing the source gives for a day ends that day).")

_V4_P2B = (
    "Background about a place the source names (its history, age, builder, architecture, nickname, region, position "
    "relative to another place, how it is usually described or compared) is allowed even when the source does not "
    "state it, and so is background about a craft, food or custom the source names (how it is made, how long it has "
    "belonged to the region). A sentence that joins such background to a visit the source gives is supported. The "
    "visited place's own lasting features (its architecture, carving or inlay, its setting high on a hillside or "
    "amid water, the size of what stands there) stay background when the sentence puts the traveller before them "
    "('walk its arcades', 'see the carved screens of Karsk chapel up close'): a visit the source gives means seeing "
    "what is there. Background needs the source only when the background turns into a promise about this trip: an "
    "activity, a service, a time, a condition the traveller will meet, access the source does not give (a closed "
    "hall, a roof), or a view, which means a sight beyond the place that the traveller looks out at, or the place "
    "itself lit, timed or emptied as the source does not give it (at sunrise, under snow, without visitors).")

_V4_P3 = (
    "A label made only of source words for something the source describes is a paraphrase, with capitals or "
    "without: 'the Karsk Valley Lodge' when the source says the nights are at a lodge in the Karsk valley. A name "
    "with a word the source does not give ('the Karsk Royal Lodge') is a new name and unsupported. A day heading or a "
    "sentence that names where the trip or a day begins and ends is supported when the start is where the trip "
    "already is (the last night's stop, the arrival town, or the town the trip returns to at the end) and the end "
    "and the means of travel are in the source.")

_V4_P4 = (
    "A sentence about the text itself (what this guide, article or page covers, explains, or gives most room to) "
    "promises nothing about the trip by saying so. Judge only the trip facts inside it (a route, a first stop, where "
    "the trip starts) by the other rules: a wrong first stop or start inside such a sentence is still unsupported.")

_V4_B5 = (
    "Still unsupported, the promises this check exists for (the source must state them):\n"
    "  - the character of a meal ('a family-style dinner', 'recipes handed down for generations') when the source "
    "names only a restaurant or a meal break;\n"
    "  - qualities of the guide ('a guide who grew up in the valley') when the source says only that a guide "
    "leads;\n"
    "  - a group or social experience ('new friends by the end of the week') when the source speaks of one "
    "traveller or says nothing about other travellers;\n"
    "  - an arrival or crowd atmosphere ('the plaza is deserted for us', 'before the coach parties') the source "
    "does not give;\n"
    "  - an experience at a stop (a view from it, its light or weather, wildlife, a sound heard, a mood felt there) "
    "the source does not describe; the visited place's own lasting features are background, not such an "
    "experience;\n"
    "  - pace, effort, comfort, safety or who the trip suits ('an unrushed week', 'easy enough for anyone', "
    "'gentle walking throughout') when the source does not state it: rest days, short drives or level ground do not "
    "by themselves make a trip slow or easy;\n"
    "  - an order, sequence or schedule the source does not give ('the ferry comes first and the market second' when "
    "the source only names both);\n"
    "  - a number or measure bound to the wrong thing: read 'it', 'there' and 'this' as the thing named just BEFORE; "
    "a size, height, date or count the source gives for another place, or not at all, is unsupported;\n"
    "  - a number, name, day, order or direction that contradicts the source.")

_V4_RULES = (
    _V4_P1,                                                              # P1 + pace, purpose, new specific, 2 steps
    _V3_RULES[1],                                                        # P2 unchanged
    _V4_P2B,                                                             # P2b + lasting features, what a view is
    _V3_RULES[3],                                                        # B3 unchanged
    _V3_RULES[4],                                                        # B4 unchanged
    _V4_P3,                                                              # P3 + sentences
    _V4_P4,                                                              # P4 (new): text about the text
    _V4_B5,                                                              # B5 + pace item, lasting features
)
ENTAILMENT_SYSTEM_V4 = (
    "You check ONE sentence of a travel text against the SOURCE the trip is built from: is the reader promised "
    "anything about THIS trip that the source does not give?\n"
    + "\n".join(f"- {r}" for r in _V4_RULES)
    + "\nAnswer 'unsupported' only for such a promise or contradiction, and in 'why' name the fact that the source "
      "does not give. Otherwise answer 'supported' and quote the source line you relied on (empty when the sentence "
      "is voice, background or a heading). Reply with the JSON object only.")
EXTRACTION_SYSTEM_V4 = (
    "You read a travel TEXT and the SOURCE it was written from, and list every sentence of the TEXT that promises "
    "something about THIS trip that the SOURCE does not give.\n"
    + "\n".join(f"- {r}" for r in _V4_RULES)
    + "\nQuote each such sentence whole, exactly as it appears in the TEXT, name its class from the list given, and "
      "in 'why' name the fact that the source does not give. List nothing that is supported, background, voice or a "
      "heading. Reply with the JSON object only.")


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
    # Run-id prefixes (8 hex) of the evaluations whose misses shaped this version. Their claims are in-sample for it,
    # so instrument_eval refuses a live run of it that does not --exclude each of those run reports.
    shaped_on: tuple[str, ...] = ()

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
    # v3 sends v2's user prompts (whole source, BEFORE / SENTENCE / AFTER); only the system texts change.
    "v3": PromptVersion("v3", ENTAILMENT_SYSTEM_V3, EXTRACTION_SYSTEM_V3, v2_entailment_user, extraction_user,
                        (V2_ENTAILMENT_USER, NONE_TEXT, EXTRACTION_USER), shaped_on=("3018008d",)),
    # v4 sends the same user prompts again; it was shaped on the misses of v2's run (through v3) and of v3's run.
    "v4": PromptVersion("v4", ENTAILMENT_SYSTEM_V4, EXTRACTION_SYSTEM_V4, v2_entailment_user, extraction_user,
                        (V2_ENTAILMENT_USER, NONE_TEXT, EXTRACTION_USER), shaped_on=("3018008d", "33835b87")),
}


def get(version: str) -> PromptVersion:
    if version not in VERSIONS:
        raise UnknownPromptVersion(f"unknown prompt version {version!r} (known: {', '.join(sorted(VERSIONS))})")
    return VERSIONS[version]


__all__ = ["DEFAULT", "ENTAILMENT_SYSTEM", "ENTAILMENT_SYSTEM_V2", "ENTAILMENT_SYSTEM_V3", "ENTAILMENT_SYSTEM_V4",
           "EXTRACTION_SYSTEM", "EXTRACTION_SYSTEM_V2", "EXTRACTION_SYSTEM_V3", "EXTRACTION_SYSTEM_V4",
           "PromptVersion", "UnknownPromptVersion", "VERSIONS", "around", "extraction_user", "get",
           "v1_entailment_user", "v2_entailment_user"]
