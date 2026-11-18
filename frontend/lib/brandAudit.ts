// AA-1127 (C1, S283 UI audit): one reading of a version's brand audit status for every Master Content badge (the
// compare panel and the Versions table). The backend writes pass / flagged / manual_check and, after the fix pass
// (revalidate_node, AA-215), "fixed"; the badge used to know only the first three and showed "—" for fixed.
export type BrandAuditKind = "pass" | "fixed" | "flagged" | "manual" | null;

export function brandAuditKind(status: string | null | undefined, fixPassApplied: boolean): BrandAuditKind {
  if (!status) return null;
  if (status === "pass") return "pass";
  if (status === "fixed") return "fixed";
  if (status === "flagged") return fixPassApplied ? "fixed" : "flagged";
  if (status === "manual_check") return "manual";
  return null;
}
