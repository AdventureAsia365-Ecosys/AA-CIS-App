import { defineConfig, devices } from '@playwright/test';

// AA-732: two projects.
//   • "chromium" — the legacy live-verify specs (everything under tests/e2e except the smoke dir).
//     Unchanged behaviour; kept so existing specs still run when invoked by name/grep.
//   • "smoke" — the CI UI smoke suite (tests/e2e/smoke/*). CI runs ONLY this project
//     (`--project=smoke`), so the legacy specs are never run against a Vercel preview. The admin
//     login happens once in globalSetup; the smoke project loads that storageState so every page
//     test is independent (no serial mode).
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60000,
  retries: 1,
  workers: 1, // Sequential — avoid auth conflicts

  // Logs the admin in once and writes tests/e2e/results/smoke/admin-state.json (no-op without
  // admin env). Each page test then opens its own context from that state — independent, no serial.
  globalSetup: './tests/e2e/smoke/global-setup.ts',

  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:3001',
    headless: true,
    screenshot: 'only-on-failure',
    video: 'off',
  },

  projects: [
    {
      name: 'chromium',
      testIgnore: /smoke\//,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'smoke',
      testMatch: /smoke\/.*\.spec\.ts$/,
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  reporter: [
    ['list'],
    ['html', { outputFolder: 'tests/e2e/results/smoke/playwright-report', open: 'never' }],
    ['json', { outputFile: 'tests/e2e/results/report.json' }],
  ],
});
