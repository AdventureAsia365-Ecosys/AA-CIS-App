// app/admin/places/cards/shared.ts — AA-1039 (R6 rest): the place-card API shapes of the Place cards page (moved
// with the Cards tab of Content quality; the API stays /admin/content-quality/place-cards*).

export interface Paged<T> { data: T[]; total: number; limit: number; offset: number }

export interface PlaceCard {
  card_id: string;
  display_name: string;
  locality: string | null;
  region: string | null;
  country: string;
  naming_note: string | null;
  facts: { text: string; kind: string }[];
  status: "auto_checked" | "verified" | "rejected";
  cost_usd: number;
  verified_by: string | null;
  verified_at: string | null;
  created_at: string;
  atoms: number;
}

// AA-774 (S235) — GET /place-cards/{id} and PATCH /place-cards/{id}/facts: one card with each fact's
// index in the stored array and whether the T9 writer seed shows it (what_it_is/history/setting/naming).
export interface PlaceCardFactRow { index: number; kind: string; text: string; in_writer_seed: boolean }
export interface PlaceCardDetail extends PlaceCard {
  updated_at: string;
  fact_rows: PlaceCardFactRow[];
  fact_kinds: string[];
}
export interface FactEdit { index: number; kind?: string; text?: string; drop?: boolean; expected_text?: string }

// S234 (AA-772) — GET /place-cards/duplicates: candidate clusters an admin may merge.
export interface DuplicateCard {
  card_id: string;
  display_name: string;
  locality: string | null;
  region: string | null;
  country: string;
  status: PlaceCard["status"];
  facts: number;
  atoms: number;
  cost_usd: number;
  created_at: string;
}
export interface DuplicateCluster {
  country: string;
  reasons: ("same_core_name" | "name_contains" | "alias")[];
  cards: DuplicateCard[];
}
export const DUPLICATE_REASON_LABELS = {
  same_core_name: "same name",
  name_contains: "one name contains the other",
  alias: "alias match",
};
