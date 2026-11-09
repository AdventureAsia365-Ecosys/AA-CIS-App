# AA-900 — S272 lane l7: cassette gaps found live

Root ADR 0020 "Gateway amendments" d8. The S272 live check (Linear AA-900 comment of 07/11/2026, probe job
`669a2188`) found three gaps in the W0 cassette recorder (CIS 385, `shared/llm_client/cassette.py`, lane l17 notes in
`AA-900-l17-w0-cassette.md`):

1. An attempt that ends **billed_unusable** (the provider answered and billed, the reply cannot be used) wrote no
   cassette row: `LLMClient._attempt` only called `record_attempt` on the success path.
2. The **embedding** call (cohere-embed-v4 on acc3) wrote no row: by design or a gap?
3. acc3 **haiku-4-5 and sonnet-4-6** showed 2 ledger rows `ok` each and 1 cassette row each.

One CIS code PR (no migration), one root docs PR (ADR 0020 note).

## Decisions

- **Gap 1: record the unusable reply from the exception, no migration.** The four places that raise
  `BilledUnusableReply` inside a provider call (Converse, the Anthropic API, the OpenAI format; `_billed_unusable`)
  now attach what the provider sent on the exception: `reply_facts` = the raw reply text (the Converse tool input as
  JSON, the text blocks; the Anthropic / OpenAI text before any JSON parse) and, when the adapter knows them, the
  provider request id, the reported model id and the effort sent. `_attempt`'s except path, for a billed exception
  only, calls `cassette.record_unusable_attempt`: one row, built by the same `build_row` from a stand-in reply. The
  mark: **`stop_reason = 'billed_unusable'`**, or `'billed_unusable:<provider stop reason>'` when the provider gave
  one (fits the 64-character CHECK; e.g. `billed_unusable:max_tokens`). `reply_withheld` could not take the mark: its
  CHECK of 385 allows only `'pii_stage'` and forces `reply_text` NULL. A billed exception that carries no reply (a
  stream that broke after its usage, a Converse body without `output`) still gets its row with an empty reply
  (`reply_bytes` 0), so every billed attempt has exactly one cassette row.
- **Replay never serves an unusable reply.** `get_by_request_sha` skips rows whose stop_reason starts with
  `billed_unusable` (a replay asks for an answer the gateway accepted). `get_by_call_log_id` is unchanged: a
  `billed_unusable` ledger row now finds its unusable reply (same `(route_trace, attempt_no)` key).
- **Billed records now carry `route_trace` + `route_position`** (`_enrich_billed`, from the attempt scope).
  The route path already put `route_trace` in the billed row's quality_signal (`_call_key_once` extra); the admin
  probe (`LLMClient.probe`) did not, so a probe's `billed_unusable` ledger row could not be joined to its cassette
  row. `_with_route` keeps a value the signal already has, so route rows are unchanged.
- **Gap 2: embeddings are not recorded, by design.** An embedding reply is a vector, not text: nothing to replay
  against a changed rule, and the caller already stores the vector in its own column (`embedding_space`). Re-embedding
  is deterministic and costs fractions of a cent. Written in the `cassette.py` and `embed.py` docstrings and in root
  ADR 0020 (d8 note). Jev (`decide`) stays out too (not a chat reply; its own client).
- **Gap 3 explained: two deployments, two names, no missing row.** acc3 has two text deployments of each of these
  models on the same wire id: the legacy key (`haiku` / `sonnet`, `api_style anthropic_native`, InvokeModel, CIS 304)
  and the catalog key (`haiku-4-5` / `sonnet-4-6`, Converse, CIS 366). The daily probe plans one probe per enabled
  deployment, so it calls both. Ledger `model` is the reply's label after `_normalize_model_provider`: the legacy path
  answers `haiku-4-5` (`bedrock_satellite.invoke_claude` model label), the Converse path `satellite-haiku-4-5` →
  `haiku-4-5`. The cassette `model_key` is the key the gateway called: `haiku` and `haiku-4-5`. Counted by model
  name, that is 2 ledger rows : 1 cassette row; counted by attempt it is 2 : 2. It is neither a retry (each probe is
  its own gateway request: one scope, one trace, attempt 1) nor a gateway row + a caller row (the gateway writes no
  row for an answered attempt; `test_connection` writes the one `ok` row).
- **Link rule, now explicit in a test** (`test_aa900_s272_cassette_gaps.py`): one cassette row per provider attempt
  that got a reply (answered, cut, or billed unusable), joined to its ledger row on `(route_trace, attempt_no)`, never
  on model name. A truncation retry shares the cut attempt's number and is told apart by `is_truncation_retry`.

## Changed (vs the lane brief)

- Nothing beyond the brief. No migration (no CIS 388 needed).

## Tradeoffs

- The stop_reason mark loses nothing: the provider's own stop reason stays after the colon. A separate column would
  be cleaner for queries but needs a contract migration and a MIG-4 chain for a W0 record-only table.
- The legacy-key structured path (`_legacy_structured`) validates AFTER its attempt returned: that reply is already
  recorded as a normal row (unmarked) and the later BilledUnusableReply never passes `_attempt`. Left as is: the path
  logs a warning on use ("route this stage to a catalog (Converse) key") and no live route uses it for JSON stages.

## Should know

- No schema change: `stop_reason` text <= 64 (CHECK `llm_cassette_text_sizes_check`) takes the mark.
- To count unusable replies: `WHERE stop_reason LIKE 'billed_unusable%'`.
- The recorder still never raises: a reply-facts read that fails is counted as `build_failed`.
