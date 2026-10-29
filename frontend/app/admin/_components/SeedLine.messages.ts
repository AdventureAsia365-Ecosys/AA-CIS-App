// app/admin/_components/SeedLine.messages.ts — AA-994: the Slate seed line (SeedLine.tsx), English and Vietnamese.
// Product words kept as the admin uses them: Segment, Route, Subject, seed, atom.

import { defineMessages } from "../../_kit/core/i18n";

type Kind = "route" | "segment";

export const SEED_SKIP = defineMessages(
  {
    no_live_member: "the Segment has no live member",
    no_member_on_tenant_tours: "no member is on this tenant's tours",
    no_member_matches_subject_place: "no member is this Subject's place",
    subject_place_in_several_countries: "the place matches members in several countries",
    loose_place_match_country_unknown: "only a loose name match, country unknown",
    no_member_on_route_tour: "no member on the Route's tour",
    seed_atom_missing: "the seed atom no longer exists",
  },
  {
    no_live_member: "Segment không còn thành viên nào đang hoạt động",
    no_member_on_tenant_tours: "không thành viên nào nằm trên tour của tenant này",
    no_member_matches_subject_place: "không thành viên nào là địa điểm của Subject này",
    subject_place_in_several_countries: "địa điểm khớp với thành viên ở nhiều quốc gia",
    loose_place_match_country_unknown: "chỉ khớp tên lỏng, không rõ quốc gia",
    no_member_on_route_tour: "không có thành viên nào trên tour của Route",
    seed_atom_missing: "atom seed không còn tồn tại",
  },
);

export const SEED_MATCH = defineMessages(
  {
    exact: "exact name",
    name_form: "same name form",
    core_or_card: "same core name or place card",
    parenthetical: "named only in the member's parenthetical",
    loose_words: "loose word-set match",
  },
  {
    exact: "trùng tên chính xác",
    name_form: "cùng dạng tên",
    core_or_card: "cùng tên gốc hoặc thẻ địa điểm",
    parenthetical: "chỉ được nhắc trong ngoặc đơn của thành viên",
    loose_words: "khớp lỏng theo tập từ",
  },
);

export const M = defineMessages(
  {
    kind: (k: Kind) => (k === "route" ? "Route" : "Segment"),
    seedAtom: (id: string) => `Seed atom ${id}`,
    noSeedAtom: "No seed atom",
    frozen: "frozen when the Subject was picked",
    computed: "the seed a pick would use now",
    subjectPlace: (p: string) => `Subject place: ${p}`,
    placeMatch: (m: string) => `Place match: ${m}`,
    kindCountries: (k: Kind, list: string) => `${k === "route" ? "Route" : "Segment"} countries: ${list}`,
    kindLabel: (k: Kind, label: string) => `${k === "route" ? "Route" : "Segment"}: ${label}`,
    seedPlace: (place: string, country: string) => `Seed: ${place} · ${country}`,
    countryUnknown: "country unknown",
    seedId: (id: string) => `Seed: ${id}`,
    none: "none",
    placeMismatchTitle: (seed: string, subject: string) =>
      `The seed place "${seed}" is not the Subject place "${subject}" under the seed rule.`,
    placeMismatch: "Place ≠ Subject",
    spans: (k: Kind, list: string) => `This ${k === "route" ? "Route" : "Segment"} spans ${list} — it has no single country.`,
    seedIn: (country: string, k: Kind, list: string) =>
      `The seed is in ${country}; the ${k === "route" ? "Route" : "Segment"} is in ${list}.`,
    mixed: (k: Kind) => `Mixed ${k === "route" ? "Route" : "Segment"}`,
    countryMismatch: (k: Kind) => `Country ≠ ${k === "route" ? "Route" : "Segment"}`,
    noSeedTitle: (why: string) => `No seed: ${why}.`,
    noSeed: "No seed",
    seedGone: "Seed atom gone",
  },
  {
    kind: (k: Kind) => (k === "route" ? "Route" : "Segment"),
    seedAtom: (id: string) => `Atom seed ${id}`,
    noSeedAtom: "Không có atom seed",
    frozen: "cố định khi Subject được chọn",
    computed: "seed mà một lần chọn sẽ dùng bây giờ",
    subjectPlace: (p: string) => `Địa điểm của Subject: ${p}`,
    placeMatch: (m: string) => `Khớp địa điểm: ${m}`,
    kindCountries: (k: Kind, list: string) => `Quốc gia của ${k === "route" ? "Route" : "Segment"}: ${list}`,
    kindLabel: (k: Kind, label: string) => `${k === "route" ? "Route" : "Segment"}: ${label}`,
    seedPlace: (place: string, country: string) => `Seed: ${place} · ${country}`,
    countryUnknown: "không rõ quốc gia",
    seedId: (id: string) => `Seed: ${id}`,
    none: "không có",
    placeMismatchTitle: (seed: string, subject: string) =>
      `Địa điểm seed "${seed}" không phải địa điểm của Subject "${subject}" theo quy tắc seed.`,
    placeMismatch: "Địa điểm ≠ Subject",
    spans: (k: Kind, list: string) =>
      `${k === "route" ? "Route" : "Segment"} này trải qua ${list} — không thuộc một quốc gia duy nhất.`,
    seedIn: (country: string, k: Kind, list: string) =>
      `Seed ở ${country}; ${k === "route" ? "Route" : "Segment"} ở ${list}.`,
    mixed: (k: Kind) => `${k === "route" ? "Route" : "Segment"} hỗn hợp`,
    countryMismatch: (k: Kind) => `Quốc gia ≠ ${k === "route" ? "Route" : "Segment"}`,
    noSeedTitle: (why: string) => `Không có seed: ${why}.`,
    noSeed: "Không có seed",
    seedGone: "Atom seed đã mất",
  },
);
