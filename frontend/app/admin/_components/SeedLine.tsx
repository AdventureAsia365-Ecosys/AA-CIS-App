// app/admin/_components/SeedLine.tsx — AA-863. The Slate seed of one Subject / T9 piece, admin only:
// the seed atom's place · country (and, optionally, the Segment label it was written for) plus a
// warning badge when the seed does not fit the Subject (backend: services/acp_shared/seed_audit.py —
// place under subject_seed.match_grade, country vs the Segment's member countries).
// Used by Content Quality (Pieces table + piece drawer) and the atom-curation Slate tab.
// Never import this from a tenant (portal) page: seed and mismatch data are admin-only.

import { Badge, K, translator, useLang, useT, type Lang } from "../../_kit";
import { M, SEED_MATCH, SEED_SKIP } from "./SeedLine.messages";

export interface SeedInfo {
  atom_id: string | null;
  place: string | null;
  country: string | null;
  /** "request" = frozen angle_gate_request.atom_id; "computed" = what a pick would use now. */
  source: "request" | "computed" | null;
  skip_reason: string | null;
  subject_kind: "segment" | "route" | null;
  segment_id: string | null;
  segment_label: string | null;
  subject_place: string | null;
  segment_countries: string[];
  match: string | null;
  place_mismatch: boolean;
  country_mismatch: boolean;
  mismatch: boolean;
}

const line: React.CSSProperties = {
  display: "block", minWidth: 0, maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
};

function seedTitle(s: SeedInfo, lang: Lang): string {
  const t = translator(M, lang);
  const match = translator(SEED_MATCH, lang);
  const kind = s.subject_kind === "route" ? "route" : "segment";
  const parts = [
    s.atom_id ? t("seedAtom", s.atom_id) : t("noSeedAtom"),
    s.source === "request" ? t("frozen") : s.source === "computed" ? t("computed") : "",
    s.subject_place ? t("subjectPlace", s.subject_place) : "",
    s.match ? t("placeMatch", match(s.match as keyof typeof SEED_MATCH.en)) : "",
    s.segment_countries.length ? t("kindCountries", kind, s.segment_countries.join(", ")) : "",
  ];
  return parts.filter(Boolean).join(" · ");
}

/** `showSegment`: also print the Segment / Route label (the Pieces table has no Subject column). */
export function SeedLine({ seed, showSegment = false }: { seed: SeedInfo | null | undefined; showSegment?: boolean }) {
  const lang = useLang();
  const t = useT(M);
  const skip = useT(SEED_SKIP);
  if (!seed) return null;
  const mixed = seed.segment_countries.length > 1;
  const kind = seed.subject_kind === "route" ? "route" : "segment";
  const skipText = (r: string) => skip(r as keyof typeof SEED_SKIP.en);
  return (
    <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0, fontSize: 11, color: K.muted, marginTop: 2 }}>
      {showSegment && seed.segment_label && (
        <span style={line} title={seed.segment_label}>{t("kindLabel", kind, seed.segment_label)}</span>
      )}
      <span style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 4, minWidth: 0 }} title={seedTitle(seed, lang)}>
        {seed.place || seed.country ? (
          <span style={{ ...line, color: seed.mismatch ? K.ink3 : K.muted }}>
            {t("seedPlace", seed.place || "—", seed.country || t("countryUnknown"))}
          </span>
        ) : (
          <span style={line}>{t("seedId", seed.atom_id ?? t("none"))}</span>
        )}
        {seed.place_mismatch && (
          <span title={t("placeMismatchTitle", seed.place ?? "", seed.subject_place ?? "")}>
            <Badge tone="warning" dot={false}>{t("placeMismatch")}</Badge>
          </span>
        )}
        {seed.country_mismatch && (
          <span title={mixed
            ? t("spans", kind, seed.segment_countries.join(", "))
            : t("seedIn", seed.country ?? "", kind, seed.segment_countries.join(", "))}>
            <Badge tone="warning" dot={false}>{mixed ? t("mixed", kind) : t("countryMismatch", kind)}</Badge>
          </span>
        )}
        {!seed.atom_id && seed.skip_reason && (
          <span title={t("noSeedTitle", skipText(seed.skip_reason))}>
            <Badge tone="warning" dot={false}>{t("noSeed")}</Badge>
          </span>
        )}
        {seed.atom_id && seed.skip_reason === "seed_atom_missing" && (
          <span title={skip("seed_atom_missing")}><Badge tone="warning" dot={false}>{t("seedGone")}</Badge></span>
        )}
      </span>
    </span>
  );
}

/** One-line plain reason for a missing seed (for places with room to print it, e.g. the drawer). */
export function seedSkipText(reason: string | null | undefined, lang: Lang = "en"): string | null {
  return reason ? translator(SEED_SKIP, lang)(reason as keyof typeof SEED_SKIP.en) : null;
}
