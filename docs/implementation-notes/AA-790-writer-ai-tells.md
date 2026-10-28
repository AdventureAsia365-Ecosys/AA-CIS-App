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

## Round 3 (S252)

Input: a blind 3-arm panel (same judges; pilot / round 1 / round 2, 20 pieces each) on round 2 as live
(`writer_prompt_version` `afc08095`): AI-tells 5.33 (bar 5.5, not met; blog 4.96, LinkedIn 6.17, Atlas & Hearth blog
4.4), brand voice 7.33 (met, every tenant ≥ 7: must not regress), source fidelity 7.25 (LinkedIn 7.08 vs pilot 8.83:
not recovered), reader value 7.28. The round-2 pass log showed why the top denial frames survived: it SAW them but
its only move (cut the denial) left a paragraph opening on "It …", so the integrity check refused the edit
("skipped denial before its affirmation (breaks the text)"). Same constraints: gate freeze, writer side, precision
first, delete / join only, never drop a practical fact, a restriction, a superlative of access or a 'not only'
assertion. Test sentences are synthetic; no tenant text in the repo.

### Decisions

- **R3-D1 — join on the denial's own subject instead of cutting it.** A demonstrative denial + an affirmation on
  another pronoun: "This is not A. It is B …" → "This is B …" (copula, `_pair` → `_demonstrative_join`); "This is not
  A. It builds …" → "This builds …" (any verb, any length, `_pronoun_verb_pair`). The referent is unchanged, so the
  integrity check accepts it. The same pronoun twice ("It is not A. It is B.") keeps the round-1 cut (identical
  text, stable logs); 'not only / just / simply / merely' is never folded (it asserts A); round-2 `_denied_ok`
  guards (rule / practical / limit word, digit, name, seed fact) on A.
- **R3-D2 — a moved narrative verb** ("The valley's story does not begin in A. It begins in B, …" → "The valley's
  story begins in B, …", `_relocation_pair`): only for an abstract subject (story, history, heritage, significance,
  meaning, appeal, roots …), a closed verb list (begin, start, end, lie, live, rest), the same verb and the same
  preposition in the affirmation, no back-reference. "The trek / ferry does not begin / stop in A" is logistics and
  never matches. The denied place may be a name (that is the frame); the seed-fact check reads verb + place. A join
  is allowed on the first sentence (the hook is kept, only the denial goes).
- **R3-D3 — 'not A, but B' after a colon, and with a comma** (`_NOT_BUT_RE` gains an optional comma group): "what a
  dzong is: not a relic, but a working institution" → "…: a working institution"; "…, not as artefacts, but as
  living practice" → "…, as living practice". With a comma the fold happens only when B is the sentence's last phrase
  of ≤ 5 words (`_y_ends_sentence`: after ", but" a new clause often starts). In the colon frame a one-word A the
  seed uses elsewhere ("the sacred relic of …") does not count as the seed's contrast.
- **R3-D4 — staging contrast tails** (`_staging_tail`): "rather than / instead of / , not" + a closed STAGING list
  (relic, museum piece, backdrop, show, spectacle, performance, demonstration, footnote, coincidence, incidental
  detail, trick of the light, staged, scripted, choreographed …) at the sentence end, and ", not just / merely /
  only / simply" + a closed SPECTATOR list (view, glimpse, photo, postcard, backdrop, pass through it). These words
  deny that a thing is put on for show or only looked at: no seed fact sits in them, so the 'not just' half is not a
  'not only' fact. Rule / practical / digit / seed-fact checks read the tail only ("Visits are included, and the
  contact is direct rather than staged." → "…is direct."). A bare 'not' tail needs a comma ("The dance is not
  staged." would read "The dance is.").
- **R3-D5 — 'something … :' build-ups** (`_something_lead`): "offer something rarer than scenery: a way of life" →
  "offer a way of life"; also "something specific / concrete / different:" and "something that no X can substitute:".
  Only after offer / give / provide / hold / bring / add / carry / reveal / show and before an article or possessive.
- **R3-D6 — verdicts cut:** "That single fact changes how you think about …" (`_changes_verdict`: about the reader's
  view only, "how you / families / a visit …"; "That fact changes how the festival is scheduled." stays) and
  "The position is not incidental to its significance." (`_MEANT_RE` gains "to its significance / meaning / history
  …"). The round-1 protections hold (never the first sentence, never a section's only sentence).
- **R3-D7 — a heading built from the brief** (`_brief_heading`): "## Northern Mongolia and the Logic of Convergence"
  came from the angle's best final style ("convergence … as the organising logic"), not its name, so the round-1
  angle-name check missed it. A heading part (split at ':' and ' and the ') whose words outside the seed + Facts
  (≥ 2) are all the brief's is dropped; a heading is never emptied. `TellContext.angle_text` (`ai_tells.angle_text`:
  name, why it works, formula fit, best final style) is passed by `run_write_background` and the re-panel script.
- **R3-D8 — residue (F15 'stock line', one cheap repair on attempt 1, never a hold)** for what code cannot rewrite:
  "that's a detail worth knowing", 'The word "may" is worth noting', "worth pausing on", "Those numbers matter less
  than", "This is the section … that rewards / worth …", "rewards the greatest attention", "begins to reveal its
  logic", "earns its place as", "earns both names", "feel earned", "the way most good journeys should", "in a way
  that a photograph does not / a stall cannot …", "that no classroom exercise replicates", "carries real weight",
  "long after the day is over", "resets a family's sense of scale", "The geography is the lesson", "builds on it",
  "adds a layer to the same story", "Their presence here is a product of it.", "is part of what the site means",
  "closing the circle", "For families …, that context matters." / "that context changes what …", "There is no
  performance in this". Each has negative tests (porters carry weight of 15 kg, closes the loop, earned both names
  from …, a recipe no other valley uses, "That detail matters for the permit …").
- **R3-D9 — LinkedIn source fidelity, prompt (narrowest rules, SOURCE FIDELITY block of SYSTEM_PROMPT):**
  AGES ("never give an age, an age band or a minimum age … unless the content seed states it; the AUDIENCE line's
  ages describe the brand's customers, not this trip"; NUMBERS no longer lists 'age', which it allowed from the
  AUDIENCE line: that is how Terra's '7–16' became "children aged seven and up"); CAUSE AND SCOPE (never make one fact
  the reason for another, "established because it supplies artisans", unless the seed links them; never widen a
  fact: "plays a role in supplying" is not "trained here" / "a single school", "like most" is not "every", "one of
  the" is not "the"); the scene-detail line gains "feels like (cold water, a quiet hall)".
- **R3-D10 — LinkedIn source fidelity, deterministic flag that reads the seed** (`hygiene.unsupported_ages`,
  `hygiene.unsupported_reasons`, reported by `gate_voice` from the `exclude_texts` run_quality_gates already passes:
  the moment's own seed lines + Facts): an age claim ("aged seven and up", "ages 8+", "children over ten",
  "10-year-old") whose number the source's own age claims lack; a founding reason ("was established because", "exists
  because") for a subject whose PURPOSE the source states ("The institute was established to ensure …") and no
  founding 'because' of its own. Finding text "unsupported detail: an age ('…') the source does not give — say it
  without the age: '<sentence>'" (sentence_repair's quote shape), kind `UNSUPPORTED_DETAIL_PREFIX` appended last to
  `_F15_KIND_ORDER`: F15_voice, blocking on attempt 1 only, one sentence repair, `never_holds`.

### Changed (vs the round-3 brief)

- The brief asked for "a deterministic residue flag". The round-1/2 residue list (`AI_TELL_LINE_RES`) is
  seed-agnostic (`template_lines(text)` has no seed), so the seed-aware flag sits in `gate_voice` next to it, on the
  same F15 route, reading `exclude_texts`. It is a new finding KIND in an existing gate (like the residue widening);
  no new gate, registry, taxonomy or ledger entry. F13_hygiene (warn-only, no repair) was the alternative; F15 was
  chosen so the flagged sentence actually gets the one repair.
- "Established because" is flagged only against a seed-stated purpose of the same subject: an 'exists because' for
  something the seed gives no purpose for (the takin preserve story, allowed background) is not flagged. "Trained at a
  single government school" (an absolute overclaim) has no safe deterministic check: prompt only (CAUSE AND SCOPE).
- Found while measuring: the round-1 lone-denial cut removed "They were not saved." (monasteries lost in a purge, a
  fact). A passive denial (`_is_participle`: -ed or a closed irregular list) is never a lone-denial cut now.
- `test_aa777_r6_fixes.test_job_reverts_a_repair_that_adds_a_time_and_logs_it` holds `ai_tells.rewrite` to identity
  like the other deterministic steps it patches: the pass now cuts its fixture's ", not just photos." tail (a
  spectator contrast), so the fixture never reached the gate it tests.
- Not taken (fact-preserving rule wins over the panel): "offered as a possibility, not a fixed feature" (a
  restriction), "neither guaranteed nor effortless" (guarantee), "not merely documentation" / "not its scale alone,
  … though both are real" ('not only' assertions), "Most people arrive in X and stay. This route uses it as a launch
  pad." (the hook), triads and anaphora with different verbs ("still governing, still worshipping, still producing"),
  "The form and the function have travelled together." (no safe pattern). "Ready to go?" is Trail Pulse's own CTA.

### Tradeoffs

