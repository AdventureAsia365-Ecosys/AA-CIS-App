"use client";
// app/_kit/MermaidDiagram.tsx — AA-898: one Mermaid code block drawn as a diagram (CIS-local kit file, not core).
//
// Used by MarkdownView for ```mermaid fences (first user: the docs reader /admin/docs).
//   - Lazy: mermaid (and DOMPurify) are dynamic imports, so their chunks load only when a page has a diagram.
//   - Safe: securityLevel "strict" (labels HTML-encoded, click / link directives off), no HTML labels (plain SVG
//     text, no foreignObject), the source capped at MAX_SOURCE characters, and the SVG mermaid returns is sanitised
//     again with DOMPurify's SVG profile (foreignObject, image and use removed) before it is put in the page.
//     A page's %%{init}%% directive can not change the security, label, theme or CSS keys (lib/mermaidSafety.ts), and
//     an SVG that still points outside itself (a CSS url() or an href / src that is not a #fragment, an @import) is not
//     shown: the reader never loads remote resources (that would leak the viewer's address).
//   - Never blocks the page: any parse or render error (or a source over the cap) shows the source instead, labelled.
//   - Follows the admin theme (light / dark) and redraws when it flips.
//   - Draws only once the brand font (Poppins) is loaded, and draws again if it arrives later: mermaid sizes the
//     boxes from label widths it measures while drawing, so a diagram measured with the fallback face breaks its
//     labels mid-word once Poppins swaps in (lib/fontReady.ts).
// Mermaid keeps global config, so renders are serialised through one queue.

import React from "react";
import { K, RADIUS, mono, sans } from "./core/tokens";
import { useResolvedTheme } from "./useTheme";
import { useT } from "./core/useI18n";
import { SHELL_MESSAGES } from "./shell.messages";
import { MERMAID_SECURE_KEYS, svgIsSelfContained } from "@/lib/mermaidSafety";
import { fontIsReady, waitForFont } from "@/lib/fontReady";

export const MAX_SOURCE = 20_000;

let queue: Promise<unknown> = Promise.resolve();
let seq = 0;

function fontSet() {
  return typeof document !== "undefined" ? document.fonts : undefined;
}

// Subscribes to font loads so a diagram drawn before Poppins arrived is drawn again once it is in.
function subscribeFonts(onChange: () => void): () => void {
  const fonts = fontSet();
  if (!fonts?.addEventListener) return () => {};
  fonts.addEventListener("loadingdone", onChange);
  fonts.addEventListener("loadingerror", onChange);
  return () => {
    fonts.removeEventListener("loadingdone", onChange);
    fonts.removeEventListener("loadingerror", onChange);
  };
}
const brandFontReady = () => fontIsReady(fontSet(), sans);
const serverFontReady = () => false;

async function draw(source: string, theme: "light" | "dark"): Promise<string> {
  const [{ default: mermaid }, { default: DOMPurify }] = await Promise.all([
    import("mermaid"),
    import("dompurify"),
    waitForFont(fontSet(), sans),
  ]);
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: theme === "dark" ? "dark" : "default",
    fontFamily: sans,
    maxTextSize: MAX_SOURCE,
    htmlLabels: false,
    flowchart: { htmlLabels: false },
    suppressErrorRendering: true,
    secure: MERMAID_SECURE_KEYS,
  });
  seq += 1;
  const id = `aa-mermaid-${seq}`;
  try {
    const { svg } = await mermaid.render(id, source);
    const clean = DOMPurify.sanitize(svg, {
      USE_PROFILES: { svg: true, svgFilters: true },
      FORBID_TAGS: ["foreignObject", "image", "use"],
    });
    if (!svgIsSelfContained(clean)) throw new Error("diagram refers to an outside resource");
    return clean;
  } finally {
    // mermaid leaves its scratch element behind when a render fails.
    if (typeof document !== "undefined") document.getElementById(`d${id}`)?.remove();
  }
}

/** Serialised render (mermaid's config is global). */
function render(source: string, theme: "light" | "dark"): Promise<string> {
  const job = queue.then(() => draw(source, theme));
  queue = job.catch(() => undefined);
  return job;
}

// The result of one render, keyed by what was drawn; a different key means "still drawing". `base` leaves the font
// state out, so a redraw for a font that just arrived keeps the previous drawing on screen instead of flashing.
type Drawn = { key: string; base: string; svg: string | null };

export function MermaidDiagram({ source }: { source: string }) {
  const t = useT(SHELL_MESSAGES);
  const theme = useResolvedTheme();
  const fontReady = React.useSyncExternalStore(subscribeFonts, brandFontReady, serverFontReady);
  const tooBig = source.length > MAX_SOURCE;
  const base = `${theme}\u0000${source}`;
  const key = `${base}\u0000${fontReady ? "font" : "fallback"}`;
  const [drawn, setDrawn] = React.useState<Drawn | null>(null);

  React.useEffect(() => {
    if (tooBig) return;
    let live = true;
    render(source, theme).then(
      (svg) => live && setDrawn({ key, base, svg: svg.trim() ? svg : null }),
      () => live && setDrawn({ key, base, svg: null }),
    );
    return () => {
      live = false;
    };
  }, [source, theme, tooBig, key, base]);

  const current = drawn && drawn.key === key;
  const stale = drawn && !current && drawn.base === base && drawn.svg; // font redraw in progress
  const state = current
    ? (drawn.svg ? { kind: "ok" as const, svg: drawn.svg } : { kind: "error" as const })
    : stale
      ? { kind: "ok" as const, svg: drawn.svg as string }
      : { kind: "loading" as const };

  if (tooBig || state.kind === "error") {
    return (
      <pre data-testid="mermaid-fallback">
        <span className="aa-md-label">{t("diagramSource", tooBig ? "tooBig" : "failed")}</span>
        <code>{source}</code>
      </pre>
    );
  }
  if (state.kind === "loading") {
    return (
      <div
        data-testid="mermaid-loading"
        style={{
          margin: "0.8em 0",
          padding: "18px 14px",
          border: `1px dashed ${K.line}`,
          borderRadius: RADIUS.md,
          color: K.muted,
          fontSize: 12,
          fontFamily: mono,
        }}
      >
        {t("drawingDiagram")}
      </div>
    );
  }
  return (
    <div
      data-testid="mermaid-diagram"
      role="img"
      aria-label={t("diagram")}
      style={{
        margin: "0.8em 0",
        padding: 12,
        border: `1px solid ${K.line}`,
        borderRadius: RADIUS.md,
        background: K.card,
        overflowX: "auto",
        textAlign: "center",
      }}
      // Sanitised above: mermaid strict mode, then DOMPurify's SVG profile.
      dangerouslySetInnerHTML={{ __html: state.svg }}
    />
  );
}
