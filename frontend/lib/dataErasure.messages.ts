// lib/dataErasure.messages.ts — AA-994: the words of lib/dataErasure.ts (form problems, apply gate reasons, subject
// labels, action help) in English and Vietnamese: `applyGate(…, ERASURE_WORDS_MESSAGES[lang])`,
// `ACTION_HELP_MESSAGES[lang][action]`. Action names (delete, cascade, …) and table names stay as sent.

import { defineMessages } from "../app/_kit/core/i18n";
import { ACTION_HELP_EN, ERASURE_WORDS } from "./dataErasure";

export const ERASURE_WORDS_MESSAGES = defineMessages(ERASURE_WORDS, {
  pickTenant: "Chọn một tenant hoặc dán id của nó.",
  tenantUuid: "Id tenant là một uuid (8-4-4-4-12 ký tự hex).",
  tenantPlatform: "Không bao giờ được xoá tenant nội bộ AA.",
  noRows: "không có dòng nào",
  snapshotEmpty: "Nhập id của snapshot RDS thủ công đã chụp cho lần xoá này.",
  snapshotShape: "Id snapshot RDS bắt đầu bằng một chữ cái và chỉ gồm chữ cái, chữ số và dấu gạch nối đơn.",
  runDryFirst: "Hãy chạy DRY RUN trước.",
  notErasure: "Job này không phải là một lần xoá dữ liệu.",
  isApply: "Job này là một lần áp dụng, không phải DRY RUN.",
  notSucceeded: "DRY RUN chưa thành công.",
  noSubject: "Job không nêu đối tượng (tenant hay khách du lịch).",
  verifyRun: "Đây là DRY RUN kiểm chứng sau một lần áp dụng; không thể áp dụng nó.",
  redacted: "Vai trò của bạn chỉ xem được các con số của job này.",
  noFingerprint: "DRY RUN không có fingerprint.",
  blocked: "DRY RUN có điểm chặn: xử lý chúng rồi chạy một DRY RUN mới.",
  usedBy: (id: string) => `DRY RUN này đã được job áp dụng ${id} dùng; hãy chạy một DRY RUN mới.`,
  queuedApply: (id: string) => `Job áp dụng ${id} đang dùng DRY RUN này.`,
  hashGone: "Job không còn giữ hash của khách; hãy chạy một DRY RUN mới.",
  tooOld: (hours: number) => `DRY RUN đã cũ hơn ${hours} giờ; hãy chạy DRY RUN mới và duyệt nó.`,
  applyOff: "Môi trường này đang tắt áp dụng (ERASURE_APPLY_ENABLED không phải true trên API và worker).",
  tenantNotOffboarded: (state: string | null) =>
    `Tenant đang ở trạng thái ${state ?? "không tồn tại"}, chưa offboard.`,
  retention: (days: number, window: number) =>
    `Đã offboard ${days} ngày trước; thời gian lưu giữ là ${window} ngày, nên job sẽ từ chối.`,
  unknownId: "id không rõ",
  tenantNamed: (name: string, slug: string, id: string) => `tenant ${name} (${slug}) · ${id}`,
  tenantId: (id: string) => `tenant ${id}`,
  travellerHash: (prefix: string) => `hash của khách ${prefix}…`,
  travellerNoHash: "khách du lịch (không còn giữ hash)",
  typeFingerprint: (n: number) => `Nhập ${n} ký tự đầu của fingerprint.`,
  approverEmpty: "Nhập người đã duyệt fingerprint này.",
  approverLong: "Tên người duyệt tối đa 120 ký tự.",
  tickConfirm: "Đánh dấu ô xác nhận.",
  serverRefused: (msg: string) => `Máy chủ từ chối biểu mẫu: ${msg}`,
  requestFailed: (status: number) => `Yêu cầu thất bại (${status})`,
});

export const ACTION_HELP_MESSAGES = defineMessages(ACTION_HELP_EN, {
  delete: "xoá các dòng",
  cascade: "xoá dòng con cùng dòng cha (tìm qua khoá ngoại)",
  null_first: "đặt null một khoá ngoại trước để phá vòng",
  redact: "gỡ hash hoặc địa chỉ khỏi dòng; dòng vẫn giữ",
  detach: "giữ dòng, đặt null liên kết tenant và các khoá mềm",
  merge: "cộng vào dòng của nền tảng (không tenant) cùng ngày",
  keep: "giữ lại (nhật ký kiểm toán chỉ ghi thêm)",
  manual: "một người xử lý (job không thay đổi)",
});
