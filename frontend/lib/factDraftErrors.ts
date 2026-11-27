// frontend/lib/factDraftErrors.ts — AA-1181: the text of a refused sheet-draft action (the tour drawer's sheet draft,
// TourFactDraft.tsx, the upload merge panel, upload/MergePanel.tsx, and the upload page's Confirm all). The API answers {detail: {code, message}};
// a code the page knows is shown in the reader's language, anything else as the API's (English) message.

/** A supplier sheet never changes a tour made by hand in the content workspace (merge and publish refuse it). */
export const FACT_DRAFT_HAND_MADE = "fact_draft_hand_made";

/** ``known[code]`` for a known refusal code, else the API's message (string detail or detail.message), else
 *  ``fallback``. */
export function refusalText(body: unknown, fallback: string, known: Record<string, string> = {}): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  const { code, message } = (d ?? {}) as { code?: unknown; message?: unknown };
  if (typeof code === "string" && Object.hasOwn(known, code)) return known[code];
  return typeof message === "string" && message ? message : fallback;
}
