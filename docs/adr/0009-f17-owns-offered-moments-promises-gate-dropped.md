# F17 owns offered moments; the promises_an_option gate is dropped (AA-787)

- **Status:** Accepted (20/10/2026, S245 — Nghiệp approved; first approved S234).
- **Amends:** origin ADR 0023 ("A Gate flags rather than blocks") and ADR 0026 ("An offered moment is
  ranked, and never promised") from Ms. Thư's `aa-social-media` repo, as CIS carries them in T10.

## Context

ADR 0026 kept offered (optional, at-a-price) moments in the ranking and gave the "never promised" rule to
a gate, `promises an option`. CIS ported it in AA-514 as a warn-only T10 gate (ADR 0023 flag-not-block),
`promises_an_option`.

Measured in CIS:

- S234 gate evaluation (80 pieces against the agent panel): it fired on 32 of 32 blogs, about 2.5 flags
  per blog on headings, CTA and geology lines, and caught none of the panel's "optional stated as planned"
  defects. `F17_trip_fact` caught 7 of those 9.
- AA-773 made the gate read the seed's Status lines. At S235 r5 it flagged 0 of 8 blogs; the panel also
  counted 0 of 8 offered/included flips.
- `F17_trip_fact` checks the same failure: an optional activity stated as part of the plan. It is
  blocking and sentence-repairable on the cheap repair stage, so it repairs the sentence instead of
  only noting it.

## Decision

1. T10 no longer runs `promises_an_option`. `F17_trip_fact` alone checks the "never promised" rule of
   ADR 0026. It blocks and repairs. When the repair fails, the piece is held for the admin. Per AA-770,
   the tenant still receives the content.
2. The ranking half of ADR 0026 is unchanged. Offered moments are still ranked, and the seed still marks
   them with the AA-773 Status line that F17 reads.
3. Guard: if the AA-729 golden-set replay finds a real failure that `promises_an_option` would catch and
   F17 misses, the check comes back folded into F17 as a non-blocking sub-check (option c of the S245
   decision memo), and Nghiệp is told. The replay reads the removed function from git
   (`services/acp_content_writing/quality_gates.py` at the parent of the AA-787 commit; it is pure and
   needs no DB).

## Consequences

- A piece built on an offered moment no longer carries a note by default. Under ADR 0023 the note was
  "the normal case", but in practice it was noise.
- ADR 0023's other rule, that a writer never repairs a world claim, is kept for F17: its repair keeps or
  cuts a sentence that the seed already bounds. It does not add a hedge to the prose.
- `t10_offer_as_certain` (migration 194, a Jev shadow question) was already `mode=off` and its observer
  was removed. Its rows stay as history.
- Old pieces keep `promises_an_option` in their `gate_ledger` and `flags`. The admin Content Quality page
  labels the gate as retired.
