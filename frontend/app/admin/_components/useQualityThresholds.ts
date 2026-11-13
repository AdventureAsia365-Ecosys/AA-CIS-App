"use client";
// S279 thresholds lane L2 — the quality values in force (pass line, score colour bands, SEO bands) for every admin
// page that colours a score or checks an SEO length. One request per 30 s at most (the settings cache window);
// undefined until it loads (the helpers in lib/qualityThresholds.ts then return neutral values).
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../_kit";
import { QUALITY_THRESHOLDS_PATH, type QualityThresholds } from "@/lib/qualityThresholds";

export function useQualityThresholds(): QualityThresholds | undefined {
  const q = useQuery({
    queryKey: ["quality-thresholds"],
    queryFn: () => apiGet<QualityThresholds>(QUALITY_THRESHOLDS_PATH, { admin: true }),
    staleTime: 30_000,
    retry: 1,
  });
  return q.data;
}
