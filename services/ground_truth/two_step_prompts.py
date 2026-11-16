"""AA-1048 — the step texts of the two-step claim instrument from v6 on (services/ground_truth/two_step.py).

Split out of services/ground_truth/prompts.py (code-health file limit). prompts.py builds the versions (`VERSIONS`,
their tags hash these texts unchanged) and re-exports every public name here, so callers keep `prompts.V7C_SYSTEM`.
The v5 step texts stay in prompts.py (v5j's user template extends v2's). Every example uses invented places and
phrasing (the golden-set text guard). Pure: no import of the rest of the package, no DB, no network, no model call.
"""

from __future__ import annotations

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


__all__ = ["V6C_SCHEMA", "V6C_SYSTEM", "V6J_SYSTEM", "V7C_SCHEMA", "V7C_SYSTEM", "V7J_FINDINGS", "V7J_SCHEMA",
           "V7J_SYSTEM"]
