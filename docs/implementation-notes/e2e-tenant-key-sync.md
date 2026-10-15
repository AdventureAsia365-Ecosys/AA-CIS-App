# E2E test tenant key sync (S237)

## Decisions
- A test tenant is recognised by the existence of `aa-cis/dev/e2e-tenant/<slug>`; no config list. The app only overwrites (PutSecretValue), never creates, so real tenants are untouched.
- Runs after the DB transaction commits (generate-key, restore), off the event loop (`asyncio.to_thread`), only on ECS (`AWS_EXECUTION_ENV`).

## Changed
- `GenerateKeyResponse` / `RestoreTenantResponse` gain `test_secret_sync` and the message says when the copy was updated or failed.

## Tradeoffs
- The task role can now overwrite the four E2E key copies. Acceptable: they are test credentials and the role already holds the admin secret.

## Should know
- IAM: AA-CIS-Infra `modules/ecs` `ecs_task_policy`, Describe + PutSecretValue on `aa-cis/dev/e2e-tenant/*`.
- A failed sync never fails the rotation; the admin message says to update the copy by hand.
