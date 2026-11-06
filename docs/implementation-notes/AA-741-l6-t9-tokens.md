# AA-741 — S271 phase-1 finding: T9 blog output tokens (t9_write v2)

Lane l6 of S271 (with the Blog Slate fix, notes `AA-741-l6-blog-slate.md`). No paid call was made; every LLM call in the
tests is a fake.

## Finding
On the 4 audit tenants (06/11/2026) t9_write for a blog averaged 1,846 output tokens but reached 11,465, and 4 calls
hit the 12,000 ceiling (3 cut, 1 billed but unusable: 0.58 USD lost); t9_write cost 2.1 of the phase's 3.67 USD. The
published blogs are 620-950 words, 5-6 `## ` sections (3,700-5,300 characters, about 1,300-2,000 tokens with their
citation tags and the JSON envelope).

What the model is asked to return (services/acp_content_writing/prompts.py): ONE JSON envelope (`seo_title`,
`meta_description`, `slug`, `body`, `summary`), the body a markdown article with `## ` sections, a 3-4 pair FAQ and an
`[R:<atom id>]` tag after every seed-fact sentence (atom ids are 15 characters). No multiple drafts, no reasoning field
in the output, no schema block. So the visible reply cannot reach 11-12k tokens without breaking the F4 gate
(13,000 characters ~ 3,500 tokens).

Cause:
1. Thinking. Sonnet 5.5 (the t9_write route, Anthropic API, effort `high` by the adapter's family default unless the deployment row sets one) thinks
   adaptively by default and its thinking is billed as output tokens inside `max_tokens` (claude-api reference: Sonnet 5.5
   "omitting thinking runs adaptive"). The shared SYSTEM_PROMPT ends with a silent reread "sentence by sentence"
   against a long rule list (ai_tells.SELF_CHECK_RULE); on a long blog that invites the model to draft the whole
   article in its thinking, check every sentence, then copy it out again. The 1,846 average is the visible size; the
   tail is the hidden draft and check.
2. No length. The blog prompt had no word target at all (only LinkedIn has a channel spec); the channel row asks for
   "long-form SEO content, hub/pillar pages, in-depth destination guides" and F4 lets a body run to ~2,200 words.

Live check of the cause (read-only, orchestrator): per t9_write blog row `tokens_out` vs
`quality_signal.output_len_chars` (the visible reply): `tokens_out - output_len_chars / 3.6` is the hidden share.

## Decisions
- One block for every blog prompt (first write and rewrite, single moment and route walk), after the format
  requirements and before the JSON envelope (`_BLOG_LENGTH_INSTRUCTIONS`):
  - a word band of 900-1,300 words including the FAQ, never under 900 and never more than 1,600: inside the F4 band
    (4,700-13,000 characters), with the target low in it (lessons: models overshoot counts). The floor is F4's floor
    in words (900 x 5.3 >= 4,700). A thin seed reaches it with depth from its own CONTENT SEED, Place background and
    FACTS lines and the FAQ, never with invented facts or filler. (S271 review: the first cut said "a seed with few
    facts gives a shorter article", which licensed going under a blocking gate and contradicted an F4 rewrite's
    feedback "content is only N chars"; removed.)
  - at most 6 `## ` sections before the FAQ; depth to the richest moment (the existing rule);
  - write the article once: plan only headings and which seed facts go where, then write the final text; the silent
    reread is one quick pass fixing a sentence in place, never a second draft;
  - return one article only (no alternative versions, notes, outline or list of facts used).
- SYSTEM_PROMPT is unchanged (other channels byte-identical; the AI-tell / source-fidelity / d21 / AI-mark rules and
  gates are untouched). The reread stays: only its form on a blog is bounded.
- Ceilings unchanged (blog 12,000, retry 24,000): the fix is the cause, not a lower ceiling (a lower one would cut
  more replies and pay twice).
- Prompt register: t9_write v2 in `shared/llm_client/prompt_versions.json`, with the block's four symbols added to the
  hashed symbols (a later edit of the block alone now fails the AA-900 gate; v1 hashed only SYSTEM_PROMPT and
  build_user_prompt, so a constant's edit went unseen). Registered in shared.prompt_version by migration 380.

## Changed
- `services/acp_content_writing/prompts.py`: `_BLOG_WORDS`, `_BLOG_WORDS_MAX`, `_BLOG_SECTIONS_MAX`,
  `_BLOG_LENGTH_INSTRUCTIONS`; `build_user_prompt` appends it for a blog.
- `shared/llm_client/prompt_versions.json`: t9_write v2. `api/migrations/380_prompt_versions_aa741_blog.sql` (contract,
  MIG-4 first PR; it also registers t9_write v3 and t8_angle_gen v2 of the stacked Blog Slate PR, one Migration
  Apply run for the lane) + `tests/integration/test_aa741_migration_380.py`.
- `docs/llm/prompt-register.md` (t9_write row symbols), `CONTEXT.md` (ceiling = head-room line).
- Tests: new `tests/unit/test_aa741_s271_t9_blog_output_budget.py` (output contract: block once in every blog prompt and
  nowhere else, band inside F4, the stated floor clears F4 and no "shorter article" licence (also on an F4 rewrite), a full-length reply under a third of the ceiling, envelope keys, manifest covers the
  block, write and rewrite with a fake client). Pins regenerated on purpose with a note: aa771 blog hashes (`single`,
  `route`), round-9/10 fixture sha, aa919 release-0 pins (only the 20 T9 blog user prompts move); round 5 / 6
  byte-identity tests compare without the block; aa642 325 test compares 325's rows with each stage's FIRST manifest
  entry (v2 is 380's).

## Tradeoffs
- The effort lever (a `shared.stage_model_params` row t9_write x sonnet-5-5 `effort = medium`, no code) would cap the
  thinking directly, but trades depth on the hardest step of the tenant pipeline; not set here. Measure v2 first; if the
  tail persists, A/B effort medium vs high on the golden set.
- The prompt asks the model to think less; it cannot enforce it. The ceiling and the one budget retry stay as the
  backstop; F0_complete still holds a cut piece.
- The 900-word floor is now stated as a hard minimum, so a thin seed is pushed toward the floor from its own facts.
  The writer may still fall short on a very thin seed (F4 then holds it, as today); the Slate side keeps thin Blog
  Subjects out (AA-741-l6-blog-slate.md, "Thin Subjects"), so such seeds should be rare.

## Should know
- Eval: R2 `t9_blog` is informative only today (R2 judge kappa 0.137 on 06/11, not calibrated). Read after deploy:
  t9_write blog rows with `prompt_version = 'v2'`: max / p95 `tokens_out`, count of `stop_reason = 'max_tokens'`,
  `output_len_chars`, F4 / F0 results in `gate_ledger`, and cost per blog vs v1.
- MIG-4 order: the migration-only PR (migration 380) first, its Infra copy applied by Migration Apply (class
  contract), then this code PR (rebase onto main after the migration PR merges so the two migration commits drop
  out: `git rebase --onto origin/main <migration branch tip>`), then the Blog Slate PR stacked on this one. Between
  this deploy and the Slate deploy, t9_write rows carry 'v2'; after it, 'v3'.
