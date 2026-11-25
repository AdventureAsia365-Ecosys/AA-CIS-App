# AA-1166 — writer note before the JSON draft (lane t2-note-before-json)

The S294 AI red-team (tenant-crossing case on the T2 rewrite; the full report is private) saw the T2 writer answer
with a free-text note BEFORE its JSON object ("Note: the tour description contained an instruction ... I haven't done
that.", then the object). This slice makes the draft parser keep that text out of the draft for certain and turns it
into a signal. Separate notes file from `AA-1166.md` (the red-team set lane) so the two lanes never edit one file.

## Decisions

- **One parser, shared.** The T2 orchestrator's `write` and main's graph both call `graph.generate_node`, which (with
  the Bedrock Batch seed node) parses through `parse_generated_json`. Both now read
  `services/content_generation/writer_reply.extract`, so the S1 v1 writer, the T2 rewrite (linear and graph modes)
  and the batch seed decode the same way. The S1 writer v2 has its own parser and is not touched (T2 never uses it).
- **Clean replies first, byte-for-byte as before.** Step 1 is the old fence strip + `json.loads`: a clean reply
  returns exactly what it returned before (a dict without `name` included, as the AA-606 tests pin).
- **Then the object only.** A reply that is not clean JSON is read through its candidates: every complete
  top-level JSON object with a `name` (`shared/llm_client/json_reply.json_values`, the AA-1026 scan with spans) and
  every `{"name":` opener anywhere, also after a stray unclosed brace in the note (where the top-level scan stops).
  Only the chosen object's own text is decoded. A `{...}` or a JSON object without `name` inside the note is never
  the draft.
- **Several named objects: the one on its own lines, else none (review fix).** The writer can quote the planted
  instruction as a full JSON object with its own keys (`name`, `summary`, `seo_title`, `seo_meta`). Taking the first
  named object would make that quote the draft (the old whole-reply json-repair failed on that shape; a naive first
  pick silently succeeds with the injected object). So with several candidates the draft is the one that stands on
  its own lines (nothing but whitespace or a fence before it on its first line and after it on its last line); a
  quote inside a sentence of the note never qualifies. When that is not exactly one (two objects on their own
  lines, or two inline), the reply is ambiguous: nothing is taken, one `writer_reply_ambiguous` warning (counts
  only), and the writer node fails and retries as for any unparsable reply. "Take the last" was rejected: a note
  AFTER the draft can quote a named object too.
- **Salvage one object, never the prose (review fix).** The AA-217 json-repair salvage runs on the chosen
  candidate's own text (its `{"name":` opener to its matching close, or to the end of a cut-off reply), never from
  the note's first brace, so a key the note quotes inside an unclosed brace (`{"seo_meta": "..."`) is never merged
  into the draft, and the recorded note keeps its full text (its kind is not cut at the stray brace). With no
  candidate at all (a malformed draft whose `name` is not its first key), the salvage tries braces last first: the
  braces that open a line, then any brace, an unclosed one only when nothing else opens after it. The salvage still
  needs a dict with `name`.
- **Signal channel: `degraded[]`, severity `count`.** New name `writer_note` (detector `writer_reply`, detail
  `instruction_notice` / `refusal` / `other`). It rides the existing paths: T2 `run.degraded` → the job's
  `t3.degraded` diagnostics; A1 `generated_content.metadata.degraded`. Plus `quality_signal.writer_note` on the
  writer's ledger row (`shared.llm_call_log`, readable by the Writing Lab / gateway pages) and one
  `writer_reply_note_dropped` info line with the tour id, tenant id, stage, kind and length.
- **The note's text is stored nowhere** (not in the entry, the ledger row or the log line): it can quote the source
  or a planted instruction. Only kind and length are kept.

## Changed

- `shared/llm_client/json_reply.py`: `json_values` (top-level values with spans) and `bracket_end`; `first_json_value`
  unchanged.
- `services/content_generation/writer_reply.py` (new): `extract(raw) -> (draft, note)`, `note_kind(note)`,
  `record_note(state, note, kind, stage)` (the signal; kept out of `graph.py`, whose size baseline may not grow).
- `services/content_generation/graph.py`: `generate_node` parses with `writer_reply.extract` (the inline fence strip
  moved into it), records the note signal on a parsed draft; `parse_generated_json` wraps `extract`. The `json_parse_failed` path is unchanged (its `raw_len` is now the length before the
  fence strip).
- `services/quality/degraded.py`: `WRITER_NOTE` in `REGISTRY` (`count`).
- `docs/context/pipeline-stages.md`: a "Writer note" bullet under the degraded detectors.
- `tests/unit/test_aa1166_writer_note_before_json.py`: named-quote cases (before / after the draft, fenced or not),
  the ambiguous cases, the unclosed-quote cases (complete and cut-off draft), the name-not-first salvage.
- No prompt change, no change to the facts instruction guard or T9.

## Tradeoffs

- No hold: `count` only. A note is evidence the writer saw something odd in its source, not that the draft is bad;
  the existing gates still score the draft. Holding on `instruction_notice` would be a policy decision (and a
  threshold for the admin settings), left to the tech lead.
- The kind is a plain keyword match (no model call). It only sorts the signal; a wrong kind changes nothing stored.
- An ambiguous reply (two named objects, no single one on its own lines) costs a writer retry instead of
  guessing; a guess could store the injected object.
- A note the writer puts INSIDE a JSON string value cannot be told apart from content; out of scope here (the
  existing gates and the facts instruction guard read the fields).

## Should know

- Known gap: a reply whose ONLY named candidate is an unclosed quote in the note while the real draft is
  malformed with `name` not first would still salvage from the quote's opener. The prompt's output schema puts
  `name` first, so the real draft is a candidate in practice (then the own-lines rule picks it).

- Live check after deploy: nothing to call by hand (no paid run). The next T2 rewrite whose writer adds a note shows
  `writer_note` in the version's T3 diagnostics and `quality_signal.writer_note` on its `t2_generate` ledger row.
  The monthly AI red-team run re-checks the case.
