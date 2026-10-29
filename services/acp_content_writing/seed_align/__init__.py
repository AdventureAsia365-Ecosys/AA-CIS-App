"""AA-789 tier 1 — seed alignment: deterministic checks that compare copy with the typed facts of its seed.

`nlp` loads spaCy, `seed_graph` reads the seed, `checks` finds candidates, `modes` decides per class whether a
candidate is ignored (off), recorded only (shadow), sent to one sentence repair (repair) or may hold (hold), and
routes the live ones under the existing gates (F17_trip_fact, F18_rarity, F13_leak, F13_repeat) with an `sa:` prefix.
Plan: root docs/audits/2026-10-29-S257-aa789-design/PLAN.md.
"""
