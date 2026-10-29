// app/admin/content-quality/messages.ts — AA-994: Content Quality (the page, its tabs and the label maps of
// shared.ts), English and Vietnamese. The drawers' text is drawer.messages.ts. Gate names come from the quality
// registry (lib/quality-registry.generated[.vi].ts) through useCqText(); gate codes, channels and stage names stay as
// the API sends them.

import { defineMessages } from "../../_kit/core/i18n";
import {
  BINDING_LABELS, CLAIM_KIND_LABELS, DEFECT_NAMES, DUPLICATE_REASON_LABELS, LABEL_HINTS, LABEL_NAMES, TOUR_FIELD_LABELS,
} from "./shared";

export const TOUR_FIELD = defineMessages(TOUR_FIELD_LABELS, {
  subtitle: "Phụ đề", summary: "Tóm tắt", highlights: "Điểm nổi bật", itineraries: "Lịch trình",
});

export const BINDING = defineMessages(BINDING_LABELS, {
  brand_fit: "Độ hợp thương hiệu",
  cross_brand: "Độ khác biệt giữa các thương hiệu",
  mission: "Trần sứ mệnh",
  tiebreak: "Jev phân xử hoà",
  validate: "Kiểm cấu trúc",
  unjudged: "Chưa chấm",
});

export const CLAIM_KIND = defineMessages(CLAIM_KIND_LABELS, {
  age: "Độ tuổi", group_size: "Quy mô nhóm", ethos: "Tinh thần thương hiệu",
});

export const DUPLICATE_REASON = defineMessages(DUPLICATE_REASON_LABELS, {
  same_core_name: "cùng tên", name_contains: "tên này chứa tên kia", alias: "khớp bí danh",
});

export const LABEL_NAME = defineMessages(LABEL_NAMES, {
  as_is: "Giữ nguyên", light: "Sửa nhẹ", heavy: "Sửa nhiều", reject: "Loại bỏ",
});

export const LABEL_HINT = defineMessages(LABEL_HINTS, {
  as_is: "Đăng được mà không cần sửa",
  light: "≤10 phút, ≤3 câu, không cần tra cứu",
  heavy: "Nhiều hơn một lần sửa nhẹ",
  reject: "Không đáng để sửa",
});

export const DEFECT = defineMessages(DEFECT_NAMES, {
  fact: "Dữ kiện", voice: "Giọng văn", format: "Định dạng", ai_phrasing: "Văn phong AI",
});

/** Defect levels of a label (shared.ts DefectLevel). */
export const DEFECT_LEVEL = defineMessages(
  { none: "none", light: "light", heavy: "heavy" },
  { none: "không", light: "nhẹ", heavy: "nặng" },
);

/** T9 piece status (content_piece.status) and place-card status, as the filters and badges show them. */
export const STATUS = defineMessages(
  { approved: "approved", held: "held", failed: "failed", processing: "processing",
    auto_checked: "auto_checked", verified: "verified", rejected: "rejected" },
  { approved: "đã duyệt", held: "bị giữ", failed: "thất bại", processing: "đang xử lý",
    auto_checked: "tự kiểm", verified: "đã xác minh", rejected: "bị từ chối" },
);

