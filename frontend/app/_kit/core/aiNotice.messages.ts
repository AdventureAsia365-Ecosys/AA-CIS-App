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
//   traveller  — public pages that show AI-written tour text (TripPlanner tour page and footer);
//   narration  — a traveller who asked the AI for text and reads its answer (TripPlanner trip narration): the
//                person is told they are reading an AI system's output (EU AI Act Art. 50(1), AA-1040).
//
// The badge (AA-1040): one short label, "AI-assisted", for lists and cards where a full sentence does not fit
// (Content studio piece cards). AiNotice renders it with `form="badge"`; the variant's sentence is its accessible
// description, so the badge never says less than the line.
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
    narration:
      "This text was written by AI when you asked for it and may contain mistakes. Prices, itineraries and visa " +
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
    narration:
      "Đoạn này do AI viết theo yêu cầu của bạn và có thể có sai sót. Giá, lịch trình và điều kiện visa được " +
      "xác nhận trong báo giá chính thức của bạn.",
  },
);

/** The badge text (AA-1040), the same in every app: a short label beside AI-written content in lists and cards. */
export const AI_BADGE_MESSAGES = defineMessages({ badge: "AI-assisted" }, { badge: "Có AI hỗ trợ" });

export type AiNoticeVariant = keyof typeof AI_NOTICE_MESSAGES.en;

export const AI_NOTICE_VARIANTS: readonly AiNoticeVariant[] = ["staff", "publisher", "traveller", "narration"];

function isVi(lang?: string | null): boolean {
  return (lang ?? "").toLowerCase().split("-")[0] === "vi";
}

/** The notice text of a variant in a language; anything but "vi" (or a "vi-*" tag) is English. */
export function aiNoticeText(variant: AiNoticeVariant, lang?: string | null): string {
  return (isVi(lang) ? AI_NOTICE_MESSAGES.vi : AI_NOTICE_MESSAGES.en)[variant];
}

/** The badge text ("AI-assisted") in a language, with the same language rule as aiNoticeText. */
export function aiBadgeText(lang?: string | null): string {
  return (isVi(lang) ? AI_BADGE_MESSAGES.vi : AI_BADGE_MESSAGES.en).badge;
}
