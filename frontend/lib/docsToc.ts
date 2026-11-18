// lib/docsToc.ts — AA-1133 follow-up (S284 live check): the pure part of the docs reader's scroll-spy
// (app/admin/docs/DocToc.tsx). No React here, so node --test can load it (lib/docsToc.test.mjs).
//
// The side table of contents marks the heading the reader is at: the last heading whose top has passed a line near the
// top of the viewport. A heading the reader just went to (a click in the table of contents, a #doc-<slug> link, a
// search hit) stays marked while it is on screen, even when the page cannot scroll it up to that line (a short last
// section); any scroll the reader makes by hand (wheel, touch, keys) ends that.

/** technical-constant: px from the viewport top a heading must pass to count as "being read" (layout, not policy). */
export const ACTIVE_LINE_PX = 96;
/** technical-constant: px above the viewport top a pinned heading may sit and still count as on screen. */
export const PIN_SLACK_PX = 8;

/** Index of the heading the reader is at, from each heading's top (px from the viewport top, in document order;
 *  Infinity for a heading not in the page). `pinned` is the index of the heading the reader went to, or -1. */
export function pickActiveIndex(
  tops: readonly number[],
  opts: { line?: number; viewport: number; pinned?: number },
): number {
  const line = opts.line ?? ACTIVE_LINE_PX;
  const pinned = opts.pinned ?? -1;
  if (pinned >= 0 && pinned < tops.length && isOnScreen(tops[pinned], opts.viewport)) return pinned;
  let current = 0;
  tops.forEach((top, i) => {
    if (top <= line) current = i;
  });
  return current;
}

function isOnScreen(top: number, viewport: number): boolean {
  return Number.isFinite(top) && top >= -PIN_SLACK_PX && top < viewport;
}

/** The heading slug a location hash names (`#doc-<slug>`, URL-encoded or not), or "" when it names none. */
export function slugFromHash(hash: string, prefix: string): string {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  let id = raw;
  try {
    id = decodeURIComponent(raw);
  } catch {
    /* a malformed escape: use the hash as typed */
  }
  return id.startsWith(prefix) ? id.slice(prefix.length) : "";
}

/** The hash that names a heading (`#doc-<slug>`, the slug URL-encoded), the form a shared link carries. */
export function headingHash(slug: string, prefix: string): string {
  return slug ? `#${prefix}${encodeURIComponent(slug)}` : "";
}

/** The heading a fresh page load asks for: the slug its `#doc-<slug>` address names; "" without a location (server
 *  render) or when the hash names no heading. Seeds the reader's jump, since the browser's own fragment scroll runs
 *  before the page text is loaded. */
export function addressHeading(loc: { hash: string } | null | undefined, prefix: string): string {
  return loc ? slugFromHash(loc.hash, prefix) : "";
}
