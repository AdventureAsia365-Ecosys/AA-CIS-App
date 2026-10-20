// tests/e2e/cognito/mailbox-parse.spec.ts
// AA-816 (E1) — unit test for the mailbox helper's MIME parsing (no browser, no AWS, no env).
// Fixtures are synthetic messages with fake codes, in the forwarded shape (To = the company address,
// Delivered-To / X-Forwarded-To = the SES mailbox): quoted-printable plain + base64 HTML (multipart, folded
// Content-Type), HTML-only base64 with CSS colours, a stray recipient, and an ambiguous invite mail.
import { test, expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { E2E_COGNITO_EMAIL, extractCode, htmlToText, isAddressedTo, textBodies } from '../helpers/mailbox';

const fixture = (name: string) => fs.readFileSync(path.join(__dirname, 'fixtures', name), 'utf8');

test.describe('AA-816 — mailbox MIME code extractor', () => {
  test('multipart: QP plain part decoded (soft breaks, =2E) and code found', () => {
    const raw = fixture('cognito-qp-b64.eml');
    const bodies = textBodies(raw);
    expect(bodies.length).toBe(2);
    expect(bodies[0]).toContain('expires in 15 minutes.');
    expect(extractCode(raw)).toBe('48213907');
    expect(isAddressedTo(raw, E2E_COGNITO_EMAIL)).toBe(true);
  });

  test('multipart: base64 HTML part decoded, style dropped, same code', () => {
    const raw = fixture('cognito-qp-b64.eml');
    const html = textBodies(raw)[1];
    expect(html).toContain('48213907');
    expect(html).not.toContain('#123456');
    // The subject holds an 8-digit date; headers are never searched.
    expect(extractCode(raw)).not.toBe('20261019');
  });

  test('HTML-only base64: 6-digit code via the single-number fallback, CSS colour ignored', () => {
    const raw = fixture('html-only-b64.eml');
    expect(extractCode(raw)).toBe('731052');
    // Display name + mixed case address still match.
    expect(isAddressedTo(raw, E2E_COGNITO_EMAIL)).toBe(true);
  });

  test('another recipient is not ours', () => {
    expect(isAddressedTo(fixture('other-recipient.eml'), E2E_COGNITO_EMAIL)).toBe(false);
  });

  test('mail sent straight to the SES mailbox (not forwarded from the company address) is not ours', () => {
    // The pre-A10 shape: To = the mailbox itself. The E2E user is now the company address only.
    const raw =
      'From: no-reply@lumiguides.it.com\r\nDelivered-To: e2e@e2e-mail.lumiguides.it.com\r\n' +
      'To: e2e@e2e-mail.lumiguides.it.com\r\nSubject: Sign in\r\nContent-Type: text/plain\r\n\r\n' +
      'Your code is 112233\r\n';
    expect(E2E_COGNITO_EMAIL.endsWith('@adventure.asia')).toBe(true);
    expect(isAddressedTo(raw, E2E_COGNITO_EMAIL)).toBe(false);
  });

  test('ambiguous numbers without a code give null (keep waiting, never guess)', () => {
    const raw = fixture('invite-no-code.eml');
    expect(isAddressedTo(raw, E2E_COGNITO_EMAIL)).toBe(true);
    expect(extractCode(raw)).toBeNull();
  });

  test('htmlToText decodes entities and keeps the code apart from markup', () => {
    expect(htmlToText('<p>Your&nbsp;code&#58; <b>123456</b>&amp;</p>')).toContain('Your code: 123456 &');
  });
});
