// lib/dataCoverage.messages.ts — AA-994: the words of lib/dataCoverage.ts in English and Vietnamese
// (`statusBadge(row, DC_MESSAGES[lang])`, `csvColumns(DC_MESSAGES[lang])`, …).

import { defineMessages } from "../app/_kit/core/i18n";
import { DC_WORDS } from "./dataCoverage";

export const DC_MESSAGES = defineMessages(DC_WORDS, {
  badgeOk: "OK",
  badgeBacklog: "Tồn đọng",
  badgeGap: "Thiếu",
  badgeInfo: "Thông tin",
  badgeNa: "không có",
  ageSeconds: (n: number) => `${n} giây`,
  ageMinutes: (n: number) => `${n} phút`,
  ageHours: (n: number) => `${n} giờ`,
  notAvailable: "không có dữ liệu",
  csvStage: "Giai đoạn",
  csvMetric: "Chỉ số",
  csvMetricId: "Mã chỉ số",
  csvCount: "Số lượng",
  csvOf: "Trên",
  csvOfLabel: "Trên (nhãn)",
  csvLimit: "giới hạn",
  csvMissing: "Còn thiếu",
  csvExpectedGap: "Mức thiếu dự kiến",
  csvExpectedNote: "Ghi chú dự kiến",
  csvKind: "Loại",
  csvStatus: "Trạng thái",
  csvReason: "Lý do",
  csvIssue: "Issue",
  csvSource: "Nguồn",
  csvAsOf: "Thời điểm",
});
