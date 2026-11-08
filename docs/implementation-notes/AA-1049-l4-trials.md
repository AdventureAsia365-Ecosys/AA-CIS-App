# AA-1049 (lane l4-quality-trials, S273) — per-job trial switches for the W3 paid proof runs

Scope: the paid proof runs that close AA-1047 (Quality W1) and advance AA-1049 (Quality W3) are run by the session
orchestrator. This lane builds the one code hook those runs need (a W3 switch on for one job, audit tenants only) and
the trial / replay scripts (kept outside the repo, in the root workspace `.tmp-session/S273/l4-trials/`, because they
print tour text and ids). No paid call was made in this lane.

## Decisions

- **Job payload, not a request header.** The W3 switches (`T9_REGRESSION_GUARD_MODE`, `T9_SEO_BUILDER`,
  `T2_ORCHESTRATOR`) are process env switches with default = main (S272). Setting one on the worker would change
  every tenant's writes. A tenant request header would need a tenant session and a router change on a tenant API; the
  trial is staff work, so the trial script enqueues the normal `t9_write` / `t2_rewrite` job with a `trial_flags`
  payload field and no router ever sets it.
- **Audit tenant = `shared.tenants.kind = 'demo'` and not the pilot.** That is the Dev definition of a synthetic
  tenant (`services/tenant_account/dev_guard.py`, root ADR 0019 d16). A real agency, the pilot, the platform tenant,
  an unknown tenant or a lookup error: the request is refused and the job runs main's path (fail closed to main,
  never a failed job, so a stray payload can never break a real tenant's write).
- **Allow-list per job kind and value.** `t9_write`: `T9_REGRESSION_GUARD_MODE` (stable_keys | gate_names),
  `T9_SEO_BUILDER` (on | off); `t2_rewrite`: `T2_ORCHESTRATOR` (linear | graph). A test checks every allowed value is
  one its reader recognises (an unknown value would silently run main and waste the trial's money).
- **ContextVar, set inside the job's task.** The worker runs each job in its own asyncio task; `asyncio.to_thread`
  copies the context, so the sync steps of the T2 orchestrator and the SEO fit see the value too. A job running
  beside the trial and the process env never see it (test).
- **Readers.** `service.regression_guard_mode`, `service.seo_builder_mode`, `t2_orchestrator.mode` read
  `trial_flags.get(name)`: the job's value when set, else `os.environ` exactly as before. The `T2_INTERIM=off`
  break glass still wins over a linear trial (as over the env switch).
- **Recorded in the job result** (`shared.job.result.trial_flags`: requested / applied / refused): the trial report
  reads it per job, so a trial that ran main by mistake is visible, not a silent pass.
- **config_fp untouched.** The release fingerprint reads the process env: a one-job trial is not a platform
  configuration change and must not raise a reverify.

## Changed

- New `services/quality/trial_flags.py`; `services/jobs/t9_write_job.py` and `services/jobs/t2_rewrite_job.py` run
  the write inside `trial_flags.scope(...)` and add `trial_flags` to the result when the payload asked;
  the three readers above; `CONTEXT.md` (T2 section).
- Tests: `tests/unit/test_aa1049_trial_flags.py` (parse, allow-list vs readers, fail-closed tenants, task isolation,
  both jobs), `tests/integration/test_aa1049_trial_flags_db.py` (the tenant read on the real schema).

## Tradeoffs

- One extra `shared.tenants` read per trial job only; a job without `trial_flags` makes no extra read (the exact
  fetch order of `test_aa469_viec1_t4_t5_split` pins that).
- A refused trial still runs and bills (main's path) instead of failing: a refusal can only come from a wrong tenant
  in the trial script, which checks the tenant before it enqueues, and failing a real write would be worse.

## Should know

- **AA-1047 (a) — contradiction suite.** `tests/unit/quality/test_aa1047_contradictions.py`
  (`test_example_is_decided_as_the_policy_expects`, 150 examples of `writing_policy.yaml` v1: block 103, warn 12,
  pass 35, 100%; plus `test_the_code_vs_code_contradictions_are_proved_gone`) passes on origin/main dec928a1;
  `python -m services.quality.writing_policy --check` = fresh, `python -m services.quality.lint` = 0 errors,
  `policy_sha` 739223d6cc24f1e5. The 8 `main_gaps` and 53 `w2_intent` examples are recorded, not asserted (S272
  fail-closed decision); they are W2 input.
- The trial needs this PR deployed on the worker AND the api (same SHA) before the trial script enqueues: an old
  worker ignores `trial_flags` and the job result then has no `trial_flags` key (the report flags it).
- Trial / replay scripts and their runbook: root `.tmp-session/S273/l4-trials/README.md` (not committed: they print
  tour ids and download tour text for the offline replay).
