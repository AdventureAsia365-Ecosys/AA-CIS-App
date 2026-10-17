// app/admin/_components/SeedLine.tsx — AA-863. The Slate seed of one Subject / T9 piece, admin only:
// the seed atom's place · country (and, optionally, the Segment label it was written for) plus a
// warning badge when the seed does not fit the Subject (backend: services/acp_shared/seed_audit.py —
// place under subject_seed.match_grade, country vs the Segment's member countries).
// Used by Content Quality (Pieces table + piece drawer) and the atom-curation Slate tab.
// Never import this from a tenant (portal) page: seed and mismatch data are admin-only.

import { Badge, K } from "../../_kit";

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

const SKIP_TEXT: Record<string, string> = {
  no_live_member: "the Segment has no live member",
  no_member_on_tenant_tours: "no member is on this tenant's tours",
  no_member_matches_subject_place: "no member is this Subject's place",
  subject_place_in_several_countries: "the place matches members in several countries",
  loose_place_match_country_unknown: "only a loose name match, country unknown",
  no_member_on_route_tour: "no member on the Route's tour",
  seed_atom_missing: "the seed atom no longer exists",
};

const MATCH_TEXT: Record<string, string> = {
  exact: "exact name",
  name_form: "same name form",
  core_or_card: "same core name or place card",
  parenthetical: "named only in the member's parenthetical",
  loose_words: "loose word-set match",
};

const line: React.CSSProperties = {
  display: "block", minWidth: 0, maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
};

function seedTitle(s: SeedInfo): string {
  const parts = [
    s.atom_id ? `Seed atom ${s.atom_id}` : "No seed atom",
    s.source === "request" ? "frozen when the Subject was picked" : s.source === "computed" ? "the seed a pick would use now" : "",
    s.subject_place ? `Subject place: ${s.subject_place}` : "",
    s.match ? `Place match: ${MATCH_TEXT[s.match] ?? s.match}` : "",
    s.segment_countries.length ? `${s.subject_kind === "route" ? "Route" : "Segment"} countries: ${s.segment_countries.join(", ")}` : "",
  ];
  return parts.filter(Boolean).join(" · ");
}

/** `showSegment`: also print the Segment / Route label (the Pieces table has no Subject column). */
export function SeedLine({ seed, showSegment = false }: { seed: SeedInfo | null | undefined; showSegment?: boolean }) {
  if (!seed) return null;
  const mixed = seed.segment_countries.length > 1;
  const kindWord = seed.subject_kind === "route" ? "Route" : "Segment";
  return (
    <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0, fontSize: 11, color: K.muted, marginTop: 2 }}>
      {showSegment && seed.segment_label && (
        <span style={line} title={seed.segment_label}>{kindWord}: {seed.segment_label}</span>
      )}
      <span style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 4, minWidth: 0 }} title={seedTitle(seed)}>
        {seed.place || seed.country ? (
          <span style={{ ...line, color: seed.mismatch ? K.ink3 : K.muted }}>
            Seed: {seed.place || "—"} · {seed.country || "country unknown"}
          </span>
        ) : (
          <span style={line}>Seed: {seed.atom_id ?? "none"}</span>
        )}
        {seed.place_mismatch && (
          <span title={`The seed place "${seed.place}" is not the Subject place "${seed.subject_place}" under the seed rule.`}>
            <Badge tone="warning" dot={false}>Place ≠ Subject</Badge>
          </span>
        )}
        {seed.country_mismatch && (
          <span title={mixed
            ? `This ${kindWord} spans ${seed.segment_countries.join(", ")} — it has no single country.`
            : `The seed is in ${seed.country}; the ${kindWord} is in ${seed.segment_countries.join(", ")}.`}>
            <Badge tone="warning" dot={false}>{mixed ? `Mixed ${kindWord}` : `Country ≠ ${kindWord}`}</Badge>
          </span>
        )}
        {!seed.atom_id && seed.skip_reason && (
          <span title={`No seed: ${SKIP_TEXT[seed.skip_reason] ?? seed.skip_reason}.`}>
            <Badge tone="warning" dot={false}>No seed</Badge>
          </span>
        )}
        {seed.atom_id && seed.skip_reason === "seed_atom_missing" && (
          <span title={SKIP_TEXT.seed_atom_missing}><Badge tone="warning" dot={false}>Seed atom gone</Badge></span>
        )}
      </span>
    </span>
  );
}

/** One-line plain reason for a missing seed (for places with room to print it, e.g. the drawer). */
export function seedSkipText(reason: string | null | undefined): string | null {
  return reason ? SKIP_TEXT[reason] ?? reason : null;
}