- **Residue grows again.** On the 20 round-2 texts after the pass: 34 residue sentences on 17 pieces (round-2 code: 4
  on 4); round-1 texts 38 → 40; pilot 17 → 18; golden fixture + S234 pack 29 → 42 (every new hit read by hand: a real
  tell). Each flagged piece gets one cheap sentence repair on attempt 1 (t9_repair), never a hold; the seed-aware
  findings add 4 on the 6 round-2 LinkedIn posts (exactly the judges' quotes), 0 on the pilot texts, 1 on a round-1
  blog ("as legible to a twelve-year-old as to a geographer": an age the seed lacks; the repair drops the age).
- **The re-panel script runs no gates**, so residue and the seed-aware findings do NOT reach a re-panel built with
  `t9_writer_repanel.py`; only the pass edits and the prompt do. A re-panel that should show their effect needs the
  F15 sentence repair step (or a live T9 write) in the loop.
- **Joins read a little flatter than the writer's rhythm** ("This builds across several days, …"). Grammatical,
  nothing added; the prompt is the real fix.
- **Closed lists again** (narrative nouns, relocation verbs, staging / spectator words): real tells outside them stay.

### What could move brand voice (watch in the re-panel)

- PACE / RESTRAINT are unchanged (test pins their text). Voice risk comes from three places: (1) staging tails and
  'something …:' cuts remove contrast flourishes Atlas & Hearth's 'Atmospheric / Intelligent' voice may have earned
  credit for (the round-2 judges scored A&H voice ≥ 7 with them in); (2) the AGES rule removes the explicit age line
  Terra's family voice used ("for families with children aged …"): the post must say who it is for without an age;
  (3) CAUSE AND SCOPE may make LinkedIn posts read as a list of facts if the writer stops linking them at all. If
  Terra or A&H voice drops below 7, soften CAUSE AND SCOPE to the founding / purpose case only, and drop the
  SPECTATOR 'not just' tail (keep STAGING).

### Should know

- **$0 measure on the 20 round-2 texts** (the 68 phrases the AI-tells judges quoted, as stored after the round-2
  pass; scratch census, not committed): round-2 code catches 3 (0 by the pass, 3 by residue); round-3 code catches
  41 (14 by the pass with 18 edits on 12 pieces, 0 refused; 27 by residue). Blog 34 / 53, LinkedIn 7 / 15, Atlas &
  Hearth 14 / 21. All seven forms named in the brief are fixed by the pass. Every changed sentence was read by hand.
- **Other corpora (edit logs, $0):** pilot texts 62 → 64 edits (a staging tail, a colon 'not … but'; one cut became a
  join, a brief-words heading); round-1 texts 8 → 10; golden fixture + S234 pack 171 → 170 log lines (7 denial cuts
  became joins with the same text after them, 1 refused pair now joins, the "They were not saved." cut is gone, 3
  sentences already edited for a stock word also lose a 'not … but' / staging tail; the LinkedIn "That's not filler.
  It's a structural choice." pairs are still refused by the integrity check, as before).
- **`writer_prompt_version` moves `afc08095` → `24363541`** (SYSTEM_PROMPT hash). The guard test
  `test_blog_prompts_are_byte_identical` has a dated note; `single` and `route` do not change. `AI_TELLS_VERSION` →
  `ai_tells.v3`.
- Tests: `tests/unit/test_aa790_writer_round3.py` (146: exact before / after text, negatives for every rule, the
  seed-aware findings with and without the seed stating them, prompt text, PACE / RESTRAINT pinned, 8000-char
  adversarial inputs < 50 ms for every new or changed regex incl. `_NOT_BUT_RE` and every round-3 residue entry).
- No migration, Terraform, new gate / registry / taxonomy entry, LLM call site or frontend change. Needs a CIS api +
  worker deploy in a wave gap; the measure is a re-panel (with the repair step, see Tradeoffs).

## Round 3 review fixes (S252)

Input: an independent review of the round-3 commit (444b489a), six blocking findings, each with the reviewer's own
sentences. Every fix has exact-text regression tests (the finding's sentences must stay; the panel's quoted tells
must still go) in `tests/unit/test_aa790_writer_round3.py` (section "round-3 review fixes").

### Decisions

- **R3R-D1 — the seed-fact guard holds in the colon 'not X, but Y' frame** (finding 1). Round 3 skipped it for any
  one-word X, so "The best months are dry: not in the monsoon, but in April." (seed: avoid the monsoon) lost its
  seasonal limit. The only exception now is a staged-thing noun (`_STAGED_NOUN_X_RE`: relic, museum piece, backdrop,
  stage set, set piece, theme park, curiosity, showpiece) that the seed uses as a plain noun and never denies
  (`_seed_denies`: no "not / no / never / nor … relic" in the source text). The panel's "not a relic, but a working
  institution" (seed: "houses the sacred relic of …") is still folded; a seed that says "is not a relic" keeps it.
  The source text reaches the rule through `_SourceWords` (a frozenset of the seed words, as every rule reads it,
  that also carries `.text`).
- **R3R-D2 — staging tails keep only words that say "put on for show"** (finding 3): staged, scripted,
  choreographed, stage-managed, museum piece, backdrop, stage set, incidental detail / feature, trick of the light.
  Show, performance, demonstration, spectacle, relic, footnote, coincidence … left the list (expectation and welfare
  facts: "see the dance rehearsal rather than the full performance", "rather than used in shows"). Between the
  article and the word only a closed modifier list (mere, carefully, artfully, painted …), never a free word or a
  verb ("rather than used as backdrops" stays).
- **R3R-D3 — an audience in a denial is a fact** (finding 4): `_audience` (hygiene `_PERSON_NOUNS` + toddlers,
  infants, babies, beginners, novices, first-timers, seniors, elderly, wheelchair users, walkers, riders, swimmers,
  climbers) guards `_denied_ok` (the round-2/3 verb and pronoun joins, the lone 'do' denial), `_lone_denial` and the
  round-1 `_pair` (the copula join the demonstrative join rides on). "This is not a walk for small children. It
  climbs …" is never joined or cut, at a paragraph start or inside one.
- **R3R-D4 — residue entries no longer match schedules and practical sentences** (finding 5). "There is no
  performance / show …" needs the aphorism form ("in this / here / about it", or ", no …") and no day, time, season
  or schedule word anywhere in the sentence (`_SCHEDULE_NEG`, one look-ahead from the sentence start). "This is the
  section … worth / that rewards" and "rewards … attention" never match a sentence with a booking, limit, footing or
  weather word (`_ATTENTION_FACT_NEG`); the second is now anchored at the sentence start for that guard and names its
  phrase as group `p` (`template_lines` reports `p` when a pattern has it). "closes the circle" is not followed by
  back / to / at / on / via … (a loop road).
- **R3R-D5 — an age is known when the seed writes the number in any wording** (findings 2 and 6):
  `unsupported_ages` builds `known` from every age-sized number in the source (`_SOURCE_NUMBER_RE`: digits and number
  words, never a group of "2,000"), so "Minimum age is 12", "under-5s", "at least 14", "Children 6–12", "from 8 years",
  "children 10 and older" all make the age known. 'N-year-old' counts only as a person: "year-olds", or "year-old"
  used as a noun (before a person noun, a modal or verb, a function word, or nothing: "a 10-year-old can …", "legible
  to a twelve-year-old as …"); "the 300-year-old temple" never counts, "2,000-year-old" is never read as "000", and a
  value over 120 is never an age.

### Changed (vs the review's suggestions)

- Finding 6 suggested 'year-old' + nothing / a person noun only. The round-1 blog's "as legible to a twelve-year-old
  as to a geographer" (a real unsupported age the census found) has 'as' after it, so a closed function-word list (as,
  and, or, who, than, with, in, at, to, for, on) also counts as the noun use; an attributive thing age is never
  followed by one of those in the corpus.
- `_seed_denies` reads the source text with a bounded window (up to three words between the negation and the
  noun's stem): linear, tested with the 8000-char inputs.

### Tradeoffs

- **Recall given back for precision.** On the AA-729 golden fixture + S234 pack (70 texts) the fixed code makes the
  same 170 edits minus one staging tail ("… rather than performance"); on the 20 round-2 texts one staging tail goes
  ("… commitment rather than a demonstration", not a judge's quote); on the pilot texts two edits go ("geology rather
  than spectacle", and "These are not details to hide from children. They are …", an audience word). The panel's
  quoted forms are unaffected (below).
- `_SCHEDULE_NEG` is broad (any day / month / season / 'during' / 'when' / digit): some real "There is no
  performance …" aphorisms that mention a time stay. They go to the prompt, not the residue.

### Should know

- **$0 quote census on the 20 round-2 texts** (local script, not committed: every quote in an r2 AI-tells or
  source-fidelity judgment matched to its sentence in the stored text; "caught" = the pass changes the sentence, or
  residue / a seed-aware finding flags it after the pass). AI-tells: 63 matchable quotes; origin/main (round 2) 3
  (0 pass, 3 residue); round 3 as first committed 37 (11 pass, 26 residue); after these fixes 37 (11 pass, 26
  residue): no panel quote lost. Source fidelity: 51 quotes; 0 → 9 → 9. Seed-aware findings: 4 on the round-2
  LinkedIn posts (2 ages, 2 founding reasons, exactly the judges' quotes) and 1 on the round-1 texts, unchanged by
  the fixes. (The round-3 note above counted 68 / 41 with a looser quote split; same direction.)
- No prompt change in this fix: `writer_prompt_version` stays `24363541`, `AI_TELLS_VERSION` stays `ai_tells.v3`.
- Tests: `tests/unit/test_aa790_writer_round3.py` 208 (was 146 + the review cases); the linear-time list gains
  `_STAGED_NOUN_X_RE`, `_AUDIENCE_RE`, `_SOURCE_NUMBER_RE`, the two new look-ahead guards (inside their residue
  entries) and `_seed_denies` / `_audience` in the function timing test.

### Second review (4 blocking findings on d09b5b6c)

Principle for every fix: a rule removes only the words that are the tell. It never deletes a fact, a restriction, an
audience limit or a seasonal / time limit, and it never acts on a sentence the pass itself wrote. Each finding's
sentences have exact-output tests (paragraph start and mid-paragraph) in the "second review of round 3" section of
`tests/unit/test_aa790_writer_round3.py`. Before the fix, 56 of the new tests fail.

- **R3R2-D1 — no pass acts on a sentence an earlier pass wrote; a short 'This is the …' join is not made** (finding
  1). The demonstrative join turned "This is not the dry season. It is the tail of the monsoon." into "This is the
  tail of the monsoon.", and the second structure pass then cut that as a verdict line, so both sentences were lost.
  Two changes. (a) `rewrite` collects the `Edit.new` sentences of every kept edit and hands them to the later passes
  (`written`). A written sentence is never cut (lone denial, verdict, aphorism pair), never the denial of a pair, never
  part of an anaphora run, and never cut down by the CTA-clause rule. It may still be the affirmation that a join
  keeps word for word: the stock-word pass often cleans that sentence first ("… something genuinely rare.").
  (b) `_pair` returns `KEEP_PAIR` when the demonstrative join would be a short verdict-shaped line
  (`_verdict_shape`: `_VERDICT_RE` + `_VERDICT_REST_RE`, ≤ 10 words, no seed check). In that case the pair is a
  correction of which thing it is, not a reframe, so both sentences stay and the lone-denial cut is skipped too.
  `_LIMIT_RE` (only, until, except …) now guards the round-1 `_pair` and `_lone_denial`, as it already guarded the
  round-2 pairs: "This is not the only path to the monastery." stays.
- **R3R2-D2 — the audience guard now covers every rule that drops a denied part** (finding 3): `_not_but` (same
  preposition and the colon / comma frame), `_not_tail` and `_inline_pair` check `_audience` on X. `_AUDIENCE_RE`
  gains passengers, pedestrians, cyclists, motorists and drivers ("not for foot passengers but for vehicles only").
- **R3R2-D3 — a seed number counts as a known age only in an age context** (findings 2 and 4). `_SOURCE_NUMBER_RE`
  is gone. `_source_ages` reads two things: every `_AGE_CLAIM_RE` match in the source, and `_SOURCE_AGE_RE`, which
  has two forms. (1) An age cue right before the number: age / ages / aged / minimum age, a person noun, infants,
  students, riders …, under / over / at least / older than. At most four closed linking words may sit between the cue
  and the number (is, must, be, at, least, of, the, from …), and the number is not followed by a unit or a head count
  (`_UNIT_AFTER_RE`, `_COUNT_AFTER_RE`). A bare preposition cue never counts before a plural noun ("over 30
  monasteries"). (2) The number with an age tail: "12 years old", "12 and over", "10 years and over", "12+ years".
  This form never counts right after "day(s)". A number next to ':' / '.' / ',' digits (12:30, 1,300) never counts.
  "Day 8", "3-4 hours", "max 12 guests", "Lunch at 12:30", "Day 7 of the trip", "10 km" and "six years" of training
  no longer hide "aged 8 and up", "12-year-old", "4-year-olds", "a 30-year-old who", "aged seven and up", "aged 10
  and over", "children over six" or "a 7-year-old can". Every wording the first review needed is still known: "Minimum
  age is 12", "Minimum age: 7", "under-5s", "at least 14", "Children 6–12", "from 8 years", "children 10 and older",
  "under the age of 7", "aged 10+".

#### Changed (vs the review's suggestions)

- Finding 1 suggested `_verdict(joined, src)` as the test. That test depends on the seed: with "monsoon" in the seed,
  the join would still drop "not the dry season". The shape test does not read the seed, so both facts stay
  whatever the seed holds. The general `written` guard covers the chain the review describes for every rule, not
  only for the verdict cut.
- Finding 3 asked for the rafting sentence to come back unchanged. The inline-pair fold no longer happens. The
  round-1 semicolon split still applies: "… not for non-swimmers. It is for confident swimmers aged 12 and over."
  The split changes punctuation only and keeps every word, as it already did for any practical "is not …; it is …".
  The structure pass never joins the two sentences it wrote. The test pins this exact output.
- Finding 4 suggested an age cue anywhere in the same seed line or sentence. The cue has to sit right before the
  number (four closed linking words at most), because a line such as "Day 7: children visit the 10 km gorge" would
  otherwise count 7 and 10.

#### Tradeoffs

- Census (local scripts, $0; before = 58048707, after = this fix):
  - AI-tells quotes on the 20 round-2 texts: 63 matchable, 37 caught (11 pass, 26 residue) before and after.
  - Source-fidelity quotes: 51 matchable, 9 caught (2 pass, 5 residue, 2 seed-aware) before and after.
  - Seed-aware findings: the same 4 on the round-2 posts (2 ages, 2 founding reasons), 0 on their `before_text`, and
    the same 1 on the round-1 texts.
  - The edit log on all three text sets is byte-identical.
  - AA-729 golden fixture + S234 pack (70 texts): 170 → 169 edits. The one dropped edit is ", not something travellers
    need to organise independently beforehand", a practical fact that now keeps its audience word. Residue stays at 42.
- `KEEP_PAIR` also keeps a real tell pair whose affirmation is a short "the …" verdict ("That is not X. It is the
  opening."). Round 1 cut only the denial there; now neither sentence goes. These pairs are rare in the corpora (0 in
  the censuses above), and the residue / prompt still see them.

#### Should know

- `_PASSES` functions now take `(text, ctx, written)`. `written` defaults to empty, so any direct caller still works.
- Tests: `tests/unit/test_aa790_writer_round3.py` has 279 tests (was 208). The linear-time list replaces
  `_SOURCE_NUMBER_RE` with `_SOURCE_AGE_RE`, `_COUNT_AFTER_RE` and `_PLURAL_AFTER_RE`, and gains four adversarial
  inputs (age cues, "under-" runs, "7 and" runs, "7-" runs). The unit files that import ai_tells / hygiene / prompts /
  quality_gates / service (104 files) give 3796 passed and 1 skipped. Before the fix they gave 3723 passed (plus 2
  that need a git checkout).
- Also wraps a 121-character `_STAGED_NOUN_X_RE` line (flake8 max 120) that came in with d09b5b6c.
- No prompt change: `writer_prompt_version` stays `24363541` and `AI_TELLS_VERSION` stays `ai_tells.v3`.

### Third review (6 blocking findings on 67bb5550)

Same principle as the second review. Every quoted sentence has an exact-output test, at a paragraph start and
mid-paragraph, with no source and with the Taktsang seed, in the "third review of round 3" section of
`tests/unit/test_aa790_writer_round3.py`. Before the fix, 98 of the new tests fail.

- **R3R3-D1 — the comma / colon 'not X, but Y' frame folds on the copula path only** (finding 1). After ', but' or
  ': not', a same-preposition contrast is a place or time choice ("Dinner is not at the lodge, but at a farmhouse.",
  ": not on Sundays, but on Saturdays"), so `same_prep` no longer fires there. The bare frame ("set not by a calendar
  but by the forest") still folds.
- **R3R3-D2 — `_LIMIT_RE` guards every rule that drops a denied part** (finding 2, and the limit half of finding 1):
  `_not_but`, `_not_tail` and `_inline_pair` now check it, as `_pair`, `_lone_denial` and `_denied_ok` already did.
  "not the only route up, but the shortest", "not the only path …; it is the most direct", ", not the only one." and
  "not passable after heavy rain; it is …" keep their limit.
- **R3R3-D3 — no-age numbers in the seed** (findings 3 and 6). `_source_ages` now treats these as counts, durations or
  a thing's age:
  (1) Form 1 (cue before the number): a unit, a head count or 'per …' after the number (`_PER_AFTER_RE`:
  "Participants: 2-12 per departure"). A plural noun after it for every cue except the explicit age words (age, ages,
  aged, minimum age): "Students from 12 districts", "Hikers at 7 viewpoints", "Guests at over 30 stalls". A person
  noun joined by 'of' ("Families of 4"), unless 'age' is in the link or 'years' follows ("children of 8 years and
  over" stays known). A bare preposition cue before 'N years' with no 'old' / 'of age' (`_YEARS_AFTER_RE`: "for over
  12 years", "opened over 8 years ago", "for at least 10 years").
  (2) Form 2 'N years old / of age' counts only when an age cue (a person noun, age / aged, 'must', 'minimum') sits at
  most four linking words before it (`_YEARS_OLD_CUE_RE`, on an 80-character window). So "The new visitor centre is 6
  years old" and "The festival, now 12 years old" are a thing's age.
  (3) The seed's own `_AGE_CLAIM_RE` matches pass the unit / count / per check ("Adults over 2 nights").
  `_PLURAL_AFTER_RE` no longer reads a verb ending in -s as a plural noun ("Children under 12 stays free" stays known).
- **R3R3-D4 — audience words** (finding 4): `_AUDIENCE_RE` gains people, persons, anyone, those, visitor(s),
  vegetarian(s), vegan(s), 'the unfit', 'the faint-hearted' ('guests' was already in `_PERSON_NOUNS`).
- **R3R3-D5 — split sentences are written sentences; superlative and 'only' facts are no verdict lines** (finding
  5). (a) `rewrite` adds each sentence of `Edit.new` (`sentence_spans(e.new)`) to `written`, so the two halves of a
  semicolon split are never cut by a later pass. (b) `_verdict` returns False when the complement holds a
  `_LIMIT_RE` word or a superlative (`_SUPERLATIVE_RE`: a closed list of -est forms, or most / least + a word).

#### Changed (vs the review's suggestions)

- Finding 1: the role frame 'not as X, but as Y' still folds after a comma. It is the panel's quoted tell ("not as
  cultural artefacts, but as living practice", one of the 11 pass catches). 'as' names a role, not a place or time,
  so the place / time reasoning does not apply to it. Without this exception the census loses that quote.
- Finding 4: the bare words (anyone, people, those, visitors) cover the suggested 'for anyone / people / those /
  visitors with / who' frame, so there is no separate frame pattern.
- Finding 5 (b): most / least + a closed evaluative word (important, rewarding, striking, memorable, meaningful …)
  is still a verdict ("This is the most rewarding part of the day." is still cut). Those words judge the piece; they
  do not rank a thing on a scale a reader plans by. 'best' is in the fact list (precision first: "This is the best
  month to visit.").
- Finding 6 (2): the review allows 'N years' after a bare preposition cue when a person noun sits before the cue.
  That is not implemented as a free look-back: "The Dorji family has run this workshop for over 12 years" has a
  person noun before 'over' in the same clause. A person noun right before the cue, with only linking words between
  ("children over 10 years"), is already form 1 with the person noun as the cue, so it stays known.

#### Tradeoffs

- Census (local scripts, $0; before = 67bb5550, after = this fix):
  - AI-tells quotes on the 20 round-2 texts: 63 matchable, 37 caught (11 pass, 26 residue) before and after.
  - Source-fidelity quotes: 51 matchable, 9 caught (2 pass, 5 residue, 2 seed-aware) before and after.
  - Seed-aware findings: the same 4 on the round-2 posts and 1 on the round-1 texts. Residue is unchanged.
  - Edit log: the round-2 `text` set is byte-identical. The round-2 `before_text` set loses two edits: ", not a craft
    market and not a demonstration staged for visitors" ('visitors' is now an audience word) and ": not with an
    itinerary handed over at check-in, but with a conversation" (a colon + comma same-preposition frame). The pilot
    set is unchanged.
  - AA-729 golden fixture + S234 pack (70 texts): 169 → 168 edits. The one dropped edit is the verdict line "This is a
    useful lens before the day moves outward." ('before' is a `_LIMIT_RE` word). Residue stays at 42.
- `_AUDIENCE_RE` with bare 'those' / 'people' keeps a few denials that are not about an audience ("not those of
  the coast"). This is precision first: the denial stays and goes to the prompt / residue.

#### Should know

- Tests: `tests/unit/test_aa790_writer_round3.py` has 408 tests (was 279). The linear-time list gains
  `_SUPERLATIVE_RE`, `_PER_AFTER_RE`, `_EXPLICIT_AGE_CUE_RE`, `_YEARS_AFTER_RE`, `_YEARS_OLD_TAIL_RE`,
  `_YEARS_OLD_CUE_RE`, `_OF_LINK_RE` and `_AGE_WORD_RE`. It also gains five adversarial inputs ("the most" runs, "7
  years" runs, "children must be" chains, "7 per" runs, "families of" runs), and `_superlative_fact`, `_verdict` and
  `_source_ages` in the function timing test. `_AUDIENCE_RE`, `_PLURAL_AFTER_RE` and `_SOURCE_AGE_RE` changed and
  were already in the list.
- The unit files that import ai_tells / hygiene / prompts / quality_gates / service (97 files) give 3916 passed and
  1 skipped. On 67bb5550 the same files give 3787 passed and 1 skipped.
- No prompt change: `writer_prompt_version` stays `24363541` and `AI_TELLS_VERSION` stays `ai_tells.v3`.

## Round 3: fact-preservation invariant (S252)

Input: the second and third reviews of round 3 (8 open blocking findings on 7b998197; several also on origin/main,
i.e. live round 2). Three review rounds each found a new chain of rules that deleted a fact, so this round stops
patching rule by rule. The rules stay as proposers. One invariant decides: an edit that removes a sentence or a
clause is refused unless every fact token of the removed words is still there after it.

### Decisions

- **FI-D1 — the invariant is the arbiter in `ai_tells._accept`.** Every proposed edit (word rules, pairs, cuts,
  anaphora joins, heading and CTA-clause edits) carries what it removed (`Edit.olds`, now set on cuts too). An edit is
  kept only when `hygiene.missing_facts(olds, new, …)` is empty. A refused edit is logged as
  `ai_tells: skipped <kind> (drops a fact: <tokens>): '<sentence>'`. The all-at-once path checks every edit against one
  `word_forms` of the result. The one-at-a-time path keeps running word-form counts and checks each edit in O(its own
  words). The check is linear in the text.
- **FI-D2 — fact tokens** (`hygiene.fact_tokens`, one pass, closed lists): numbers and number words (ordinals
  included), a unit right after a number ('900 m', 'two-hour'), time / day / month / season / weather words, limit and
  rule words (only, except, until, after, before, closed, open, passable, free …), person and audience nouns (gender,
  religious role, visitor type, group, solo, pregnant …), place / amenity / access / vehicle / use / position nouns
  (road, bridge, gate, entrance, ferry, pier, trail, footpath, toilet, water, drinking, swimming, alcohol, shop, food,
  online, room, heated, signal, halfway, end, top …), superlatives and the word after most / least, the practical
  words of `_PRACTICAL_RE` / `_PROTECTED_RE`, proper names (a capitalised word that does not open its sentence or
  line), settlement nouns (village, town, city) and any other content word of the seed + Facts. Evaluative words are
  never facts. Words are matched through their plural forms ('Leeches' = 'leech', 'ferries' = 'ferry').
- **FI-D3 — two kinds of "still there".** A closed-class token must survive in the edit's own new text, as often as
  in what it replaced (count). A name, a settlement or a seed word must still be somewhere in the text after the edit
  (presence). The count rule is stricter than "still appears in the output". The reason: with presence, "This is not
  the end of the climb. This is the halfway point." lost the denial because the test piece ends on "The day ends at
  the riverside camp." The same closed word elsewhere is usually another claim. Names and seed words are the topic,
  so a mention elsewhere keeps them. With this split, the panel's relocation tell ("does not begin in Thimphu. It
  begins in the east") is still joined.
- **FI-D4 — headings and the seed template.** A heading edit (angle label, brief words, CTA heading) is checked on
  seed and settlement words only. Title case is not a name, and the angle's "Two Worlds", "Family Anchor" are brief
  words. The seed's line labels and boilerplate ("Source itinerary:", "Why it matters:", "(do not place it in any
  other town, region or country)", the Place background header, "[Fact id=…]", "of the trip") and naming verbs
  (referred, called, known …) never make seed words. A word right before a particle is not a fact: 'warm-up',
  'check-in', 'opens up' ('up to 12' stays a limit).
- **FI-D5 — the same invariant on the hygiene repeat deletes** (`hygiene.dedupe`: copy / contained / restate /
  phrase deletes, orphan lines, closing recaps, echoed openings; `autofix`'s nearby repeat). A repeat's facts are by
  design in the sentence it repeats, so the check is presence for every class (`closed_by_count=False`). A repeat
  that adds a fact word the piece has nowhere else stays, and F13_repeat sees it. `dedupe` takes an optional
  `source` (autofix passes the seed + Facts). The other autofix deletes (borrowed-trip sentences, prompt leaks,
  social proof) remove unsupported claims on purpose and are not arbitrated.
- **FI-D6 — ages: a seed number is a known age only after an explicit age cue** (findings 5 and 7). `_source_ages`
  reads small regexes, each bounded: 'aged / age(s)' + at most three linking words + N ('Minimum age is 12', 'Minimum
  age: 7', 'under the age of 7', 'aged 10+'); a person noun + N–M ('Children 6–12'); a person noun + must be / aged /
  from / of / at least / over / under + N with an age tail ('+', 'and over / older / up', 'N years' after aged / from /
  of) or bare after 'must be' ('Riders must be at least 14'); a person noun + under / over / above N, never before
  'N years' without 'old'; 'under-5s'; 'N years and over' that closes its clause; 'N years old / of age' after a
  person / age / must / minimum / at least / under / over cue; 'N-year-old(s)' in a person use. Never a number next to
  ':' / '.' / ',' digits, or before a unit, a head count, 'per' or a plural noun. Group sizes and discounts ('Groups
  of 10 and over', 'Parties of 12 or over', 'Max participants: 12', 'Participants: 2-12'), altitudes ('above 4,000
  m'), durations ('15+ years of experience', 'over 12 years', 'for 12 years and over three generations'), ratings,
  grades and prices ('adults USD 45, children 8') no longer make an age known. On the output side the 'c' group
  ('children over N') skips a grouped number and a measurement ('Hikers over 30 km a day').

### How each open finding is handled

| Finding (third review) | Handled by | The fact token that blocks the deleting edit |
|---|---|---|
| 1 same-pronoun correction pairs ('This is not the dry season. This is the tail of the monsoon.') | invariant | dry, season (the denial cut), tail, monsoon, halfway, gate, heat (the verdict cut) |
| 2 lone denials with a safety / access / use fact | invariant | water, drinking, swimming, beach, road, campsite, public, footpath, guided |
| 3 `_inline_pair` access / location / amenity | invariant | road, village (no other mention), heated |
| 4 place / time / channel / goods / vehicle choices, no comma; ', not X' tails | invariant | lodge, Sundays, pier, April, English, dining, room, noon, online, food, cars |
| 5 group-size numbers as known ages | explicit-cue age reading | — |
| 6 verdict lines that restate a seed fact in another inflection | invariant | leech (seed 'Leeches'), mobile, signal |
| 7 altitudes, durations, head counts, ratings, prices as known ages | explicit-cue age reading + 'c'-group digit guard | — |
| 8 gender, religious-role and visitor-type access limits | invariant | women, men, monks, pilgrims, tourists, groups, solo, pregnant |

Every quoted sentence of both review files has an exact-output test (paragraph start and mid-paragraph, with and
without the review's seed) in `tests/unit/test_aa790_fact_invariant.py`. On 7b998197, 83 of the 92 placements of the
third review's pass sentences lose a fact; after this round none do. The second review's sentences were already
pinned in `test_aa790_writer_round3.py` and still pass.

### Changed (vs the brief)

- "Still appears in the output" is count-based for the closed classes (FI-D3), presence for names and seed words.
- Headings are checked on seed and settlement words only. A unit counts only after a number. Phrasal particles and
  the seed template are excluded (FI-D4). Without these, every angle-label heading edit on the pilot texts ("Where
  Two Worlds Meet …") and "That's not a warm-up." were refused, and 'itinerary', 'region', 'country' became seed
  words of every piece.
- The rule guards added in the earlier review fixes stay. They are now redundant with the invariant, but they keep
  a refused edit from being proposed at all, so the log stays quiet.
- The age reading is not "the same number anywhere in the seed". It needs the cue shapes above, which still cover
  every wording the earlier reviews needed ("Minimum age is 12", "under-5s", "at least 14", "Children 6–12", "from 8
  years", "children 7 and older", "10 years and over").

### Tradeoffs

- **Proposals the invariant now refuses on the stored corpora (all read by hand):**
  - "In Bhutan, heritage is not only preserved. It remains in active use." 'not only X' asserts X, so the join lost
    "heritage is preserved".
  - "Kuang Si Falls is more than a famous landmark. It is a threshold." 'more than' asserts the landmark, which is a
    seed word.
  - "To visit their settlements is not to encounter a preserved exhibit. It is …" ('encounter' is a seed word: noise
    from the seed-word class on verbs).
  - "… offers something that no guided commentary can substitute: …" ('guided'). The residue entry still flags it,
    so the census still counts it.
  - Round-1 text: "This kind of stay does not position the village as a backdrop." ('stay' is a seed word).
  - Edit counts: r2 texts 18 → 17, r2 `before_text` / pilot texts 59 → 56 (pilot set alone 53 → 50), round-1 texts
    10 → 9.
- **Existing test expectations that changed, each with a refused-case test that names the fact:**
  - round 1: "not only preserved" (only); "That is a good second day." (second); a Khaling internal label named once
    (national). The label test now names the centre again later, and then the label still goes.
  - round 2: "from the road" (road; the fixture now says 'ridge'); "present / position the village as a stage /
    backdrop" as the only mention (village; with a second mention the cut still goes); "the two cultures … not
    accidental" (two).
  - round 3: the staged-noun colon fold needs the piece to name the relic again, as the panel's text does.
  - S234 dedupe: the Sod Camp and caldera repeats add 'optional' and 'travellers', so they now stay for F13_repeat
    unless the piece has those words elsewhere. A closing recap that is the only mention of Uran Togoo / the Tsaatan
    stays. The near-verbatim autofix repeat that adds "visits to local nomadic families are available" stays.
- **Hold risk.** A repeat that `final_pass` no longer deletes is final-pass residue (F13_repeat), which holds an
  approved piece. On the 60 stored texts (r2, r2 `before_text`, round 1) autofix + final_pass make 0 repeat deletes
  before and after, so no change was measured. Watch `F13_repeat` holds in the next re-panel.
- **The seed-word class reads verbs too** ('encounter', 'stay'). It errs toward keeping. A residue false positive
  costs one repair call; a false negative is a fidelity defect.

### Should know

- **$0 census (local scratch scripts, not committed; before = 7b998197, after = 8ecdd3b2):**
  - AI-tells quotes on the 20 r2 texts: 64 matchable, 38 caught (11 pass, 27 residue) before and after, with the
    same quote-by-quote result. (This script splits quotes a little differently from the earlier note's 63 / 37.)
  - Source-fidelity quotes: 54 matchable, 10 caught (2 pass, 6 residue, 2 seed-aware) before and after.
  - Seed-aware findings: 4 on the r2 texts (2 ages, 2 founding reasons), 0 on `before_text`, 1 on round 1: unchanged.
  - Known ages over the 60 judge-view sources (`panel-batch-*.json`): none before or after.
- **Generated property test:** 1,580 sentences: 10 frames × 41 fact phrases in 9 classes × paragraph start / mid ×
  seed / no seed, plus chained correction pairs. On 7b998197, 508 of them lose the planted fact word. Now 0 do, and
  every fact token of the input is still in the output.
- New `hygiene` API: `fact_tokens`, `word_forms`, `source_forms`, `missing_facts`, `SUPERLATIVE_WORDS`,
  `EVALUATIVE_WORDS`, `dedupe(…, source=None)`. Removed age helpers: `_SOURCE_AGE_RE`, `_cue_is_count`,
  `_PREPOSITION_CUE_RE`, `_EXPLICIT_AGE_CUE_RE`, `_YEARS_OLD_TAIL_RE`, `_OF_LINK_RE`, `_AGE_WORD_RE`.
  `_UNIT_AFTER_RE` gains cm, inches and pounds.
- Linear time: the round-3 regex list gains the six source-age readers, `_DURATION_TAIL_RE`, `_UNIT_AFTER_RE`,
  `_FACT_TOKEN_RE`, `_PARTICLE_AFTER_RE` and `_SEED_LABEL_RE`. `test_aa790_fact_invariant.py` times the invariant
  check on 8000-character adversarial inputs and checks that 8× the text costs < 16× the time. A pass with 140
  refused edits on 8000 characters takes about 50 ms; 7b998197 took about 500 ms on the same text, because a refused
  edit now skips the integrity trial. The pre-existing integrity fallback is still slow on texts that repeat one
  sentence many times: "The dzong is not a relic, but a working institution." × 150 takes about 27 s on both
  commits. This change did not cause it.
- Tests: `test_aa790_fact_invariant.py` 192 (new), `test_aa790_writer_round3.py` 414, `test_aa790_writer_round2.py`
  201, `test_aa790_writer_ai_tells.py` 100. The 98 unit files that import `acp_content_writing` give 4015 passed.
- No prompt change: `writer_prompt_version` stays `24363541` and `AI_TELLS_VERSION` stays `ai_tells.v3`.

### Round 3: fact-preservation invariant (S252) — fourth review fixes

Input: three blocking findings on acf6b85b. (1) The invariant only knew closed-list words, names and seed words, so
`_lone_denial`, `_inline_pair` and the pair joins still deleted ordinary practical words ('The spring water is not
potable; it is glacier melt.' → 'The spring water is glacier melt.'). The property test drew its facts from the same
lists, so it could not find this. (2) `final_pass` / `settle` called `dedupe` without the seed, and a repeat that adds a
non-fact word ('…, unroped and unmarked') was still deleted. (3) Clock-time ranges, a thing's age and eras still made
a seed number a known age.

#### Decisions

- **FI-D7 — a denied word is a fact by default** (`hygiene.fact_tokens`, kind `DENIED`). Every content word in the
  scope of not / no / never / nor / cannot / n't counts, up to the end of its clause (, ; : . ! ? ( ) — or 'but') and
  at most 8 words. The edit keeps it only when its own new text still has the word. So 'not a performance of
  tradition. It is tradition' still joins, because the new sentence keeps 'tradition'. 'not because …' and 'not just /
  merely / simply …' deny nothing. A name, a settlement or a seed word keeps its own reading (presence anywhere), as
  before. The one-pass tokeniser stays linear.
- **FI-D8 — the allowlist of words a denial may cut** (`_DENIABLE_WORDS`). It covers staged, dead or packaged things
  (museum, performance, relic, backdrop, replica, landscaped, memorial, monument …), the tourist gaze (postcard, photo,
  view, attraction, glimpse …), things that sell or announce themselves (itself, announce, advertise, sell, charm,
  age …) and the abstract frame of a reframe (incidental, accidental, gradual, fade, calendar, guidebook, substitute,
  version, dramatic …). This matches `_STAGED_NOUN_X_RE` in spirit. A word not on the list is kept.
- **FI-D9 — a repeat delete takes no word out of the piece** (`hygiene.added_words`). Every number and content word of
  the deleted sentence must still be in the text after the delete. Stop words, evaluative and generic frame words, and
  a short `_REPEAT_FREE_WORDS` list are left out: the experience words a recap or paraphrase adds ('rewards those who
  travel with patience', 'offers a quieter rhythm'). The comparison matches plural forms and a rough stem ('holding' =
  'holds'). This is applied to the near-duplicate deletes, the far / closing repeats and autofix's nearby repeat. Orphan
  lines and echoed openings keep the fact-token check only.
- **FI-D10 — `final_pass` and `settle` take `source`.** The default is `hyg.source_text` (seed + Facts), and
  `_fix_all` forwards it to `dedupe`. The service callers pass `hyg`, so they read the seed with no call-site change.
- **FI-D11 — the repeat checks are O(the removed words) per candidate.** `dedupe` and `autofix` build one form and stem
  count of the text per pass. Each candidate is checked against the counts minus its own span (`_TextWithout`).
  `tidy_after_cut` now runs only on an accepted delete. Before this, each candidate cost O(text). With the stricter
  rule, most candidates are refused, and an intermediate version took 1 h 42 m on the unit suite. Now the suite runs in
  46 s.
- **FI-D12 — ages.** (a) A person range after 'from' needs an age tail ('years') or a clause end. No range counts
  before a time or visiting tail (am / pm / daily / every day / in the morning / until / can enter …), after
  'group(s) / party / team / size / max (of)', or when it descends ('9 to 5'). (b) 'aged N', 'aged N years (and
  over / old)' count only after a person noun or an age-limit word in the same clause, read at most 80 characters back
  (`_AGE_LOOKBACK_CUE_RE`). 'Middle / Stone / Bronze / Iron … Age(s) N' is an era. A plural noun after 'Age(s) N' is a
  count. The output side skips a thing's 'aged N' too, so a seed's 'single malt aged 12 years' copied into the text is
  not flagged now that the seed no longer makes 12 known.

#### How each finding is handled

| Finding | Handled by | Result |
|---|---|---|
| 1 open-class practical words cut by lone denial / inline pair / joins | FI-D7, FI-D8 | The 12 quoted sentences are unchanged with no seed and with the unrelated seed, at paragraph start and mid-paragraph (48 placements: 48 changed on acf6b85b, 0 now). Each refusal logs `drops a fact: potable` etc. |
| 1 property test drawn from the closed lists | 12 open-class phrases added (potable, paved, waymarked, insured, lit, air-conditioned, smoking, plowed, covered, insulated, unmarked, unroped) | On acf6b85b, 9 of the 10 frames lose a planted word. Now none do. |
| 2 `final_pass` / `settle` without the seed | FI-D10 | A spy test checks that the seed reaches `dedupe`. |
| 2 leech / lifejackets / unroped-unmarked repeats deleted | FI-D9 (+ FI-D7 for 'no lifejackets') | autofix → final_pass → settle leave all four unchanged, with and without the seed. F13_repeat still reports them, so the repair rewrites them. |
| 3 clock-time person ranges | FI-D12 (a) | 'Visitors from 10 to 12 can enter', 'Students from 7 to 9 in the morning', 'Kids from 9 to 5 daily' → no known age |
| 3 thing ages, eras, 'Group of travellers 2–12' | FI-D12 (b), `_GROUP_BEFORE_RE` | Every repro → no known age. The positive cues ('Minimum age is 12', 'Children 6–12', 'under-5s', 'Riders must be at least 14', 'children from 8 years', 'Ages 6–12') still count. |

#### Changed (vs the brief)

- The repeat rule is "every content word stays in the piece", not "adds no word missing from the sentence it repeats".
  A word the piece tells elsewhere is not lost. The strict form also refused the panel's r3 #12 / #17 / #19 recap and
  paraphrase deletes, which FI-D5 kept deliberately.
- The denied-word check compares against the edit's own new text (presence), not by count. The joined affirmation
  often repeats the denied noun ('It is tradition').

#### Tradeoffs

- **Proposals now refused (each has a refusal test):** 'The hall is not grey but very green.' (grey), 'The bridge is
  not the original. It was rebuilt.' (original), 'Tsaatan settlements are not permanent villages. They are seasonal
  camps …' (permanent; a real fact), 'The reindeer are not raised primarily for meat; …' without the seed (meat; the
  semicolon split goes with the refused edit), 'The valley's story does not begin in the capital. It begins …'
  (capital; a named place still joins when the piece names it again), 'The crater does not announce itself from the
  ridge …' (ridge), '… not from any trick of the light.' (light).
- **Test fixtures that changed:** the round-2 crater / bridge / grey fixtures now use a staged noun or no place phrase.
  The relocation hook names Thimphu. `missing_facts(['This is not the end of the climb.'])` now also reports 'climb'.
  In the S234 round-4 fixtures, the caldera repeat needs the piece to name the cone, the green steppe and the patches
  of forest, and the r3 #12 recap needs the piece to name the caldera, before they are deleted.
- **Hold risk.** More repeats stay for F13_repeat. On the 60 judge-view texts, autofix makes 2 repeat deletes and
  final_pass makes 0, before and after. 8 texts have final-pass residue, before and after.

#### Should know

- **$0 census (local scratch scripts, not committed; before = acf6b85b, after = dd5b649c), 60 judge-view texts
  (`panel-batch-*.json`, seed from `source_facts`):**
  - `ai_tells.rewrite` edits 78 → 77, refusals 6 → 7, texts changed 33 → 33. One output differs: ', not X' 6 → 5, the
    "trick of the light" tail. The 'permanent villages' join was already refused.
  - `final_pass` (seed passed) deletes 0 → 0. autofix repeat deletes 2 → 2. Known ages over the 60 sources: 0 → 0.
  - Panel ai_tells quotes (this script's split: 152 matchable): 34 by the pass, 39 by residue, 79 missed. Same before
    and after.
- Linear time: `_AGE_TAIL_RE`, `_TIME_TAIL_RE`, `_GROUP_BEFORE_RE`, `_AGE_LOOKBACK_CUE_RE`, `_ERA_BEFORE_RE`,
  `_AGED_BEFORE_RE` and the changed `_SRC_PERSON_RANGE_RE` are in the round-3 regex list. It gains adversarial inputs
  for range tails, 'aged N years' runs, era runs, 'every' and 'groups of', and each input stays under 50 ms on 8000
  characters. The invariant check also covers inputs for the denial scope ('not' runs, 'no X,' runs). `added_words` and
  `_repeat_delete_ok` each run under 50 ms on 8000 characters. dedupe with 100 refused near-duplicates (~8000
  characters) takes about 0.3 s.
- New `hygiene` API: `DENIED`, `added_words`; `text_integrity.final_pass(…, source=None)`,
  `settle(…, source=None)`.
- Tests: `test_aa790_fact_invariant.py` 308, `test_aa790_writer_round3.py` 420, `test_aa790_writer_round2.py` 203,
  `test_aa790_writer_ai_tells.py` 100, `test_aa768_s234_round4.py` 70. The 101 unit files that import
  `acp_content_writing` give 4170 passed.
- No prompt change: `writer_prompt_version` and `AI_TELLS_VERSION` stay the same.

## Round 3: ship criterion (S252)

Input: seven blocking findings on dd5b649c (fifth review). The ship criterion for this round: on every case the
reviews quote, and on the generated property test, the branch never loses a fact that origin/main keeps. A harness
outside the repo (`.tmp-session/s252/aa790/compare_main.py`) checks this. It runs `ai_tells.rewrite` +
`text_integrity.final_pass` from both trees, each in its own subprocess, and compares the fact tokens of each case
before and after.

### Decisions

- **SC-D1 — the staging exemption holds only in the copula frame** (`hygiene._deniable_frame`). A `_DENIABLE_WORDS`
  word is free only when its denial follows a copula or a 'do' auxiliary ('is not a museum', 'is no accident', 'does
  not present itself as a backdrop'), a relative 'that / which' ('that no guidebook can substitute'), or opens a
  role or agent phrase ('not as …', 'not by …'). A colon frame ('…: not a relic, but …') and a ', not X' tail on a
  copula complement ('a government enterprise, not a heritage attraction') also count. It never holds in these cases:
  - a ', not X' tail after a lexical verb, a modal or a prepositional complement ('You can sketch the murals, not
    photograph them', 'sells raw wool, not souvenirs', 'is for prayer, not photos');
  - a denial that opens on a place, time or purpose preposition ('not in the garden'; 'not to the lake', while 'not
    to encounter' stays an infinitive);
  - a sentence that opens on its denial ('No photos inside the temple.');
  - an inline pair ('The water is not treated; it is drawn …').

  Each look-around reads at most 200 characters, so the read stays linear. The word list itself is unchanged.
- **SC-D2 — 'not only / just / simply / merely X' is never cut alone.** `_pair` returns KEEP_PAIR for any such
  denial (the pronoun-subject cut lost the glacier, and the next 'It' then pointed at the previous sentence). The
  noun-subject join is kept whole too. `_lone_denial` and `_inline_pair` skip the frame.
- **SC-D3 — a 'This / That is …' sentence names facts by presence** (`hygiene._verdict_nouns`). The nouns of its
  complement's first noun phrase (at most 3 words; it stops at a stop word, 'worth' or an -ly word), and the N of
  'This kind of N', are `fact_tokens` of kind False. A verdict cut therefore stands only while the piece names them
  elsewhere. Piece-meta words, generic and evaluative words, -ing words and judging adjectives (good, great, proper …)
  never count, so 'This is the section worth slowing down for.', 'That's the opening.' and 'This is the most
  rewarding part of the day.' still go.
- **SC-D4 — an aphorism-pair cut keeps a subject named nowhere else** (`ai_tells._cut_both_subject`, inside
  `_edit_facts_lost`). The invariant adds the head noun of the denial's subject as a presence fact for a CUT_BOTH
  edit. So 'The old forge is not a relic. It works.' stays unless the piece mentions the forge again.
- **SC-D5 — quantity and frequency words are facts in a repeat** (`_REPEAT_QUANTITY_WORDS`). every, each, all,
  both, only, still, always, never, ever, own, even, alone, once, twice, daily … are out of `_REPEAT_FREE_WORDS`, and
  `added_words` counts them even when the stop list holds them. The one exception is a floating quantifier right after
  a copula or an auxiliary ('… are all available as optional activities'), which adds no scope.
- **SC-D6 — an output 'aged N' is a person's age by default** (`hygiene._thing_aged_out`). It is a thing's age only
  right after a closed list of aged goods or objects (malt, whisky, wine, tea, pu'er, cheese, ham, cask, barrel,
  temple, tree …), and only if no person noun or age word appears anywhere in the sentence. `_AGE_LOOKBACK_CUE_RE`
  gains the grand- forms (no word boundary before 'kids' in 'grandkids') and activity nouns (trekkers, beginners,
  novices, cyclists, rafters, pilgrims, members, all …). The source side reads them as cues too.
- **SC-D7 — head counts and space-grouped altitudes are never known ages.** A person noun + N with an 'and over / up'
  or '+' tail, or + N–M, counts only for an age-only noun (children, kids, teens, infants, toddlers, babies, juniors,
  minors, students, pupils, boys, girls). Any other noun counts only with 'aged' / 'between', a 'years' tail, 'must
  be', or an age word earlier in the clause. 'Families of 4 and over', 'Travellers 6 and up', 'Participants 4-8',
  'Couples 2 to 4' and 'Explorers 4 to 6' are group sizes. A number followed by a space-grouped thousand ('3 500 m',
  '4 000 m', with a thin or no-break space too) is never read.

### How each finding is handled

| Finding | Handled by | Result |
|---|---|---|
| 1 `_DENIABLE_WORDS` in tails and inline pairs | SC-D1 | (a) with the Tango seed, (b) with the camp seed, and the six no-seed sentences come back unchanged at both positions |
| 2 'not just X' cut alone | SC-D2 | glacier / monastery / pond (+ 'not merely') unchanged at both positions, with and without a seed |
| 3 verdict cut of an identification | SC-D3 | sky burial site, cremation ground, herders' pasture and 'where the porters change shifts' kept; one is cut when the piece names the pasture elsewhere |
| 4 CUT_BOTH removes the subject | SC-D4 | forge and mill pairs kept; the forge pair goes when the forge is named elsewhere |
| 5 frequency words free in a repeat | SC-D5 | ferry text unchanged through autofix, `final_pass` and `settle`, with and without a seed |
| 6 output 'aged N' passed by default | SC-D6 | all 13 quoted sentences flagged with an empty source and with a Punakha seed; single malt, pu'er and barrels are not flagged |
| 7 group sizes / spaced altitudes known | SC-D7 | the 9 seeds give no known age, and each paired claim is flagged; 'Children 6–12', 'children 7 and older', 'Travellers aged 6 to 12' and 'Minimum age: travellers 10 and over' still count |

### Changed (vs the review's suggestions)

- Finding 1: we took the frame fix only. The practical words stay on `_DENIABLE_WORDS`, and there is no stem match
  with the seed ('photograph' ~ 'photography'). Outside the copula frame those words are facts anyway, and inside it
  they are the staging nouns the list is for.
- Finding 3: the fix is in the invariant (presence of the noun phrase), not in `_verdict`, so every rule that deletes
  a 'This is …' sentence is covered.
- Finding 4: the pair is kept, not joined ('The old forge works.').

### Tradeoffs

- **Panel tells now kept (each has a test):** the pilot inline pair 'This is not a performance; it is an offer that
  depends on the evening.' (d99c621c) is refused ('drops a fact: performance'). In the $0 census (below), '…
  something real to encounter, not a softened version of the destination.' also stays, because it is a tail after a
  lexical verb.
- **Test fixtures that changed:** the round-2 aphorism-cut case now names the temple elsewhere ('Visitors walk the
  temple courtyard.'). 'heritage is not only preserved. It remains in active use.' is no longer proposed, so it moved
  from the refusal list to its own test. The inline 'performance' pilot moved from the rewritten list to the refusal
  list.
- A false "unknown" age costs one F15 repair. This round prefers that to a false "known" (SC-D6, SC-D7).

### Should know

- **Comparison harness** (`.tmp-session/s252/aa790/compare_main.py`; origin/main 303ae6e9 in the
  `.tmp-session/wt/cis-main-ref` worktree). It runs 3056 cases: the generated property test, plus the 150 sentences
  quoted in r3-review{2,3,5}-blocking.json. Each quoted sentence runs with no seed, an unrelated seed, a seed that
  states it, and the finding's own seeds, at a paragraph start and mid-paragraph.
  - Fact losses: main 1012, branch 6. Regressions vs main (main keeps a fact, the branch loses it): **0**.
  - The 6 branch losses are the reviewer's elided seed quote 'Day 8: … Drive 3-4 hours … max 12 guests.' used as
    prose. `fix_fragments` deletes 'max 12 guests.' as a broken sentence, on main as well.
  - Before these fixes (dd5b649c) the branch lost facts in 40 cases, also with 0 regressions.
- **$0 census, 60 judge-view texts** (local scratch, dd5b649c → this commit, seed from `source_facts`):
  - `ai_tells.rewrite` edits 69 → 67 and refusals 9 → 11. The two outputs that differ are the tradeoffs above.
  - `final_pass` deletes 0 → 0. Age flags 3 → 3.
- Linear time: new adversarial inputs (', not X' tail runs, 'This is …' runs, 'Grandkids aged' runs, aged-thing runs,
  'Families of N and over' runs) stay inside the existing budgets.
- Tests: `test_aa790_fact_invariant.py` 440, `test_aa790_writer_round3.py` 420, `test_aa790_writer_round2.py` 203,
  `test_aa790_writer_ai_tells.py` 100. The 98 unit files that import `acp_content_writing` give 4271 passed in about
  47 s.
- No prompt change: `writer_prompt_version` and `AI_TELLS_VERSION` stay the same.

### Comparative review: regressions vs main (S252)

Input: a comparative review found 15 sentences that origin/main keeps and the branch cut. None of them is in
r3-review*-blocking.json, so `compare_main.py` did not see them. All 15 come from two round-3 rules that main does
not have.

#### Decisions

- **CR-D1: the `_staging_tail` spectator branch cuts a tail only when the tail names nothing.** Before the spectator
  word, only a closed modifier is allowed (`_SPECTATOR_MODIFIERS`: quick, brief, passing, fleeting, distant …).
  Before this fix any free word was allowed, so 'not just a rim view' lost the rim. After the spectator word, only
  'of it' or 'of them' is allowed. Before, any 'of the / a X' was allowed, so 'glimpses of the leopards', 'a view
  of the volcano' and 'a backdrop of the hearth' lost the thing seen. This limit on 'of …' also covers the staging
  branch.
- **CR-D2: the `_something_lead` 'than X' comparand comes from a closed list** (`_SOMETHING_COMPARANDS`: scenery,
  sightseeing, views, glimpses, photos, postcards, snapshots, pretty views or pictures). Before, the comparand could
  be any one or two words, and the cut took an amenity (air conditioning), an activity (trekking), a road (the main
  highway) or an effort level (more relaxed than the trek). The lead without 'than' ('something specific:',
  'something that no guidebook can substitute:') is unchanged.
- **CR-D3: `_PRACTICAL_RE` now lists air conditioning (and its -ed / -er forms) and Wi-Fi.** Whether a room has or
  lacks these is a fact a family plans by. The pattern only guards: a denied or judged part that names one is kept,
  and nothing new is cut.

#### Changed (vs the review)

- Nothing beyond the 15 findings. The rules still cut a tail or comparand that names nothing: 'not just a view of
  them', 'not just a quick glimpse of them', 'something rarer than scenery / sightseeing:'.

#### Tradeoffs

- A 'not just a view of the X' tail where X is also named in the main clause stays. The branch no longer checks the
  seed or the rest of the piece for X. Main keeps these sentences too, so this is not a regression.

#### Should know

- Tests: `test_a_tail_or_comparand_that_names_a_thing_stays` runs the 15 sentences x seed '' / unrelated seed x
  paragraph start / mid paragraph (60 cases). `test_a_tail_or_comparand_that_names_nothing_is_still_cut` covers what
  is still cut. `test_aa790_writer_round3.py` has 482 tests. `fact_invariant` 440, `round2` 203 and `ai_tells` 100
  are unchanged. The 98 unit files that import `acp_content_writing` give 4364 passed in about 52 s.
- `compare_main.py` (3056 cases): main losses 1012, branch losses 6, regressions vs main 0. These numbers did not
  change, because the harness does not contain the 15 sentences.
- A separate run of the 15 sentences (both seeds, both positions, rewrite + `final_pass`) gives 0 changed on main and
  0 changed on the branch.
- The $0 census was not rerun. The regexes changed only toward cutting less, and linear-time budgets are unchanged.

## Round 4 (S252, 26/10/2026)

Input: a blind 4-arm panel (pilot / round 1 / round 2 / round 3, 20 pieces each, same four lenses) on round 3 as
live (`writer_prompt_version` `24363541`). AI-tells scored 5.22 against a bar of 5.5. Terra Family brand voice
scored 6.85. The judges' reasons quote the writer's own shapes: staccato denials ('X is not A. It is B',
'not as … but as …', '… rather than a souvenir'), verdict and aphorism lines ('earns its place', 'that fact
matters', 'is the point', 'asks something of a traveller before it gives anything back', 'travels home with you'),
template closers, lists of three used for rhythm, and label lines before the CTA. On Terra, children appear once
or only in general terms. The round-3 pass already preserves facts, so this round is **prompt-side only**. The
post-pass gets no new deletion.

### Decisions

- **R4-D1: the AI-TELLS brief now gives concrete DON'T / WRITE INSTEAD pairs** (`ai_tells.PROMPT_RULES`).
  - The existing bullets keep their wording, and every pinned phrase is still there. The bullets now also name
    these frames:
    - 'not as A but as B' and 'A rather than B'
    - four more verdict lines
    - template closers
    - lists of three
    - label lines before the CTA
  - A denial stays only when the denial is itself a practical fact from the seed ('Lunch is not included.'). This
    matches what the pass protects (`_PROTECTED_RE` / `_PRACTICAL_RE`).
  - There are ten pairs. Each negative example paraphrases a panel quote, keeping a short phrase at most. Places are
    swapped for generic ones (fortress, steppe, valley, weaving), and no example is longer than 16 words.
  - The positive half never carries a fact of its own. It is one of three things:
    - a `<…>` slot filled from the content seed ('The fortress <does what the content seed says it does>.');
    - 'nothing' (the line goes);
    - the call to action alone.
  - Reason: a concrete rewrite such as 'still houses the district's monks' would be a fact the writer could copy
    into an unrelated piece. Source fidelity outranks a vivid example.
- **R4-D2: a silent self-check is the last rule before the output line** (`ai_tells.SELF_CHECK_RULE`). It goes
  into `SYSTEM_PROMPT` after VISIT DETAIL and directly before "Return ONLY the final content itself", so it is the
  last thing the writer reads. It names the frames as "make sure no sentence has the form …" and tells the writer
  to rewrite such a sentence as the plain fact or delete it. It also says "never mention it in the piece", so the
  check does not leak into the output as meta-commentary.
- **R4-D3: there is a FAMILY BRAND line, and it is separate from `brand_voice_rule`**
  (`ai_tells.family_presence_rule`, `FAMILY_RULE`).
  - When it is added: when the pack's **voice** or **brand type** names families or children ('Family-aware',
    'Premium family adventure travel brand', 'parents', 'kids'). A negated word ('not a family brand') does not
    count.
  - When it is not added: the audience line alone never adds it, because a general brand can list 'couples and
    families'. It is not keyed on `core_idea` either.
  - What it says:
    - SOURCE FIDELITY comes first.
    - The family appears in every section, but only through seed or FACTS facts. Who an activity is for, or who
      does it, is said only when the seed or FACTS say so; otherwise the piece speaks to the family as a whole
      ('your family') and never assigns an activity to the children or the parents.
    - At most one family sentence per section, built on a seed fact a parent can plan from (timing, effort, what
      is optional, where the night is, a meal or rest stop).
    - An age, an age band or 'suitable for children' appears only when the seed or FACTS state one. The AUDIENCE
      line's ages are the brand's customers, never this trip (the same rule as round 3's AGES).
    - The writer never invents what children feel, learn or remember, never writes 'works for every age', and uses
      no school-trip framing (the panel flagged 'something a classroom cannot' and 'easy to explain to younger
      travellers').
    - When a section's seed facts give a family nothing to use, the section states those facts and adds no
      made-up family line.
    - Ownership is not audience: 'Family-run', 'family owned since 1990', 'family business' (and -owned,
      -operated, -led, -founded) never trigger it. 'Kid-friendly' and 'child-friendly' do.
  - Why it is separate from `brand_voice_rule`: the round-2 contract test pins `brand_voice_rule(FAMILY) == ""`,
    and the PACE and RESTRAINT lines must not move (round-3 test). The line is appended in `build_user_prompt`
    right after `brand_voice_rule`, on every channel.
- **R4-D4: the versions.**
  - `writer_prompt_version` moves from `24363541` to **`2b510e08`** (`0463a554` before the review fixes below).
    This is the `SYSTEM_PROMPT` hash, and the guard docstring carries a dated note.
  - `single` and `route` (the user-prompt hashes) do not change: the fixture pack is Trail Pulse, not a family
    brand.
  - `AI_TELLS_VERSION` stays `ai_tells.v3`, because `rewrite` and `census` behave the same.

### Review fixes (26/10/2026)

An independent review of the first round-4 commit raised four blocking points. All four are fixed in a second
commit.

- **Family attribution was not bounded by the seed.** 'With each fact the seed gives, say who in the family it is
  for' would push the writer to write 'the children ride …' for a steep or age-limited activity, an invented
  suitability claim. The rule now says who only when the seed or FACTS say it, speaks to 'your family' otherwise,
  and allows at most one family sentence per section.
- **The self-check listed bare "more than A".** Read literally, it would delete or reword a seed quantity ('more
  than 300 monks'). It now quotes the full set-up ("X is more than A. It is B." / "more than just A") and says a
  seed quantity stays word for word.
- **The family trigger fired on ownership.** `family_presence_rule({'voice': ['Family-run']})` returned the rule,
  so a family-run luxury or cultural brand would have been told to write for families, silently (the line is
  user-prompt only). Ownership compounds and a following 'owned' / 'run' / 'business' / 'operated' / 'led' /
  'founded' no longer count.
- **Two CTA pairs contradicted the system rule** "worked naturally into the piece, not pasted on as a bare final
  sentence". The WRITE halves now ban only the label line and keep the CTA worked into the last fact.

Each fix has a test (`test_family_rule_bounds_who_does_what_by_the_seed`,
`test_self_check_quotes_the_full_more_than_set_up_and_keeps_seed_quantities`, five ownership non-triggers, three
'-friendly' triggers, `test_cta_pairs_do_not_contradict_the_worked_in_cta_rule`). The $0 baseline below does not
change: it measures the round-3 texts.

### Second review fixes (26/10/2026)

An independent review of the two round-4 commits raised two more points. Both are fixed in a third commit.

- **The family trigger still had false positives.** `{'brand_type': 'Adults-only luxury brand, not suitable for
  children'}` and `'Run by the same family for three generations'` returned the FAMILY BRAND line. The negation
  window is now four words before the family / children word (not / never / no / nor / without / adults-only,
  and 'adults only'). It is read clause by clause, so it never reaches past a comma ('Calm, never preachy,
  family-focused' still triggers). Ownership now also covers 'by' up to three words before a family word ('run by
  the same family', 'owned by a local family'), 'same family', and 'family for <n> generations / decades / years'.
  'by' counts only before 'family', so 'Designed by parents, for parents' still triggers.
- **A copied prompt slot could ship.** The DON'T / WRITE INSTEAD pairs contain `<...>` slots. A writer that copies
  one leaves `<place>` in the piece. `hygiene.PLACEHOLDER_SLOT_PATTERN` is now the last `AI_TELL_LINE_RES` entry. It
  is residue: it rides the F15_voice stock-line repair (one sentence repair on attempt 1, then a warning). It is
  never a silent delete, and the deterministic pass does not touch it.
  - It matches slot-like content only: a letter, then letters, spaces, underscores, hyphens or apostrophes, up to
    60 characters in total. It also matches the bare `<...>` / `<…>`. The brief said 40 characters, but the
    prompt's own longest slot ('<the length of time the content seed gives>') is 41.
  - T9 blog bodies are markdown (the prompt asks for `## ` headings), not HTML. Even so, a bare HTML tag (`<p>`,
    `<em>`, `<strong>`, `<br>` …) is excluded by name. `<h2>`, `</p>`, `<br/>` and `<a href="…">` never match the
    character class.
  - It is bounded, so it runs in linear time. It is also covered by the round-2 / round-3 linear-time tests, which
    run every `AI_TELL_LINE_RES` entry after their start marker.

Neither change touches `SYSTEM_PROMPT` (the regex is code, and the family line is user-prompt only), so
`writer_prompt_version` stays `2b510e08`. `AI_TELLS_VERSION` stays v3 because the pass makes no new edit.

Tests in `tests/unit/test_aa790_writer_round4.py` (67 after this fix, 38 before):
- 6 new non-triggers and 2 new triggers for the family rule.
- Every prompt slot is caught.
- 6 slot sentences are residue and an F15_voice violation, and the pass keeps them.
- 7 HTML / plain-text negatives.
- 7 growth-ratio linear-time cases on 8000 characters of near-misses (4x the input must cost < 8x the time).

Limit: `template_lines` reads prose, bullets and FAQ answers only. A slot in a heading or a FAQ question is not
flagged. F15 also quotes only sentences of 4+ words (`_MIN_QUOTE_WORDS`), so a bare '<Place>.' line is not routed.

### Changed (vs the round-4 brief)

- The brief asked for 6–10 pairs. There are 10, because the panel quotes fall into ten distinct shapes. The test
  allows 6–10.
- The brief said to paraphrase panel quotes. A first draft kept the quotes nearly verbatim (dzong / Mongolia /
  'template still being built'). They were rewritten to generic places and wording, keeping short phrases only.

### Tradeoffs

- **The prompt is longer.** `SYSTEM_PROMPT` goes from 1471 to about 2060 words (+~800 input tokens per write),
  and a family-brand user prompt gains about 190 words. At T9 write volume the cost is negligible next to the
  output. The risk is that more rules dilute attention. The self-check at the end is the counterweight.
- **'A rather than B' is now discouraged everywhere.** Some uses are factual ('by boat rather than by road' when
  the seed says so). The prompt keeps the practical-fact exception, and the pass is unchanged. A legitimate one
  therefore survives if the writer writes it.
- **The family line can push toward a formula** ('For families, …' in every section). It asks for one plain
  sentence at most and allows none when the seed gives nothing. The panel's brand-voice lens judges whether it
  reads naturally.
- **The family line is user-prompt only, so `writer_prompt_version` does not cover it.** This is the same as PACE
  and RESTRAINT already. A future change to the family line alone would not move the version. The
  `writer_input_snapshot` brand pack and the code SHA still identify it.

### $0 baseline (the 20 round-3 texts, `.tmp-session/s252/aa790/aa790-repanel-r3.json`)

The scratch script runs `ai_tells.census` and `residue` with this branch's code (the pass did not change), plus a
regex count of the round-4 target shapes. The seed is not stored in that file, so the census context has the CTA
and brand words only.

- Writer-side census (stored, before the pass): 19 fixable edits. By kind: stock word 11, semicolon split 5, not X
  but Y 1, denial before its affirmation 1, verdict line 1.
- Residue: 6 lines. 666 sentences, mean 20.9 words, 41 over 35 words.
- Final text (after the pass): 0 fixable edits, residue 6, 40 sentences over 35 words.
- Round-4 target shapes in the final texts: **65** in total.
  - 'rather than': 15
  - 'is not … . It is': 10
  - 'matters' / 'is the point': 9
  - 'not as / only … but': 6
  - aphorisms (asks / gives / travels home / stays with): 6
  - 'the shape / kind of': 5
  - lists of three: 5
  - 'worth holding' and similar: 4
  - 'earns its': 3
  - 'the word "…"': 1
  - 'more than a': 1
  - CTA label lines: 0
- The deterministic pass sees almost none of these (it already preserves facts). That is why this round works on
  the writer. **This is a baseline only. The real check is the paid re-panel the orchestrator runs after deploy.**
  Rerun the same script on the round-4 texts and compare the shape total and residue with 65 / 6.

### Should know

- Tests:
  - New file `tests/unit/test_aa790_writer_round4.py` (38 tests after the review fixes): brief frames, 6–10 pairs, each positive is a
    slot / delete / CTA, each negative ≤ 16 words, self-check position, no em-dash / 'premium' / AA, the version,
    family-rule triggers (5) and non-triggers (9), its source-fidelity wording, every channel, order after the
    energy line and before the blog format, PACE / RESTRAINT unchanged.
  - After the review fixes, the 102 unit files that import `acp_content_writing` (round 4 included): 4402 passed
    in about 54 s, run on the final prompt text (`2b510e08`).
  - After the second review fixes, the same 102 files: 4433 passed in about 49 s (prompt version still
    `2b510e08`). The round-4, round-3, fact-invariant and S234 voice-contract files on their own: 1079 passed.
- `tests/unit/test_aa771_s234_voice_contract.py:140` has an E501 (123 characters). It is already on main, and this
  branch does not touch that line.

## Round 5 (S252, 26/10/2026): family voice

Input: a blind 5-arm panel (pilot / round 1 / round 2 / round 3 / round 4, 20 T9 pieces per arm, same judges) on
round 4 as live (`writer_prompt_version` `2b510e08`). Round 3 → round 4:

- AI-tells 5.05 → 5.50 (bar 5.5).
- Source fidelity 7.53 → 7.65. Reader value 7.22 → 7.15.
- **Brand voice 7.30 → 6.50.** Round 4 loses 14 of 20 pieces against round 3. Terra Family 7.0 → 5.5, LinkedIn
  6.92 → 5.08.

The judges' reasons on the round-4 Terra pieces: "children never appear concretely (only 'your family')",
"families appear only as an abstraction ('For families, this is a stop with a clear frame')", "dry and
institutional rather than warm", "no pace or comfort cues", "reads like a schedule entry". The round-3 Terra pieces
had the children in them, but the fidelity lens docked the invented visit formats ("Children can see a craft
discipline in progress", "Children can understand what they are looking at", "can follow them at their own pace").

Root cause: round 4's `FAMILY_RULE` told the writer to speak to "your family" as a whole, never give an activity to
the children or the parents, and write at most one family sentence per section. The writer obeyed, so the family
became a label. Round 5 is prompt-side only and keeps every round-4 AI-tell change.

### Decisions

- **R5-D1: the children and the parents are part of the group.** When the seed or FACTS say the group sees, does,
  tastes, walks, rides or sleeps somewhere, the writer may name the children or the parents as the ones who do it.
  That is the seed's own moment, not a new fact.
  - Bound: never a moment, view or activity the seed does not give the group. This is the r3 fidelity failure
    ("Children can see a craft in progress" when the seed never says the group watches anyone work), and it is the
    second DON'T pair.
  - Bound kept from the round-4 review: when the seed marks a moment optional, demanding or limited to some of the
    group, the writer keeps the seed's own words about who it is for and speaks to the family as a whole there.
    So 'the children ride …' on a steep or age-limited ride is still out.
- **R5-D2: warmth from the seed's own pace, effort and rest cues** (timing, walking or driving time, a meal or rest
  stop, what is optional, a free or easy afternoon, where the night is). Invented reassurance is named as forbidden
  ("no rush", "easy for little legs", "nothing to worry about", "at their own pace"). The last one is the r3
  fidelity quote.
- **R5-D3: no hard cap.** "At most one family sentence per section" is gone. Instead: "Bring the family into most
  sections, never as a formula", vary the words, no "For children, …" / "For families, …" opener section after
  section, never "tends to land differently", and "your family" must not be the only sign of a family. The rule
  says "The AI TELLS rules apply here too".
- **R5-D4: still forbidden.** Child-specific services, prices, facilities and activities the seed does not give
  (examples: a children's menu, a child rate, a family room, a kids' club). Ages and "suitable for children" only
  from the seed or FACTS. Never what a child will feel, notice, learn, understand or remember. No "works for every
  age" or school-trip framing. A section with nothing family-usable states its facts without a made-up family line.
- **R5-D5: three DON'T / WRITE INSTEAD pairs**, built from the panel quotes. Each WRITE half is a `<slot>` filled
  from the seed, never a fact of its own (same principle as R4-D1).
  - DON'T "For families, this is a stop with a clear frame." → WRITE "At <place>, the children <do what the
    content seed says the group does>."
  - DON'T "Children can watch a craft in progress." (the seed never says the group watches anyone work) → WRITE
    "You and the children <do what the content seed says happens there>."
  - DON'T "This is a structured, organised stop on day six." → WRITE "Day six <moves at the pace the content seed
    gives>, with <the meal or rest stop it names>."
  - The brief's own example ("…, something children can watch up close.") was not used as-is. "Watch up close" is
    a visit-format claim (proximity), which is exactly what VISIT DETAIL forbids and what the fidelity lens docked
    in round 3.
  - Every slot is a single line, at most 46 characters, and is caught by `hygiene.PLACEHOLDER_SLOT_PATTERN` if
    copied (residue → F15_voice repair, never a silent ship).
- **R5-D6: a short-post line** (`ai_tells.FAMILY_SHORT_POST_RULE`): "In this short post one concrete family moment
  from the CONTENT SEED is enough … Keep the post short." It is appended after `FAMILY_RULE` for a family brand on
  the plain-text channels (`FAMILY_SHORT_POST_CHANNELS` = `prompts.PLAIN_TEXT_CHANNELS`: LinkedIn, Facebook,
  Instagram, TikTok, ads). `family_presence_rule(pack, channel=None)` gets an optional channel. Blog, email,
  landing page and no channel get `FAMILY_RULE` alone, so the round-4 call shape `family_presence_rule(pack)` is
  unchanged.
- **R5-D7: the version bump goes through `SYSTEM_PROMPT`.** `writer_prompt_version` is the `SYSTEM_PROMPT` hash
  (AA-916 WM-08, pinned by `test_aa916_capture_edits`), and the family line is user-prompt only, so a family-line
  change alone would not move it. `VISIT_DETAIL_RULE` (in `SYSTEM_PROMPT`) gets one bridge sentence: "When the
  brief has a FAMILY BRAND line, naming the children or the parents as the ones who see or do what the content seed
  says the group sees or does is not a new fact; the moment itself still comes only from the content seed."
  - It has a real job: VISIT DETAIL forbids saying what a visit "lets the reader watch, see or do" unless the seed
    says so, and without the bridge the writer could read that as forbidding the round-5 family moments.
  - It is conditional on a FAMILY BRAND line, so for a non-family brand it says nothing new.
  - `writer_prompt_version` moves **`2b510e08` → `79cd046a`**. The S234 voice contract pins the new
    `SYSTEM_PROMPT` sha with a dated note. `single` and `route` do not change (Trail Pulse fixture).
  - `AI_TELLS_VERSION` stays `ai_tells.v3` (the pass is unchanged).

### Changed (vs the round-5 brief)

- The first pair's WRITE half drops "something children can watch up close" (see R5-D5).
- The brief said "LinkedIn: one concrete family moment". The line covers every plain-text post channel, because
  Facebook, Instagram, TikTok and ads posts are as short as LinkedIn and have no sections either.
- The brief did not ask for a `SYSTEM_PROMPT` change. It is the only way to bump `writer_prompt_version` without
  changing the AA-916 formula (R5-D7).

### Tradeoffs

- **Fidelity risk vs voice.** Letting the children do what the group does brings back the r3 shape that the
  fidelity lens docked when the seed did not give the moment. The rule and the second pair aim at exactly that
  failure. The paid re-panel must check that source fidelity stays near 7.65 while brand voice recovers.
- **More family text, more chances for a formula.** Dropping the cap may bring back 'For children, …' in every
  section. The rule names those openers and "tends to land differently". The AI-tells lens will show it.
- **The `SYSTEM_PROMPT` change reaches every brand's bytes** (so every new piece carries `79cd046a`), but its
  meaning is conditional on a FAMILY BRAND brief. Non-family user prompts are byte-identical to round 4 (tested).
- The family user prompt gains about 170 words over round 4 (on LinkedIn, plus about 30 for the short-post line).

### Should know

- Tests: new `tests/unit/test_aa790_writer_round5.py` (165 tests):
  - versions (`79cd046a`, AI_TELLS v3) and `PROMPT_RULES` + `SELF_CHECK_RULE` byte-identical to round 4;
  - the VISIT DETAIL bridge;
  - rule text: group membership, seed bound, optional / limited moments, pace cues, forbidden inventions, no cap,
    no formula openers;
  - 3 pairs: panel quotes, every WRITE half a slot, DON'T ≤ 16 words, every slot caught by the slot residue
    regex and routed to F15_voice;
  - trigger: 6 family packs; 13 non-family / ownership / negated packs × 9 channel values return '';
  - channels: the short-post line on the 5 plain-text channels (before the plain-text format block), not on blog,
    email or landing page;
  - 20 non-family user prompts (Trail, Atlas, WildKind, a family-run lodge × blog, LinkedIn, Facebook, email,
    landing page) pinned to their round-4 hashes from `origin/main` `d7f485cd`.
- `test_aa790_writer_round4.py`: the exact version pin becomes "not round 3 / round 4 pre-review"; the "your
  family" / one-sentence-cap pin becomes the round-5 seed bound.
- Runs: the 100 unit files that import `acp_content_writing`: **4567 passed** in about 53 s. flake8 clean on the
  touched files.
- **Re-panel after deploy** (orchestrator, paid): same 20 pieces, same judges. Watch Terra brand voice (round 3 7.0,
  round 4 5.5), Terra source fidelity, LinkedIn voice, and AI-tells holding ≥ 5.5.

## Round 6 (S252, 26/10/2026): brand texture

Input: a blind 6-arm panel (pilot / round 1 … round 5, same judges, all arms in one run;
`.tmp-session/s252/aa790/r5panel/panel-scores.json`) on round 5 as live (`writer_prompt_version` `79cd046a`).
Round 3 (before round 4) → round 5:

- AI-tells 4.88 → 5.58 (keep). Source fidelity 7.35 → 7.60 (keep). Reader value 7.00 → 7.22.
- **Brand voice 7.15 → 6.83** (5 wins / 4 ties / 11 losses). Terra Family is fine again (6.65 → 6.75), so
  `FAMILY_RULE` is not touched.
- Non-family brands lost voice: Trail Pulse 6.83 → 6.00, Atlas & Hearth 8.20 → 7.40, WildKind 7.50 → 7.00.

What the judges said, per brand (brand_voice and ai_tells lenses, r3 vs r5, read in full):

- **Trail Pulse.** r5: "long middle stretches read like an encyclopedia" ("Kuang Si Falls is a multi-tiered
  limestone waterfall system"), "the FAQ is formal brochure copy", "long geology passages kill the momentum".
  r3's voice wins came partly from lines the ai_tells lens docked: "Four days in." (staccato), "After a
  30-kilometre morning on a mountain road, that matters." (verdict). The r3 lines liked for voice and not docked:
  "This route starts there and keeps going", "you look up more than you look ahead", "active second-person verbs".
- **Atlas & Hearth.** r5: "mostly reference prose with little atmosphere", "very long fact-dense passages read
  more like a reference work", "flat fact-listing ('Paro River, or Paro Chhu, flows through the valley…')",
  closing praise "one of the most layered destinations in Central Asia". r3's 8.2 came mostly from aphorisms the
  ai_tells lens scored 3.8: "Mongolia asks something of a traveller before it gives anything back", "the land is
  still deciding what it wants to be", "The objects ask to be read, not just admired". The clean atmosphere the
  judges liked: "the light to settle over the two rivers", calm, slow, polished sentences.
- **WildKind.** r5: "reads like an encyclopedia more than observant, respectful nature writing; the brand voice
  shows up in one bolted-on line", "dramatic karst geology" off-voice. r3's voice lines were partly tells
  ("That word 'may' is worth holding to", "this is a village, not a programme"); the clean ones: "leave the terms
  of any exchange to the community itself", "with a light footprint". r5 tells: "The geology here drives
  everything visible above it.", "This stop gives the route its northernmost cultural depth.".

So round 4/5 removed the tell lines that carried the voice and nothing replaced them: the writer fell back to
reference prose. Round 6 tells the writer where the voice comes from for each brand type, without bringing a
verdict, denial or aphorism back.

### Decisions

- **R6-D1: one BRAND TEXTURE block, per brand type, read from the pack** (`ai_tells.brand_textures` /
  `brand_texture_rule`, user prompt, right after the PACE / RESTRAINT line and before the channel format block).
  Three textures, rendered in a fixed order when several fire:
  - MOMENTUM (energetic): short, concrete action sentences built on what the seed says the reader does (riding,
    walking, swimming, climbing, paddling), written to "you", verb early. A short sentence carries an action or a
    fact, never a comment ("Four days in.", "That's the pace this route sets.", "This is the day the route
    announces itself." are named as what not to write).
  - ATMOSPHERE (luxury / cultural): concrete sensory or material detail the seed or FACTS give (light, stone,
    wood, a river, silence, a meal, the time of day) in unhurried sentences within the AI TELLS length limits,
    with the reader present. It restates that SOURCE FIDELITY forbids invented scene detail, and names aphorism
    ("The architecture is the devotion made structural.", "the land is still deciding …"), praise ("one of the most
    layered destinations in …") and what-a-place-asks lines as the wrong way to get atmosphere. With no sensory
    detail in the seed, the calm comes from the sentence itself.
  - OBSERVATION (nature / respectful): name what the seed says is there (habitat, plants and animals, the village
    and its people) in plain, exact words and what the reader does among them. No drama words ("dramatic",
    "spectacular", "stunning"; the WildKind r5 "dramatic karst geology"), no verdict about what a place means or
    drives, never a sighting or encounter the seed does not give.
  - Common lines (once per block): every factual paragraph comes back to the traveller within two sentences (no
    reference-work stretch; with no seed moment, two sentences of facts and move on); texture never adds content
    and never needs a tell (SOURCE FIDELITY, VISIT DETAIL and AI TELLS apply; no verdict, aphorism, praise,
    denial set-up or list of three); `<...>` in the pairs is a seed fact.
  - Blog only: "The FAQ answers are written in this voice too, never as formal brochure copy" plus one FAQ
    sentence per texture ("you", short sentences, the fact first / the same calm, full sentences, no brochure
    phrasing such as "is renowned for", "boasts" / plain and exact, the same observant words). Other channels have
    no FAQ, so they do not get these lines.
- **R6-D2: detection reuses `brand_voice_rule`'s.** MOMENTUM is exactly the PACE line's test
  (`_energetic_entries`, now shared by both; `brand_voice_rule` output is byte-identical and tested). ATMOSPHERE and
  OBSERVATION read the pack's voice words and its `brand_type` with the same tokeniser (`_VOICE_WORD_RE`) and the
  same clause split as the family check (`_PACK_CLAUSE_RE`); inside a clause, words after a not / never / no / nor
  do not count ("Not a luxury brand", "Small group tours, never luxury", "City breaks, no wildlife" give nothing).
  - ATMOSPHERE words: voice elegant, atmospheric, refined, sophisticated, cultured, luxurious, polished; brand type
    luxury, luxurious, upscale, high-end, cultural, culture, heritage. The same grouping as
    `channel_spec.brand_register`'s EDITORIAL (luxury / cultural) and `voice_rubric`'s luxury check.
  - OBSERVATION words: voice nature-led, observant, respectful, ethical, responsible, sustainable; brand type
    nature, nature-led, wildlife, conservation, responsible, eco, eco-friendly, ecological, sustainable. Same idea
    as `voice_rubric._NATURE`.
  - Left out on purpose: "calm" alone (that is the RESTRAINT line, not a texture), "boutique" (not a luxury
    signal; the family-run lodge fixture stays byte-identical) and "premium" (Terra's brand type; a family brand
    gets nothing anyway).
  - No tenant name anywhere: renaming the Atlas pack "Trail Pulse" gives the Atlas texture (tested).
- **R6-D3: a family brand gets no texture.** `brand_textures` returns {} whenever `family_presence_rule` fires,
  even when the family pack also has an energetic or luxury word. FAMILY_RULE carries a family brand's voice and
  round 5 fixed Terra (6.75 > r3 6.65); the brief says do not touch it. Terra user prompts are byte-identical to
  round 5 (tested on 5 channels).
- **R6-D4: a pack with no texture signal keeps its round-5 user prompt byte for byte** (`brand_texture_rule`
  returns ''): tested for the family-run lodge (Calm), a plain "Clear, Friendly" pack and the empty pack, on 5
  channels each.
- **R6-D5: six DON'T / WRITE INSTEAD pairs, two per texture, from the panel reasons**, each WRITE half a
  `<slot>` sentence, each DON'T half slot-free:
  - MOMENTUM: DON'T "The falls are a multi-tiered limestone system known for their turquoise pools." (the Trail
    r5 encyclopedia opener) → WRITE "You ride <the distance or road the content seed gives> to <place>, then <do
    what the content seed says you do there>." DON'T "After a long morning on the road, that matters." (the r3
    voice beat the ai_tells lens docked) → WRITE "After <the morning's ride the content seed gives>, you <do what
    the content seed says comes next>."
  - ATMOSPHERE: DON'T "The valley is one of the most layered destinations in the region." (Atlas r5 praise) →
    WRITE "You reach <place> <at the time of day the content seed gives>, when <the light or stone the content
    seed names>." DON'T "The river, also known by its local name, flows through the valley and joins a larger
    river downstream." (Atlas r5 "flat fact-listing") → WRITE "You <stay or eat where the content seed says>,
    beside <the river the content seed names>."
  - OBSERVATION: DON'T "The geology here drives everything visible above it." → WRITE "On the way to <place>, you
    pass <the habitat the content seed names>." DON'T "This stop gives the route its northernmost cultural depth."
    → WRITE "In <village>, <the people the content seed names> <do what the content seed says>, and you <join the
    moment the content seed gives>." (both WildKind r5 verdicts)
  - None of them is an AI-tell template: each WRITE half, filled from a made-up seed, passes `rewrite` unchanged,
    has no residue and no template line, and has no 'not', 'rather than', 'more than', 'earns', 'matters',
    'worth', 'the kind of', 'asks' or three-item list (tested). The r3 lines the judges liked for voice but the
    ai_tells lens docked ("Four days in.", "that matters", "the land is still deciding …", "this is a village, not
    a programme") are only ever DON'T examples.
  - Every slot is one line, at most 62 characters, has no comma, and is caught by the placeholder residue regex
    if copied (F15_voice repair, never a silent ship). A first draft had "<the light, stone or water …>": the
    comma made the placeholder regex miss it, so the slots lost their commas.
- **R6-D6: the version bump goes through `SYSTEM_PROMPT`** (same reason as R5-D7: `writer_prompt_version` is the
  `SYSTEM_PROMPT` hash, the texture block is user-prompt only). The style-guide rule ("SOURCE FIDELITY and VISIT
  DETAIL override the brand's style guide …") gains one sentence: "A BRAND TEXTURE block, when the brief has one,
  belongs to the style guide: it sets sentence shape and where the reader stands, never content." It has a job:
  it puts the texture under SOURCE FIDELITY, so "atmosphere" or "momentum" never licenses invented scene detail.
  `writer_prompt_version` moves **`79cd046a` → `b5978839`**. `AI_TELLS_VERSION` stays `ai_tells.v3`.

### Changed (vs the round-6 brief)

- The brief said "1-2 DON'T/WRITE pairs per brand type"; each type has 2.
- The brief named energy / luxury / nature / culture signals "already used by brand_voice_rule". Only energy (and
  calm) were there. MOMENTUM reuses that test as is; luxury / culture and nature / respectful are new word sets
  read with `brand_voice_rule`'s tokeniser and negation rule, plus the pack's `brand_type` (the Atlas and WildKind
  brand types carry the clearest signal). Culture is grouped with luxury under ATMOSPHERE, as
  `channel_spec.brand_register` groups luxury / premium / cultural as EDITORIAL.
- The "every factual paragraph returns to the traveller within two sentences" and FAQ lines go only to brands with
  a texture signal, not to every brand: family brands are covered by FAMILY_RULE and no-signal packs stay
  byte-identical (the brief's "if that is how you design it").
- `SYSTEM_PROMPT` change: not asked, needed for the version bump (R6-D6).

### Tradeoffs

- **Momentum and atmosphere push toward scene detail.** Both blocks restate that the detail comes only from the
  seed and the bridge puts the block under SOURCE FIDELITY, but a writer asked for atmosphere may still invent
  light or silence. The re-panel must check source fidelity holds near 7.60 for Atlas and Trail.
- **"You" in every WRITE half.** Second person is the momentum brand's voice; for Atlas (discreet, private) and
  WildKind it is only the way the reader is placed in the moment. If judges find "you" too chatty for Atlas, the
  ATMOSPHERE pairs can drop it without touching the rest.
- **Prompt length.** The block adds about 330-450 words to a Trail / Atlas / WildKind user prompt (blog more than
  LinkedIn because of the FAQ lines). The PACE line's "background one sentence at most" and the new "back to the
  traveller within two sentences" overlap for an energetic brand; the stricter PACE line wins in practice.
- **Detection by word lists.** Lesson AA-985 says word lists drift; here they read the brand pack's own
  adjectives (a handful of words per tenant), not the generated copy, which is the same trade `brand_voice_rule`
  and `voice_rubric` already make.

### Should know

- Code: `services/acp_content_writing/ai_tells.py` (`BRAND_TEXTURE_*` texts, `brand_textures`,
  `brand_texture_rule`, shared `_energetic_entries` / `_pack_voice`), `services/acp_content_writing/prompts.py`
  (one call after `brand_voice_rule`, the SYSTEM_PROMPT bridge sentence).
- Tests: new `tests/unit/test_aa790_writer_round6.py` (226 tests):
  - versions (`b5978839`, AI_TELLS v3); `SYSTEM_PROMPT` minus the bridge is the round-5 sha `79cd046a…`;
    `PROMPT_RULES` + `SELF_CHECK_RULE`, `FAMILY_RULE`, `FAMILY_SHORT_POST_RULE`, `VISIT_DETAIL_RULE`,
    `BLOG_HEADING_RULE`, `SOCIAL_AUDIENCE_RULE` pinned to their round-5 hashes; PACE / RESTRAINT unchanged;
  - detection: the three panel brands; MOMENTUM == the PACE test on 9 voices; 11 voice / brand-type signals; 11
    no-signal or negated packs × 9 channel values; 4 family packs (with energetic / luxury / nature words) × 9
    channel values give nothing; no tenant name in the detection code;
  - rule text per brand type (common, MOMENTUM, ATMOSPHERE, OBSERVATION, FAQ lines), exact composition per type
    for blog vs LinkedIn, several textures in a fixed order with one common part;
  - pairs: 1-2 per type, WRITE halves are slot sentences, DON'T halves are the panel shapes, every slot caught if
    copied and routed to F15_voice, the filled WRITE halves are not AI tells;
  - contracts: no em-dash, no braces, no genuinely / precisely / premium / luxury, no tenant or place name;
  - user prompt: block once, right after the energy line, before the channel format, FAQ lines on blog only, on
    8 channels × 3 brands; Terra, family-run lodge, plain and empty packs × 5 channels pinned to their round-5
    hashes (origin/main `33c09fc0`).
- `test_aa790_writer_round5.py`: the exact version pin becomes "not round 3 / round 4"; the 20 non-family round-4
  hashes still hold once the texture block is removed (and the family-run lodge has none).
- `test_aa771_s234_voice_contract.py`: `SYSTEM_PROMPT`, `single` and `route` shas updated with a dated note (the
  Trail Pulse fixture is energetic; without the block both are the round-5 bytes).
- Runs: the 101 unit files that import `acp_content_writing`: **4793 passed** in about 54 s. flake8 clean on the
  touched code (the one E501 in `test_aa771_s234_voice_contract.py` line 140 is already on main).
- **Re-panel after deploy** (orchestrator, paid): same pieces, same judges. Watch Trail / Atlas / WildKind brand
  voice (r3 6.83 / 8.2 / 7.5, r5 6.0 / 7.4 / 7.0), AI-tells holding ≥ 5.5 (r5 5.58), fidelity near 7.60, Terra
  unchanged.

## Round 7 (S254, 28/10/2026): brand focus, angle brief as a brief

Input: the 7-arm blind panel of S252 (pilot / round 1 … round 6, 560 scores, one run; private root
`.tmp-session/s252/aa790/r6panel/`), the stored re-panel texts (`aa790-repanel-r2..r6.json`), the pilot
`writer_input` rows (`s251/aa790writer/out/seeds.json`) and the four test brand packs. No paid call for the
diagnosis: the user prompts were rebuilt offline from the stored `writer_input`.

Brand voice in that run: round 3 7.17, round 6 6.75 (3 wins / 5 ties / 12 losses against round 3). Per branch, round
6 is below round 3 on every one: Atlas & Hearth blog 7.60 → 7.40 (n = 5), Terra blog 6.88 → 6.50 (n = 4), Terra
LinkedIn 6.83 → 6.42 (n = 6), Trail Pulse blog 7.17 → 6.67 (n = 3), WildKind blog 7.75 → 6.75 (n = 2).

### Diagnosis

- **D-a, noise is as large as the per-branch gaps.** Terra's round-5 and round-6 user prompts are byte-identical
  (R6-D3); only one conditional sentence of `SYSTEM_PROMPT` differs. Its brand voice still moved 7.25 → 6.50 (blog,
  n = 4) and 6.67 → 6.42 (LinkedIn, n = 6) between those two arms, and one piece moved 7.5 → 5.5. So a per-branch
  reading at n = 2–6 is not evidence on its own (lessons AA-346 / AA-748). Round 7 needs a control arm: the live
  round-6 prompt re-run in the same session, scored in the same panel run.
- **D-b, Terra LinkedIn carries an institutional closer in every arm, and it comes from the angle brief.** 20 of the
  42 Terra LinkedIn posts of rounds 1–6 (and 4 of 6 in round 6) have "… is a structured, accessible stop" (or
  "an organised, purposeful stop", "a structured, dependable stop", "a structured, stable institution"). The T8
  angle's `best_final_style` for that request ends on the school being state-run and so an "accessible,
  structured" stop (paraphrased: golden-set text stays out of this public repo, AA-919). The LinkedIn WARM_PRACTICAL
  structure asks for "one reassuring practical point", and the seed has no pace, comfort or logistics fact for
  that stop, so the writer copies the brief's wording. Round 4 already named "a grounded, accessible stop" as a
  DON'T in `PROMPT_RULES`; the brief still wins. The judges quote it as "institutional", "cool", "flat" (the
  "stiff / institutional" reasons: round 3 4, round 6 9 of 20).
- **D-c, the brand's own style-guide points are missing, and nothing restates them.** The brand-voice reasons name a
  style-guide point the piece leaves out in 6 of 20 round-6 pieces: Terra "nothing on pace, rest, comfort or
  safety" (its style guide asks for pace and comfort), Atlas & Hearth "says little about guiding, privacy or
  stays" (its guide lists guiding and privacy), Trail Pulse "little momentum or social feel" (its guide asks for a
  social, mobile feel), WildKind "little observant nature language" (its guide asks for specific habitats); the
  guides are paraphrased here, not quoted (AA-919 golden-text guard). "Encyclopedic /
  reference prose" is in 9 of 20 round-6 reasons (round 3: 8). The style guide sits in the brand block with a
  long hedge ("never state its claims about pace, rest … unless the content seed says so"), followed by
  1,500–2,000 words of DON'T rules. Rounds 4–6 told the writer what not to do; no line maps the brand's own asks
  to the seed facts that can meet them. Terra's seeds do have pace facts (day numbers, overnights, an optional
  boat ride, a lunch stop); round 3 used them (a line on moving through the landscape at the terrain's own
  pace), round 6 Terra blogs mention pace 0.5 times a piece against 2.25 in round 3.
- **D-d, family brands never got the "back to the traveller" line.** Round 6 gave it only to texture brands
  (R6-D3); two of four round-6 Terra blogs are "an encyclopedia entry" to the judges.
- **D-e, the ATMOSPHERE texture lengthened sentences.** Mean sentence length (voice_rubric `measure`): Atlas &
  Hearth 21.4 (round 5) → 24.4 words (round 6), WildKind 18.9 → 24.3. The judges quote the long ones (a heritage-density
  sentence of 15 words, "clunky lines"). The texture says "told in
  unhurried, well-built sentences"; "unhurried" is a word the S1 writer v2 already lists as abstract filler, and a
  round-5 WildKind tell paired "careful, unhurried" with the terrain.
- Not a cause: the deterministic pass (round 6 made 23 edits on 20 pieces, none on a voice line of Trail Pulse or
  WildKind), reader presence (the share of sentences that name the reader is 0.25 in round 6 against 0.21 in round
  3, blog), and the one "the seed describes" leak (a re-panel artefact: the live T9 loop's F13 `LEAK_RE` catches
  "the seed"; the re-panel script runs no gate).

What round 3 had that round 6 lacks is mostly the aphorisms and verdicts the AI-tells lens docks (a land-still-deciding
aphorism, a "not X but Y" village line). Bringing them back would trade AI-tells for
voice; round 7 instead gives the writer the brand's own points to cover from the seed, and removes the brief that
makes the Terra LinkedIn closer.

### Decisions

- **R7-D1: the ANGLE BRIEF line** (`ai_tells.ANGLE_BRIEF_RULE`), right after the three angle lines of every T9 user
  prompt (every channel, every brand): the angle lines are an internal brief, not copy; follow their focus and shape,
  never copy their phrasing (only the place names and facts they share with the content seed), and leave out any
  detail, reassurance or framing they ask for that the seed does not give. It targets D-b at its source: T8 keeps
  writing briefs (an "accessible, structured" closer, a line on what a visiting family watches) that SOURCE FIDELITY and VISIT DETAIL forbid, and the writer resolved that conflict for the brief. The
  line says "content seed" in lower case so the section label "CONTENT SEED (" stays the first upper-case
  "CONTENT SEED" of the prompt (test_aa779 indexes it). T8's own prompt is not touched (out of this lane; the brief is
  still useful as shape).
- **R7-D2: the BRAND FOCUS block** (`ai_tells.brand_focus_points` / `brand_focus_rule`), last among the brand blocks
  (after PACE / RESTRAINT, BRAND TEXTURE and FAMILY BRAND), before the channel format. Targets D-c.
  - The points are the style guide's own first rules, in its own words: every line (or sentence) up to the first
    header. A header is a label of at most five words before a colon with nothing after it ("Write like this:",
    "Avoid:"), or a known example / don't label with text after it ("Avoid: hype.", "Examples: …", "Don't: …",
    "Never: …"); a "Label: rule" line ("Tone: warm and calm.", "Explain the pace: day lengths, rest.") is a point
    (review fix R7-F3). Bullets and numbers are dropped; a line that quotes an example sentence (4+ words in quotes,
    or an unclosed quote) is skipped (the brand block keeps the examples and says they are never copy), a quoted
    word ('Use "you" for the traveller.') is not; sentences split at . ! ? before a word that does not start in lower
    case and never after e.g. / i.e. / vs. / St. / Mt. / approx. / incl.; at most 8 points, each 2+ words and at most
    200 characters. Generic: it reads the pack, no
    tenant name, no per-brand word list. On the four test brands it gives 5 / 4 / 5 / 6 points, exactly the "do"
    section of each guide.
  - How to meet them: only with what the seed or FACTS give (a day number, a walking or driving time, where the night
    is, a meal, what is optional, who leads, what is private); use every such fact; when the seed gives nothing for a
    point, leave it out, never a general line in its place ("a structured, accessible stop", "a comfortable pace
    throughout", "expert guiding at every step"); SOURCE FIDELITY, VISIT DETAIL and AI TELLS apply. So the block
    cannot license an invented pace or guide; it points the writer at seed facts it was leaving out (Terra blogs:
    pace mentions 2.25 a piece in round 3, 0.5 in round 6, from the same seeds).
  - A pack with no style guide (or no point before the first header) gets nothing: '' (tested on 8 channels).
- **R7-D3: `TRAVELLER_RETURN_LINE` for brands without a texture block.** The round-6 "every factual paragraph comes
  back to the traveller within two sentences" line becomes one constant (`BRAND_TEXTURE_COMMON` is rebuilt from it,
  byte-identical, tested by sha). `brand_focus_rule` appends it, behind its own lead-in ("One more rule for this
  brand (not from its style guide):"), only when `brand_textures(pack)` is empty, so a family brand (D-d) gets it
  and every prompt with a focus or texture block has it exactly once (tested: 5 packs × 8 channels). **Limit:** the
  line rides the focus block, so a brand with no texture block AND no style-guide point (empty style guide, or one
  that opens with a header) still gets no traveller line. That is on purpose for now: those packs' prompts stay
  byte-identical to round 6 (the round-5/6 pins and the aa919 snapshot hold for them), and every panel brand has
  points. If a no-guide family brand shows the encyclopedic pattern, emit the line on its own and move the pins.
- **R7-D4: ATMOSPHERE asks for ordinary sentence length** (D-e): "told in unhurried, well-built sentences (still
  within the AI TELLS length limits)" → "told in calm, plainly built sentences of ordinary length (one fact or one
  moment each; the AI TELLS length limits apply)". Every other word of the block is the same (tested on the
  whitespace-flattened text; the line breaks moved). MOMENTUM, OBSERVATION, the FAQ lines and FAMILY_RULE are
  byte-identical.
- **R7-D5: the stop verdict is residue**, one structural pattern in `hygiene.AI_TELL_LINE_RES`: a copula (also
  "it's" / "that's" / "there's") / "as" / "makes it" + "a ADJ, ADJ" + a trip-unit noun (stop, visit, institution,
  part of the day / trip / itinerary …), only when one ADJ is in the CLOSED verdict lexicon `_STOP_VERDICT_ADJ`
  (structured, accessible, purposeful, organised, dependable, reliable, stable, straightforward, grounded, settled,
  welcoming, calm, easy(-to-navigate), manageable, meaningful, rewarding, respected …) and the other is not a fact
  about the stop (`_STOP_FACT_ADJ`: optional, included, short, guided, private, overnight, a number …). Every word
  ends at `(?![\w'’-])`, so "free-flowing" is not "free" and "easier" is not "easy" (review fix R7-F1, R7-F2). It rides the existing F15_voice "stock line" route (blocking on attempt 1 only → one
  one-sentence repair → never a hold): no new gate, the registry and the gate count do not move. Census ($0) over
  350 stored texts (AA-729 golden 30, S234 label pack 40, re-panel rows r2–r6 100, both S251/S252 panel sets 180):
  28 new residue hits, all the Terra LinkedIn closer (14 distinct sentences), 0 on any other text, 0 residue hits
  lost. The pass does not cut it (the sentence carries facts: "government-run", the city), so `AI_TELLS_VERSION`
  stays `ai_tells.v3`.
- **R7-D6: the version bump goes through `SYSTEM_PROMPT`** (same reason as R5-D7 / R6-D6): the style-guide rule
  gains "A BRAND FOCUS block, when the brief has one, is the style guide's own points: it says which of them to cover
  where the content seed has a fact for it, never content of its own." It puts the block under SOURCE FIDELITY.
  `writer_prompt_version` moves **`b5978839` → `ccad14c4`**.
- **R7-D7: the paid check needs a control arm** (D-a): the orchestrator re-runs the live round-6 prompt on the same
  20 inputs BEFORE deploying round 7 (arm `r6b`), then round 7 after the deploy (arm `r7`); the blind panel scores
  all nine arms in one run. Round 7 ships only if its brand-voice gain over `r6b` is larger than |`r6b` − `r6`|
  (two runs of one prompt). `t9_writer_repanel.py` gains `--arm` (stored on every row) and, in `--dry-run`, the
  focus-point count and whether the ANGLE BRIEF line is in the prompt (`getattr`, so the same script runs on the
  round-6 image for the control arm).

### Changed (vs the round-7 brief)

- The brief asked to restore brand voice "in the writer prompts/rules or the post-repair step". The post-repair step
  is unchanged (the pass is not the cause, D-a list); the fix is prompt-side plus one residue shape.
- The "the seed describes" leak seen in one round-6 text needs no change: the live T9 loop's F13 `LEAK_RE` already
  catches "the seed"; the re-panel script runs no gate, so the panel saw it.
- No brand-specific rule: everything is read from the pack (style guide, voice, brand type).

### Tradeoffs

- **Coverage pressure vs fidelity.** "Cover each point" could push the writer to invent a pace, a guide or privacy.
  The block says, three ways, that a point the seed does not support is left out, and the SYSTEM_PROMPT bridge puts it
  under SOURCE FIDELITY. The paid panel must show source fidelity holding (bar: no drop beyond the same-prompt noise,
  floor 0.15, against the better round-6 run).
- **Prompt length.** About +250 words a prompt (blog 3,350–5,460 words, LinkedIn 2,364 on the pilot inputs; SYSTEM
  2,211). Cost on the pilot set: about +$0.02 for 20 writes.
- **Atlas & Hearth's own "Use slower, more polished sentences."** now sits in its focus list next to the ATMOSPHERE
  line asking for ordinary length. Both stand: it is the brand's rule, and "slower" is pace, not length; the AI TELLS
  limits (most sentences under 25 words, none over 40) decide.
- **The angle line reaches every brand and channel.** It is the cheapest place to fix a T8 brief that asks for what
  the seed lacks; fixing T8 itself would change slate content for every tenant and is a separate change.
- **Residue on the stop verdict costs one cheap repair** on attempt 1 when it still appears (t9_repair, Haiku,
  ~$0.003–0.009); the angle line should make it rare.

### Should know

- Code: `services/acp_content_writing/ai_tells.py` (`ANGLE_BRIEF_RULE`, `TRAVELLER_RETURN_LINE`, `BRAND_FOCUS_*`,
  `brand_focus_points`, `brand_focus_rule`, ATMOSPHERE wording), `prompts.py` (angle line, focus block, SYSTEM_PROMPT
  bridge), `hygiene.py` (`_STOP_FACT_ADJ` + one `AI_TELL_LINE_RES` pattern), `scripts/aa790/t9_writer_repanel.py`
  (`--arm`, dry-run focus / angle-brief output).
- Tests: new `tests/unit/test_aa790_writer_round7.py` (version `ccad14c4`; round-6 rules byte-identical by sha;
  ATMOSPHERE only its length words; the four panel guides parsed to their "do" sections; 12 parsing edge cases; caps;
  block text and shape; traveller line once in 40 prompts; placement on 8 channels × 5 packs; angle line placement
  and the `lines.insert` positions; text contracts; 12 stop verdicts are residue and reach F15 as a blocking stock
  line on attempt 1 only; 11 fact sentences are not; the pass leaves the verdict for the repair).
  `test_aa790_writer_round5.py` / `round6.py`: the byte-identity pins strip the round-7 additions (ANGLE BRIEF line,
  round-7 bridge) and still hold. `test_aa771_s234_voice_contract.py`: SYSTEM_PROMPT / `single` / `route` shas
  updated with a dated note (without the round-7 additions they are the round-6 bytes). `snapshots/
  aa919_release0_prompts.json` regenerated on purpose (`AA919_WRITE_PINS=1`): only the 40 T9 entries change, the 40
  A1 / T2 entries are untouched.
- Runs: the 196 unit files that import `acp_content_writing`, `acp_shared`, `writing_memory` or AA-790 code:
  **7615 passed, 1 skipped** in about 90 s. flake8 (max 120) clean on every touched file (the E501 at
  `test_aa771_s234_voice_contract.py:140` is on main).
- $0 harness, before → after on the same 170 pieces (golden 30 + label pack 40 + re-panel r2–r6 100):
  `integrity_census` identical (0 pieces with a hit), `voice_rubric_replay` identical (Trail false passes 3/3
  caught, other light pieces 0 fires). The round-7 change is prompt-side; these prove the shared detectors did not
  move.
- **Paid plan (orchestrator):** in the PR body / lane result. Private panel helpers for it:
  root `.tmp-session/S254/aa790r7-panel/` (`build_blind_r7.py`, `panel9_wf.js`, `aggregate_r7.py`; smoke-tested
  with fake inputs).

### Review fixes (S254, after the lane review)

- **R7-F1 (major): the stop verdict was a deny-list.** The first pattern fired on any two stacked adjectives not in
  `_STOP_FACT_ADJ`, so plain seed facts were stock lines ("The lodge is a small, family-run stop by the river.",
  "The market is a busy, open-air stop on day two.", "The museum is a large, modern institution in the capital.",
  "It is a shorter, easier stop than the fort.") and went to the one-sentence repair on attempt 1 on every channel.
  Now one adjective must come from the closed verdict lexicon (R7-D5). The old FACTS case "a small, family-run
  place" never tested the exemption ("place" is not a trip noun); 10 FACTS cases on the real nouns now do.
- **R7-F2: recall.** Contracted copulas ("It's / It’s / That's a … stop") and a hyphenated verdict word that starts
  with a fact word ("a calm, free-flowing visit") now fire; "a long-standing, respected institution" fires through
  "respected". 5 new STOP_VERDICTS cases.
- Census after the fix ($0, `stop_census.py` in the lane scratchpad): every string in the stored AA-790 JSON under
  root `.tmp-session/s250`, `s251`, `s252/aa790` (4,364 files, 42,707 distinct sentences: pilot / re-panel texts,
  seeds, judge reasons) gives the same 38 hits before and after, all the Terra closer family ("is a structured,
  accessible stop" 13, "makes it a calm, easy-to-navigate part of the day" 6, "organised, purposeful" 4,
  "structured, stable institution" 4, …), 0 lost, 0 new.
- **R7-F3: style-guide parsing.** A "Label: rule" line ended the focus list ("Tone: warm and calm.",
  "Explain the pace: day lengths, rest and comfort.", "Lead with the practical: pace, rest, comfort." all gave []);
  a quoted single word dropped the line; "e.g." split a point in two. Fixed as in R7-D2; the test pinning
  "Note: the brand is calm. …" → [] now expects both sentences as points. The four panel guides parse exactly as
  before (5 / 4 / 5 / 6 points), so the prompts and every pin are unchanged. 16 new parsing cases.
- **R7-F4: per-arm cost.** `t9_writer_repanel.py` passes `--arm` into the `shared.llm_call_log` quality signal
  (both `record_call_sync` and `record_unusable_sync`), so cost per arm is `quality_signal->>'arm'`, not run_ids
  copied from stdout.
- **R7-F5: the TRAVELLER_RETURN_LINE limit** is stated in R7-D3 (kept as is, see there).

### Round 7 outcome (S254): reverted

Blind 9-arm panel (720 scores, control arm r6b): brand_voice r7 6.60 vs r6b 6.75 / r6 6.67 (noise 0.08), reader_value r7 6.95 vs 7.15–7.22; AI-tells 5.75 and source fidelity 7.70 held. The ship rule failed, so the code was reverted (round 6 live again). The diagnosis above stays valid for the next round: the angle-brief copying and the missing style-guide focus are real, but the generic BRAND FOCUS block did not lift brand voice and cost reader value; next try the T8 angle generator itself (it writes briefs the seed cannot support). Report: root `docs/audits/2026-10-28-S254-aa790-r7-panel/`.

## Round 8 (S255, 28/10/2026): the T8 angle brief, not the T9 prompt

Input: the round-7 report (root `docs/audits/2026-10-28-S254-aa790-r7-panel/`: r7 failed brand_voice and
reader_value against the same-night control r6b, ai_tells and fidelity held; reverted in #1087), the stored
`writer_input.angle` of the 20 pilot pieces (`s251/aa790writer/out/seeds.json`, private root `.tmp-session/`) and the
S252 7-arm panel scores (`s252/aa790/r6panel/panel-scores.json`). No paid call for the diagnosis. The decision from
round 7 stands: change the T8 angle generator, leave the round-6 T9 prompt (`b5978839`) as it is.

### Diagnosis

- **D8-a, every round so far wrote from old briefs.** The 20 pilot pieces reuse 10 T8 requests whose angles were
  written before the AA-771 T8 contract (#690, 13/10): their briefs still set a tone ("Tone is reassuring"), prescribe
  the opening ("opens mid-moment") and name the exact invented scene the current T8 prompt cites as a DON'T. So
  rounds 1-7 measured the T9 writer on briefs the live T8 no longer writes, and nothing has measured T8 itself. An
  A/B of a T8 change must regenerate the angles in BOTH arms (control = the live round-6 brief, flag off), or it
  compares the change with briefs from an older prompt.
- **D8-b, the long briefs are fact outlines, and they score lower.** Brief length on the 10 requests: 63-124 words
  for four of them, 145-221 for six. The long ones list which facts go in which section, with dates and percentages
  (up to 7 figures), and ask for a stop-by-stop survey. Brand voice in the S252 panel, rounds 5 + 6: the four short
  briefs 7.25-7.75 (n = 8 scores), the six long ones 6.25-7.38 (n = 32, mean about 6.7). Inside one tenant the order
  holds (Terra blog: 63 words 7.75, 124 words 7.25, 154 words 6.25; Terra LinkedIn 145 words 6.54). Reference-prose
  reasons ("encyclopedic", "reference", "guidebook", "fact-listing", "institutional") in brand-voice / reader-value
  reasons: 18 of 64 for long briefs, 3 of 16 for short ones. Small n and tenant-confounded (the six Terra LinkedIn
  pieces share one long brief), but it is the same direction as the round-7 reasons.
- **D8-c, the briefs set a thesis to prove.** "the idea of convergence", "heritage practised rather than preserved",
  "a quiet revelation about what makes this trip different", "what the ruins represent": a brief built on an
  argument makes the writer state a verdict; rounds 4-6 cut the verdict lines as AI tells and nothing is left but the
  fact outline.
- **D8-d, the briefs ask for what the seed lacks.** The Terra LinkedIn brief asks for "one reassuring practical note"
  that makes the stop accessible and structured, and for what a visiting family observes; the seed has neither, so
  the writer copies the brief's wording (round-7 D-b: 20 of 42 Terra LinkedIn posts carry that closer). A Terra blog
  brief asks for distances and how the pace is set across the days, on a seed without distances.
- **D8-e, the briefs are not brand-specific.** Trail Pulse (energetic cycling) and WildKind (nature, observant) got
  the same angle on the same seed: the same landmark as the hook, the same "destination guide / pillar guide with an
  H2 per stop" outline, why_it_works on search volume ("dominates the search demand signal"). With the same outline,
  two brands can only differ in sentence-level voice, which is exactly what rounds 4-7 could not lift. No brief says
  which of the brand's own style-guide asks (Terra pace and comfort, Atlas guiding and privacy, Trail momentum,
  WildKind habitats) this piece carries, or with which seed fact; round 7 tried that as a generic block in T9 for all
  points at once and cost reader value.

### Decisions

- **R8-D1: a brief contract in the T8 system prompt, behind a mode switch.** `prompts.SYSTEM_PROMPT_R8` =
  `SYSTEM_PROMPT` with (a) the best_final_style bullet replaced by a round-8 bullet and (b) one brand-first paragraph
  before "Every angle must fit the BRAND". Every other byte is the same (test: reversing the two replacements gives
  `SYSTEM_PROMPT` back). The user prompt is the same in both modes. Mode: api/worker env `T8_ANGLE_BRIEF_MODE`, only
  the value `r8` turns it on; unset (default) or anything else is `off`, the live prompt byte for byte (sha
  `df5bd75e54608e21` pinned). The A/B harness passes the mode explicitly (`generate_angles(brief_mode=...)`), so both
  arms run on one deployed image with the env unset.
- **R8-D2: the round-8 brief.** At most 90 words, four labelled parts in order: `Reader:` (what the reader does, sees,
  eats or where they stay, in the seed's own terms: targets reader value and the reference-prose pattern, D8-b),
  `Brand:` (one or two of the brand's own style-guide or mindset points this angle carries, each tied to the seed fact
  that carries it, or "voice only": D8-e, scoped to the angle instead of round 7's every-point block), `Lead with:` (at
  most three seed details) and `Shape:` (the structure in one sentence; it may name sections, never assigns facts to
  each). Rules: not an outline of facts (at most three facts, dates or figures, no survey of every stop; D8-b), no
  thesis or verdict to reach (D8-c), no voice, no first sentence, no other audience (kept from round 6), and nothing the
  seed lacks, now naming reassurance, practical note, comfort, pace, safety, logistics and "what visitors see"
  explicitly, with "when the style guide asks for one and the seed has nothing, leave it out of the brief" (D8-d).
- **R8-D3: brand first, search second.** The new paragraph: build each angle on the seed moments THIS brand's reader
  comes for, two brands on one seed should get different angles, and why_it_works names why this brand's audience
  reads it (search demand may support it, never the whole reason). D8-e.
- **R8-D4: a $0 contract check on the brief, through the existing retry.** `generate.r8_brief_problems`: the four
  labels (any case), at most 120 words (the prompt asks 90; models overshoot a word target), at most 5 figures (day
  numbers count). In mode `r8` its problems join `angle_contract_violations`, so a breaking set gets the existing one
  retry with the problems as feedback (no new retry loop, no new gate, no registry or ledger entry). Mode `off` checks
  exactly what it did before (tested with a fact outline: no violation, one call).
- **R8-D5: one input builder for the live job and the A/B.** `service.set_goal_and_generate` is split, with no
  behaviour change, into `angle_inputs` (every $0 DB read T8 does: route guard, atom seed or Route digest, brand pack,
  channel spec, trip, search demand + real questions, piece history; returns `AngleInputs`), `measurable_rank` (the
  AA-512 ranking before Jev) and `sanitize_briefs` (the AA-771 sanitiser). The place-card build stays in
  `set_goal_and_generate` (it is an LLM step; it now runs before the AA-914 route guard instead of after it: the two
  are independent). Lesson "one value, two code paths → one function": the harness builds its angle call from the
  same object, so the A/B cannot drift from the live inputs.
- **R8-D6: `_generate_once` takes `stage`, `model_tier` and `quality_signal_extra`** (defaults: `t8_angle_gen`, None,
  none), for the harness's `adhoc_aa790` stage with the live `t8_angle_gen` model id, like the writer harness already
  does. The live call passes none of them: same LLMRequest, same log row (test pins the quality-signal keys). In mode
  `r8` the row's quality signal carries `brief_mode: r8`.
- **R8-D7: the harness regenerates angles** (`scripts/aa790/t9_writer_repanel.py --angles regen --t8-mode off|r8
  --arm <label>`): one T8 call (plus at most one contract retry) per distinct request of the selected pieces, ranked
  as live (`measurable_rank`; the Jev answer check `verify_answers` is skipped: it only moves the pick when Jev
  rejects a claimed PAA answer, and it would add Jev calls to an adhoc run), sanitised as live, the recommended angle
  used for every piece of that request, T9 written with the live round-6 prompt. No app-table write. `--arm` (removed
  with the round-7 revert) is back on every row and in every call's quality signal (T8 and T9). The three angles per
  request, the pick, the brief census and the T8 cost go to `<out>.angles.json`. The T8 spend counts against the same
  `--max-usd` cap (reserve $0.10 a request).

### Changed (vs the brief)

- The brief asked for "the change behind a flag/route": it is an env mode switch read per call, not an
  `llm_role_config` route, because the change is a prompt, not a model. The harness passes the mode, so the A/B needs
  no env change and no Terraform.
- The brief asked for "control r6 vs r8". The stored r6 texts were written from the pre-#690 briefs (D8-a), so the
  control that isolates the T8 change is a fresh-angle arm with the live round-6 brief (`r8c`, mode off). The stored r6
  texts stay in the panel as the link to rounds 1-7. The r6b / r7 texts of S254 are not on disk any more (the private
  `S254/aa790r7-panel/` folder is gone and nothing is left in the S3 scripts bucket), so they cannot be re-scored.
- `service.py` refactor: not asked; needed so the harness uses the live inputs (R8-D5).

### Tradeoffs

- **Cost vs a clean control.** Two fresh-angle arms (r8c + r8) cost about 2 × ($0.9 writes + ~10 T8 calls × ~$0.03,
  plus retries) ≈ $2.2-2.5, over the $2 cap. The run plan is sequential: r8 first (cap $1.40); only if r8 passes the
  bars against r6 does the orchestrator spend the r8c control (cap $1.30, a second approval), because a pass is what
  needs the attribution. A fail needs no control.
- **A label format in the brief.** The writer sees "Reader: … Brand: … Lead with: … Shape: …" as its brief. The labels
  could leak into copy; the T9 F13 leak checks and the panel will show it. The tenant UI shows the same brief in the
  angle picker, which reads well as four short parts.
- **The retry can double T8 cost** for a request whose briefs break the contract (about +$0.03); the limits are lenient
  (120 words, 5 figures) so the retry is the exception.
- **Brand: from the style guide again.** Round 7 failed with style-guide points in the T9 prompt. Here T8 picks one or
  two per angle and ties each to a seed fact, so it is specific, and T9's prompt is not touched; if the panel shows the
  round-7 reader-value drop again, the `Brand:` part is the first thing to drop.
- **Live pack vs stored pack.** The harness's T8 step reads the live brand pack (as the live job would), while its T9
  step uses the pack stored in `writer_input`. For the four demo tenants they are the same unless a pack was edited
  after S251.

### Should know

- Code: `services/acp_angle_gate/prompts.py` (`BRIEF_MODE_*`, `R8_BRIEF_*`, `_BRIEF_SPEC_OFF/_R8`, `_BRAND_FIRST_R8`,
  `SYSTEM_PROMPT_R8`, `brief_mode`, `system_prompt`), `generate.py` (`T8_STAGE`, `r8_brief_problems`, the mode in
  `_generate_once` / `angle_contract_violations` / `generate_angles`), `service.py` (`AngleInputs`, `angle_inputs`,
  `measurable_rank`, `sanitize_briefs`), `scripts/aa790/t9_writer_repanel.py` (`--arm`, `--angles`, `--t8-mode`,
  `regen_angles`, `request_keys`, `chosen_angle`, `brief_census`).
- Tests: new `tests/unit/test_aa790_t8_round8.py` (off = round-6 sha; the two replacements are the only difference;
  label order; round-6 rules kept; new rules present; no tenant / place name or em-dash in the additions; env switch
  values; explicit mode beats env; brief check cases incl. figure counting; violations by mode; LLMRequest and log row
  unchanged in off; r8 switches only the system prompt; harness stage / model / arm pass-through; one retry on an
  outline in r8, none in off; unusable reply logged under the given stage; `AngleInputs.generate_kwargs` single atom
  and Route; ranking and sanitiser helpers; harness helpers and the `--t8-mode` without `--angles regen` error).
- Ship path if round 8 passes: set `T8_ANGLE_BRIEF_MODE=r8` on the worker (t8_angles runs there) and the api (admin
  regenerate-angles), or a one-line PR that makes `r8` the default; existing requests keep their stored angles.
- Not in this PR: the paid A/B, the blind panel and the decision (orchestrator, plan in the lane result).
