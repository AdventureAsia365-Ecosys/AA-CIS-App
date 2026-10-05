// app/(tenant)/portal/_components/catalogApi.ts
// AA-662 PR 3 — My Content (CatalogTab) data layer, used with react-query. Tenant calls go through
// the BFF proxy /api/tenant/* (no reviewer-id header; tenant identity is the session cookie the
// proxy forwards). Plain functions; CatalogTab wires them into useQuery/useMutation.

import { apiGet } from "../../../_kit";

export interface Version {
  id: string;
  version_number: number;
  status: string;
  quality_score: number | null;
  edit_source: string;
  rewrite_language: string;
  created_at: string;
  edited_at: string | null;
  rewritten_content: string;
  seo_mode: string;
  aa_name: string;
  aa_subtitle: string;
  aa_summary: string;
  aa_highlights: string;
  aa_itineraries: string | null;
  aa_seo_title: string;
  aa_seo_meta: string;
  aa_quality_score: number;
  country: string | null;
  duration: string | null;
  inclusions?: string | null;
  exclusions?: string | null;
  published_tour_id?: string;
  job_status?: string | null;
  tour_id?: string;
}

export function fetchMyVersions(): Promise<Version[]> {
  return apiGet<{ data?: Version[] }>(`/api/tenant/v1/tours/my-versions?page_size=50`).then(
    (d) => d.data ?? [],
  );
}

export function fetchVersionDetail(id: string): Promise<Version> {
  return apiGet<Version>(`/api/tenant/v1/tours/versions/${id}`);
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function fetchPoolTour(publishedTourId: string): Promise<any> {
  return apiGet(`/api/tenant/v1/tours/pool/${publishedTourId}`);
}

export async function retryRewrite(versionId: string): Promise<void> {
  const r = await fetch(`/api/tenant/v1/tours/versions/${versionId}/retry`, { method: "POST" });
  if (!r.ok) throw new Error(`Retry failed (${r.status})`);
}

export async function saveVersionEdit(
  versionId: string,
  editedContent: Record<string, unknown>,
): Promise<{ status: string; new_version_id?: string; version_number?: number }> {
  const r = await fetch(`/api/tenant/v1/tours/versions/${versionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "edit", edited_content: editedContent, edited_by: "tenant" }),
  });
  if (!r.ok) throw new Error(`Save failed (${r.status})`);
  return r.json();
}

export async function requestRewrite(
  publishedTourId: string,
  rewriteLanguage: string,
): Promise<void> {
  const r = await fetch(`/api/tenant/v1/tours/pool/${publishedTourId}/rewrite`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rewrite_language: rewriteLanguage || "en-US" }),
  });
  if (!r.ok) throw new Error(`Rewrite request failed (${r.status})`);
}

// ── writing-state helpers (unchanged semantics from the original CatalogTab) ──
const JOB_ENDED_UNSUCCESSFULLY = ["failed", "cancelled", "stopped_budget"];

export function isAiWriting(
  v: Pick<Version, "status" | "edit_source" | "job_status">,
): boolean {
  return (
    v.status === "pending" &&
    v.edit_source === "ai_generated" &&
    !JOB_ENDED_UNSUCCESSFULLY.includes(v.job_status ?? "")
  );
}

export function isRewriteFailed(
  v: Pick<Version, "status" | "edit_source" | "job_status">,
): boolean {
  return (
    v.status === "failed" ||
    (v.status === "pending" &&
      v.edit_source === "ai_generated" &&
      JOB_ENDED_UNSUCCESSFULLY.includes(v.job_status ?? ""))
  );
}
