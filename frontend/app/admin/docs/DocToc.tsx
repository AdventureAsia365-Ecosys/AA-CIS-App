"use client";
// app/admin/docs/DocToc.tsx — AA-1133: the page's table of contents ("On this page").
//
// A wide reader (ReaderColumns decides from the reader's own width, lib/docsReader.ts tocColumnFits): a sticky column
// beside the text with scroll-spy (the heading nearest the top of the viewport is marked, aria-current="location"),
// like market docs sites. A narrower reader: the disclosure above the text, open by default when the page is long
// enough to need it. Only h2 / h3 headings are listed; the ids are the ones MarkdownView gives the headings
// (lib/docsReader.ts outline), so a link and a search hit land on the same place.

import { useEffect, useRef, useState } from "react";
import { K, RADIUS, useT } from "../../_kit";
import { HEADING_ID_PREFIX, scrollToHeading } from "../../_kit/MarkdownView";
import { M } from "./messages";
import type { Heading } from "@/lib/docsReader";
import { headingHash, pickActiveIndex, slugFromHash } from "@/lib/docsToc";

// AA-1133 follow-up (S284 live check): the mark used to follow IntersectionObserver callbacks only, which stop once a
// heading is inside the observed band, so after a click or a smooth scroll the previous heading stayed marked. Now the
// mark is recomputed on every scroll (at most once a frame, any scrolling box: capture listener), when a scroll ends
// (`scrollend` where the browser has it), on a resize and on a #doc-<slug> change; the heading the reader went to stays
// marked while it is on screen (lib/docsToc.ts pickActiveIndex), until they scroll by hand.

type Spy = { active: string; go: (slug: string) => void };

const USER_SCROLL_EVENTS = ["wheel", "touchstart", "keydown"] as const;

/** Window listeners for the scroll-spy; returns the function that removes them. */
function listen(onScroll: () => void, onEnd: () => void, onHash: () => void, onUser: () => void): () => void {
  const opts = { capture: true, passive: true } as const;
  window.addEventListener("scroll", onScroll, opts);
  window.addEventListener("scrollend", onEnd, opts);
  window.addEventListener("resize", onScroll, { passive: true });
  window.addEventListener("hashchange", onHash);
  USER_SCROLL_EVENTS.forEach((e) => window.addEventListener(e, onUser, opts));
  return () => {
    window.removeEventListener("scroll", onScroll, opts);
    window.removeEventListener("scrollend", onEnd, opts);
    window.removeEventListener("resize", onScroll);
    window.removeEventListener("hashchange", onHash);
    USER_SCROLL_EVENTS.forEach((e) => window.removeEventListener(e, onUser, opts));
  };
}

/** Each listed heading's top in px from the viewport top (Infinity when it is not in the page). */
function headingTops(slugs: readonly string[]): number[] {
  return slugs.map((slug) => {
    const el = document.getElementById(`${HEADING_ID_PREFIX}${slug}`);
    return el ? el.getBoundingClientRect().top : Number.POSITIVE_INFINITY;
  });
}

/** The heading the reader is at, and `go` (scroll to a heading, mark it, put it in the address). */
function useActiveHeading(items: Heading[], enabled: boolean): Spy {
  const [active, setActive] = useState("");
  const pinned = useRef("");
  useEffect(() => {
    if (!enabled || !items.length || typeof window === "undefined") return;
    const slugs = items.map((h) => h.slug);
    const pick = () => {
      const i = pickActiveIndex(headingTops(slugs), { viewport: window.innerHeight,
                                                      pinned: slugs.indexOf(pinned.current) });
      setActive(slugs[i] ?? "");
    };
    let frame = 0;
    const onScroll = () => {   // throttled: at most one pick per animation frame
      if (!frame) frame = window.requestAnimationFrame(() => { frame = 0; pick(); });
    };
    const onHash = () => {
      pinned.current = slugFromHash(window.location.hash, HEADING_ID_PREFIX);
      onScroll();
    };
    const onUser = () => { pinned.current = ""; };
    const stop = listen(onScroll, pick, onHash, onUser);
    pinned.current = slugFromHash(window.location.hash, HEADING_ID_PREFIX);
    onScroll();   // first mark on the next frame (not a synchronous setState in the effect)
    return () => {
      window.cancelAnimationFrame(frame);
      stop();
    };
  }, [items, enabled]);
  const go = (slug: string) => {
    pinned.current = slug;
    setActive(slug);
    scrollToHeading(slug);
    try {
      window.history.replaceState(window.history.state, "", headingHash(slug, HEADING_ID_PREFIX));
    } catch {
      /* the address stays as it was; the scroll still happened */
    }
  };
  return { active, go };
}

function TocLinks({ items, active, onGo }: { items: Heading[]; active: string; onGo: (slug: string) => void }) {
  return (
    <>
      {items.map((h) => {
        const on = h.slug === active;
        return (
          <a
            key={h.slug}
            href={headingHash(h.slug, HEADING_ID_PREFIX)}
            aria-current={on ? "location" : undefined}
            onClick={(e) => {
              e.preventDefault();
              onGo(h.slug);
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
  const spy = useActiveHeading(items, sticky);
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
        <TocLinks items={items} active={spy.active} onGo={spy.go} />
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
        <TocLinks items={items} active="" onGo={spy.go} />
      </nav>
    </details>
  );
}
