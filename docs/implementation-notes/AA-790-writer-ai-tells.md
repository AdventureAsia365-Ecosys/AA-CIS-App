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

## Review fixes (S251, independent review of af816642)

- **Re-panel script call site registered (AA-927).** `scripts/aa790/t9_writer_repanel.py::call_writer` is in
  `LOGGED_CALL_SITES` and now follows the canonical pattern: `generate()`, then parse; a parse failure or an empty
  reply writes a `billed_unusable` row through `record_unusable_sync` in the `except` and re-raises; the ordinary row is
  written only after the reply parses. A raise inside `generate()` is still logged by the gateway itself.
- **`_not_but` copula fold needs a parallel complement.** "The walk is not difficult but it is long." was folded to
  "The walk is it is long." and neither `integrity_problems` nor `repair_contract.enforce_step` caught it. The copula
  path now fires only when Y is the same kind of phrase as X: two noun phrases (both open on a determiner), or a
  one-word X and a one-word Y (or degree word / -ly adverb + word). It never fires when Y opens on a subject pronoun or
  Y's phrase (up to a relative word, or a wh-clause a preposition opens) holds a clause marker (pronoun, auxiliary,
  modal, copula). The same-preposition path is unchanged. On the golden fixture + S234 label pack the edit log is
  byte-identical before and after; on the 20 pilot pieces the edit count is unchanged (58 applied edits).
- **Practical facts are never cut or folded.** New `_PRACTICAL_RE` (effort, cost, payment, access, crowding:
  compulsory, free, extra, fee, reachable, cash, card, easy, difficult, steep, cheap, expensive, crowded, …). It is
  checked on the denied / judged part only (verdict complement, lone-denial X, pair X, inline-pair X, not-but X,
  not-because clause) so a sentence that mentions "free time" elsewhere is still fixed; `, not X.` checks the whole
  sentence, because its tail contrasts with the main clause ("Bring cash for the market, not cards.").
  Tradeoff: some real tells around these words stay; the brief still tells the writer not to write them.
