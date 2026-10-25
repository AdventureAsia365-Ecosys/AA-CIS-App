// tests/e2e/helpers/cognitoSignIn.ts
// AA-816 — the managed-login email-code steps shared by the Cognito E2E spec (tests/e2e/cognito/) and the staff UI
// smoke setup (tests/e2e/smoke/auth.setup.ts, S2 path). The E2E staff user is "E2E Cognito"
// (helpers/mailbox.ts E2E_COGNITO_EMAIL); its code is read from the SES inbound mailbox in S3 (read-only role, CI
// only). The code is a live credential: it is never logged or printed, and no screenshot is taken while it may be
// on screen (callers pass a `shot` that runs only before the code is typed).
import { expect, type Locator, type Page } from '@playwright/test';
import { E2E_COGNITO_EMAIL, MAILBOX_PREFIX, mailboxBaseline, waitForSignInCode } from './mailbox';

export const STAFF_AUTH_HOST = 'staff-auth.lumiguides.it.com';

/** True while the page is on the managed-login host (callers must not screenshot it after a code was typed). */
export function onStaffAuth(page: Page): boolean {
  try {
    return new URL(page.url()).host === STAFF_AUTH_HOST;
  } catch {
    return false;
  }
}

/** After "Next": either the code page directly (passwordless user), or a sign-in method choice first. */
export async function reachCodeStep(page: Page): Promise<Locator> {
  const codeField = page.getByLabel(/verification code|sign-in code|^code$/i);
  await expect
    .poll(
      async () => {
        if (await codeField.first().isVisible().catch(() => false)) return 'code';
        // A user with a password gets a method choice; pick the email code option.
        const emailChoice = page
          .getByRole('button', { name: /email|one-time code/i })
          .or(page.getByRole('radio', { name: /email/i }))
          .first();
        if (await emailChoice.isVisible().catch(() => false)) {
          await emailChoice.click({ timeout: 5000 });
          const next = page.getByRole('button', { name: /^(next|continue)$/i }).first();
          if (await next.isVisible().catch(() => false)) await next.click({ timeout: 5000 }).catch(() => {});
        }
        return 'waiting';
      },
      { timeout: 30000, intervals: [500, 1000, 2000] },
    )
    .toBe('code');
  return codeField.first();
}

/**
 * On the managed-login email form: type the E2E address, ask for a code, read it from the mailbox, submit it.
 * `shot` (optional) runs only before the code is typed: once with the address filled (`emailShot`), once on the
 * empty code field (`codeShot`). `email` / `prefix` default to the A10 reviewer (E2E_COGNITO_EMAIL, inbound/); the
 * A10b admin-evidence spec passes E2E_CIS_ADMIN_EMAIL and MAILBOX_ADMIN_PREFIX.
 */
export async function signInByEmailCode(
  page: Page,
  opts: {
    shot?: (name: string) => Promise<void>;
    emailShot?: string;
    codeShot?: string;
    email?: string;
    prefix?: string;
  } = {},
): Promise<void> {
  const email = opts.email ?? E2E_COGNITO_EMAIL;
  const prefix = opts.prefix ?? MAILBOX_PREFIX;
  const emailField = page.getByLabel(/email/i).first();
  await expect(emailField).toBeVisible({ timeout: 15000 });
  await emailField.fill(email);
  if (opts.shot && opts.emailShot) await opts.shot(opts.emailShot);

  // Snapshot the mailbox BEFORE asking for a code, then ask.
  const baseline = await mailboxBaseline(prefix);
  const since = new Date();
  await page.getByRole('button', { name: /^next$/i }).click({ timeout: 10000 });
  const codeField = await reachCodeStep(page);
  if (opts.shot && opts.codeShot) await opts.shot(opts.codeShot);

  // Code from S3 (hard 90 s cap inside the helper).
  const code = await waitForSignInCode({ since, baseline, timeoutMs: 90000, recipient: email, prefix });
  await codeField.fill(code);
  await page.getByRole('button', { name: /^(continue|sign in|next)$/i }).first().click({ timeout: 10000 });
}
