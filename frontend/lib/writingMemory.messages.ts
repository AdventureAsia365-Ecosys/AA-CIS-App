// lib/writingMemory.messages.ts
// AA-994 — the text of lib/writingMemory.ts in English and Vietnamese, the same keys as the English constants there:
//   useT(LINT_LABEL_MESSAGES)(check), useT(CHECK_CODE_MESSAGES)(code), useT(EVIDENCE_WORD_MESSAGES)(word),
//   owner(scope, id, name, OWNER_MESSAGES[lang]), wmSend(path, method, body, SEND_MESSAGES[lang]),
//   checkSentence(report, CHECK_SENTENCE_MESSAGES[lang]). actorLabel() prints "admin" + an id in both languages.

import { defineMessages } from "../app/_kit/core/i18n";
import { CHECK_CODE_LABELS, CHECK_TEXT, LINT_LABEL, OWNER_TEXT, SEND_TEXT } from "./writingMemory";

export const LINT_LABEL_MESSAGES = defineMessages(LINT_LABEL, {
  shape: "Kích thước",
  constraint_only: "Chỉ giới hạn",
  lexicon_specifics: "Từ thay không thêm gì",
  five_gram_overlap: "Không chép văn bản tenant",
  predict: "Giải thích được các sửa giữ lại",
});

export const CHECK_CODE_MESSAGES = defineMessages(
  { qa_trace: CHECK_CODE_LABELS.qa_trace, tenant_leak: CHECK_CODE_LABELS.tenant_leak,
    fingerprint_mismatch: CHECK_CODE_LABELS.fingerprint_mismatch },
  { qa_trace: "dấu vết QA trong văn bản", tenant_leak: "văn bản của tenant khác",
    fingerprint_mismatch: "lệch mã băm quy tắc" },
);

export const OWNER_MESSAGES = defineMessages(OWNER_TEXT, {
  platform: "Nền tảng",
  tenantShort: (id: string) => `Tenant ${id}`,
});

export const SEND_MESSAGES = defineMessages(SEND_TEXT, {
  requestFailed: (status: number) => `Yêu cầu thất bại (${status})`,
  noAnswer: "Máy chủ không phản hồi; hãy tải lại trang trước khi thử lại.",
});

export const CHECK_SENTENCE_MESSAGES = defineMessages(CHECK_TEXT, {
  neverActive: "Bản phát hành này chưa từng hoạt động, nên không viết gì để kiểm.",
  emptyRelease: "Bản phát hành rỗng không thêm gì vào prompt, nên không có gì để kiểm.",
  violation: "Ít nhất một lần viết dùng bản phát hành này vi phạm một điểm chặn cứng. Lượt kiểm hằng giờ sẽ quay "
    + "lui nó; lần chạy thử này không thay đổi gì.",
  pass: (carried: number) => `${carried} lần viết mang bản phát hành này và không lần nào vi phạm điểm chặn cứng.`,
  pending: (carried: number, n: number, checked: number) =>
    `${carried} trên ${n} lần viết cần có đã mang bản phát hành này, chưa lần nào vi phạm điểm chặn cứng `
    + `(đã đọc ${checked} lần viết).`,
});

/** evidenceWord()'s words ("—" stays as it is). */
export const EVIDENCE_WORD_MESSAGES = defineMessages(
  { "—": "—", missing: "missing", running: "running", baseline: "baseline", passed: "passed", failed: "failed" },
  { "—": "—", missing: "không thấy", running: "đang chạy", baseline: "baseline", passed: "đạt", failed: "trượt" },
);
