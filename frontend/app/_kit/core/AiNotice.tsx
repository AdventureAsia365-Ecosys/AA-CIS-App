// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/AiNotice.tsx
// AA-1033 — the one notice under AI-written text, the same in every app (wording: core/aiNotice.messages.ts).
//
//   <AiNotice />                        staff / agency users, under an AI output they act on (the default)
//   <AiNotice variant="publisher" />    Content studio, where a tenant reviews, publishes or exports AI content
//   <AiNotice variant="traveller" />    a public page with AI-written tour text
//   <AiNotice variant="narration" />    text the AI wrote when a traveller asked for it (AA-1040, EU AI Act Art. 50(1))
//   <AiNotice variant="publisher" form="badge" />   the "AI-assisted" label on a card or list row of AI content (AA-1040)
//
// A `<p role="note">` (a quiet, non-interactive line, read in place by screen readers), muted 12px text with an info
// icon, no fixed height: it takes its own line and never shifts what is around it. Both themes through `K`. The
// language is the nearest I18nProvider's (English outside one); an app with its own i18n (CRM web) passes `lang`.
// `style` and `className` place it (margins, flex basis); they never change the wording.
//
// `form="badge"` (AA-1040): a small inline pill with the short "AI-assisted" text, for places where the line does not
// fit. It is a `<span role="note">` whose accessible name and tooltip are the variant's full sentence, so a screen
// reader hears the same notice as the line, and it carries `data-ai-notice-form="badge"`.

import type { CSSProperties } from "react";
import { Info, Sparkles } from "lucide-react";
import { K, RADIUS, sans } from "./tokens";
import { useLang } from "./useI18n";
import { aiBadgeText, aiNoticeText, type AiNoticeVariant } from "./aiNotice.messages";

export type AiNoticeForm = "line" | "badge";

export function AiNotice({
  variant = "staff",
  form = "line",
  lang,
  testId = "ai-notice",
  style,
  className,
}: {
  variant?: AiNoticeVariant;
  /** "line" (default): the sentence under the content; "badge": the short "AI-assisted" pill for cards and rows. */
  form?: AiNoticeForm;
  /** The UI language when the app does not render the kit I18nProvider ("en" | "vi"; a "vi-VN" tag works too). */
  lang?: string | null;
  testId?: string;
  style?: CSSProperties;
  /** For Tailwind apps; inline-style apps leave it out. */
  className?: string;
}) {
  const providerLang = useLang();
  const sentence = aiNoticeText(variant, lang ?? providerLang);
  if (form === "badge") {
    const badge = aiBadgeText(lang ?? providerLang);
    return (
      <span
        role="note"
        aria-label={`${badge}. ${sentence}`}
        title={sentence}
        data-testid={testId}
        data-ai-notice={variant}
        data-ai-notice-form="badge"
        className={className}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          flexShrink: 0,
          padding: "1px 7px",
          borderRadius: RADIUS.pill,
          border: `1px solid ${K.line}`,
          background: K.neutralBg,
          fontFamily: sans,
          fontSize: 11,
          fontWeight: 600,
          lineHeight: 1.6,
          whiteSpace: "nowrap",
          color: K.muted,
          ...style,
        }}
      >
        <Sparkles size={11} aria-hidden="true" focusable="false" style={{ flexShrink: 0, color: K.muted2 }} />
        <span aria-hidden="true">{badge}</span>
      </span>
    );
  }
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
      <span>{sentence}</span>
    </p>
  );
}