export const M = defineMessages(
  {
    // tabs
    tabPieces: "Pieces (T9)",
    tabVersions: "Tour versions (T2)",
    tabStats: "Gate stats",
    tabCards: "Place cards",
    // pieces
    colWritten: "Written",
    colTenant: "Tenant",
    colChannel: "Channel",
    colTitleSeed: "Title / angle · seed",
    colStatus: "Status",
    colLabel: "Label",
    colBlockingFails: "Blocking fails",
    colWarnings: "Warnings",
    colAttempt: "Att.",
    colCost: "Cost",
    piecesEmpty: "No pieces match these filters",
    piecesEmptyDesc: "T9 pieces from every tenant show here.",
    searchPieces: "Search title / text…",
    searchPiecesAria: "Search pieces",
    channelAria: "Channel",
    allChannels: "All channels",
    statusAria: "Status",
    allStatuses: "All statuses",
    gateAria: "Gate",
    labelAria: "Label",
    anyLabel: "Any label",
    labelled: "Labelled",
    notLabelled: "Not labelled",
    allTenants: "All tenants",
    // versions
    colTour: "Tour",
    colVersion: "v",
    colScore: "Score",
    colQa: "QA",
    qaAuto: " · auto",
    colSetBy: "Set by",
    colBrandClaims: "Brand claims",
    notInSource: (n: number) => `${n} not in source`,
    colEscalations: "Escalations",
    versionsEmpty: "No tour versions yet",
    searchTour: "Search tour…",
    searchToursAria: "Search tours",
    // stats
    colGate: "Gate",
    colRuns: "Runs",
    colFailed: "Failed",
    colFailRate: "Fail rate",
    colPieces: "Pieces",
    colApproved: "Approved",
    colHeld: "Held",
    colLlmCost: "LLM cost",
    statsNote: "Last 30 days, final attempt of each piece. Counted from the gate ledger — every gate, not only the "
      + "one named in held_reason.",
    statsEmpty: "No pieces in this window",
    // duplicates
    keepAria: (name: string) => `Keep ${name}`,
    keep: "keep",
    mergeAria: (name: string) => `Merge ${name}`,
    merge: "merge",
    noLocality: "no locality",
    cardCounts: (atoms: number, facts: number) => `${atoms} atoms · ${facts} facts`,
    mergeInto: (n: string, name: string) => `Merge ${n} into “${name}”`,
    merged: (n: number) => `Merged — ${n} atom link(s) moved`,
    couldNotMerge: (e: string) => `Could not merge: ${e}`,
    hideDuplicates: "Hide duplicates",
    findDuplicates: "Find duplicate cards",
    candidateGroups: (n: number) => `${n} candidate group(s) — never merged automatically`,
    loading: "Loading…",
    couldNotLoadDuplicates: (e: string) => `Could not load duplicates: ${e}`,
    mergeTitle: "Merge place cards",
    mergeBody: (n: number, name: string) => `Merge ${n} card(s) into “${name}”? Their atoms move to this card, their `
      + "names become aliases, and the merged cards are deleted.",
    mergeConfirm: "Merge",
    // place cards
    couldNotSave: (e: string) => `Could not save: ${e}`,
    jobQueued: (job: string) => `Place-card job queued (${job})`,
    couldNotQueue: (e: string) => `Could not queue: ${e}`,
    colPlace: "Place",
    colCheckedFacts: "Checked facts",
    colAtoms: "Atoms",
    verify: "Verify",
    reject: "Reject",
    facts: "Facts",
    cardsEmpty: "No place cards yet",
    cardsEmptyDesc: "Cards are built when T8 writes angles for an atom, or for a whole tour below.",
    searchPlaces: "Search place / country…",
    searchPlacesAria: "Search places",
    cardStatusAria: "Card status",
    tourIdAria: "Tour id",
    buildCards: "Build cards for tour",
    retryMissesTitle: "Retry places whose last draft found no place or was rejected",
    retryMissesAria: "Retry cached misses",
    retryMisses: "retry misses",
    // page
    title: "Content Quality",
    description: "Every tenant's written content with the quality signals tenants never see: gate results, flagged "
      + "sentences, what the writer was given, cost. Tenants always receive their content.",
  },
  {
    tabPieces: "Bài viết (T9)",
    tabVersions: "Phiên bản tour (T2)",
    tabStats: "Thống kê gate",
    tabCards: "Thẻ địa điểm",
    colWritten: "Ngày viết",
    colTenant: "Tenant",
    colChannel: "Kênh",
    colTitleSeed: "Tiêu đề / góc viết · seed",
    colStatus: "Trạng thái",
    colLabel: "Nhãn",
    colBlockingFails: "Lỗi chặn",
    colWarnings: "Cảnh báo",
    colAttempt: "Lần",
    colCost: "Chi phí",
    piecesEmpty: "Không có bài nào khớp bộ lọc",
    piecesEmptyDesc: "Bài T9 của mọi tenant hiện ở đây.",
    searchPieces: "Tìm tiêu đề / nội dung…",
    searchPiecesAria: "Tìm bài viết",
    channelAria: "Kênh",
    allChannels: "Mọi kênh",
    statusAria: "Trạng thái",
    allStatuses: "Mọi trạng thái",
    gateAria: "Gate",
    labelAria: "Nhãn",
    anyLabel: "Mọi nhãn",
    labelled: "Đã gắn nhãn",
    notLabelled: "Chưa gắn nhãn",
    allTenants: "Mọi tenant",
    colTour: "Tour",
    colVersion: "v",
    colScore: "Điểm",
    colQa: "QA",
    qaAuto: " · tự động",
    colSetBy: "Quyết định bởi",
    colBrandClaims: "Khẳng định thương hiệu",
    notInSource: (n: number) => `${n} không có trong nguồn`,
    colEscalations: "Chuyển cấp",
    versionsEmpty: "Chưa có phiên bản tour nào",
    searchTour: "Tìm tour…",
    searchToursAria: "Tìm tour",
    colGate: "Gate",
    colRuns: "Lượt chạy",
    colFailed: "Trượt",
    colFailRate: "Tỉ lệ trượt",
    colPieces: "Bài",
    colApproved: "Đã duyệt",
    colHeld: "Bị giữ",
    colLlmCost: "Chi phí LLM",
    statsNote: "30 ngày gần nhất, lần thử cuối của mỗi bài. Đếm từ sổ gate — mọi gate, không chỉ gate được nêu trong "
      + "held_reason.",
    statsEmpty: "Không có bài nào trong khoảng này",
    keepAria: (name: string) => `Giữ ${name}`,
    keep: "giữ",
    mergeAria: (name: string) => `Gộp ${name}`,
    merge: "gộp",
    noLocality: "không có địa phương",
    cardCounts: (atoms: number, facts: number) => `${atoms} atom · ${facts} dữ kiện`,
    mergeInto: (n: string, name: string) => `Gộp ${n} vào “${name}”`,
    merged: (n: number) => `Đã gộp — chuyển ${n} liên kết atom`,
    couldNotMerge: (e: string) => `Không gộp được: ${e}`,
    hideDuplicates: "Ẩn thẻ trùng",
    findDuplicates: "Tìm thẻ trùng",
    candidateGroups: (n: number) => `${n} nhóm có thể trùng — không bao giờ tự gộp`,
    loading: "Đang tải…",
    couldNotLoadDuplicates: (e: string) => `Không tải được thẻ trùng: ${e}`,
    mergeTitle: "Gộp thẻ địa điểm",
    mergeBody: (n: number, name: string) => `Gộp ${n} thẻ vào “${name}”? Các atom của chúng chuyển sang thẻ này, tên `
      + "của chúng thành bí danh, và các thẻ đã gộp bị xoá.",
    mergeConfirm: "Gộp",
    couldNotSave: (e: string) => `Không lưu được: ${e}`,
    jobQueued: (job: string) => `Đã xếp job thẻ địa điểm (${job})`,
    couldNotQueue: (e: string) => `Không xếp được job: ${e}`,
    colPlace: "Địa điểm",
    colCheckedFacts: "Dữ kiện đã kiểm",
    colAtoms: "Atom",
    verify: "Xác minh",
    reject: "Từ chối",
    facts: "Dữ kiện",
    cardsEmpty: "Chưa có thẻ địa điểm nào",
    cardsEmptyDesc: "Thẻ được dựng khi T8 viết góc cho một atom, hoặc cho cả một tour ở bên dưới.",
    searchPlaces: "Tìm địa điểm / quốc gia…",
    searchPlacesAria: "Tìm địa điểm",
    cardStatusAria: "Trạng thái thẻ",
    tourIdAria: "Id tour",
    buildCards: "Dựng thẻ cho tour",
    retryMissesTitle: "Thử lại các địa điểm mà lần soạn cuối không tìm thấy địa điểm hoặc bị từ chối",
    retryMissesAria: "Thử lại các lần trượt đã lưu",
    retryMisses: "thử lại lần trượt",
    title: "Chất lượng nội dung",
    description: "Nội dung đã viết của mọi tenant cùng các tín hiệu chất lượng mà tenant không bao giờ thấy: kết quả "
      + "gate, câu bị gắn cờ, những gì trình viết được cung cấp, chi phí. Tenant luôn nhận được nội dung của mình.",
  },
);
