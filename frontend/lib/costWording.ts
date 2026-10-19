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

/** "AI estimate $144.35 in all; $0.42 of it … already in the AWS bill, so it is counted once." */
export function aiOnBillNote(aiUsd: number, aiOnBillUsd: number, fmt: (n: number) => string): string {
  if (!aiOnBillUsd) return `${AI_ESTIMATE_LABEL} ${fmt(aiUsd)} in all; none of it is on the AWS bill.`;
  return (
    `${AI_ESTIMATE_LABEL} ${fmt(aiUsd)} in all; ${fmt(aiOnBillUsd)} of it is Bedrock on the main AWS account, ` +
    "already in the AWS bill, so it is counted once."
  );
}
