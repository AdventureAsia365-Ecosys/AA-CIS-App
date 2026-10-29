---
id: APP-06-eval
title: Eval runbook for prompt, model and route changes
description: How to measure a prompt, model or route change on the AA-729 golden set by hand - which eval, the cost cap, the commands, reading the result and recording the run ID in the PR.
type: how-to
status: draft
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-cis-api, aa-cis-worker]
last_reviewed: 2026-10-29
review_every_days: 90
related: [APP-06-prompts, APP-06-routes, CAT-01, AA-900, AA-919, AA-729]
---

# Eval runbook for prompt, model and route changes

> [!WARNING]
> UNTESTED - do not follow in an incident without checking each step.

A PR that changes a prompt, a model or a route records an eval run ID and its cost (engineering handbook §5.7,
decision D-23). Evals run by hand; there is no per-PR eval trigger (decision N-4: it would be new recurring spend).
This page says which eval to run, how much it may cost, how to start it, how to read it and what to write in the PR.

Prompts and their eval baselines: [prompt register](prompt-register.md). Current routes and models: the generated
route table in the root repo, `docs/generated/llm-routes.md`.

## Which eval

| You changed | Eval | Run it |
|---|---|---|
| A writer prompt of `s1_generate`, `t2_generate` or `t9_write` (the register says `R2 <writer>`) | R2 run, no release, goal `non_inferior`, label `adhoc_aa900` | After the change is deployed on the worker |
| The model or route of `s1_generate`, `t2_generate` or `t9_write` | The same R2 run | After the route change is live (admin Settings or the route migration) |
| A judge prompt, rubric, anchor or judge route (`Q2` in the register) | Q2 judge re-score (PR template, "Quality" section) | Before merge |
| A prompt with `Q0` / `R0` / `Q3` only | The quality-system tier of the file (`services/quality/paths.yaml`) | As that tier says |
| A prompt whose baseline is `none` | No eval exists: describe the manual check (inputs, outputs read) in the PR | Before merge |

Why after deploy: an R2 run regenerates through the writer code running on `aa-cis-dev-worker`. Its control is the
stored output of the writer's active release (release 0 = the frozen AA-729 baseline outputs), so the "before" side
already exists and only the "after" side needs money. Dev has no real users, so measuring right after deploy and
reverting on a failed verdict is the accepted order.

R2 writers: `a1` (S1 graph), `t2` (tenant rewrite + T3 repair rounds), `t9_blog`, `t9_social` (first draft only:
the T10 gate and repair loop is not run, so a T9 candidate can only look worse than its stored final control).

## Cost

| Limit | Value | Where it is enforced |
|---|---|---|
| Per run | `--cap-usd`, at most 10 USD (`R2_RUN_CAP_USD`). Use 2 USD for a change eval unless the DRY RUN estimate needs more | The runner stops (`budget_cap`, verdict `invalid`) before an item that would pass the cap, from `shared.llm_call_log` |
| Per month | 25 USD over every writing-memory stage, `adhoc_aa900` included (`MONTH_CAP_USD`, `MEMORY_STAGES`) | Checked before each item |
| Per call | Every call goes through `LLMClient` and lands in `shared.llm_call_log` under stage `adhoc_aa900`, `quality_signal.eval_run = <run-id>`, `quality_signal.real_stage = <writer stage>` | `call_log.bind_eval_stage` |

A `--limit` run is cheaper but ends `informative` (it cannot pass). Use it only to check that the run starts.

## Steps

1. Confirm the account.

   ```bash
   aws sts get-caller-identity --profile aa365-admin
   ```

   Expected: the acc2 account of the account map. Abort on any other account.

2. Confirm the change is live: api and worker run the same image, and it contains the PR's merge commit.

   ```bash
   aws ecs describe-services --cluster aa-cis-dev-cluster --services aa-cis-dev-api aa-cis-dev-worker --profile aa365-admin --region us-west-1 --query "services[].[serviceName,taskDefinition,deployments[0].rolloutState]" --output text
   ```

   Expected: both rollouts `COMPLETED` on task definitions whose image tag is `dev-<merge-sha>`. Abort while a
   rollout is still in progress.

3. Get a task to run the DRY RUN in.

   ```bash
   TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-worker --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
   ```

