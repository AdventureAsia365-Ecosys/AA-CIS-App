"use client";
// app/(tenant)/portal/_components/CtaReviewNotice.tsx
// AA-819 follow-up (S237) — the tenant's call to action is published exactly as they type it. When
// it mentions reviews or ratings ("Read our five-star reviews", "Rated 4.9 on TripAdvisor") the
// portal shows one plain, non-blocking line; the save still happens. The rule is the backend's
// (POST /v1/content-writing/cta-check while typing, `cta_warning` on the save response), so there
// is one source of truth. Tenant-sees-content-only: the copy never names checks, gates or flags.

import { useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { K, sans } from "../../../_kit";

export const CTA_REVIEW_NOTICE =
  "Only mention reviews or ratings if they are real and you can show them publicly. AI-written copy never adds reviews for you.";

export type CtaWarning = { code: string; phrases: string[] } | null;

/** Pull `cta_warning` out of a save response body (absent / malformed -> null). */
export function ctaWarningFrom(body: unknown): CtaWarning {
  const w = (body as { cta_warning?: unknown } | null)?.cta_warning as CtaWarning | undefined;
  return w && Array.isArray(w.phrases) && w.phrases.length > 0 ? w : null;
}

/** Live check while the tenant types: debounced call to the same rule the save uses. Any
 *  error (offline, session expired) just means no warning — the save response still carries it. */
export function useCtaReviewCheck(cta: string, delayMs = 450): CtaWarning {
  const text = cta.trim();
  const [result, setResult] = useState<{ text: string; warning: CtaWarning }>({ text: "", warning: null });

  useEffect(() => {
    if (!text) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      fetch("/api/tenant/v1/content-writing/cta-check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cta: text }),
        signal: ctrl.signal,
      })
        .then(r => (r.ok ? r.json() : null))
        .then(d => setResult({ text, warning: ctaWarningFrom(d) }))
        .catch(() => { /* aborted or offline — no live warning */ });
    }, delayMs);
    return () => { window.clearTimeout(timer); ctrl.abort(); };
  }, [text, delayMs]);

  return text && result.text === text ? result.warning : null;
}

export function CtaReviewNotice({ warning, style }: { warning: CtaWarning; style?: React.CSSProperties }) {
  if (!warning) return null;
  return (
    <div
      role="note"
      data-testid="cta-review-notice"
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        padding: "9px 12px",
        background: K.amberSoft,
        border: `1px solid ${K.amber}`,
        borderRadius: 8,
        fontFamily: sans,
        fontSize: 12,
        lineHeight: 1.5,
        color: K.body,
        ...style,
      }}
    >
      <TriangleAlert size={14} aria-hidden style={{ flexShrink: 0, marginTop: 2, color: K.amber }} />
      <div>
        <div>{CTA_REVIEW_NOTICE}</div>
        <div style={{ marginTop: 4, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", color: K.muted }}>
          <span>Mentions:</span>
          {warning.phrases.map(p => (
            <span key={p} style={{
              padding: "1px 8px", borderRadius: 999, background: K.card, border: `1px solid ${K.line}`,
              color: K.ink2, fontWeight: 600, fontSize: 11,
            }}>{p}</span>
          ))}
        </div>
      </div>
    </div>
  );
}
