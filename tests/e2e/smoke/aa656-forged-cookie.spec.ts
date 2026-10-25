// tests/e2e/smoke/aa656-forged-cookie.spec.ts
// AA-656 — a forged session is rejected on every protected page and every BFF proxy route.
// Runs in the "smoke" project against the Vercel preview, WITHOUT the admin storage state: each
// request carries only hand-made cookies. The page list is lib/rbac.ts PAGE_RULES itself, so a new
// page added to the matrix is covered automatically.
//   • pages  → a redirect to /login, never a 200 (AA-885: the portal is its own app; its forged-cookie
//     checks are in AA-Partner e2e/smoke-portal/aa885-portal-host.spec.ts since AA-979);
//   • proxies → 401/403, never a forwarded backend answer.
// AA-816 (C4): cis_admin_token can now hold a Cognito access token (RS256, has `iss`). The forged
// RS256 cases below are signed with a key made up on the spot but carry every claim a real WORKFORCE
// access token has (issuer, client_id, token_use, cis-admin group, email), so only the signature is
// wrong. An expired one is sent to the refresh route first (middleware), which also lands on /login.
import { generateKeyPairSync, createSign, randomUUID } from 'node:crypto';
import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import { PAGE_RULES } from '../../../frontend/lib/rbac';
import { baseUrl } from './smoke-helpers';

const WORKFORCE_ISSUER = 'https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS';
const CIS_ADMIN_CLIENT_ID = '3oqkumijh04cp81k3nlnub6qtv';

function b64url(input: Buffer | string): string {
  return Buffer.from(input).toString('base64url');
}

/** An RS256 JWT shaped like a WORKFORCE access token, signed with a throwaway (attacker) key. */
function forgedCognitoToken(expOffsetS: number): string {
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: 'RS256', typ: 'JWT', kid: 'forged-kid' };
  const payload = {
    iss: WORKFORCE_ISSUER,
    sub: randomUUID(),
    client_id: CIS_ADMIN_CLIENT_ID,
    token_use: 'access',
    scope: 'openid profile email',
    'cognito:groups': ['cis-admin'],
    email: 'attacker@adventure.asia',
    username: randomUUID(),
    iat: now - 60,
    auth_time: now - 60,
    exp: now + expOffsetS,
    jti: randomUUID(),
  };
  const signingInput = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(payload))}`;
  const signature = createSign('RSA-SHA256').update(signingInput).sign(privateKey);
  return `${signingInput}.${b64url(signature)}`;
}

const FORGED: Record<string, string> = {
  'role cookie only (admin)': 'cis_role=admin',
  'role cookie only (content)': 'cis_role=content',
  'role cookie only (reviewer)': 'cis_role=reviewer',
  'garbage admin token': 'cis_role=admin; cis_admin_token=not-a-jwt',
  'garbage tenant token': 'cis_role=tenant; cis_tenant_token=not-a-jwt',
  'forged RS256 Cognito token': `cis_role=admin; cis_admin_token=${forgedCognitoToken(600)}`,
  'forged RS256 Cognito token (expired)': `cis_role=admin; cis_admin_token=${forgedCognitoToken(-600)}`,
};

const PROXY_ROUTES: { method: 'GET' | 'POST'; path: string }[] = [
  { method: 'GET', path: '/api/admin/jobs' },
  { method: 'POST', path: '/api/admin/waves/check' },
  { method: 'GET', path: '/api/pipeline/status' },
  { method: 'GET', path: '/api/tour-full/00000000-0000-0000-0000-000000000000' },
  { method: 'GET', path: '/api/tenant/pipeline/brand-identity' },
  { method: 'GET', path: '/api/tenant/admin/tenants' },
  // AA-885: /api/playground/proxy moved to the portal app (404 here, aa885-portal-redirect.spec.ts).
];

async function contextWith(cookie: string): Promise<APIRequestContext> {
  const headers: Record<string, string> = { cookie };
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

test.describe('AA-656 — forged staff/tenant sessions are rejected', () => {
  for (const [label, cookie] of Object.entries(FORGED)) {
    test(`pages: ${label}`, async () => {
      const ctx = await contextWith(cookie);
      const leaks: string[] = [];
      try {
        for (const rule of PAGE_RULES) {
          const res = await ctx.get(rule.prefix, { maxRedirects: 0, timeout: 15000 });
          const location = res.headers()['location'] ?? '';
          // A garbage admin token on /portal lands on /login (staff login), a tenant one on
          // /tenant-login — either is a rejection. AA-816: an expired Cognito token goes to the
          // refresh route first; with no refresh cookie that route answers with /login (checked below).
          const toLogin = /\/(tenant-)?login(\?|$)/.test(new URL(location || '/', baseUrl()).pathname + '?');
          const toRefresh = new URL(location || '/', baseUrl()).pathname === '/api/auth/refresh';
          if (res.status() >= 300 && res.status() < 400 && toRefresh) {
            const next = await ctx.get(location, { maxRedirects: 0, timeout: 15000 });
            const nextLoc = next.headers()['location'] ?? '';
            const refusedToLogin =
              next.status() >= 300 && next.status() < 400 && new URL(nextLoc || '/', baseUrl()).pathname === '/login';
            if (!refusedToLogin) leaks.push(`${rule.prefix} → refresh → ${next.status()} ${nextLoc}`);
            continue;
          }
          const redirected = res.status() >= 300 && res.status() < 400 && toLogin;
          if (!redirected) leaks.push(`${rule.prefix} → ${res.status()} ${location}`);
        }
      } finally {
        await ctx.dispose();
      }
      expect(leaks, `pages reachable with a forged session (${label}):\n${leaks.join('\n')}`).toEqual([]);
    });

    test(`proxies: ${label}`, async () => {
      const ctx = await contextWith(cookie);
      const leaks: string[] = [];
      try {
        for (const r of PROXY_ROUTES) {
          const res =
            r.method === 'GET'
              ? await ctx.get(r.path, { maxRedirects: 0, timeout: 15000 })
              : await ctx.post(r.path, { maxRedirects: 0, timeout: 15000, data: { endpoint: '/v1/tours/my-versions' } });
          if (![401, 403].includes(res.status())) leaks.push(`${r.method} ${r.path} → ${res.status()}`);
        }
      } finally {
        await ctx.dispose();
      }
      expect(leaks, `proxy routes answered a forged session (${label}):\n${leaks.join('\n')}`).toEqual([]);
    });
  }
});
