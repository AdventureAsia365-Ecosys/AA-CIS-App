// tests/e2e/helpers/mailbox.ts
// AA-816 (E1) — read the Cognito sign-in code for the E2E staff user from the SES inbound mailbox.
//
// Mail path (AA-CIS-Infra accounts/aa365/e2e_mailbox.tf, AA-816 I5): Cognito emails the code straight to the
// E2E staff user's own address e2e@e2e-mail.lumiguides.it.com (S243 option B: a TEMPORARY exception to the
// @adventure.asia-only staff rule, root ADR 0006a; no forwarder) → SES receipt rule `e2e-cognito` →
// s3://aa-cis-dev-e2e-mail-<acc2>/inbound/<message id> (1-day expiry).
//
// The runner's AWS CLI does the reads (no new npm dependency): `s3api list-objects-v2` + `s3api get-object`,
// via execFile (fixed argv, no shell). In CI the credentials are the read-only role
// aa-cis-dev-e2e-mail-reader (configure-aws-credentials); it has NO delete permission, so this helper never
// deletes — the bucket lifecycle does. The code is a live credential: never log or print it.
//
// AA-816 A10b (S252, root ADR 0006a A10b): the Dev-only E2E CIS admin e2e-cis-admin@ gets its codes through its own
// rule `e2e-cognito-admin` into inbound-admin/, which ONLY the MFA admin role can read (bucket policy Deny for every
// other principal, the CI reader role included). The list / baseline / wait helpers take that prefix; the defaults
// keep the inbound/ behaviour. Used only by the agent-run Playwright project admin-evidence (never CI).
import { execFile } from 'child_process';
import { mkdtemp, readFile, rm } from 'fs/promises';
import os from 'os';
import path from 'path';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);

// The bucket name embeds the account ID; it is read from the runner's credentials (STS GetCallerIdentity,
// no permission needed) instead of being written here — this repo is public (AA-902).
export const MAILBOX_BUCKET_PREFIX = 'aa-cis-dev-e2e-mail-';
export const MAILBOX_PREFIX = 'inbound/';
export const MAILBOX_REGION = 'us-west-1';
// Must equal api/services/staff_directory.py E2E_TEST_STAFF_EMAIL (the only non-company staff address).
export const E2E_COGNITO_EMAIL = 'e2e@e2e-mail.lumiguides.it.com';
// A10b: must equal api/core/e2e_staff.py E2E_CIS_ADMIN_TEST_STAFF_EMAIL; its codes land under MAILBOX_ADMIN_PREFIX.
export const E2E_CIS_ADMIN_EMAIL = 'e2e-cis-admin@e2e-mail.lumiguides.it.com';
export const MAILBOX_ADMIN_PREFIX = 'inbound-admin/';

/** Each AWS CLI call has its own cap; the whole wait has another (lesson: every wait is bounded). */
const CLI_TIMEOUT_MS = 20_000;
const DEFAULT_WAIT_MS = 90_000;
const POLL_INTERVAL_MS = 3_000;
/** S3 LastModified (server clock) vs. the runner clock. The baseline key set is the real guard. */
const CLOCK_SKEW_MS = 60_000;
/** A Cognito code mail is a few KB; anything this large is not one. */
const MAX_OBJECT_BYTES = 1_000_000;

// Headers that can carry the recipient (SES stores the message as received; the extra ones matter only if a
// forwarder is ever put in front of the mailbox again).
const RECIPIENT_HEADERS = ['to', 'cc', 'delivered-to', 'x-original-to', 'x-forwarded-to', 'envelope-to'];

export type MailObject = { key: string; lastModified: Date; size: number };

// ── AWS CLI ───────────────────────────────────────────────────────────────────

async function aws(args: string[]): Promise<string> {
  try {
    const { stdout } = await execFileAsync('aws', [...args, '--region', MAILBOX_REGION, '--output', 'json'], {
      timeout: CLI_TIMEOUT_MS,
      maxBuffer: 16 * 1024 * 1024,
    });
    return stdout;
  } catch (err) {
    // execFile's message carries stderr (AccessDenied, ExpiredToken, …) but never a message body.
    const e = err as { message?: string; stderr?: string };
    throw new Error(`aws ${args.slice(0, 2).join(' ')} failed: ${(e.stderr || e.message || String(err)).trim()}`);
  }
}

let mailboxBucketName: Promise<string> | undefined;

/** aa-cis-dev-e2e-mail-<account of the credentials in use> (the mailbox role lives in that account). */
export function mailboxBucket(): Promise<string> {
  mailboxBucketName ??= aws(['sts', 'get-caller-identity']).then((out) => {
    const account = String((JSON.parse(out) as { Account?: string }).Account ?? '');
    if (!/^\d{12}$/.test(account)) throw new Error('sts get-caller-identity returned no account');
    return `${MAILBOX_BUCKET_PREFIX}${account}`;
  }).catch((err: unknown) => {
    mailboxBucketName = undefined; // a failed lookup is retried on the next poll
    throw err;
  });
  return mailboxBucketName;
}

