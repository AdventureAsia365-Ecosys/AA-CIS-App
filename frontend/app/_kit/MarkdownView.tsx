"use client";
// app/_kit/MarkdownView.tsx — AA-898: the one Markdown renderer of the CIS admin (CIS-local kit file, not core).
//
// First user: the docs reader /admin/docs. Safe by construction:
//   - raw HTML in the Markdown is dropped (skipHtml), never rendered; there is no rehype-raw;
//   - every link goes through `resolveHref` (the page decides: another published page, an in-page anchor, an
//     https / mailto link opened in a new tab with noopener, or plain text); react-markdown's defaultUrlTransform
//     still strips javascript: / data: URLs first;
//   - images are not loaded (a remote image would leak the viewer's IP): the alt text is shown instead, except an
//     image the page resolves with `resolveImage` (AA-697: an SVG of the docs bundle, served by the same-origin BFF
//     after a safety check), drawn with <img> so the SVG can neither run script nor fetch anything;
//   - GitHub-flavoured Markdown (tables, task lists, strikethrough, autolinks) via remark-gfm. ```mermaid blocks are
//     drawn by MermaidDiagram (lazy-loaded, strict mode, SVG sanitised; the labelled source on any error);
//   - GitHub alerts ("> [!NOTE]", [!TIP], [!IMPORTANT], [!WARNING], [!CAUTION]) are callouts with a label
//     (lib/markdownCallouts.ts, a remark plugin: the content stays ordinary Markdown).
// Heading ids come from `slugForLine` (the page builds them from the same text as its table of contents), prefixed
// with "doc-" so they never collide with ids of the shell.

import React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { K, RADIUS, mono, sans } from "./core/tokens";
import { MermaidDiagram } from "./MermaidDiagram";
import { CALLOUT_LABEL, calloutKindOf, remarkCallouts, type CalloutKind } from "@/lib/markdownCallouts";

export type MarkdownLink =
  | { kind: "internal"; href: string; onNavigate: () => void }
  | { kind: "anchor"; id: string }
  | { kind: "external"; href: string }
  | { kind: "text" };

export const HEADING_ID_PREFIX = "doc-";

