// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/AiNotice.tsx
// AA-1033 — the one notice under AI-written text, the same in every app (wording: core/aiNotice.messages.ts).
//
//   <AiNotice />                        staff / agency users, under an AI output they act on (the default)
//   <AiNotice variant="publisher" />    Content studio, where a tenant reviews, publishes or exports AI content
//   <AiNotice variant="traveller" />    a public page with AI-written tour text
//
// A `<p role="note">` (a quiet, non-interactive line, read in place by screen readers), muted 12px text with an info
// icon, no fixed height: it takes its own line and never shifts what is around it. Both themes through `K`. The
// language is the nearest I18nProvider's (English outside one); an app with its own i18n (CRM web) passes `lang`.
// `style` and `className` place it (margins, flex basis); they never change the wording.

import type { CSSProperties } from "react";
import { Info } from "lucide-react";
import { K, sans } from "./tokens";
import { useLang } from "./useI18n";
import { aiNoticeText, type AiNoticeVariant } from "./aiNotice.messages";

export function AiNotice({
  variant = "staff",
  lang,
  testId = "ai-notice",
  style,
  className,
}: {
  variant?: AiNoticeVariant;
  /** The UI language when the app does not render the kit I18nProvider ("en" | "vi"; a "vi-VN" tag works too). */
  lang?: string | null;
  testId?: string;
  style?: CSSProperties;
  /** For Tailwind apps; inline-style apps leave it out. */
  className?: string;
}) {
  const providerLang = useLang();
  return (
    <p
      role="note"
      data-testid={testId}
      data-ai-notice={variant}
      className={className}
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 6,
        margin: 0,
        minWidth: 0,
        fontFamily: sans,
        fontSize: 12,
        lineHeight: 1.5,
        color: K.muted,
        ...style,
      }}
    >
      <Info size={13} aria-hidden="true" focusable="false" style={{ flexShrink: 0, marginTop: 2, color: K.muted2 }} />
      <span>{aiNoticeText(variant, lang ?? providerLang)}</span>
    </p>
  );
}