/** Every object under `prefix` (default inbound/; the CLI paginates). */
export async function listMailObjects(prefix: string = MAILBOX_PREFIX): Promise<MailObject[]> {
  const bucket = await mailboxBucket();
  const out = await aws(['s3api', 'list-objects-v2', '--bucket', bucket, '--prefix', prefix]);
  const parsed = (out.trim() ? JSON.parse(out) : {}) as {
    Contents?: { Key: string; LastModified: string; Size: number }[];
  };
  return (parsed.Contents ?? []).map((o) => ({ key: o.Key, lastModified: new Date(o.LastModified), size: o.Size }));
}

/** Download one message to a private temp dir, read it, remove the local copy. */
export async function readMailObject(key: string): Promise<string> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'aa816-mail-'));
  const file = path.join(dir, 'message.eml');
  try {
    await aws(['s3api', 'get-object', '--bucket', await mailboxBucket(), '--key', key, file]);
    return await readFile(file, 'utf8');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

// ── MIME (just enough for a Cognito code mail) ────────────────────────────────

type Headers = Map<string, string[]>;

function splitHeadBody(raw: string): { head: string; body: string } {
  const m = /\r?\n\r?\n/.exec(raw);
  if (!m) return { head: raw, body: '' };
  return { head: raw.slice(0, m.index), body: raw.slice(m.index + m[0].length) };
}

export function parseHeaders(head: string): Headers {
  const headers: Headers = new Map();
  const unfolded = head.replace(/\r?\n[ \t]+/g, ' ');
  for (const line of unfolded.split(/\r?\n/)) {
    const i = line.indexOf(':');
    if (i <= 0) continue;
    const name = line.slice(0, i).trim().toLowerCase();
    const value = line.slice(i + 1).trim();
    headers.set(name, [...(headers.get(name) ?? []), value]);
  }
  return headers;
}

function first(headers: Headers, name: string): string {
  return headers.get(name)?.[0] ?? '';
}

function param(headerValue: string, name: string): string | undefined {
  const m = new RegExp(`(?:^|;)\\s*${name}\\s*=\\s*(?:"([^"]*)"|([^;\\s]+))`, 'i').exec(headerValue);
  return m ? (m[1] ?? m[2]) : undefined;
}

function decodeQuotedPrintable(text: string): Buffer {
  const soft = text.replace(/=\r?\n/g, '');
  const bytes: number[] = [];
  for (let i = 0; i < soft.length; i++) {
    const c = soft[i];
    if (c === '=' && /^[0-9A-Fa-f]{2}$/.test(soft.slice(i + 1, i + 3))) {
      bytes.push(parseInt(soft.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      // Raw 8-bit text in a QP part is invalid but harmless: keep its UTF-8 bytes.
      bytes.push(...Buffer.from(c, 'utf8'));
    }
  }
  return Buffer.from(bytes);
}

function decodeBody(body: string, transferEncoding: string, charset: string | undefined): string {
  const cte = transferEncoding.toLowerCase();
  let buf: Buffer;
  if (cte === 'base64') buf = Buffer.from(body.replace(/\s+/g, ''), 'base64');
  else if (cte === 'quoted-printable') buf = decodeQuotedPrintable(body);
  else return body;
  const cs = (charset ?? 'utf-8').toLowerCase();
  return buf.toString(cs === 'iso-8859-1' || cs === 'latin1' || cs === 'us-ascii' ? 'latin1' : 'utf8');
}

const ENTITIES: Record<string, string> = { nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", '#39': "'" };

export function htmlToText(html: string): string {
  return html
    .replace(/<(style|script|head)\b[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<br\s*\/?>|<\/(p|div|tr|td|h\d|li)>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&(#\d+|#x[0-9a-f]+|[a-z]+);/gi, (whole, ent: string) => {
      const e = ent.toLowerCase();
      if (e in ENTITIES) return ENTITIES[e];
      const cp = e.startsWith('#x') ? parseInt(e.slice(2), 16) : e.startsWith('#') ? parseInt(e.slice(1), 10) : NaN;
      return cp >= 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : whole;
    })
    .replace(/[ \t ]+/g, ' ');
}

/** Decoded text bodies, plain parts first, HTML parts (as text) after. Recurses into multipart/*. */
export function textBodies(raw: string): string[] {
  const plain: string[] = [];
  const html: string[] = [];
  const walk = (part: string, depth: number) => {
    if (depth > 8) return;
    const { head, body } = splitHeadBody(part);
    const headers = parseHeaders(head);
    const contentType = first(headers, 'content-type') || 'text/plain';
    const type = contentType.split(';')[0].trim().toLowerCase();
    if (type.startsWith('multipart/')) {
      const boundary = param(contentType, 'boundary');
      if (!boundary) return;
      const delimiter = `--${boundary}`;
      const chunks = body.split(delimiter).slice(1); // [0] is the preamble
      for (const chunk of chunks) {
        if (chunk.startsWith('--')) break; // closing delimiter
        walk(chunk.replace(/^[ \t]*\r?\n/, ''), depth + 1);
      }
      return;
    }
    if (type === 'message/rfc822') {
      walk(body, depth + 1);
      return;
    }
    if (!type.startsWith('text/')) return;
    const text = decodeBody(body, first(headers, 'content-transfer-encoding') || '7bit', param(contentType, 'charset'));
    if (type === 'text/html') html.push(htmlToText(text));
    else plain.push(text);
  };
  walk(raw, 0);
  return [...plain, ...html];
}

/** True when any recipient-carrying header of the top-level message names `address`. */
export function isAddressedTo(raw: string, address: string): boolean {
  const headers = parseHeaders(splitHeadBody(raw).head);
  const needle = address.toLowerCase();
  return RECIPIENT_HEADERS.some((h) => (headers.get(h) ?? []).some((v) => v.toLowerCase().includes(needle)));
}

/**
 * The 6–8 digit sign-in code in a raw MIME message, or null. Bodies only (never headers). First a number
 * shortly after the word "code"; otherwise the single distinct standalone 6–8 digit number (a `#` before it
 * marks a CSS colour, not a code). Null when ambiguous, so the caller keeps waiting instead of guessing.
 */
export function extractCode(raw: string): string | null {
  const bodies = textBodies(raw);
  for (const text of bodies) {
    const m = /\bcode\b[^0-9]{0,80}?(?<![\d#])(\d{6,8})(?!\d)/i.exec(text);
    if (m) return m[1];
  }
  for (const text of bodies) {
    const all = new Set([...text.matchAll(/(?<![\d#\w])(\d{6,8})(?![\d\w])/g)].map((x) => x[1]));
    if (all.size === 1) return [...all][0];
  }
  return null;
}

// ── Wait for the code ────────────────────────────────────────────────────────

export type CodeWait = {
  /** When the test asked Cognito for a code (runner clock). */
  since: Date;
  /** Keys already in inbound/ before the code was requested; never read again. */
  baseline: Set<string>;
  recipient?: string;
  timeoutMs?: number;
  /** The mailbox prefix to poll (default inbound/; the A10b admin uses MAILBOX_ADMIN_PREFIX). */
  prefix?: string;
};

/** Snapshot of the keys under `prefix` (default inbound/), taken BEFORE the code is requested. */
export async function mailboxBaseline(prefix: string = MAILBOX_PREFIX): Promise<Set<string>> {
  return new Set((await listMailObjects(prefix)).map((o) => o.key));
}

/**
 * Poll inbound/ until a NEW message (not in the baseline, written after `since`) to the E2E address yields
 * a code. Newest first. Hard cap `timeoutMs` (default 90 s). Throws a message without any mail content.
 */
export async function waitForSignInCode(opts: CodeWait): Promise<string> {
  const recipient = opts.recipient ?? E2E_COGNITO_EMAIL;
  const prefix = opts.prefix ?? MAILBOX_PREFIX;
  const deadline = Date.now() + (opts.timeoutMs ?? DEFAULT_WAIT_MS);
  const notBefore = opts.since.getTime() - CLOCK_SKEW_MS;
  const checked = new Set<string>(opts.baseline);
  let polls = 0;
  let lastError = '';
  while (Date.now() < deadline) {
    polls++;
    try {
      const fresh = (await listMailObjects(prefix))
        .filter((o) => !checked.has(o.key) && o.lastModified.getTime() >= notBefore && o.size <= MAX_OBJECT_BYTES)
        .sort((a, b) => b.lastModified.getTime() - a.lastModified.getTime());
      for (const obj of fresh) {
        const raw = await readMailObject(obj.key);
        // A message is judged once: not ours, or ours without a code, stays skipped.
        checked.add(obj.key);
        if (!isAddressedTo(raw, recipient)) continue;
        const code = extractCode(raw);
        if (code) return code;
      }
      lastError = '';
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
    }
    const left = deadline - Date.now();
    if (left <= 0) break;
    await new Promise((r) => setTimeout(r, Math.min(POLL_INTERVAL_MS, left)));
  }
  throw new Error(
    `No sign-in code for ${recipient} in s3://${MAILBOX_BUCKET_PREFIX}<account>/${prefix} within ` +
      `${Math.round((opts.timeoutMs ?? DEFAULT_WAIT_MS) / 1000)} s (${polls} polls, ${checked.size - opts.baseline.size} ` +
      `new message(s) checked)${lastError ? `; last error: ${lastError}` : ''}. Check that the E2E staff user ` +
      `exists with this address and the SES receipt rule ` +
      `${prefix === MAILBOX_ADMIN_PREFIX ? 'e2e-cognito-admin' : 'e2e-cognito'} is active.`,
  );
}
