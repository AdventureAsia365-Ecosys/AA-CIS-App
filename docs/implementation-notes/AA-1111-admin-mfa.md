# AA-1111 slice S2 — administrator MFA check (lane l17, S285)

Tech lead decisions: admin MFA = TOTP (S283); drop the WAF geo rule of the staff hosts once admin MFA is enforced
(S285). The pool already allowed TOTP as optional MFA (AA-CIS-Infra `identity.tf`).

## Decisions
- **Claim**: the verified access token's `amr` (Amazon Cognito "authentication levels": password + TOTP gives
  `["pwd", "otp", "mfa"]`, e-mail code `["email_otp"]`). Cognito computes it and a pre token trigger cannot change it.
  Only `otp` counts (TOTP, the tech lead's choice); `pwd` + `email_otp` would also carry `mfa` if e-mail MFA were ever
  enabled, which is not the decided factor.
- **Read from the token string, not the Principal**: `api/core/identity_verifier.py` is byte-copied into Booking and
  AA-Partner (vendored-copy CI check against CIS main). Adding `amr` to `Principal` would chain three repos; the
  signature is already verified on that exact string, so `staff_mfa.token_amr` decodes it without verifying again.
- **Who is checked**: role `admin` (group `cis-admin`) or the owner flag. Not checked: Preview viewer (capped, never an
  owner), Dev-only read-only E2E identities (A10b/A10d; they sign in with e-mail codes from a test mailbox and cannot
  exist outside Dev).
- **Switch**: platform setting `cis.platform.admin_mfa_required` (CIS 459, bool, default off, maker-checker, impact
  high, no env override so an env var can never turn MFA off). Unknown key (code before 459) reads off.
- **Off = log mode**: an admin session without the code is logged `staff_admin_mfa_check outcome=would_refuse`, so the
  tech lead (and the agent, in CloudWatch) can see the claim on a real TOTP sign-in before the switch goes on.
- **Placement**: `staff_identity.authenticate`, the single path of the bearer middleware and `/auth/verify-admin`.
- **UI**: the Cognito callback maps 403 `mfa_required` to `/login?error=mfa_required` with an EN + VI message.

## Changed
- Nothing on a non-admin path. With the switch off nothing is refused anywhere.

## Tradeoffs
- Fail-open on an unknown key (only possible before CIS 459 is applied) vs. locking every administrator out of the
  page that turns the switch on. The switch is a settings row with history, so "off" is visible and audited.
- The log is throttled (one line per identity and outcome per 10 minutes, a technical constant) instead of one line
  per request.

## Should know
- A token without `amr` at all (Cognito rollout not reached the pool, or the classic hosted UI) is "no MFA": the
  runbook's `staff-totp-check` step proves the claim on this pool before the switch goes on.
- After the switch is on, an administrator refused mid-session lands on the sign-in page on the next refresh.
