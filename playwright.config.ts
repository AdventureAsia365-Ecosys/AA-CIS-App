import { defineConfig, devices } from '@playwright/test';

// AA-732: three projects.
//   • "chromium" — the legacy live-verify specs (everything under tests/e2e except the smoke dir).
//     Unchanged behaviour; it never runs the smoke setup, so legacy runs never log in.
//   • "smoke-setup" — a smoke-only setup project (tests/e2e/smoke/*.setup.ts) that signs the staff smoke
//     account in once and writes tests/e2e/.auth/admin-state.json. AA-816 S2 path: in CI it is the Cognito E2E
//     user (E2ECOGNITO=1, email code from the mailbox), so no retries (a retry asks for another code) and no
//     automatic screenshots or traces (the code page could be captured).
//   • "smoke" — the AUTHENTICATED UI smoke (tests/e2e/smoke/*.spec.ts tagged @staff). It `dependencies` on
//     "smoke-setup", so the sign-in runs once before the page tests, which then load that storageState and each
//     run independently. Runs only on the two Cognito callback hosts (ui-smoke.yml staff job).
//   • "smoke-public" (AA-816 S2 path) — every smoke spec WITHOUT the @staff tag: no session, no setup. This is
//     what a per-PR *.vercel.app preview runs (Cognito has no wildcard callbacks, and the legacy password login
//     is going away). The staff job runs it too.
// AA-885: two more, for the tenant portal app (portal/, its own host):
//   • "smoke-portal-setup" — tests/e2e/smoke-portal/*.setup.ts: the tenant login (API key from
//     E2E_TENANT_API_KEY) once, writing tests/e2e/.auth/tenant-state.json; skips without the key.
//   • "smoke-portal" — tests/e2e/smoke-portal/*.spec.ts against a PORTAL BASE_URL
//     (ui-smoke-portal.yml runs `--project=smoke-portal`).
// AA-816 (E1): one more, for the real Cognito email-code sign-in:
//   • "cognito" — tests/e2e/cognito/*.spec.ts, always against the production admin host (the only
//     registered callback). Out of every other project; the sign-in test itself skips unless E2ECOGNITO=1
//     (set only by .github/workflows/e2e-cognito.yml), so a plain `playwright test` never signs in. The
//     MIME parser unit test in the same folder needs nothing. No retries (a retry asks for another code),
//     no automatic screenshots or traces (the code page could be captured); the spec takes its own.
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60000,
  retries: 1,
  workers: 1, // Sequential — avoid auth conflicts

  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:3001',
    headless: true,
    screenshot: 'only-on-failure',
    video: 'off',
  },

  projects: [
    {
      name: 'chromium',
      testIgnore: /(smoke(-portal)?|cognito)\//,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'smoke-setup',
      testMatch: /smoke\/.*\.setup\.ts$/,
      retries: 0,
      timeout: 180000,
      use: { ...devices['Desktop Chrome'], screenshot: 'off', trace: 'off', video: 'off' },
    },
    {
      name: 'smoke',
      testMatch: /smoke\/.*\.spec\.ts$/,
      grep: /@staff/,
      dependencies: ['smoke-setup'],
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'smoke-public',
      testMatch: /smoke\/.*\.spec\.ts$/,
      grepInvert: /@staff/,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'smoke-portal-setup',
      testMatch: /smoke-portal\/.*\.setup\.ts$/,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'smoke-portal',
      testMatch: /smoke-portal\/.*\.spec\.ts$/,
      dependencies: ['smoke-portal-setup'],
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'cognito',
      testMatch: /cognito\/.*\.spec\.ts$/,
      retries: 0,
      timeout: 180000,
      use: {
        ...devices['Desktop Chrome'],
        baseURL: 'https://aa-cis.lumiguides.it.com',
        screenshot: 'off',
        trace: 'off',
        video: 'off',
      },
    },
  ],

  reporter: [
    ['list'],
    ['html', { outputFolder: 'tests/e2e/results/smoke/playwright-report', open: 'never' }],
    ['json', { outputFile: 'tests/e2e/results/report.json' }],
  ],
});
