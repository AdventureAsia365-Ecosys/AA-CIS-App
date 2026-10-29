// lib/aaContent.messages.ts — AA-994: the labels and sentences of lib/aaContent.ts in English and Vietnamese.
// `AA_WORDS_MESSAGES[lang]` is the optional last argument of stateBadge / pendingDraftLabel / formProblems /
// aaErrorText / aaCall; the label maps are keyed by the API code (kind, locale, status filter).

import { defineMessages } from "../app/_kit/core/i18n";
import { AA_WORDS, KIND_LABEL, LOCALE_LABEL, STATUS_FILTER_LABEL } from "./aaContent";

export const AA_WORDS_MESSAGES = defineMessages(AA_WORDS, {
  retired: "Đã ngừng",
  publishedV: (v: string) => `Đã đăng v${v}`,
  draft: "Bản nháp",
  draftWaiting: (v: number) => `Bản nháp v${v} đang chờ`,
  titleRequired: "Cần có tiêu đề.",
  titleLong: (max: number) => `Tiêu đề dài hơn ${max} ký tự.`,
  textLong: (max: number) => `Văn bản dài hơn ${max} ký tự.`,
  tourIdLong: (max: number) => `Có một id tour dài hơn ${max} ký tự.`,
  network: "Lỗi mạng: chưa gửi được gì. Hãy thử lại.",
  stale: "Người khác đã lưu mục này sau khi bạn mở nó. Tải lại để xem thay đổi của họ, rồi sửa lại.",
  roleAdminOnly: "Vai trò của bạn không được thay đổi điều này (đăng, ngừng và khôi phục chỉ dành cho admin).",
  roleCannot: "Vai trò của bạn không được thay đổi điều này.",
  unavailable: "Kho nội dung AA chưa sẵn sàng. Hãy thử lại sau giây lát.",
  refusedShape: "Yêu cầu bị từ chối: thiếu một trường hoặc trường sai định dạng.",
  requestFailed: (status: number) => `Yêu cầu thất bại (${status}).`,
});

export const KIND_LABEL_MESSAGES = defineMessages(KIND_LABEL, {
  prep_pack: "Gói chuẩn bị trước chuyến đi",
  starter_template: "Mẫu khởi đầu",
  campaign_block: "Khối chiến dịch",
});

export const LOCALE_LABEL_MESSAGES = defineMessages(LOCALE_LABEL, {
  en: "Tiếng Anh",
  vi: "Tiếng Việt",
});

export const STATUS_FILTER_LABEL_MESSAGES = defineMessages(STATUS_FILTER_LABEL, {
  draft: "Có bản nháp",
  published: "Đã đăng",
  retired: "Đã ngừng",
});
