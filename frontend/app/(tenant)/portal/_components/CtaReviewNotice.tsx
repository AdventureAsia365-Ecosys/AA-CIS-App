"use client";
// app/(tenant)/portal/_components/CtaReviewNotice.tsx
// AA-819 follow-up (S237) — the tenant's call to action is published exactly as they type it. When
// it mentions reviews or ratings ("Read our five-star reviews", "Rated 4.9 on TripAdvisor") the
// portal shows one plain, non-blocking line; the save still happens. The rule is the backend's
// (POST /v1/content-writing/cta-check while typing, `cta_warning` on the save response), so there
// is one source of truth. Tenant-sees-content-only: the copy never names checks, gates or flags.

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";
import { K, sans, apiSend } from "../../../_kit";

export const CTA_REVIEW_NOTICE =
  "Only mention reviews or ratings if they are real and you can show them publicly. AI-written copy never adds reviews for you.";

export type CtaWarning = { code: string; phrases: string[] } | null;

/** Pull `cta_warning` out of a save response body (absent / malformed -> null). */
export function ctaWarningFrom(body: unknown): CtaWarning {
  const w = (body as { cta_warning?: unknown } | null)?.cta_warning as CtaWarning | undefined;
  return w && Array.isArray(w.phrases) && w.phrases.length > 0 ? w : null;
}

/** Live check while the tenant types: debounced react-query call to the same rule the save uses
 *  (cached per text, so retyping a CTA already checked costs no request). Any error (offline,
 *  session expired, a staff session in the portal) just means no live warning — the save response
 *  still carries it. */
export function useCtaReviewCheck(cta: string, delayMs = 450): CtaWarning {
  const text = cta.trim();
  const [settled, setSettled] = useState(text);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(text), delayMs);
    return () => window.clearTimeout(timer);
  }, [text, delayMs]);

  const q = useQuery({
    queryKey: ["tenant-cta-check", settled],
    enabled: settled !== "" && settled === text,
    queryFn: () => apiSend<{ cta_warning?: unknown }>("/api/tenant/v1/content-writing/cta-check", {
      body: { cta: settled },
    }),
    staleTime: 10 * 60_000,
    retry: false,
  });

  return text !== "" && settled === text ? ctaWarningFrom(q.data) : null;
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
      <div style={{ minWidth: 0 }}>
        <div>{CTA_REVIEW_NOTICE}</div>
        <div style={{ marginTop: 4, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", color: K.muted }}>
          <span>Mentions:</span>
          {warning.phrases.map(p => (
            <span key={p} style={{
              padding: "1px 8px", borderRadius: 999, background: K.card, border: `1px solid ${K.line}`,
              color: K.ink2, fontWeight: 600, fontSize: 11, overflowWrap: "anywhere",
            }}>{p}</span>
          ))}
        </div>
      </div>
    </div>
  );
}