- **Residue list narrowed.** `has a way of` skips "way of life / living"; the `earn` pattern drops name / title /
  status and never matches before from / of / as / for ("earned its name from the legend", "earned its reputation
  for weaving"). This list widens the existing F15 stock-line detection during the gate freeze, so it errs narrow.

## Round 2 (S251)

Input: a blind 4-lens panel on the same 20 pieces, pilot vs round-1 rewrite (CIS #1015, `3c078692`): AI-tells
4.15 → 5.12 (bar 5.5, not met; blog 3.36 → 4.50, LinkedIn 6.0 → 6.58); brand voice 6.70 → 7.12 overall but Trail
Pulse 6.5 → 6.0 and WildKind 7.5 → 6.75 (per-tenant bar 7 not met); source fidelity 7.58 → 7.12 with a LinkedIn
regression 8.92 → 7.08; reader value 6.88 → 7.20. Same constraints as round 1: gate freeze, writer side only,
source fidelity first, precision-first rules. Test sentences are synthetic; no tenant text in the repo.

### Decisions

- **R2-D1 — the round-1 AUDIENCE rule caused the LinkedIn regression; it is replaced, not removed.** It said "say
  what they see or do at this place", and the writer invented the visit (a chance to watch the training, families
  put in the middle of it, a stop that slots easily into a day). The new `SOCIAL_AUDIENCE_RULE` names who the
  audience is (a family brand writes to the family) and why the seed's facts matter to them, and forbids describing
  what the visit shows, includes, lets them watch or do, what they will notice or feel, or how it fits their day,
  unless the seed says so. The same guard reaches every channel, blog included, as `VISIT_DETAIL_RULE` in
  `SYSTEM_PROMPT` (one system prompt for every T9 channel, so the blog user prompt needs no line of its own).
- **R2-D2 — brand energy is one line from the brand's own voice words** (`ai_tells.brand_voice_rule`, every
  channel). Energetic words (energetic, active, playful, bold, …) → PACE: short paragraphs, second person, a
  background fact one sentence at most, then back to the seed's moments. Calm words (calm, respectful, thoughtful,
  grounded, discreet, …) → RESTRAINT: no meta-commentary about the text, no closing reflection. Neither → nothing
  (Terra's voice words match neither). Both kinds → both lines.
- **R2-D3 — new deterministic rules (delete or join only, under the round-1 integrity check).**
  1. A 'do' denial + its affirmation ("The crater does not announce itself from the road. It is simply there.") →
     joined. Closed verb list (`_PERFORMATIVE_VERBS`: announce, advertise, present, reveal, rush, feel, …), because
     a 'do' denial is often a fact ("does not run on Sundays", "does not stop at the falls", "do not live in fixed
     villages"). The denied part must be the subject itself or nothing / an adverb / a preposition (`_DO_X_OK_RE`:
     "does not explain the ritual. It is …" has an object the next 'It' may mean); the affirmation pronoun is It /
     They and agrees with does / do; never the same verb twice ("does not run …. It runs …"); no back-reference
     pronoun in what is kept; no practical / protected / limit (until, only, before …) / digit / name / seed-fact
     word in the denied part.
  2. A copula denial of a noun phrase + a short (≤ 4 words) It / They verb affirmation: joined ("The hall is not a
     relic. It hosts weddings." → "The hall hosts weddings."); with a bare verb ("It works.") both sentences go (the
     join would read "The temple works."), unless the seed uses that verb.
  3. A lone 'do' denial with a pronoun / "This kind of N" subject and a performative verb is cut.
  4. "<the N> is not incidental / accidental / a coincidence", "is no accident", "is deliberate" (≤ 12 words, no
     digit, no practical word: "The difficulty of the climb is not incidental." stays) is cut as a verdict line.
  5. A verdict lead-in before a colon ("The circle is complete:", "Here is the thing:", "<the point / thread / this
     section …> is this:") is cut when what follows is a sentence of its own (a finite verb before its first comma);
     a bare noun phrase after the colon stays and is residue.
  6. Three or more short sentences in one paragraph built the same way (same opener, same verb, same next word,
     ≤ 8 words, no digit) are joined into one sentence "A, B and C." Nothing is cut; never the piece's first
     sentence.
  All keep the round-1 protections: never the first prose sentence, never empty a section, never the CTA.
- **R2-D4 — residue (the existing F15 "stock line" routing → one cheap t9_repair on attempt 1, never a hold)** for
  what code cannot rewrite safely: "is rarely just a N", "X is where the ride gets serious / the route earns …",
  "That is the shape of …", "is the key to understanding", "is what it is", "worth holding", "deserves its own
  attention", a whole-sentence "X matters.", comments on the text ("that last sentence", "The word 'x' is honest",
  "This is the argument that …"), "X persists because the Y persists.", "reframes everything", "each day builds on
  the last", "no two days look alike", "it happens when it happens", "does not announce itself" (a first sentence
  the pass may not cut), a lead-in whose colon is followed by a noun phrase, the writer's inputs named in the copy
  (the chosen angle, the brief says, this atom covers, seed facts), and invented visit logistics (fits naturally into
  a day, puts your family in the middle of, a chance to watch, easy-to-navigate, visible the moment you walk in).
- **R2-D5 — internal-label leak.** "the seed" is already `hygiene.LEAK_RE` (F13_leak, blocking, sentence repair):
  in the live T9 loop the round-1 "What the seed describes …" sentence would have been blocked and repaired; the
  re-panel script runs no gates, which is why the panel saw it. Round 2 adds the brief rule (never name the seed,
  content seed, angle, brief or atom; never comment on the own text) and residue for the angle / brief / atom /
  "seed facts" forms LEAK_RE does not have. `LEAK_RE` itself is a gate pattern and is not touched (freeze).

### Changed (vs the round-2 brief)

