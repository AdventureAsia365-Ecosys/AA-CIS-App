// portal/app/portal/_components/markdownText.tsx
// AA-836 (D6) — writer output (blog pieces especially) is markdown; a tenant must never see raw
// `##` / `**` in the portal. Two helpers, no dependency, no dangerouslySetInnerHTML (piece text is
// turned into React elements, so it can never inject HTML):
//   - stripMarkdown(text, opts?) → plain text, for one-line / clamped previews (T11 Ready to Publish).
//   - <MarkdownBody text/> → readable rendering for a full body (T10 My Content drawer):
//     headings, paragraphs, bullet / numbered lists, quotes, bold / italic, links as their text.
// Hashtags (`#Bhutan`, no space after `#`) are text, not headings — same rule as the backend's
// services/acp_content_writing/generate.py::strip_markdown.
import { Fragment, type ReactNode } from "react";
import { T, sans } from "./ui";

const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const BULLET_RE = /^\s*[-*+•]\s+(.*)$/;
const ORDERED_RE = /^\s*\d{1,3}[.)]\s+(.*)$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
const RULE_RE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const FENCE_RE = /^\s*```/;

// One inline token per match: image, link, code, bold (** / __), italic (* / _).
// Italic needs a non-word char (or line start) before the opening marker and after the closing
// one, so snake_case words and `2*3*4` stay as they are. Group 1 of the italic branches is that
// leading char (no lookbehind: the tsconfig target is ES2017).
const INLINE_RE =
  /!\[([^\]]*)\]\([^)\s]*\)|\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\*\*([^*]+?)\*\*|__([^_]+?)__|(^|[^\w*])\*([^\s*][^*\n]*?)\*(?![\w*])|(^|[^\w_])_([^\s_][^_\n]*?)_(?![\w_])/g;

// Leftovers of a span the preview cut in half (the T11 `content_preview` is the first 280 RAW
// characters, so the slice can end inside `**bold`, `[text](https://ex` or `` `code ``). Applied
// after INLINE_RE, so everything matched here is an unpaired marker:
//   - a trailing partial link / image `[text`, `[text]`, `[text](partial` → its text;
//   - any `**` / `__` still present (closed pairs are already gone);
//   - a single `*` / `_` / `` ` `` that opens a word (start or after a space) and never closes,
//     and a lone marker run at the very end. `2 * 3` and snake_case stay: no word follows / no
//     space precedes.
const CUT_LINK_RE = /!?\[([^\]\n]*)(?:\](?:\([^)\s]*)?)?$/;
const CUT_STRONG_RE = /\*\*|__/g;
const CUT_OPENER_RE = /(^|\s)[*_`]+(?=\S)/g;
const CUT_TAIL_RE = /\s[*_`]+$/;
const BARE_HEADING_RE = /^\s{0,3}#{1,6}\s*$/;

export type StripMarkdownOptions = {
  /** Drop the first block when it is a heading whose text matches this string (compared on
   * letters and digits only, case-insensitive). A card that already shows that same title would
   * otherwise repeat it. A heading that differs (e.g. the writer's post title under the angle name
   * on a T11 card) is kept — it is what goes live. */
  dropHeadingMatching?: string | null;
  /** Joins two blocks (a heading, or text separated by a blank line) when the first one does not
   * already end in sentence punctuation. Default " " — one run of text, the original behaviour. */
  blockSeparator?: string;
};

