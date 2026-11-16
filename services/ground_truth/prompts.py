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
  v5c / v5j  S281: the TWO-STEP instrument (services/ground_truth/two_step.py). v2, v3 and v4 were all no_go and two
      rounds in a row traded one side for the other (v4's new-specific guard made paraphrase fire again), so the one
      prompt that both spots a new specific and judges its support is split in two. Step 1 (v5c, `step="classify"`)
      only sorts a sentence: a restatement of the source (paraphrase, a one-step reading, tone, text about the text,
      background about a named place: allowed under the writer source-fidelity policy) or a new claim (a new number,
      name, time, service, activity, view, pace ...), listing the new specifics. Step 2 (v5j, `step="judge"`) judges
      only the specifics step 1 listed, with the source's support rules. A step version is entailment-only and is
      refused by r1.instruments (it runs only inside two_step). Shaped on the v2, v3 and v4 runs.
  v6c / v6j  S282: v5's gated run (9b0d46f8) was no_go: step 1 sent 19 of 25 false positives on and step 2 cleared
      2 of them. Both v5 steps looked each fact up before applying the background rule. v6c sets aside background,
      the writer's reading, tone and words naming the reader first, then lists the trip specifics left and looks
      them up with more one-step readings (its schema asks for `set_aside` first); v6j first asks whether a listed
      specific is a trip specific at all. Shaped on the v2-v4 runs and on the false positives of 9b0d46f8.

Every example in these prompts uses invented places and phrasing (the golden-set text guard). The B5 examples name the
pattern classes of the five claims the tech lead relabelled to violation in S279, in wording of their own: checked
against the evaluation pool before S279 shipped, no example shares a three-word sequence (with a content word) with any
pool claim, and none shares a content word with a relabelled claim other than 'before'. The relabelled claims
still shaped the patterns, so they are in-sample for v2: instrument_eval scores them apart and gates on the stored
violations only. Pure: no DB, no network, no model call.
"""
from __future__ import annotations

import hashlib
import json
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
# 90%: fail). Two of the three it lost are pace promises (the trip called slow-moving): P1 set aside "the writer's
# reading" and its recall guard had no pace item (v1's "pace ... only as the source states it" was dropped in v2).
# The third joins two source facts into one new activity (a ride the source offers by two kinds of boat plus paddling
# on a lake, written as one paddling trip). Its 8 remaining false fires: a purpose given to a source fact, a
# part of the day that follows from the source's order, a day said to end at its last listed stop, a sentence about
# the article itself, a trip start that P3 allowed in headings only, and three lasting features of a visited monument
# (its decoration, its setting, its lower floors) that read as "scenery seen". v4 keeps v3's restatement test and adds:
# pace, effort and suitability to the facts (P1 step 2, B5); the new-specific guard (P1); two one-step examples (P1);
# a visited place's own lasting features as background (P2b, B5); sentences in P3; and P4 for text about the text.
# P2, B3 and B4 are v3's strings unchanged. Every example is invented (Karsk, Brellin, Ostra, Velm, Tarro), and the
# S280 leak check found no three-word sequence with a content word shared with any claim of the evaluation pool.
# S280 fix round: v4's first draft named a visited place's size and age as background (P2b) while its B5 item still
# called a size or date the source does not give unsupported; B5 now keeps that only for a number about the trip or
# one bound to another place, and the P1 guard names the place rule as an exception.

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
    "A restatement must not add a new specific about this trip: a number, name, time, service, activity or view "
    "that the whole source does not give and that does not follow from it in one obvious step (background about a "
    "place, craft or custom the source names, as the place rule below allows it, is not such a specific). Joining "
    "two source facts into one new specific adds one ('kayaking through the reed beds of Velm' when the source has "
    "the reed beds 'by kayak or launch' and the kayaking on Velm lake), and so do firming one side of an 'or' the "
    "source leaves open and stretching a fact to days or nights the source gives to something else.\n"
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
    "a size, height, date or count the source gives for another place is unsupported, and so is a number about "
    "this trip (a distance, duration, time, price or group size) that the source does not give; a size, height, "
    "age or date that describes a place the source names is background under the place rule above, even when "
    "the source does not give it;\n"
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

# ── v5c / v5j (S281: the two-step instrument) ──────────────────────────────────────────────────────────────────────
# One prompt that both finds a new specific and judges its support kept trading false positives for lost violations
# (v2 48 / 90, v3 68 / 85, v4 48 / 85; v4's new-specific guard made paraphrase fire again). Step 1 asks the easy,
# recall-first question (is anything here not a restatement?) and names the specifics; step 2 judges only those
# specifics with the support rules (one-step readings, place background, set-aside wording) and the promise list. Every
# example is invented (Morvane, Quell, Tasska, Ilbern); the S281 leak check (scratch) found no three-word sequence
# with a content word shared with any claim of the S279 pool.

V5C_SYSTEM = (
    "You read ONE sentence of a travel text and the SOURCE the trip is built from. You do not decide whether the "
    "sentence is right. You only sort it into one of two kinds, so that a second reader checks the right "
    "sentences.\n"
    "- restatement: every fact the sentence gives about this trip is already in the source, in the same or other "
    "words. This covers paraphrase and summary; a heading, title or label made of source words; one obvious step "
    "from the source (the stop listed after lunch is an afternoon stop; coming back to the start town makes a round "
    "trip); tone, feeling and emphasis words that add no fact ('unhurried', 'memorable', 'truly'); a sentence about "
    "the text itself (what this guide covers); and background about a place, craft, food or custom the source names "
    "(its history, age, builder, architecture, setting, size), as long as it promises nothing the traveller will do, "
    "get or see beyond the place itself.\n"
    "- new_claim: the sentence gives at least one specific about this trip that you cannot point to in the source, "
    "or that goes against it. Specifics are: a number, a name, a day, a clock time or a time of day, an order or "
    "schedule, a means of travel, a service, the character of a meal, a quality of the guide, a group or crowd "
    "condition, an activity, a view or sighting, access to a place, the trip's pace, effort, comfort or "
    "safety, or who it suits. Two source facts joined into one new specific, one side of an 'or' made firm, and a fact "
    "stretched to other days are new specifics too.\n"
    "Look through the WHOLE source (overview, highlights, every day, inclusions, place background) before you call a "
    "specific new. When you are unsure, answer new_claim: the second reader decides whether the source supports "
    "it. Examples:\n"
    "  - source 'walk the ridge above Morvane', sentence 'a ridge walk high over Morvane' -> restatement;\n"
    "  - source 'a stop at Ilbern's granary', sentence 'the granary of Ilbern, three storeys of timber from "
    "the age of the river guilds' -> restatement (background about the place);\n"
    "  - source 'the Tasska ferry over to Ilbern', sentence 'the crossing to Ilbern leaves at dawn' -> "
    "new_claim ('at dawn');\n"
    "  - source 'six nights in guesthouses', sentence 'an easy week that suits any walker' -> new_claim ('easy', "
    "'suits any walker');\n"
    "  - source 'a boat trip on Quell lake or a cycle along its shore', sentence 'a boat trip on Quell lake' -> "
    "new_claim (one side of an 'or' made firm).\n"
    "List each new specific in 'new_specifics' in the sentence's own words (an empty list for a restatement), and say "
    "in 'why' in one sentence what decided it. Reply with the JSON object only.")

V5J_SYSTEM = (
    "You check ONE sentence of a travel text against the SOURCE the trip is built from. A first reader listed the "
    "SPECIFICS of the sentence that it could not point to in the source at a glance; every other fact of the sentence "
    "it found in the source. Decide whether the source gives each listed specific after all.\n"
    "- A specific is supported when the source states it anywhere (overview, highlights, any day, inclusions, place "
    "background), in any words, or when it follows from the source in one obvious step (what comes after lunch is "
    "the afternoon; the last stop the source gives for a day ends that day; coming back to the start makes a round "
    "trip; nine nights make ten days).\n"
    "- Background about a place, craft, food or custom the source names (history, age, builder, architecture, "
    "setting, size, how it is usually described) is allowed even when the source does not give it, and so is seeing "
    "such a place's own lasting features on a visit the source gives. Lines under 'Place background' in the source "
    "support sentences about that place.\n"
    "- Wording that adds no checkable fact (synonyms, images, emphasis, the traveller's point of view, a purpose given "
    "to a fact the source states) is not a specific: a listed specific that is only such wording is supported.\n"
    "- Unsupported, the promises this check exists for: a number, name, day, time, order or means of travel the "
    "source does not give or that goes against it; the character of a meal when the source names only the meal; "
    "qualities of the guide; a group, social or crowd experience; a view from a place, its light, season or weather, "
    "wildlife or a mood at a stop; access the source does not give; an activity or service the source does not give, "
    "also one made by joining two source facts or by firming one side of an 'or'; pace, effort, comfort, safety or "
    "who the trip suits when the source does not state it (rest days or short drives do not by themselves make a "
    "trip slow or easy).\n"
    "When the list says '(the whole sentence)', judge every fact of the sentence. Answer 'unsupported' when at least "
    "one listed specific is such a promise, and name it in 'why'. Otherwise answer 'supported' and quote the source "
    "line you relied on (empty for background or wording). Reply with the JSON object only.")
V5J_USER = V2_ENTAILMENT_USER + "\n\nSPECIFICS TO CHECK:\n{specifics}"
WHOLE_SENTENCE = "(the whole sentence)"

# ── v6c / v6j (S282: v5's gated run 9b0d46f8 was no_go, FP dropped 32 %) ──────────────────────────────────────────
# v5c sent 19 of the 25 false positives on (17 of them restatements by the panel's step-1 answer) and v5j cleared 2 of
# the 19 (docs/implementation-notes/AA-1048-v6.md, counts only). The reasons show both steps ask "can you point to
# every fact in the source?" first: background about a named place, people or craft (7), an over-literal reading of a
# paraphrase (6), the writer's reading of the source facts (4) and a word naming the reader (2) all failed that
# lookup before the background rule was ever applied. v6c orders the work: set aside what is not about what THIS trip
# gives the traveller, then list the trip specifics left, then look each up with more one-step readings; its schema
# asks for `set_aside` before `kind`. v6j first asks whether a listed specific is a trip specific at all, then looks
# it up, then keeps v5j's promise list. The recall guard of both: wording about meals, the guide, other travellers,
# crowds, pace, effort, comfort, safety, suitability or what the traveller will see, hear or feel at a stop is a trip
# specific even when it reads like a judgement. Every example is invented (Halvik, Orsa, Drevna, Kettle Ford, Lusk,
# Tasska, Ilbern, Quell); the S282 leak check (scratch) found no three-word sequence with a content word shared with
# any claim of the fresh pool.

_V6_SET_ASIDE = (
    "  - background about a place, people, craft, food, custom or region the source names: what it is or was, its "
    "history, age, builder, purpose, meaning, status or reputation, the people who live there and how they live, "
    "its landscape, plants, climate or position, how it compares with other places or regions;\n"
    "  - the writer's reading of the source facts: what a day or a stop offers, means or shows, an interpretation of "
    "the itinerary as a whole, a comparison or an intensifier ('more', 'far', 'quite unlike', 'truly'), an image or "
    "figure of speech, a purpose or intent given to a fact the source states ('deliberately', 'on purpose');\n"
    "  - tone and feeling words that add no fact ('unhurried', 'memorable');\n"
    "  - a word that names who the text speaks to or about ('you', 'families', 'travellers', 'couples') when it adds "
    "nothing about other people the traveller will meet or travel with;\n"
    "  - a sentence about the text itself (what this guide covers), and a heading or label made of source words.\n")
_V6_NEVER_ASIDE = (
    "Wording about the trip's meals, the guide, other travellers, crowds, pace, effort, comfort, safety or who the "
    "trip suits, or about what the traveller will see, hear or feel at a stop, is always a trip specific and never "
    "set aside: 'an easy day', 'a family-style dinner', 'a guide who knows every trail', 'the square to ourselves' are "
    "trip specifics even when they read like the writer's judgement or tone.")
_V6_ONE_STEP = (
    "these or other words, or when it follows from the source in one obvious step: the stop listed after lunch is "
    "an afternoon stop; a tour that returns to its start town is a round trip; a town the source has the trip stop "
    "at or near on the way lies on its road; a place the source names may be called by its other well-known name; a "
    "meal on an evening the source gives at one hotel or camp, with no other place for it, is at that hotel or "
    "camp; a sight the source gives on an earlier day is already behind the traveller on a later day; two facts the "
    "source gives for the same stop and time may be told together")

V6C_SYSTEM = (
    "You read ONE sentence of a travel text and the SOURCE the trip is built from. You do not decide whether the "
    "sentence is right. You only sort it, so that a second reader checks the sentences that add something to THIS "
    "trip. Work in this order.\n"
    "1. Set aside every part of the sentence that is not about what THIS trip gives the traveller. Set it aside even "
    "when the source does not mention it, and list it in 'set_aside':\n"
    + _V6_SET_ASIDE
    + _V6_NEVER_ASIDE + "\n"
    "2. What is left are the trip specifics: what the traveller does, gets, sees or meets on this trip, and when, "
    "where, how and with whom. They are: a number about the trip, the name of a place the trip goes to or of a "
    "service it gives, a day, a clock time or a time of day, an order or schedule, a route or a means of travel, a "
    "service, a meal and its character, a quality of the guide, other travellers, a group or crowd condition, an "
    "activity, a view or sighting, access to a place, the trip's pace, effort, comfort or safety, and who it suits.\n"
    "3. Look each trip specific up in the WHOLE source (overview, highlights, every day, inclusions, place "
    "background). It is in the source when the source gives it in " + _V6_ONE_STEP + ". Two source facts joined into "
    "a new activity, one side of an 'or' made firm, and a fact stretched to other days are NOT in the source.\n"
    "Answer 'restatement' when nothing is left after step 1, or when every trip specific is in the source. Answer "
    "'new_claim' when at least one trip specific is not in the source or goes against it, and list each such "
    "specific in 'new_specifics' in the sentence's own words. When you are unsure whether a trip specific is in the "
    "source, answer new_claim: the second reader decides. Never answer new_claim for background, the writer's reading "
    "or a word that names the reader alone. Examples:\n"
    "  - source 'a morning at the Halvik salt works', sentence 'salt has been raked from the Halvik flats since the "
    "guilds held the coast, and the works still set the rhythm of village life' -> restatement (background about "
    "the place and its people);\n"
    "  - source 'Day 2: the mill at Orsa, the weavers' lane and the old toll bridge', sentence 'the day reads less "
    "like a list of sights than one long story of how Orsa earned its living' -> restatement (the writer's reading "
    "of the source's stops);\n"
    "  - source for a family trip 'Day 5: the fort at Drevna', sentence 'families climb up to Drevna fort on the "
    "fifth day' -> restatement (the word names the readers);\n"
    "  - source 'lunch close to Kettle Ford on the drive north', sentence 'the northbound drive goes by Kettle Ford' "
    "-> restatement (one step);\n"
    "  - source 'the Tasska ferry over to Ilbern', sentence 'the crossing to Ilbern leaves at dawn' -> new_claim "
    "('at dawn');\n"
    "  - source 'six nights in guesthouses', sentence 'an easy week that suits any walker' -> new_claim ('easy', "
    "'suits any walker');\n"
    "  - source 'a free afternoon in Lusk', sentence 'a slow lunch with the family who keep the harbour inn' -> "
    "new_claim (the meal and the people met);\n"
    "  - source 'a boat trip on Quell lake or a cycle along its shore', sentence 'a boat trip on Quell lake' -> "
    "new_claim (one side of an 'or' made firm).\n"
    "Say in 'why' in one sentence what decided it. Reply with the JSON object only.")
# The v6c reply: what was set aside comes before the decision (the schema is hashed into the v6c tag).
V6C_SCHEMA = {"name": "claim_restatement_v6", "schema": {
    "type": "object", "additionalProperties": False, "required": ["set_aside", "new_specifics", "kind", "why"],
    "properties": {"set_aside": {"type": "array", "items": {"type": "string"}},
                   "new_specifics": {"type": "array", "items": {"type": "string"}},
                   "kind": {"type": "string", "enum": ["restatement", "new_claim"]},
                   "why": {"type": "string"}}}}

V6J_SYSTEM = (
    "You check ONE sentence of a travel text against the SOURCE the trip is built from. A first reader listed the "
    "SPECIFICS of the sentence it could not find in the source. It was told to list whatever it was unsure of, so "
    "many listed specifics are not about the trip at all. Take each listed specific in this order.\n"
    "1. Is it about what THIS trip gives the traveller: what the traveller does, gets, sees or meets, and when, "
    "where, how and with whom, or the trip's pace, effort, comfort, safety or who it suits? If it is not, it is "
    "supported, whatever the source says:\n"
    + _V6_SET_ASIDE
    + "  Lines under 'Place background' in the source support sentences about that place, and seeing a place's own "
    "lasting features on a visit the source gives is background too.\n"
    + _V6_NEVER_ASIDE + "\n"
    "2. A trip specific is supported when the source states it anywhere (overview, highlights, any day, inclusions, "
    "place background) in " + _V6_ONE_STEP + ".\n"
    "3. Unsupported, the promises this check exists for: a number, name, day, time, order, route or means of travel "
    "the source does not give or that goes against it; the character of a meal when the source names only the meal; "
    "qualities of the guide; a group, social or crowd experience; a view from a place, its light, season or weather, "
    "wildlife or a mood at a stop; access the source does not give; an activity or service the source does not give, "
    "also one made by joining two source facts or by firming one side of an 'or'; pace, effort, comfort, safety or "
    "who the trip suits when the source does not state it (rest days or short drives do not by themselves make a "
    "trip slow or easy).\n"
    "When the list says '(the whole sentence)', judge every trip specific of the sentence. Answer 'unsupported' when "
    "at least one listed specific is a trip specific that the source does not give or that goes against it, and name "
    "it in 'why'. Otherwise answer 'supported' and quote the source line you relied on (empty for background, the "
    "writer's reading or wording). Reply with the JSON object only.")

# ── v7c / v7j (S282: v6's gated run c5983e03 was no_go, FP dropped 56 %) ──────────────────────────────────────────
# v6c routed 11 of 25 false positives (9 of them restatements by the panel's step-1 answer) and v6j cleared none of the
# 11 (docs/implementation-notes/AA-1048-v7.md, counts only). Both steps compared words, not facts: "the source says X,
# but does not say Y" where X and Y were the same fact or the same quality in other words; in 6 of the 11 step 2 quoted
# the source line that gives it and flagged anyway. v7c writes a row per trip specific (the closest source wording,
# same fact or not) before it decides, with a close-paraphrase list; v7j writes a row per listed specific with a
# finding (not_trip / same_fact / one_step / not_given / against) and clears the sentence when no row is not_given or
# against. The recall guard of both: a source place, stop or time with a NEW quality, mood, light, measure, group or
# suitability added is not the same fact, nor is a fact stretched to more days or nights. Every example is invented
# (Vessa, Pellow, Brannock, Mirre, Saint Avel, Cobbe, Corran, plus v6's names); the S282 leak check (scratch) found no
# three-word sequence with a content word shared with any claim of the S281 fresh pool or the new eligible export.

_V7_SAME_FACT = (
    "    - the same thing under another name: a synonym or near-synonym, a looser or more general name for a stop or "
    "thing the source names, singular or plural, a verb for the source's noun (a visit to a hall is a walk through "
    "it);\n"
    "    - the same number or measure written another way: other units, digits or words, a rounded figure or a range "
    "that holds the source's figure, nights told as days (nine nights are ten days);\n"
    "    - the same quality in other words: a walk the source calls meditative is unhurried; activities the source "
    "calls optional let each traveller choose what to join; an evening the source gives at the hotel or camp with "
    "nothing else planned is a free evening;\n"
    "    - a feature the source's place background gives for a place the trip visits, seen or used on that visit;\n"
    "    - landscape the source or its place background puts around a stop, crossed on the way to that stop.\n")
_V7_ONE_STEP = (
    "    the stop listed after lunch is an afternoon stop; a tour that returns to its start town is a round trip; a "
    "town the source has the trip stop at or near on the way lies on its road; a place may be called by its other "
    "well-known name; a meal on an evening the source gives at one hotel or camp, with no other place for it, is at "
    "that hotel or camp; a sight the source gives on an earlier day is already behind the traveller on a later day; "
    "two facts the source gives for the same stop and time may be told together.\n")
_V7_NOT_SAME = (
    "These are NOT the same fact: a source place, stop or time with a NEW quality, mood, light, view, sound, "
    "measure, group or suitability added to it ('a visit to the Saint Avel chapel' does not give 'the hush of the "
    "chapel'; 'the Corran cliffs at dusk' does not give 'the cliffs glow at dusk'; 'the press is explained' does not "
    "give 'the press is shown working'); a fact stretched to other days, to more nights or to the whole trip; two "
    "source facts joined into a new activity; one side of an 'or' made firm.\n")

V7C_SYSTEM = (
    "You read ONE sentence of a travel text and the SOURCE the trip is built from. You do not decide whether the "
    "sentence is right. You only sort it, so that a second reader checks the sentences that add something to THIS "
    "trip. Work in this order.\n"
    "1. Set aside every part of the sentence that is not about what THIS trip gives the traveller. Set it aside even "
    "when the source does not mention it, and list it in 'set_aside':\n"
    + _V6_SET_ASIDE
    + _V6_NEVER_ASIDE + "\n"
    "2. What is left are the trip specifics: what the traveller does, gets, sees or meets on this trip, and when, "
    "where, how and with whom. They are: a number about the trip, the name of a place the trip goes to or of a "
    "service it gives, a day, a clock time or a time of day, an order or schedule, a route or a means of travel, a "
    "service, a meal and its character, a quality of the guide, other travellers, a group or crowd condition, an "
    "activity, a view or sighting, access to a place, the trip's pace, effort, comfort or safety, and who it suits.\n"
    "3. Look each trip specific up in the WHOLE source (overview, highlights, every day, inclusions, place "
    "background). Look for the FACT, not the words. Write one row per trip specific in 'checked': the specific in the "
    "sentence's words, the closest wording of the source (quote it; empty when nothing is close) and 'same_fact'. "
    "same_fact is true when the source gives the same fact in the same or other words:\n"
    + _V7_SAME_FACT
    + "  or when it follows from the source in one obvious step:\n"
    + _V7_ONE_STEP
    + "  " + _V7_NOT_SAME
    + "Answer 'restatement' when nothing is left after step 1, or when every row of 'checked' is the same fact. Answer "
    "'new_claim' when at least one row is not, and list in 'new_specifics' exactly the specifics of those rows, in the "
    "sentence's own words. When no source wording is close to a trip specific, answer new_claim: the second reader "
    "decides. Never answer new_claim for background, the writer's reading or a word that names the reader alone, nor "
    "for a specific whose fact the source gives in other words. Examples:\n"
    "  - source 'the Vessa weaving hall, its walls covered in paintings', sentence 'the painted walls of the Vessa "
    "weaving hall' -> restatement (the same fact in other words);\n"
    "  - source 'a ridge walk of 4 km above Pellow', sentence 'a four-kilometre ridge walk over Pellow' -> "
    "restatement (the same measure);\n"
    "  - source 'Day 3: back to the Brannock inn for the night', sentence 'a free evening at the Brannock inn' -> "
    "restatement (nothing else is planned);\n"
    "  - source 'optional rowing or a shore walk at Mirre', sentence 'at Mirre each traveller picks rowing or the "
    "shore walk' -> restatement (optional means each one chooses);\n"
    "  - source 'a morning at the Halvik salt works', sentence 'salt has been raked from the Halvik flats since the "
    "guilds held the coast' -> restatement (background about the place);\n"
    "  - source 'lunch close to Kettle Ford on the drive north', sentence 'the northbound drive goes by Kettle Ford' "
    "-> restatement (one step);\n"
    "  - source 'a visit to the Saint Avel chapel', sentence 'the hush of the Saint Avel chapel' -> new_claim ('the "
    "hush': a mood the source does not give);\n"
    "  - source 'one night in Cobbe, where there is no mains power', sentence 'two nights without mains power' -> "
    "new_claim (stretched to two nights);\n"
    "  - source 'the Tasska ferry over to Ilbern', sentence 'the crossing to Ilbern leaves at dawn' -> new_claim "
    "('at dawn');\n"
    "  - source 'six nights in guesthouses', sentence 'an easy week that suits any walker' -> new_claim ('easy', "
    "'suits any walker');\n"
    "  - source 'a boat trip on Quell lake or a cycle along its shore', sentence 'a boat trip on Quell lake' -> "
    "new_claim (one side of an 'or' made firm).\n"
    "Say in 'why' in one sentence what decided it. Reply with the JSON object only.")
# The v7c reply: what was set aside, then one row per trip specific with the closest source wording, before the
# decision (the schema is hashed into the v7c tag).
V7C_SCHEMA = {"name": "claim_restatement_v7", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["set_aside", "checked", "new_specifics", "kind", "why"],
    "properties": {"set_aside": {"type": "array", "items": {"type": "string"}},
                   "checked": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False,
                       "required": ["specific", "source_wording", "same_fact"],
                       "properties": {"specific": {"type": "string"}, "source_wording": {"type": "string"},
                                      "same_fact": {"type": "boolean"}}}},
                   "new_specifics": {"type": "array", "items": {"type": "string"}},
                   "kind": {"type": "string", "enum": ["restatement", "new_claim"]},
                   "why": {"type": "string"}}}}

V7J_SYSTEM = (
    "You check ONE sentence of a travel text against the SOURCE the trip is built from. A first reader listed the "
    "SPECIFICS of the sentence whose words it did not find in the source. It compares words, so many listed specifics "
    "are in the source in other words, follow from it, or are not about the trip at all. Your job is to look each one "
    "up and to clear it when the source supports it; the sentence is flagged only for a specific the source does not "
    "give. Write one row per listed specific in 'checks': the specific, the closest wording of the source (quote it; "
    "empty when nothing is close) and a 'finding':\n"
    "- not_trip: it is not about what THIS trip gives the traveller:\n"
    + _V6_SET_ASIDE
    + "  Lines under 'Place background' in the source support sentences about that place, and seeing a place's own "
    "lasting features on a visit the source gives is background too. "
    + _V6_NEVER_ASIDE + "\n"
    "- same_fact: the source gives the same fact in the same or other words. Look for the FACT, not the words:\n"
    + _V7_SAME_FACT
    + "- one_step: it follows from the source in one obvious step:\n"
    + _V7_ONE_STEP
    + "- not_given: no source line gives it in any words. " + _V7_NOT_SAME
    + "  These are the promises this check exists for: a number, name, day, time, order, route or means of travel the "
    "source does not give; the character of a meal when the source names only the meal; qualities of the guide; a "
    "group, social or crowd experience; a view from a place, its light, season or weather, wildlife or a mood at a "
    "stop; access the source does not give; an activity or service the source does not give; pace, effort, comfort, "
    "safety or who the trip suits when the source does not state it in any words (rest days or short drives do not "
    "by themselves make a trip slow or easy).\n"
    "- against: the source says otherwise.\n"
    "When the list says '(the whole sentence)', write a row for every trip specific of the sentence. Then answer "
    "'supported' when every row is not_trip, same_fact or one_step, and quote in 'quote' the source line you relied on "
    "most (empty for background or wording). Answer 'unsupported' when at least one row is not_given or against, and "
    "name it in 'why'. Reply with the JSON object only.")
# The v7j reply: one row per listed specific (closest source wording, finding) before the verdict (hashed into the
# v7j tag). The vote is still `verdict` (two_step.judge_vote); the rows are its reasons.
V7J_FINDINGS = ("not_trip", "same_fact", "one_step", "not_given", "against")
V7J_SCHEMA = {"name": "claim_entailment_v7", "schema": {
    "type": "object", "additionalProperties": False, "required": ["checks", "verdict", "quote", "why"],
    "properties": {"checks": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False,
                       "required": ["specific", "source_wording", "finding"],
                       "properties": {"specific": {"type": "string"}, "source_wording": {"type": "string"},
                                      "finding": {"type": "string", "enum": list(V7J_FINDINGS)}}}},
                   "verdict": {"type": "string", "enum": ["supported", "unsupported"]},
                   "quote": {"type": "string"}, "why": {"type": "string"}}}}


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


def v5j_user(item: Any, specifics: Any = ()) -> str:
    """The step-2 user prompt: v2's (whole source, BEFORE / SENTENCE / AFTER) and the specifics step 1 listed, one per
    line; none listed (or an unverified step 1) asks for the whole sentence."""
    before, after = around(item.field_text, item.claim_text)
    listed = "\n".join(f"- {s}" for s in specifics if str(s).strip()) or f"- {WHOLE_SENTENCE}"
    return V5J_USER.format(source=item.source_text or NONE_TEXT, field=item.field, before=before or NONE_TEXT,
                           claim=item.claim_text, after=after or NONE_TEXT, specifics=listed)


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
    # Digests (sha256 of the pool file, first 16 hex, as fresh_pool.py writes them into a manifest's excluded_pools) of
    # the labelled pools a panel read to shape this version. two_step refuses a live run of a step whose fresh manifest
    # does not exclude each of them (or is not a train-split pool). Not part of the hash.
    shaped_on_pools: tuple[str, ...] = ()
    # "" for a one-prompt version; "classify" / "judge" for a step of the two-step instrument (two_step.py): entailment
    # only, run only by two_step (r1.instruments refuses it). Not part of the hash.
    step: str = ""

    def system(self, mode: str) -> str:
        if self.step and mode != "entailment":
            raise UnknownPromptVersion(f"{self.id} is a two-step prompt ({self.step}): entailment only")
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


# The S279 pool (the 286 R1-fired claims of the held-out split, pool-v2.jsonl): the S279 panel read its whole
# false-positive side to write v2, and every version since builds on v2.
S279_POOL = "487bf4847b6136fe"
# The fresh pool of S281 (fresh_pool.py, 104 R1-fired claims of the train split): v5's gated run 9b0d46f8 and v6's
# gated run c5983e03 ran on it. v7 was shaped on c5983e03's routed false positives (and the shape of its violations),
# so the whole pool is a shaping pool of v7.
FRESH_POOL = "716df551d458fee1"
# Shaping pools that are themselves train-split pools: a train-split pool does not keep them out (the split is frozen
# per tour, so a train pool shares tours with them), so a fresh manifest must name each in excluded_pools.
TRAIN_SPLIT_POOLS = frozenset({FRESH_POOL})

_V7_SHAPED_ON = ("3018008d", "33835b87", "2977dd78", "9b0d46f8", "c5983e03")

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
    # S281 two-step: step 1 sorts (restatement / new claim), step 2 judges the specifics step 1 listed. Designed from
    # the misses and reasons of the v2, v3 and v4 runs, so all three are in-sample for both steps. No extraction text.
    "v5c": PromptVersion("v5c", V5C_SYSTEM, "", v2_entailment_user, extraction_user,
                         (V2_ENTAILMENT_USER, NONE_TEXT), shaped_on=("3018008d", "33835b87", "2977dd78"),
                         shaped_on_pools=(S279_POOL,), step="classify"),
    "v5j": PromptVersion("v5j", V5J_SYSTEM, "", v5j_user, extraction_user, (V5J_USER, NONE_TEXT, WHOLE_SENTENCE),
                         shaped_on=("3018008d", "33835b87", "2977dd78"), shaped_on_pools=(S279_POOL,), step="judge"),
    # S282 two-step v6: written from the 25 false positives of v5's gated run 9b0d46f8 on the fresh pool (FRESH_POOL)
    # as well, so that run is a shaping run too. Its false positives are held out claim by claim by the v6
    # pre-registration (two_step.py: held_out); the pool itself is not a shaping pool (its other 41 sampled false
    # positives were never read). v6c pins its reply schema in the tag.
    "v6c": PromptVersion("v6c", V6C_SYSTEM, "", v2_entailment_user, extraction_user,
                         (V2_ENTAILMENT_USER, NONE_TEXT, json.dumps(V6C_SCHEMA, sort_keys=True)),
                         shaped_on=("3018008d", "33835b87", "2977dd78", "9b0d46f8"),
                         shaped_on_pools=(S279_POOL,), step="classify"),
    "v6j": PromptVersion("v6j", V6J_SYSTEM, "", v5j_user, extraction_user, (V5J_USER, NONE_TEXT, WHOLE_SENTENCE),
                         shaped_on=("3018008d", "33835b87", "2977dd78", "9b0d46f8"), shaped_on_pools=(S279_POOL,),
                         step="judge"),
    # S282 two-step v7: written from the 11 false positives v6's gated run c5983e03 routed on the S281 fresh pool (and
    # the shape of that run's 21 violations), so c5983e03 is a shaping run and FRESH_POOL a shaping pool. The v7 run
    # needs a new fresh pool whose manifest excludes both. Both reply schemas are pinned in the tags.
    "v7c": PromptVersion("v7c", V7C_SYSTEM, "", v2_entailment_user, extraction_user,
                         (V2_ENTAILMENT_USER, NONE_TEXT, json.dumps(V7C_SCHEMA, sort_keys=True)),
                         shaped_on=_V7_SHAPED_ON, shaped_on_pools=(S279_POOL, FRESH_POOL), step="classify"),
    "v7j": PromptVersion("v7j", V7J_SYSTEM, "", v5j_user, extraction_user,
                         (V5J_USER, NONE_TEXT, WHOLE_SENTENCE, json.dumps(V7J_SCHEMA, sort_keys=True)),
                         shaped_on=_V7_SHAPED_ON, shaped_on_pools=(S279_POOL, FRESH_POOL), step="judge"),
}


def get(version: str) -> PromptVersion:
    if version not in VERSIONS:
        raise UnknownPromptVersion(f"unknown prompt version {version!r} (known: {', '.join(sorted(VERSIONS))})")
    return VERSIONS[version]


__all__ = ["DEFAULT", "ENTAILMENT_SYSTEM", "ENTAILMENT_SYSTEM_V2", "ENTAILMENT_SYSTEM_V3", "ENTAILMENT_SYSTEM_V4",
           "EXTRACTION_SYSTEM", "EXTRACTION_SYSTEM_V2", "EXTRACTION_SYSTEM_V3", "EXTRACTION_SYSTEM_V4",
           "FRESH_POOL", "PromptVersion", "S279_POOL", "TRAIN_SPLIT_POOLS", "UnknownPromptVersion", "V5C_SYSTEM",
           "V5J_SYSTEM", "V5J_USER",
           "V6C_SCHEMA", "V6C_SYSTEM", "V6J_SYSTEM", "V7C_SCHEMA", "V7C_SYSTEM", "V7J_FINDINGS", "V7J_SCHEMA",
           "V7J_SYSTEM", "VERSIONS",
           "WHOLE_SENTENCE", "around", "extraction_user", "get", "v1_entailment_user", "v2_entailment_user",
           "v5j_user"]