const CSS = `
.aa-md { color: ${K.ink}; font-family: ${sans}; font-size: 14px; line-height: 1.65; overflow-wrap: anywhere; }
.aa-md > :first-child { margin-top: 0; }
.aa-md h1, .aa-md h2, .aa-md h3, .aa-md h4, .aa-md h5, .aa-md h6 { color: ${K.ink}; font-weight: 700; line-height: 1.3;
  margin: 1.6em 0 0.6em; scroll-margin-top: 16px; }
.aa-md h1 { font-size: 24px; }
.aa-md h2 { font-size: 19px; padding-bottom: 6px; border-bottom: 1px solid ${K.line}; }
.aa-md h3 { font-size: 16px; }
.aa-md h4, .aa-md h5, .aa-md h6 { font-size: 14px; }
.aa-md p { margin: 0.7em 0; }
.aa-md ul, .aa-md ol { margin: 0.6em 0; padding-left: 1.5em; }
.aa-md ul { list-style: disc; }
.aa-md ol { list-style: decimal; }
.aa-md li { margin: 0.25em 0; }
.aa-md li > ul, .aa-md li > ol { margin: 0.2em 0; }
.aa-md ul.contains-task-list { list-style: none; padding-left: 0.4em; }
.aa-md a { color: ${K.accentDeep}; text-decoration: underline; text-underline-offset: 2px; }
.aa-md .aa-md-deadlink { color: ${K.ink2}; border-bottom: 1px dotted ${K.muted2}; }
.aa-md strong { font-weight: 700; }
.aa-md blockquote { margin: 0.8em 0; padding: 4px 14px; border-left: 3px solid ${K.accentBorder}; color: ${K.ink2};
  background: ${K.accentTint}; border-radius: ${RADIUS.sm}px; }
.aa-md blockquote[data-callout] { color: ${K.ink}; padding: 8px 14px; }
.aa-md blockquote[data-callout="note"] { border-left-color: ${K.info}; background: ${K.infoSoft}; }
.aa-md blockquote[data-callout="tip"] { border-left-color: ${K.success}; background: ${K.successSoft}; }
.aa-md blockquote[data-callout="important"] { border-left-color: ${K.accent}; background: ${K.accentTint}; }
.aa-md blockquote[data-callout="warning"] { border-left-color: ${K.amber}; background: ${K.amberSoft}; }
.aa-md blockquote[data-callout="caution"] { border-left-color: ${K.danger}; background: ${K.dangerTint}; }
.aa-md .aa-md-callout-title { display: block; font-size: 12px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; margin: 2px 0 2px; }
.aa-md blockquote[data-callout="note"] .aa-md-callout-title { color: ${K.info}; }
.aa-md blockquote[data-callout="tip"] .aa-md-callout-title { color: ${K.success}; }
.aa-md blockquote[data-callout="important"] .aa-md-callout-title { color: ${K.accentDeep}; }
.aa-md blockquote[data-callout="warning"] .aa-md-callout-title { color: ${K.amber}; }
.aa-md blockquote[data-callout="caution"] .aa-md-callout-title { color: ${K.danger}; }
.aa-md hr { border: none; border-top: 1px solid ${K.line}; margin: 1.4em 0; }
.aa-md code { font-family: ${mono}; font-size: 12.5px; background: ${K.neutralBg}; padding: 1px 5px;
  border-radius: ${RADIUS.sm}px; }
.aa-md pre { margin: 0.8em 0; padding: 12px 14px; background: ${K.neutralBg}; border: 1px solid ${K.line};
  border-radius: ${RADIUS.md}px; overflow-x: auto; overflow-wrap: normal; }
.aa-md pre code { background: none; padding: 0; font-size: 12.5px; white-space: pre; }
.aa-md .aa-md-label { display: block; font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
  text-transform: uppercase; color: ${K.muted}; margin-bottom: 6px; font-family: ${sans}; }
.aa-md .aa-md-table { margin: 0.8em 0; overflow-x: auto; border: 1px solid ${K.line}; border-radius: ${RADIUS.md}px; }
.aa-md table { border-collapse: collapse; width: 100%; font-size: 13px; overflow-wrap: normal; }
.aa-md th, .aa-md td { border-bottom: 1px solid ${K.line}; padding: 6px 10px; text-align: left; vertical-align: top;
  min-width: 80px; max-width: 520px; overflow-wrap: anywhere; }
.aa-md th { background: ${K.neutralBg}; font-weight: 700; color: ${K.ink}; }
.aa-md tr:last-child td { border-bottom: none; }
.aa-md .aa-md-img { color: ${K.muted}; font-style: italic; }
/* The diagrams are drawn on white (Graphviz canvas), so the frame stays white in dark mode too. */
.aa-md .aa-md-figure { display: block; margin: 12px 0; padding: 8px; background: #fff; border: 1px solid ${K.line};
  border-radius: ${RADIUS.md}px; overflow-x: auto; }
.aa-md .aa-md-figure img { display: block; max-width: 100%; height: auto; margin: 0 auto; }
.aa-md input[type="checkbox"] { margin-right: 6px; }
`;

const REMARK_PLUGINS = [remarkGfm, remarkCallouts];

type Props = {
  markdown: string;
  /** How to render a link found in the text (the page resolves it against what it can open). */
  resolveHref: (href: string | undefined) => MarkdownLink;
  /** The heading id (without prefix) for the heading that starts on this 1-based line, if known. */
  slugForLine: (line: number | undefined) => string | undefined;
  /** AA-697: a same-origin URL for an image the page can show (an SVG of its docs bundle), or null for alt text. */
  resolveImage?: (src: string | undefined) => string | null;
};

type HastNode = { type?: string; value?: string; tagName?: string; properties?: { className?: unknown };
                  children?: HastNode[] };

