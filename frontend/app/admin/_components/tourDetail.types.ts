// app/admin/_components/tourDetail.types.ts — the tour detail drawer's data shapes (moved out of
// TourDetailPanelV2.tsx, AA-1146, unchanged).

export interface JudgeMeta {
  brand_fit: number | null;
  distinct: number | null;
  mission_present: boolean | null;
  feedback: string | null;
  judge_score: number | null;
}

export interface TourDetailFull {
  raw: {
    tour_id: string;
    src_name: string;
    src_subtitle: string | null;
    src_summary: string | null;
    src_description: string | null;
    src_highlights: string[] | null;
    src_itineraries: string | null;
    country: string | null;
    duration: string | null;
    price_raw: string | null;
    group_size: string | null;
    period: string | null;
    provider: string | null;
    inclusions: string | null;
    exclusions: string | null;
    pipeline_status: string;
    ingest_at: string | null;
  };
  generated: {
    id: string;
    version_num: number;
    created_at: string | null;
    status: string;
    aa_name: string;
    aa_subtitle: string | null;
    aa_summary: string | null;
    aa_description: string | null;
    aa_highlights: string[] | null;
    aa_itineraries: string | null;
    seo_title: string | null;
    seo_meta: string | null;
    seo_keywords_used: string[] | null;
    model_editorial: string | null;
    score_overall: number | null;
    score_brand: number | null;
    score_seo: number | null;
    score_structure: number | null;
    score_quality: number | null;
    brand_audit_status: string | null;
    judge: JudgeMeta | null;
  } | null;
  published: {
    id: string;
    aa_name: string;
    aa_subtitle: string | null;
    quality_score: number | null;
    published_at: string | null;
  } | null;
}

export interface HistoryRow {
  id: string;
  version_num: number;
  created_at: string | null;
  status: string;
  model_editorial: string | null;
  brand_rules_version: number | null;
  brand_name: string | null;
  prompt_version: string | null;
  score_overall: number | null;
  score_brand: number | null;
  score_seo: number | null;
  score_structure: number | null;
  llm_model: string | null;
  cost_usd: number | null;
}

export interface VersionContent {
  id: string;
  version_num: number;
  model_id: string | null;
  quality_score: number | null;
  created_at: string | null;
  aa_name: string | null;
  aa_subtitle: string | null;
  aa_summary: string | null;
  aa_highlights: string[];
  aa_itineraries: string | null;
  seo_title: string | null;
  seo_meta: string | null;
}
