# AA-1183 part 2 — the CI bot key environment is `ci-bot` (S297, lane l2-rotation)

The ecosystem moves the aa-ecosys-ci-bot private key into ONE environment per repo, named `ci-bot` in all six repos
(deployment branches: main only, no required reviewers). Plan, script and order: AA-Ecosys
`docs/implementation-notes/AA-1183-l2-rotation.md` and `tools/security/ci_bot_key_consolidate.sh`.

## Decisions

- Every workflow job here that mints a ci-bot token names `environment: ci-bot` instead of `release`. The tests that
  pinned the environment name follow.
- The App id stays the repo variable `AA_CI_BOT_APP_ID` (an identifier, not a secret).

## Changed

- The ci-bot workflows (`environment:` line and header comments) and their unit tests. No application code.

## Tradeoffs

- A new environment name rather than keeping `release`: one name in every repo, and the root and Infra copies needed a
  new home anyway (the move is also a key rotation, because a GitHub secret cannot be read back).

## Should know

- Merge only after the root script's `setup` step put the new key in this repo's `ci-bot` environment; before that the
  release / sync jobs fail with "App is not configured". The old `release` copy is deleted by the script's `cleanup`.