// A block that already ends a sentence needs no separator, only a space. Tested on the block's
// plain text (inline markup removed), never on raw markdown: `[our site](https://x)` ends in a
// `)` that is not punctuation, and `**Done.**` ends in a `.` once the markers are gone.
const TERMINAL_RE = /[.!?…:;)"'”’]$/;
const TRAILING_MARKER_RE = /[*_`]+$/;

/** Inline markdown → its text (images / links as their text, code / bold / italic unwrapped). */
function inlineToText(s: string): string {
  INLINE_RE.lastIndex = 0;
  const out = s.replace(INLINE_RE, (_m, img, linkText, _url, code, b1, b2, p1, i1, p2, i2) =>
    img ?? linkText ?? code ?? b1 ?? b2 ?? (i1 !== undefined ? `${p1}${i1}` : `${p2}${i2}`),
  );
  INLINE_RE.lastIndex = 0;
  return out;
}

/** Lower-case, whitespace and punctuation removed — for "is this heading the same as the card
 * title". (No `\p{L}` + `u` flag: the tsconfig target is ES2017.) */
const NOT_TITLE_CHAR_RE = /[\s!-/:-@[-`{-~\u00a0-\u00bf\u2000-\u206f]+/g;
function normTitle(s: string): string {
  return inlineToText(s).toLowerCase().replace(NOT_TITLE_CHAR_RE, "");
}

/** Markdown → plain text: markers removed, links / images kept as their text, lines joined.
 * Also safe on a truncated slice: markers left unpaired by the cut are dropped too.
 * Without options every line is joined with a space (unchanged). S241 — previews pass
 * `{ blockSeparator }` (and `dropHeadingMatching` when the card already shows that exact title)
 * so a heading never runs into the next sentence. */
export function stripMarkdown(text: string | null | undefined, opts: StripMarkdownOptions = {}): string {
  if (!text) return "";
  const sep = opts.blockSeparator ?? " ";
  // Blocks: a heading line is one block; other lines group until a blank line. Lines inside a
  // block (list items, a hard-wrapped paragraph) are joined with a space as before.
  const blocks: { heading: boolean; text: string }[] = [];
  let para: string[] = [];
  const flush = () => {
    if (para.length) blocks.push({ heading: false, text: para.join(" ") });
    para = [];
  };
  for (const l of text.split(/\r?\n/)) {
    if (FENCE_RE.test(l) || RULE_RE.test(l) || BARE_HEADING_RE.test(l)) continue;
    if (!l.trim()) { flush(); continue; }
    const h = HEADING_RE.exec(l);
    if (h) { flush(); blocks.push({ heading: true, text: h[2] }); continue; }
    const b = BULLET_RE.exec(l) ?? ORDERED_RE.exec(l) ?? QUOTE_RE.exec(l);
    para.push(b ? b[1] : l);
  }
  flush();
  if (opts.dropHeadingMatching && blocks[0]?.heading) {
    const want = normTitle(opts.dropHeadingMatching);
    if (want && normTitle(blocks[0].text) === want) blocks.shift();
  }
  // Inline markup is removed per block BEFORE the join, so the sentence-end test sees plain text.
  const joined = blocks.reduce((acc, blk, i) => {
    const t = inlineToText(blk.text);
    if (i === 0) return t;
    const end = acc.trimEnd().replace(TRAILING_MARKER_RE, "");
    return acc + (TERMINAL_RE.test(end) ? " " : sep) + t;
  }, "");
  return joined
    .replace(/\s+/g, " ")
    .trim()
    .replace(CUT_LINK_RE, "$1")
    .replace(CUT_STRONG_RE, "")
    .replace(CUT_OPENER_RE, "$1")
    .replace(CUT_TAIL_RE, "")
    .trim();
}

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  INLINE_RE.lastIndex = 0;
  for (let m = INLINE_RE.exec(text); m; m = INLINE_RE.exec(text)) {
    const [whole, img, linkText, , code, b1, b2, p1, i1, p2, i2] = m;
    if (m.index > last) out.push(text.slice(last, m.index));
    const key = `${keyPrefix}-${n++}`;
    if (img !== undefined) out.push(img);
    else if (linkText !== undefined) out.push(<span key={key} style={{ textDecoration: "underline" }}>{linkText}</span>);
    else if (code !== undefined) out.push(code);
    else if (b1 !== undefined || b2 !== undefined) out.push(<strong key={key}>{b1 ?? b2}</strong>);
    else if (i1 !== undefined) out.push(p1, <em key={key}>{i1}</em>);
    else out.push(p2, <em key={key}>{i2}</em>);
    last = m.index + whole.length;
  }
  if (last < text.length) out.push(text.slice(last));
  INLINE_RE.lastIndex = 0;
  return out;
}

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "para"; lines: string[] }
  | { kind: "quote"; lines: string[] }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "rule" };

function toBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  let cur: Block | null = null;
  const flush = () => {
    if (cur) blocks.push(cur);
    cur = null;
  };
  for (const raw of text.split(/\r?\n/)) {
    if (FENCE_RE.test(raw)) continue; // fences dropped; the code lines below stay as text
    if (!raw.trim()) {
      flush();
      continue;
    }
    if (RULE_RE.test(raw)) {
      flush();
      blocks.push({ kind: "rule" });
      continue;
    }
    const h = HEADING_RE.exec(raw);
    if (h) {
      flush();
      blocks.push({ kind: "heading", level: h[1].length, text: h[2] });
      continue;
    }
    const b = BULLET_RE.exec(raw);
    const o = b ? null : ORDERED_RE.exec(raw);
    if (b || o) {
      const kind = b ? "ul" : "ol";
      const item = (b ?? o)![1];
      const c = cur as Block | null;
      if (c && c.kind === kind) c.items.push(item);
      else {
        flush();
        cur = { kind, items: [item] };
      }
      continue;
    }
    const q = QUOTE_RE.exec(raw);
    if (q) {
      const c = cur as Block | null;
      if (c && c.kind === "quote") c.lines.push(q[1]);
      else {
        flush();
        cur = { kind: "quote", lines: [q[1]] };
      }
      continue;
    }
    const c = cur as Block | null;
    if (c && c.kind === "para") c.lines.push(raw);
    else {
      flush();
      cur = { kind: "para", lines: [raw] };
    }
  }
  flush();
  return blocks;
}

function lines(ls: string[], key: string): ReactNode[] {
  return ls.map((l, i) => (
    <Fragment key={`${key}-${i}`}>
      {i > 0 && <br />}
      {renderInline(l, `${key}-${i}`)}
    </Fragment>
  ));
}

const HEADING_SIZE: Record<number, number> = { 1: 18, 2: 16, 3: 14.5, 4: 13.5, 5: 13.5, 6: 13.5 };

/** Read view of a markdown body. Inherits font size / colour from its container. */
export function MarkdownBody({ text }: { text: string | null | undefined }) {
  const blocks = toBlocks(text ?? "");
  return (
    <div style={{ fontFamily: sans, overflowWrap: "anywhere" }}>
      {blocks.map((b, i) => {
        const key = `b${i}`;
        const mt = i === 0 ? 0 : undefined;
        switch (b.kind) {
          case "heading":
            return (
              <div key={key} role="heading" aria-level={b.level} style={{ fontSize: HEADING_SIZE[b.level], fontWeight: 700, color: T.ink, lineHeight: 1.35, margin: `${mt ?? 16}px 0 6px` }}>
                {renderInline(b.text, key)}
              </div>
            );
          case "ul":
          case "ol": {
            const List = b.kind;
            return (
              <List key={key} style={{ margin: `${mt ?? 8}px 0 8px`, paddingLeft: 20 }}>
                {b.items.map((it, j) => (
                  <li key={`${key}-${j}`} style={{ marginBottom: 3 }}>{renderInline(it, `${key}-${j}`)}</li>
                ))}
              </List>
            );
          }
          case "quote":
            return (
              <div key={key} style={{ margin: `${mt ?? 8}px 0 8px`, padding: "2px 0 2px 12px", borderLeft: `3px solid ${T.line}`, color: T.muted }}>
                {lines(b.lines, key)}
              </div>
            );
          case "rule":
            return <hr key={key} style={{ border: "none", borderTop: `1px solid ${T.line}`, margin: "14px 0" }} />;
          default:
            return (
              <p key={key} style={{ margin: `${mt ?? 8}px 0 8px` }}>
                {lines(b.lines, key)}
              </p>
            );
        }
      })}
    </div>
  );
}
