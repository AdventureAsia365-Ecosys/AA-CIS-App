// app/(tenant)/portal/_components/AiContentNotice.tsx
// AA-819 (Anthropic Usage Policy, effective 12/11/2026) — the one neutral line a tenant sees
// wherever they review, publish or export AI-written content (T10 My Content, T11 Publish,
// My Catalog Tours export). Wording is the issue's; it says nothing about gates, scores or flags
// (tenant-sees-content-only). One component so every screen shows the same sentence, and so the
// AA-668 writer panel can reuse it beside its publish/export actions.

import { Info } from "lucide-react";
import { K, sans } from "../../../_kit";

export const AI_CONTENT_NOTICE =
  "This content was generated with AI. Review it before publishing; you are responsible for what you publish.";

export function AiContentNotice({ style }: { style?: React.CSSProperties }) {
  return (
    <p
      role="note"
      data-testid="ai-content-notice"
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 6,
        margin: 0,
        fontFamily: sans,
        fontSize: 12,
        lineHeight: 1.5,
        color: K.muted,
        ...style,
      }}
    >
      <Info size={13} aria-hidden style={{ flexShrink: 0, marginTop: 2, color: K.muted2 }} />
      <span>{AI_CONTENT_NOTICE}</span>
    </p>
  );
}
