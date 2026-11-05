// app/admin/tenant-qa/quality/messages.ts — AA-994: Content Quality (the page, its tabs and the label maps of
// shared.ts), English and Vietnamese. AA-1039: the place-card keys moved with the Cards tab to
// app/admin/places/cards/messages.ts. The drawers' text is drawer.messages.ts. Gate names come from the quality
// registry (lib/quality-registry.generated[.vi].ts) through useCqText(); gate codes, channels and stage names stay as
// the API sends them.

import { defineMessages } from "../../../_kit/core/i18n";
import {
  BINDING_LABELS, CLAIM_KIND_LABELS, DEFECT_NAMES, LABEL_HINTS, LABEL_NAMES, TOUR_FIELD_LABELS,
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

/** T9 piece status (content_piece.status), as the filters and badges show it. Place-card statuses moved with the
 *  Cards tab (app/admin/places/cards/messages.ts, AA-1039). */
export const STATUS = defineMessages(
  { approved: "approved", held: "held", failed: "failed", processing: "processing" },
  { approved: "đã duyệt", held: "bị giữ", failed: "thất bại", processing: "đang xử lý" },
);

export const M = defineMessages(
  {
    // tabs
    tabPieces: "Pieces (T9)",
    tabVersions: "Tour versions (T2)",
    tabStats: "Gate stats",
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
    // page
    title: "Content quality",
    description: "Every tenant's written content with the quality signals tenants never see: gate results, flagged "
      + "sentences, what the writer was given, cost. Tenants always receive their content.",
  },
  {
    tabPieces: "Bài viết (T9)",
    tabVersions: "Phiên bản tour (T2)",
    tabStats: "Thống kê gate",
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
    title: "Chất lượng nội dung",
    description: "Nội dung đã viết của mọi tenant cùng các tín hiệu chất lượng mà tenant không bao giờ thấy: kết quả "
      + "gate, câu bị gắn cờ, những gì trình viết được cung cấp, chi phí. Tenant luôn nhận được nội dung của mình.",
  },
);
