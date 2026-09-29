-- Migration 183: AA-690 — the Jev questions of A0 ingest, seeded in SHADOW mode.
--
-- Design: docs/architecture/at-series-v2-design.md §4.0 (A0-1, A0-3, A0-4). One decide() call per uploaded
-- row asks all three (same state: name, country, duration, summary, itinerary). In shadow they only flag
-- rows in the Upload preview and log verdicts (calibration data, AA-661); they drop or change nothing until
-- an admin sets them to `enforce` with calibrated floors on /admin/decisions.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a0_row_kind', 'a0_ingest', 'choice',
     'What kind of product is this uploaded row?',
     '{"multi_day_tour": "A tour of two or more days with a day-by-day itinerary", "day_tour": "A single-day guided tour or excursion", "poi_or_attraction": "A place, museum, temple, park or attraction on its own, not a tour", "accommodation": "A hotel, lodge, resort, homestay or camp", "transfer_or_service": "A transfer, rental, ticket, visa, guide fee or other service", "other": "Anything else"}',
     'shadow', 'AA-690 A0-1: a confident non-tour kind drops the row (not_a_tour) once enforced.', 'migration-183'),
    ('a0_itinerary_usable', 'a0_ingest', 'noul',
     'Does this itinerary describe real day-by-day activities that a writer could turn into a tour page?',
     '{"true": "Describes what happens each day: places visited, activities, travel between them.", "false": "Placeholder, headings only, TBA, or a single vague sentence with no daily content."}',
     'shadow', 'AA-690 A0-4: a confident no drops the row (thin_itinerary) once enforced.', 'migration-183'),
    ('a0_country', 'a0_ingest', 'choice',
     'Which country is this tour in?',
     '{"Japan": null, "Sri Lanka": null, "South Korea": null, "Vietnam": null, "Thailand": null, "Bhutan": null, "Nepal": null, "India": null, "Cambodia": null, "Laos": null, "Myanmar": null, "Indonesia": null, "Malaysia": null, "Singapore": null, "Philippines": null, "Mongolia": null, "China": null, "Tibet": null, "Taiwan": null, "Maldives": null, "Pakistan": null, "other": "A country not in this list, or several countries equally"}',
     'shadow', 'AA-690 A0-3: asked only when the alias resolver returns no country; a confident pick fills it once enforced.',
     'migration-183')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('183', now(), 'AA-690: A0 ingest Jev questions (a0_row_kind, a0_itinerary_usable, a0_country) in shadow')
ON CONFLICT (version) DO NOTHING;

COMMIT;