function hastText(node: HastNode | undefined): string {
  if (!node) return "";
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(hastText).join("");
}

/** The source of a ```mermaid fence (the <pre>'s <code> child), or null for any other code block. */
export function mermaidSource(pre: unknown): string | null {
  const code = (pre as HastNode | undefined)?.children?.find((c) => c.type === "element" && c.tagName === "code");
  const cls = code?.properties?.className;
  if (!Array.isArray(cls) || !cls.includes("language-mermaid")) return null;
  return hastText(code).replace(/\n$/, "");
}

function headingId(slugForLine: Props["slugForLine"], node: unknown): string | undefined {
  const line = (node as { position?: { start?: { line?: number } } } | undefined)?.position?.start?.line;
  const slug = slugForLine(line);
  return slug ? `${HEADING_ID_PREFIX}${slug}` : undefined;
}

export function scrollToHeading(slug: string): boolean {
  const el = typeof document !== "undefined" ? document.getElementById(`${HEADING_ID_PREFIX}${slug}`) : null;
  if (!el) return false;
  el.scrollIntoView({ block: "start", behavior: "smooth" });
  return true;
}

export function MarkdownView({ markdown, resolveHref, slugForLine, resolveImage }: Props) {
  const components = React.useMemo<Components>(() => {
    const heading = (Tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6") =>
      function Heading({ node, children }: { node?: unknown; children?: React.ReactNode }) {
        return <Tag id={headingId(slugForLine, node)}>{children}</Tag>;
      };
    return {
      h1: heading("h1"),
      h2: heading("h2"),
      h3: heading("h3"),
      h4: heading("h4"),
      h5: heading("h5"),
      h6: heading("h6"),
      a({ href, children }) {
        const link = resolveHref(href);
        if (link.kind === "internal") {
          return (
            <a
              href={link.href}
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                e.preventDefault();
                link.onNavigate();
              }}
            >
              {children}
            </a>
          );
        }
        if (link.kind === "anchor") {
          return (
            <a
              href={`#${HEADING_ID_PREFIX}${link.id}`}
              onClick={(e) => {
                e.preventDefault();
                scrollToHeading(link.id);
              }}
            >
              {children}
            </a>
          );
        }
        if (link.kind === "external") {
          return (
            <a href={link.href} target="_blank" rel="noopener noreferrer nofollow">
              {children}
            </a>
          );
        }
        return (
          <span className="aa-md-deadlink" title="Not published in the docs reader">
            {children}
          </span>
        );
      },
      img({ src, alt }) {
        const url = resolveImage?.(typeof src === "string" ? src : undefined) ?? null;
        if (url) {
          return (
            <a className="aa-md-figure" href={url} target="_blank" rel="noopener noreferrer" title="Open full size">
              {/* eslint-disable-next-line @next/next/no-img-element -- a same-origin SVG from the BFF, not an optimisable photo */}
              <img src={url} alt={alt ?? ""} loading="lazy" decoding="async" />
            </a>
          );
        }
        return <span className="aa-md-img">[image{alt ? `: ${alt}` : ""}]</span>;
      },
      table({ children }) {
        return (
          <div className="aa-md-table">
            <table>{children}</table>
          </div>
        );
      },
      blockquote({ children, node }) {
        const kind: CalloutKind | null = calloutKindOf(node);
        if (!kind) return <blockquote>{children}</blockquote>;
        return (
          <blockquote data-callout={kind} data-testid="md-callout" role="note" aria-label={CALLOUT_LABEL[kind]}>
            <span className="aa-md-callout-title">{CALLOUT_LABEL[kind]}</span>
            {children}
          </blockquote>
        );
      },
      pre({ children, node }) {
        const source = mermaidSource(node);
        if (source !== null) return <MermaidDiagram source={source} />;
        return <pre>{children}</pre>;
      },
    };
  }, [resolveHref, slugForLine, resolveImage]);

  return (
    <div className="aa-md">
      <style>{CSS}</style>
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} skipHtml components={components}>
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
