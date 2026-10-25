# AA-790 follow-up — `hygiene.sentence_spans` in linear time

No own Linear issue (backlog row 0m, refs AA-790). Found during the AA-790 post-repair integrity work: the
integrity guard calls `sentence_spans` on every repair step, and `sentence_spans` was quadratic on a long run of
end marks followed by a non-space (`"!" * 10_000 + "x"` took ~2.5 s; `"." * 10_000 + "x"` ~2.7 s).

## Decisions

- **Replace the regex with a one-pass scan, same matches.** The sentence-end pattern
  `[.!?…]+["'”’)*]*(?:\s?TAG)*(?=\s+\S|\s*$)` failed at the start of a mark run that a non-space follows, and
  `finditer` then re-tried every start inside the run, each scanning to the run's end (O(n²)).
  `hygiene._sentence_end_matches(line, pos)` yields the same `(start, end)` pairs:
  - the mark run and its closers are greedy, and shortening either can never satisfy the look-ahead (the next char
    would be a mark or a closer, neither whitespace), so only the end of the citation-tag chain can back off;
  - a tag body holds no bracket or newline (AA-912 `TAG`), so the `(?:\s?TAG)*` chain is deterministic: the scan
    records each chain end and picks the longest one followed by whitespace or the end of the line, exactly the
    regex's backtracking order;
  - the look-ahead `(?=\s+\S|\s*$)` on a newline-free line is "end of line, or a whitespace char next"
    (`str.isspace` is the same predicate as `re`'s `\s` on `str`);
  - a run whose start fails is skipped whole: every later start in it has the same tail and fails too.
- **The old regex is removed from `hygiene.py`** (a comment keeps its text) so CodeQL has no polynomial pattern
  left to flag there; the test file carries a verbatim copy (`_OLD_SENT_END_RE`) as the oracle, the AA-912 way.
- **`_split_first_line` uses the same scan** (it did `_SENT_END_RE.search(head)` — same quadratic on a long first
  line of marks).

## Changed

- `services/acp_content_writing/hygiene.py`: `_sentence_end_matches` added; `sentence_spans` and `_split_first_line`
  use it; `_SENT_END_RE` dropped.
- `tests/unit/test_aa790_sentence_spans_linear.py`: equivalence on every string in the unit-test JSON fixtures
  (~500 texts) plus hygiene samples, at several start offsets; 30k random lines over the characters the pattern
  cares about (marks, closers, tags, malformed tags, NBSP, `\x1c`, ` `); `sentence_spans` and
  `_split_first_line` output equal to the old implementation; runtime bound (< 1 s) on 100k-char adversarial inputs.

## Tradeoffs

- A hand-written scan is longer to read than one regex; the equivalence tests pin it to the old pattern so a later
  edit cannot drift silently.

## Should know

- No behaviour change: every sentence boundary, span and first-line split is identical (pinned by tests).
- No migration, no infra, no LLM call. Ships with the next CIS api + worker deploy (pure Python on both).
