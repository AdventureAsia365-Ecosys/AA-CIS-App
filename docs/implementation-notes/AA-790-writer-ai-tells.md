# AA-790 (S251) — T9 writer-side AI-tell fix: brief, deterministic pass, slate lead focus

Input: the S251 paid T9 pilot (n = 20; 14 blog, 6 LinkedIn) scored by a 4-lens agent panel: AI-tells 4.08 (bar
≥ 5.5), brand voice 6.53 (bar ≥ 7). Panel patterns: staccato negations ("not X. It is Y.", "not because … but
because …"), consultancy verdict lines ("That's the opening.", "This is the section worth slowing down for."),
template openers ("There is a particular quality to …"), stock adverbs (genuinely, Crucially, earns), long flat
sentences, internal labels ("referred to throughout as"), angle labels as H2s, CTA lead-ins repeated, and six Terra
LinkedIn posts on one atom that all read alike. Constraint: the 4-week gate freeze (S250 go/no-go 6b) — no new T10
gate, registry gate count unchanged. So the fix is on the writer side.

## Decisions

- **D1 — one source for the brief and the pass.** `services/acp_content_writing/ai_tells.py` holds the AI-tell
  rules the writer is told (`PROMPT_RULES`, rendered into the T9 `SYSTEM_PROMPT`) and the deterministic rewrite pass
  (`rewrite`). The residue patterns the pass cannot fix in code live next to the existing stock-line list in
  `hygiene.AI_TELL_LINE_RES`, read by `hygiene.template_lines` (F15_voice "stock line" findings, stored in the
  F2_lexicon ledger entry since AA-924). No new gate, no registry or taxonomy entry, no ledger entry.
- **D2 — the brief (SYSTEM_PROMPT, every channel).** An "AI TELLS" block after the existing cliché rule: no claim
  set up by a denial, no verdict lines, no template openers, the stock words by name, short sentences (no semicolon
  joins), no internal labels, the CTA once with no lead-in that repeats it. Our own prompt said "pick a genuinely
  different angle" in the summary line; that word is gone. Blog (both format variants): the chosen angle's name is
  never a heading (`BLOG_HEADING_RULE`, inserted after the H2 bullet). Non-blog: an AUDIENCE IN THE POST rule — say
  what the brand's audience sees or does, only from the seed; otherwise why the place matters to them in the seed's
  own facts (Terra LinkedIn read like an encyclopedia entry, voice 5.25). `writer_prompt_version` is the SYSTEM_PROMPT
  hash (AA-916 WM-08), so it moves by itself: pilot `662d4aa5` → new value (test pins "not the pilot's").
- **D3 — the pass deletes and joins; it never writes a claim.** Rules, in passes (each pass applied, then checked):
  1. per sentence: stock adverbs and intensifiers (genuinely / genuine / truly / precisely / undeniably; a sentence-
     opening "Crucially," …) with the article fixed ("a genuine encounter" → "an encounter"); internal labels;
     "not because A, but because B" → "because B"; "is not A but B" → "is B" (after a copula only, or when A and B
     open on the same preposition: "set not by a calendar but by the forest"); a closing ", not A." cut;
     "X is not A; it is B" → "X is B"; a semicolon between two clauses with their own subject → two sentences.
  2. sentence pairs (run twice, so a verdict left by a join goes too): "X is not A. It is B." → "X is B." (S2's
     verb, agreed with S1's subject); with a pronoun subject ("It is not preserved as it was. It is maintained…")
     the denial is cut; a lone short denial with a pronoun / "What …" subject is cut; a verdict line ("That's the
     opening.", "This is the section worth slowing down for.", "That kind of sequencing is deliberate.") is cut.
  3. headings and the CTA: a heading carrying the angle's name loses that part (or goes when that is all it is); a
     heading that repeats the CTA ("## Ready to Go?") goes; the last clause of the sentence before the CTA goes when
     it restates the CTA ("…, and our travel designers are ready to shape an itinerary…").
- **D4 — what is never touched.** The CTA sentence; the piece's first prose sentence (never cut, so a LinkedIn hook
  is never lost); the only prose sentence of a section (a cut would make `tidy_after_cut` drop the heading); a
  sentence that states a rule or access fact (allowed, included, open, required, safe…); a denied part with a digit;
  a denial / contrast whose words are the seed's own ("the herds are not primarily raised for meat", `_seed_fact`,
  ≥ 60 % of its content words in the seed + Facts) — the prompt's SOURCE FIDELITY rule already allows a contrast
  whose other half the seed states; a verdict that names a seed word; the brand's signature words
  (`signature_words(pack)`); FAQ question lines. Deletions are prose only (FAQ answers get the word rules).
- **D5 — integrity per pass, then per edit.** Each pass is applied whole and checked with
  `hygiene.integrity_problems`; when it reports a problem, the pass is replayed one edit at a time and an edit that
  breaks the text is skipped and logged (`ai_tells: skipped …`). The check keys a sentence by its text, so an edit to
  a sentence that ALREADY opened on a pronoun with no referent read as new (pilot d99c621c); `_new_problems` drops a
  problem the edited sentence already had under its old words (`integrity_problems('', before)`). On top, the T9
  loop runs the pass under the AA-790 step guard (`step_guard("ai_tells.rewrite", …)`): a pass that still breaks the
  settled text is reverted and counted as `degraded: repair_contract`, like every other deterministic step.
- **D6 — where it runs.** In `run_write_background`, on every attempt, right before `hygiene.autofix` (after the
  writer / sentence repair / SEO fit, before the FAQ steps and the gates). Its log lines join the admin-only
  F13_autofix flag with the hygiene log. Idempotent (a second run changes nothing; test).
- **D7 — residue rides the existing F15 stock-line routing.** `hygiene.AI_TELL_LINE_RES` (earns its reputation,
  rewards those who…, There is a particular quality…, has a way of, If there is one place…, is the thing itself,
  X is rarely just X, worth slowing down for / taking time with, not simply a X in itself, asks nothing of…) is read
  by `template_lines`, so `gate_voice` reports a "stock line" finding: stored in the F2_lexicon ledger entry
  (AA-924), blocking on attempt 1 only → one cheap sentence repair (t9_repair) → never a hold (`never_holds`). The
  gate count and the registry do not move; the change is more findings in an existing list.
- **D8 — LinkedIn slate diversity: lead fact per write.** A request is one atom and one chosen angle, so N writes on
  one request (the pilot's `t9_pilot.py write <request> --n 6`) share both; the writes start 2 s apart, so the
  existing `sibling_openers` (approved / held pieces only) saw none of the others and all six opened on "Years of
  practice on a single craft". Now each non-blog write gets its index among the request's pieces
  (`_request_write_index`, ordered by (created_at, piece_id), any status, 0 on error) and, from the second write on,
  a "LEAD WITH THIS FACT FROM THE CONTENT SEED" line: the seed's own fact lines (Source itinerary sentences, then
  Place background bullets; never "Other trips") rotated by the index (`ai_tells.lead_focus`). Deterministic, so
  writes started together still differ. Recorded in `writer_input.lead_focus`. Blog is unchanged (its prompt guard).
  **Gap (not code):** varying the ATOM needs separate requests (one per Segment from the T8 slate); the pilot used
  one request for all six Terra posts. The re-panel script runs the six with indexes 0–5.
- **D9 — the re-panel script is writer + writer-side pass only.** `scripts/aa790/t9_writer_repanel.py` rebuilds each
  pilot piece's prompt from its stored `writer_input`, calls the writer once on the `t9_write` stage's model (passed
  as `model_tier`, logged under stage `adhoc_aa790`, role writer, no tenant_id so test-tenant usage is not
  metered), then runs `ai_tells.rewrite` → `hygiene.autofix` → `ensure_cta` → `text_integrity.final_pass`. No gates,
  FAQ regeneration, sentence repair or judge: those did not change and would cost more. `--dry-run` is $0; the cap
  is `--max-usd` (default 2.0, an estimate is reserved before each call).

## Changed (vs the lane brief)

- The brief asked for "a deterministic writer-side self-check or rewrite pass". It is a rewrite pass for what code can
  fix safely; the self-check part is the residue list riding F15 (D7) — no new detector of its own in the ledger.
- Long flat sentences: only a semicolon split is deterministic; splitting at ", and" or by length would need a
  rewrite. The prompt carries the length rule; the census reports `mean_words` / `over_35_words`.
- "CTA lead-ins repeated": the heading that repeats the CTA and the clause that restates it are fixed; the
  "If … sounds like the right … for your family, message us …" lead-in (Terra, 4 pieces) is left to the prompt —
  a code cut there would leave a bare CTA sentence, which the prompt forbids.
- Not addressed here (panel findings outside the writer AI-tell scope): fact errors that passed the gates (dzong
  "unique to Bhutan", Chorten Kora district, "northernmost provincial capital") and FAQ shape (2 pairs, a FAQ naming
  a body-less detail) — gate / FAQ-step work, frozen gates.

## Tradeoffs

- **More F15 attempt-1 repairs.** The residue list adds "stock line" findings: on the 20 pilot pieces, 9 carry at
  least one (16 sentences before the pass, 14 after it). Each such piece gets one cheap sentence repair on attempt 1
  (t9_repair, Haiku, ~$0.003–0.009) when F15 is its only failure; never a hold.
- **A join can read stiffly.** "Families who visit are not arriving at a performance of culture; they are visiting a
  living community" → "Families who visit are visiting a living community …". Grammatical and true; the prompt is
  the real fix, the pass is the net.
- **A cut can lose a nuance the writer meant** ("Swimming … not the lower ones" is protected by the rule words;
  "The festival is held not in spring but in autumn" without the seed saying so loses "not in spring"). Deletions
  never add a claim, and the seed-fact check keeps every contrast the seed states.
- **CPU.** ~0.1–0.8 s per blog attempt (one `integrity_problems` per pass, more only when a pass is refused), plus the
  step guard's settle. LinkedIn: ~10 ms.

## Should know

- **Offline numbers ($0).** On the 20 stored pilot pieces the pass makes 59 edits on 15 pieces and leaves 0 fixable
  tells (census); residue 16 → 14 sentences (they go to F15). On the AA-729 golden fixture + S234 label pack (70
  pieces) every verdict and lone-denial cut read by hand is a real tell (7 verdicts, 3 lone denials); 7 edits were
  refused by the integrity check (a denial whose cut would open a paragraph on "It"), which F15's existing contrast
  rule then routes to a repair.
- **`writer_prompt_version` changes** from the pilot's `662d4aa5` to `3c078692` (the SYSTEM_PROMPT hash). The
  `test_blog_prompts_are_byte_identical` guard hashes are updated with a dated note.
- **The T8 angle prompt still says "genuinely different" (its own text);** the angle's `best_final_style` reaches the
  writer sanitised. T8 is out of this lane.
- **Live check after deploy:** a fresh T9 write shows `writer_input.writer_prompt_version = 3c078692`; a piece with a
  tell shows `ai_tells: …` lines in its F13_autofix flag (admin Content Quality drawer); a second LinkedIn write on one
  request shows `writer_input.lead_focus`. The re-panel numbers come from the script + the orchestrator's 4-lens panel.
- No migration, no Terraform, no new gate / registry / taxonomy entry, no frontend change. `quality/paths.yaml`
  lists `ai_tells.py` with the hygiene group (Q0 + R0). Needs a CIS api + worker deploy in a wave gap.