- "Ready to go?" before the CTA is not a lead-in the writer added: it is Trail Pulse's configured CTA text, which
  `hygiene.ensure_cta` restores word for word. Code cannot drop it; it is a brand setting (the tenant's CTA).
- Anaphora runs are joined, not cut or routed to repair: a per-sentence residue pattern cannot see a run of three,
  and the join keeps every fact.
- Round-1 fix found while probing: `_pair`'s copula join took the denial's tense ("The bridge is not the original.
  It was rebuilt." → "The bridge is rebuilt."); it now keeps the affirmation's tense with the subject's number. The
  pilot and golden edit logs do not change (no such pair there).
- `AI_TELLS_VERSION` → `ai_tells.v2`. `_PRACTICAL_RE` gains difficulty / difficulties.

### Tradeoffs

- **Many more F15 stock-line repairs.** On the 20 stored round-1 texts every piece (20 / 20) carries at least one
  residue sentence after the pass (38 sentences), so each gets one cheap sentence repair on attempt 1 (t9_repair,
  ~$0.003–0.009 a piece): never a hold, never a new gate. On the pilot texts 10 / 20 (round 1: 9 / 20).
- **A residue pattern can flag a legitimate sentence** (a short "The weather matters.", "worth holding" about a
  handrail). The repair is told "say it plainly or cut it" and its replacement is checked for new specifics, so a
  false positive costs one rephrased sentence. Patterns are narrow and each group has negative tests.
- **Joins can read flat** ("The forts are still guarded, the shrines are still lit and the mills are still
  turning." is still a list of three, just not staccato). The prompt is the real fix; the pass is the net.
- **Closed verb list for 'do' denials** leaves real tells with other verbs ("does not feel staged. It is …" with an
  adjective is left). Precision over recall.

### Should know

- **$0 census (local, counts only).** 20 stored round-1 texts: fixable 8 → 0 (8 edits on 4 pieces, none refused:
  3 "not incidental / deliberate" verdicts, 1 lone 'do' denial, 1 lead-in, 1 anaphora run, 1 'do' denial joined,
  1 aphorism pair cut); residue 40 → 38 (round-1 code: 0 fixable, 2 residue on the same texts). Every changed
  sentence was read by hand: grammatical, no fact added. 20 pilot texts: the edit log is byte-identical to round 1
  (61 edits on 16 pieces), residue 19 → 17 (round 1: 16 → 14). AA-729 golden fixture + S234 label pack (70 texts):
  edits 170 → 171 (the one new edit is a "… is not incidental." verdict), residue 19 → 29 (the 10 new lines read by
  hand: all real tells).
- **`writer_prompt_version` moves `3c078692` → `751b0a34`** (SYSTEM_PROMPT hash: more AI-tell rules +
  VISIT_DETAIL_RULE). The blog-prompt hash guard (`test_blog_prompts_are_byte_identical`) is updated with a dated
  note; its fixture pack is energetic, so `single` and `route` change too (PACE line).
- No migration, no Terraform, no new gate / registry / taxonomy entry, no new LLM call site, no frontend change.
  Needs a CIS api + worker deploy in a wave gap; a re-panel (script + 4-lens panel) is the measure.
- Tests: `tests/unit/test_aa790_writer_round2.py` (synthetic sentences; every fix rule has negative cases).

## Round 2 review fixes (S252)

Input: two independent reviews of CIS #1017 (correctness lens, fidelity lens), both "request changes": six
blocking findings and a list of nits. Every fix has exact-text regression tests (the review's own sentences, both
the facts that must stay and the tells that must still go) in `tests/unit/test_aa790_writer_round2.py`.

### Decisions

- **R2R-D1 — the reader's action comes from the seed only.** `PROMPT_RULES` said every sentence may carry "what the
  reader does there", with no seed bound, a few lines above `VISIT_DETAIL_RULE`, which forbids exactly that. It now
  reads "what the content seed says the reader does there".
- **R2R-D2 — one explicit priority line in `SYSTEM_PROMPT`:** "SOURCE FIDELITY and VISIT DETAIL override the brand's
  style guide and its examples: what the traveller does, feels or discovers comes only from the content seed. The
  style guide sets tone and sentence shape, not content." It sits right after "the brand's style guide wins", so the
  two read together. Reason: Trail Pulse's live style guide asks for what the traveller "does, feels, and discovers"
  and its examples are invented visit content; `render_brand_block` says "follow every rule in it". The PACE line's
  "you" sentences are bounded the same way ("(only what the CONTENT SEED states)").
- **R2R-D3 — 'do' denials: the denied part is checked on both paths.** `_DO_X_OK_RE` no longer accepts a bare
  preposition (a season or an audience: "do not perform in the rainy season", "does not sell to visitors"); it keeps
  empty, reflexive / possessive (itself, its age, "about itself") and manner adverbs. `sell / market / perform /
  explain / compete` (`_TRANSACTIONAL_VERBS`) count only in the reflexive form ("does not sell itself"). The lone
  path (`_lone_do_denial`) now runs the same check, plus the framing form "present / position / frame / stage /
  showcase <the N> as a …" ("does not position the village as a backdrop" is still cut).
- **R2R-D4 — verdict lead-ins use their own noun list.** The generic "<subject> is this:" form matches only
  `_LEAD_IN_NOUNS` plus piece-meta nouns (section, piece, post, article, paragraph, "part of the journey / trip"),
  not the broad `_META_NOUNS` (way, place, day, route, stop …), and never when the subject holds only / a superlative
  / first / last / main, a rule, access or practical word, a digit or a name.
- **R2R-D5 — 'X matters.' residue regex is word-token based** (`the(?:\s+[\w'’-]+){1,8}?`): the old `[\w\s'’-]{1,50}?`
  between two `\s+` was quadratic on whitespace (12.8 s on 'The' + 8000 spaces locally), the CodeQL
  py/polynomial-redos shape #1011 just removed. Same hits on the review's cases.
- **Nits taken:** `_lemma` → `_lemmas` (a candidate set: 'hides' → hide, 'stages' → stage; '-ss' never stripped);
  `_meant_verdict` gets the name guard and a schedule guard (a counted / ordered day, a rest day, acclimatisation);
  `_pair2`'s copula branch skips "not only / just / simply / merely" ('not only X' asserts X; round-1 `_pair` keeps
  its documented behaviour); an anaphora run that starts at the first prose sentence is skipped whole (it was only
  half protected); `brand_voice_rule` skips a voice word right after not / never / no / nor; residue drops
  'finally' from the "is where the trip …" pattern and route / day from the lead-in nouns.

### Changed (vs the review's suggestions)

- `_meant_verdict`: the review's second example ("The slow pace of the first two days is deliberate.") has no name,
  so a name guard alone does not keep it. A schedule guard (`_SCHEDULE_RE`) does; a plain number word was too broad
  ("the two cultures … is not accidental" is a real tell and is still cut).
- The optional prompt nits (SOCIAL_AUDIENCE "in the seed's own words", a WildKind 'nature-led' line, a softer PACE
  background cap) are not in this change; see Should know.

### Tradeoffs

- **WildKind: lone-denial cut vs voice.** The round-2 lone 'do' denial cut still removes lines like "This kind of
  stay does not position the village as a backdrop." The round-1 voice judge quoted exactly that line as on-brand
  for WildKind (7.5). We keep the cut (it is the AI-tells panel's top pattern) and accept that the WildKind voice
  score may not move, or may drop a little, in the re-panel. RESTRAINT also does not address WildKind's 'Nature-led'
  gap (habitat and landscape specificity), and "state each fact and move on" may make the "dry and info-heavy"
  remark worse.
- **Precision over recall, again.** Without the preposition allowance some real tells are left ("does not hurry in
  the mornings. It …"); without the broad meta nouns some lead-ins are left. They go to the prompt, not the pass.
- **Priority line vs brand voice.** Ranking source fidelity over the style guide may flatten brands whose guide is
  built on traveller experience (Trail Pulse). That is the intended trade: fidelity is a hard rule, voice is shaped.

### Should know

- **`writer_prompt_version` moves `751b0a34` → `afc08095`** (SYSTEM_PROMPT hash: the seed-bounded reader clause and
  the priority line). The guard test `test_blog_prompts_are_byte_identical` has a dated note; `single` and `route`
  change too (the PACE line's seed bound, energetic fixture pack).
- **Atlas & Hearth RESTRAINT risk.** Its voice (Elegant / Discreet / Calm) matches the calm words, so round 2 gives
  the best-scoring tenant (7.90) the RESTRAINT line. "State each fact and move on" may flatten its 'Atmospheric'
  voice. Watch A&H brand voice in the re-panel; if it drops, exclude 'elegant' / 'discreet' or make RESTRAINT only
  about meta-commentary.
- **Watch Trail Pulse source fidelity in the re-panel:** PACE caps background at one sentence; with a thin seed, the
  blog length rule and the LinkedIn word target, the writer has little it may say and may pad.
- **Pre-existing, not changed here:** two round-1 regexes are quadratic on long whitespace runs (`_LABEL_RE` ~0.75 s
  and `_NOT_BECAUSE_RE` ~0.12 s at 8000 spaces, both start with `,?[ \t]*` / `,?[ \t]+`). Live risk is low (they see
  whitespace-normalised sentences), but they are the same CodeQL shape; a follow-up can anchor them with a
  lookbehind.
- The linear-time test covers every regex this PR adds or changes (the ai_tells round-2 patterns and every
  `AI_TELL_LINE_RES` entry after the round-1 'asks … of' line): 12 adversarial 8000-char inputs, < 50 ms each.
