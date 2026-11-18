"use client";
// app/admin/docs/DocToc.tsx — AA-1133: the page's table of contents ("On this page").
//
// A wide reader (ReaderColumns decides from the reader's own width, lib/docsReader.ts tocColumnFits): a sticky column
// beside the text with scroll-spy (the heading nearest the top of the viewport is marked, aria-current="location"),
// like market docs sites. A narrower reader: the disclosure above the text, open by default when the page is long
// enough to need it. Only h2 / h3 headings are listed; the ids are the ones MarkdownView gives the headings
// (lib/docsReader.ts outline), so a link and a search hit land on the same place.

import { useEffect, useState } from "react";
import { K, RADIUS, useT } from "../../_kit";
import { HEADING_ID_PREFIX, scrollToHeading } from "../../_kit/MarkdownView";
import { M } from "./messages";
import type { Heading } from "@/lib/docsReader";

/** The slug of the heading the reader is at: the last one whose top has passed a line near the top of the viewport. */
function useActiveHeading(items: Heading[], enabled: boolean): string {
  const [active, setActive] = useState("");
  useEffect(() => {
    if (!enabled || !items.length || typeof window === "undefined" || !("IntersectionObserver" in window)) return;
    const els = items
      .map((h) => document.getElementById(`${HEADING_ID_PREFIX}${h.slug}`))
      .filter((el): el is HTMLElement => !!el);
    if (!els.length) return;
    const pick = () => {
      let current = "";
      for (const el of els) {
        if (el.getBoundingClientRect().top <= 96) current = el.id.slice(HEADING_ID_PREFIX.length);
        else break;
      }
      setActive(current || els[0].id.slice(HEADING_ID_PREFIX.length));
    };
    const io = new IntersectionObserver(pick, { rootMargin: "0px 0px -70% 0px", threshold: [0, 1] });
    els.forEach((el) => io.observe(el));
    const first = window.requestAnimationFrame(pick);   // not a synchronous setState in the effect
    return () => {
      window.cancelAnimationFrame(first);
      io.disconnect();
    };
  }, [items, enabled]);
  return active;
}

function TocLinks({ items, active }: { items: Heading[]; active: string }) {
  return (
    <>
      {items.map((h) => {
        const on = h.slug === active;
        return (
          <a
            key={h.slug}
            href={`#${HEADING_ID_PREFIX}${h.slug}`}
            aria-current={on ? "location" : undefined}
            onClick={(e) => {
              e.preventDefault();
              scrollToHeading(h.slug);
            }}
            style={{
              display: "block",
              fontSize: 13,
              lineHeight: 1.4,
              padding: "3px 0 3px 10px",
              marginLeft: h.level === 3 ? 12 : 0,
              borderLeft: `2px solid ${on ? K.accent : "transparent"}`,
              color: on ? K.ink : K.muted,
              fontWeight: on ? 600 : 400,
              textDecoration: "none",
            }}
          >
            {h.text}
          </a>
        );
      })}
    </>
  );
}

export function DocToc({ items, sticky }: { items: Heading[]; sticky: boolean }) {
  const t = useT(M);
  const active = useActiveHeading(items, sticky);
  if (items.length < 2) return null;
  if (sticky) {
    return (
      <nav
        aria-label={t("onThisPage")}
        data-testid="docs-toc"
        style={{ position: "sticky", top: 16, maxHeight: "calc(100vh - 32px)", overflowY: "auto", paddingBottom: 8 }}
      >
        <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.06em", textTransform: "uppercase", color: K.muted,
                      marginBottom: 8 }}>
          {t("onThisPage")}
        </div>
        <TocLinks items={items} active={active} />
      </nav>
    );
  }
  return (
    <details
      open={items.length >= 3}
      data-testid="docs-toc"
      style={{ marginBottom: 16, border: `1px solid ${K.line}`, borderRadius: RADIUS.md, padding: "8px 12px" }}
    >
      <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600, color: K.ink }}>
        {t("onThisPageCount", items.length)}
      </summary>
      <nav aria-label={t("onThisPage")} style={{ marginTop: 8 }}>
        <TocLinks items={items} active="" />
      </nav>
    </details>
  );
}
