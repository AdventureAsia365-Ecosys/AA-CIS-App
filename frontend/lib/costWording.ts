// frontend/lib/costWording.ts
// S243 (AA-858 / AA-884) — the one wording of cost columns, shared by the Overview "Cost per app" card and
// /admin/platform/cost, so both pages present the same numbers the same way. Row labels come from the backend
// (api/services/platform_cost.py ROW_LABELS); the netting rule is the backend's too:
//   Total = AWS bill + AI outside the AWS bill, where AI outside = AI estimate − AI already on the AWS bill
//   (Bedrock calls on the main AWS account are billed there already).

export const AWS_BILL_LABEL = "AWS bill";
export const AI_OUTSIDE_LABEL = "AI outside the AWS bill";
export const AI_ESTIMATE_LABEL = "AI estimate";
export const AI_ON_BILL_LABEL = "AI already on the AWS bill";

export const AWS_BILL_SRC =
  "AWS Cost Explorer for the main AWS account, split by each resource's App tag (refreshed about once a day)";
export const AI_OUTSIDE_SRC =
  "Estimate from our AI gateway's own call log for AI not billed on the main AWS account: the other Bedrock " +
  "accounts, OpenAI and Jev";
export const AI_ON_BILL_SRC =
  "Bedrock calls on the main AWS account: part of the AI estimate, but already inside the AWS bill, so subtracted once";

/** The aiOnBillNote() sentences (English; the Vietnamese side is lib/costWording.messages.ts COST_WORDING). */
export const AI_NOTE_WORDS = {
  aiNoneOnBill: (ai: string) => `${AI_ESTIMATE_LABEL} ${ai} in all; none of it is on the AWS bill.`,
  aiSomeOnBill: (ai: string, onBill: string) =>
    `${AI_ESTIMATE_LABEL} ${ai} in all; ${onBill} of it is Bedrock on the main AWS account, ` +
    "already in the AWS bill, so it is counted once.",
};

/** "AI estimate $144.35 in all; $0.42 of it … already in the AWS bill, so it is counted once." `words`: the UI
 * language's sentences (AA-994), English when left out. */
export function aiOnBillNote(
  aiUsd: number,
  aiOnBillUsd: number,
  fmt: (n: number) => string,
  words: typeof AI_NOTE_WORDS = AI_NOTE_WORDS,
): string {
  if (!aiOnBillUsd) return words.aiNoneOnBill(fmt(aiUsd));
  return words.aiSomeOnBill(fmt(aiUsd), fmt(aiOnBillUsd));
}

/**
 * Sum of money parts as it is shown, so the numbers on screen add up (S243 review: live October showed
 * $101.24 + $144.35 − $0.42 = $245.16 because the backend total was rounded once, from 4-decimal parts).
 * Each part is rounded to cents first (half away from zero), then summed; pass a subtracted part as a negative
 * number. A null/undefined part counts as 0. The backend keeps 4 decimals; only the display rounds.
 */
export function shownSum(...parts: (number | null | undefined)[]): number {
  let cents = 0;
  for (const p of parts) {
    if (p == null || Number.isNaN(p)) continue;
    cents += Math.sign(p) * Math.round(Math.abs(p) * 100);
  }
  return cents / 100;
}