4. DRY RUN: print the plan and the estimate. It reads only, 0 USD. Replace `t9_blog` with the writer of the row.

   ```bash
   aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container worker --interactive --command "python3 -m services.writing_memory.r2 --writer t9_blog --goal non_inferior --stage adhoc_aa900 --cap-usd 2 --requested-by aa900-<pr-number>" --profile aa365-admin --region us-west-1
   ```

   Expected: JSON with `plan`, `calibrated_possible: true`, `estimated_usd_max` at or under `cap_usd`,
   `log_stage: "adhoc_aa900"` and `month_spend_usd` well under `month_cap_usd`. If `calibrated_possible` is false the
   run can only be `informative`: stop and say so in the PR. If the estimate passes the cap, raise `--cap-usd` (at most
   10) only with the tech lead's yes for that run.

5. Start the run on the worker: the same command plus `--enqueue`. The paid run is the job `writing_memory_r2`; it
   never runs inside the exec session (a long paid run dies with the session).

   ```bash
   aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container worker --interactive --command "python3 -m services.writing_memory.r2 --writer t9_blog --goal non_inferior --stage adhoc_aa900 --cap-usd 2 --requested-by aa900-<pr-number> --enqueue" --profile aa365-admin --region us-west-1  # WRITE
   ```

   Expected: a second JSON line `{"enqueued": "writing_memory_r2", "job_id": "<job-id>", "created": true}`.
   Abort: cancel the job on the admin Jobs page (`/admin/jobs`). The job blocks backend deploys while it runs
   (deploy gate), so do not start it while a merge is waiting.

6. Wait for the job to finish (up to 90 minutes, usually far less with a 2 USD cap) on the admin Jobs page, then read
   the result with the read-only report script through the S3-mediated ECS exec pattern (root
   `docs/ops/runbooks/ecs-exec-s3-mediated.md`). Upload `scripts/llm_ops/eval_run_report.py` from this repo, then
   run it with the `run_id` from the job's result (admin Jobs page). `<bronze-bucket>` is the CIS bronze bucket; its
   name is in that runbook (this repo is public and names no account). `--latest t9_blog` instead of the run id picks
   the newest FINISHED run of that writer under `adhoc_aa900` (`--stage writing_memory_eval` for a Releases tab run);
   use it only when you know no other change eval of that writer finished after yours.

   ```bash
   aws s3 cp scripts/llm_ops/eval_run_report.py s3://<bronze-bucket>/scripts/eval_run_report.py --profile aa365-admin --region us-west-1  # WRITE
   URL=$(aws s3 presign s3://<bronze-bucket>/scripts/eval_run_report.py --profile aa365-admin --region us-west-1 --expires-in 300)
   aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container worker --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/r.py && python3 /tmp/r.py <run-id>'" --profile aa365-admin --region us-west-1
   ```

   Expected: the block between `--- AA900 EVAL RUN ---` and `--- END ---` with `run.run_id`, `run.verdict`,
   `run.stop_reason`, `run.cost_usd`, `run.log_stage: "adhoc_aa900"` and `logged_cost_usd` (the sum of the run's
   `shared.llm_call_log` rows, which must match `run.cost_usd` within a cent). `logged_calls: 0` on a finished run
   means lost cost rows, not a free run: stop and investigate (lesson S254).

7. Read the verdict.

   | Verdict | Meaning | Action |
   |---|---|---|
   | `pass` | Calibrated judge, no new failure class over noise | Record it; the change stays |
   | `fail` (`hard_stop`, `futility`) | The change is worse than the control | Record it; revert the PR, or fix and run again |
   | `informative` | The judge did not calibrate, or the run used `--limit` | Record it; it is not evidence. Say why in the PR |
   | `invalid` (`budget_cap`, `error`) | The run stopped before a verdict | Record it; fix the cause, run again |

8. Record the run in the PR (a comment on the merged PR when the run happened after merge) and in the
   implementation note of the issue:

   ```text
   LLM change eval (AA-900 runbook)
   - Prompt id(s): t9_write
   - Eval: R2 t9_blog, goal non_inferior, golden set aa729@1.2.0, label adhoc_aa900
   - Run ID: <run-id>
   - Verdict: <verdict> (<stop-reason>), judge kappa <kappa>
   - Cost: <cost-usd> USD logged (cap 2 USD), <calls> calls
   ```

## Related

- R2 runner and its stop rules: `services/writing_memory/r2.py` (module docstring).
- The golden set: `shared.eval_golden_set` / `shared.eval_golden_item` (AA-919, CIS 284), fixture in the root repo
  `docs/eval/aa729/`.
- The legacy `eval-regression.yml` workflow measures only the retired `s1_old` pipeline; it is not this eval.
