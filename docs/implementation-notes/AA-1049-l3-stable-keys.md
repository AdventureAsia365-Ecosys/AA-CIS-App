# AA-1049 — Quality W3, stable-key guard: the two S272 review findings (lane l3, S275)

Tech lead decision S275: `T9_REGRESSION_GUARD_MODE=stable_keys` stays OFF until the two major review findings of S272
round 3 are fixed, then the switch is asked again. This lane fixes both in `services/acp_content_writing/stable_keys.py`
(opt-in code only; the default `gate_names` path is main's rule and is unchanged). $0: no model call, no flag flip.

## Decisions

- **Finding 2 (style traded for a truth failure): a hold-capable tier, fail closed.** `stable_keys.hold_keys` is the
  subset of the blocking keys whose result may hold the piece (`quality_gates.never_holds` is False, the same test
  `hold_failure` uses); of an F14 result only the far-over class counts. `compare()` reverts when the rewrite has a
  hold-capable key the previous attempt did not AND is not net-better on either hold measure (as many hold-capable
  keys or more, and as many hold-capable gates or more). Chosen over "fail closed on a new `fact.*` /
  `source.contradiction` class" because the bug is about piece STATUS (held but delivered): the hold test is what
  decides status, and it covers every gate that may hold, not only the ones with a truth taxonomy class.
  Then the S272 all-key rule runs unchanged (D-guard-1 swap revert, D-guard-2 keep, never revert a rewrite that clears
  a whole gate). Order: floor rule, hold rule, all-key rule.
- **Finding 3 (F14 whole-gate key): F14 keys by violation class** (`far_over`, `under_floor`, `over`, `hashtags`;
  `stable_keys.f14_class`), so a new under-floor block is never masked by an earlier F14 key (far-over -> under-floor
  is now a new key in the same gate: reverted to the held far-over attempt; main keeps the short post).
- **Finding 3, the reported case itself, needs one more rule.** Class keys alone do not fix it: under-floor F14 + F15
  s1 -> far-over F14 + F15 s2 then has two new keys (2 >= 2, gates 2 >= 2) and still reverts, and the hold rule also
  reverts (a new far-over key). A revert into an attempt with the AA-780 under-floor block makes the call site waive
  that block (`service._waive_floor_block`), which is how the short post shipped approved. So when the previous
  attempt has the under-floor block (`stable_keys.has_floor_block`), the opt-in mode takes main's gate-name decision
  (`RULE_MAIN_FLOOR`): it never reverts into the short post where main keeps it, and the call site's AA-780
  floor-only exception then works exactly as it does on main.
- `compare()` returns the rule that decided (`rule`: main_floor | hold_keys | keys | keep) and the hold evidence
  (`new_hold`, hold key counts, hold gate sets) for the replay.

## Changed

- `stable_keys.py`: `f14_class`, F14 branch in `violation_keys`, `hold_keys`, `has_floor_block`, the three-step
  `compare()`. `regressed()` and `service.guard_decision` keep their signatures (the call site is unchanged).
- `service.py`: comments only (the mode is no longer described as having open findings).
- `scripts/quality/replay_aa1049_t9.py`:
  - every checkout records main's hold-capable gate view (`hold_gates`, via `never_holds`) and `floor_prev`;
  - new declared id **D-guard-3**: the branch reverts where main kept, rule `hold_keys`, the hold evidence equal to
    the base row's hold view, the new hold keys in main's hold gates, not fewer hold keys / gates;
  - two outcomes are never declared whatever the evidence says (judged on the base row): a keep of a rewrite that
    holds over a previous attempt that does not (approved->held), and a revert main did not make into an attempt with
    the under-floor block.

## Tradeoffs

- The hold tier makes the opt-in mode stricter than the S272 key rule in one more shape: a same-gate truth swap
  where style findings were also cleared (F9 A + F15 s -> F9 B) is reverted (both attempts are held either way).
  Accepted: it is the D-guard-1 idea (a new fabricated claim is not progress) applied to hold-capable keys.
- The floor rule gives up the stable-key view for attempt-1 under-floor pairs. Accepted: the AA-780 waiver is a
  permissive action, and main's rule plus the call-site exception are the tested behaviour there (test_aa780).
- Hold-capability is per result, not per finding (except F14): a holding result with a seed-alignment `sa:` finding
  counts that finding's key as hold-capable. More reverts, never fewer.

## Should know

- Unit tests: `tests/unit/test_aa1049_stable_keys.py` (the review cases now decide like main in both modes; finding-2
  variant main itself keeps; finding-3 both directions; a fail-closed sweep over 2,704 attempt pairs: never keep a
  holding rewrite over a hold-clean attempt, and on an under-floor previous attempt always main's decision).
  `tests/unit/test_aa1049_replay_harness.py`: D-guard-3 end to end through `--pieces`, and mutations M8 (drop the hold
  rule -> UNDECLARED approved->held) and M9 (drop the floor rule -> UNDECLARED revert into under-floor).
- Local replay result: see the S275 section below.

## S275 local replay (free, no model call, no DB)

Base: `git archive` of CIS origin/main `746e95a8` (defaults), run with this branch's copy of the replay script.
Input: root `docs/eval/aa729/fixture-1.2.0.json` (30 samples) + the S273 read-only export of 60 recent demo-tenant
pieces (local `.tmp-session` only), 90 pieces, 88 guard pairs.

| Run | Exit | Declared | Undeclared | Status flips |
|---|---|---|---|---|
| main defaults, repeat vs base | 0 | 0 | 0 | 0 |
| branch defaults vs main | 0 | 0 | 0 | 0 |
| branch `stable_keys` + `--opt-in` | 0 | D-guard-1 13, D-guard-3 5 | 0 | 0 |
| branch `stable_keys` + `T9_SEO_BUILDER=on` + `--opt-in` | 0 | D-guard-1 13, D-guard-3 5, D-seo-4 14 | 0 | 0 |

- The S273 run had D-guard-1 13 and D-guard-2 1. The one D-guard-2 pair is the finding-2 class on real data: the
  S272 key rule kept a rewrite that went from 1 hold-capable key (F9) to 3 (new F17 + F18 truth findings) because it
  cleared five F15 style keys. The hold rule now reverts it, like main (no diff any more).
- The 5 D-guard-3 pairs are all one shape: F9 claim swap with F15 style cleared (F15 + F9 -> F9, hold-capable keys
  1 -> 1 or 2 -> 2, a new F9 claim). Main keeps (no new gate), the opt-in mode reverts; both attempts are held on the
  same gate, so no status flip.
- 18 SEO-fit cassette misses (`MISS`, not compared) in the default and guard runs, 4 with the SEO builder on.
