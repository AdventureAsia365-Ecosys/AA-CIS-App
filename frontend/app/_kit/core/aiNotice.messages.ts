// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/aiNotice.messages.ts
// AA-1033 — the wording of the one AI notice of the ecosystem (core/AiNotice.tsx), English and Vietnamese. Every app
// says the same thing about AI-written text: it can contain mistakes, it is for information, and what binds is the
// confirmed quote and booking terms. The text never claims to exclude liability; the business owner's lawyer reviews
// the final legal wording later.
//
// Variants (by audience):
//   staff      — AA staff and agency users, under each AI output they act on (CIS reviewer screens, CRM AI drafts);
//   publisher  — a Content studio tenant, where they review, publish or export AI-written content (the portal line
//                since the AUP notice of 12/11/2026, kept word for word);
//   traveller  — public pages that show AI-written tour text (TripPlanner tour page and footer).
//
// No React: this file and core/i18n.ts (the only file it imports) are in the `tokens` profile (check-kit-copy.mjs
// TOKENS_PROFILE), so TripPlanner, which keeps its own B2C components (ADR 0016 decision 2), renders the same
// sentences from its byte copy. defineMessages makes tsc refuse a missing Vietnamese variant;
// lib/kitAiNotice.test.mjs checks none is empty.

import { defineMessages } from "./i18n";

export const AI_NOTICE_MESSAGES = defineMessages(
  {
    staff: "AI can make mistakes. Check prices, dates, visa and safety information before you send or publish this.",
    publisher:
      "This content was generated with AI. Review it before publishing; you are responsible for what you publish.",
    traveller:
      "Some descriptions are written with the help of AI and may contain mistakes. Prices, itineraries and visa " +
      "conditions are confirmed in your official quote.",
  },
  {
    staff:
      "AI có thể sai. Hãy kiểm tra lại giá, ngày, visa và thông tin an toàn trước khi gửi hoặc đăng nội dung này.",
    publisher:
      "Nội dung này được tạo bằng AI. Hãy xem lại trước khi đăng; bạn chịu trách nhiệm về nội dung mình đăng.",
    traveller:
      "Một số mô tả được viết với sự hỗ trợ của AI và có thể có sai sót. Giá, lịch trình và điều kiện visa được " +
      "xác nhận trong báo giá chính thức của bạn.",
  },
);

export type AiNoticeVariant = keyof typeof AI_NOTICE_MESSAGES.en;

export const AI_NOTICE_VARIANTS: readonly AiNoticeVariant[] = ["staff", "publisher", "traveller"];

/** The notice text of a variant in a language; anything but "vi" (or a "vi-*" tag) is English. */
export function aiNoticeText(variant: AiNoticeVariant, lang?: string | null): string {
  const vi = (lang ?? "").toLowerCase().split("-")[0] === "vi";
  return (vi ? AI_NOTICE_MESSAGES.vi : AI_NOTICE_MESSAGES.en)[variant];
}
