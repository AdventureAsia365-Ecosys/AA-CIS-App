// lib/supplyContent.messages.ts — AA-994: the labels and sentences of lib/supplyContent.ts in English and Vietnamese.
// `SUPPLY_WORDS_MESSAGES[lang]` is the optional last argument of publishBlockers / formProblems / supplyErrorText /
// supplyCall; the label maps are keyed by the API code (content status, entity type, photo state).

import { defineMessages } from "../app/_kit/core/i18n";
import { PHOTO_STATE, STATUS_HINT, STATUS_LABEL, SUPPLY_WORDS, TYPE_LABEL } from "./supplyContent";

export const SUPPLY_WORDS_MESSAGES = defineMessages(SUPPLY_WORDS, {
  versionIs: (status: string) => `Phiên bản này đang ở trạng thái ${status}.`,
  inactive: "Mục này đang ngừng hoạt động trong Booking.",
  outdated: (written: number, at: number) =>
    `Viết cho dữ kiện v${written}; Booking đang ở v${at}. Hãy duyệt bản nháp mới hơn hoặc viết một bản sửa.`,
  photosNotCleared: (n: number) => `${n} ảnh chưa được phép dùng (quyền sử dụng hoặc bản sao).`,
  descEmpty: "Phần mô tả đang trống.",
  descLong: (max: number) => `Phần mô tả dài quá ${max} ký tự.`,
  maxHighlights: (n: number) => `Tối đa ${n} điểm nổi bật (mỗi dòng một điểm).`,
  maxGoodToKnow: (n: number) => `Tối đa ${n} dòng "nên biết".`,
  itemLong: (max: number) => `Có một dòng điểm nổi bật hoặc "nên biết" dài quá ${max} ký tự.`,
  requestFailed: (status: number) => `Yêu cầu thất bại (${status})`,
  photoCount: (n: number) => ` (${n} ảnh)`,
  invalidValue: "giá trị không hợp lệ",
  forbidden: "Vai trò của bạn không được làm việc này.",
  network: "Lỗi mạng — hãy thử lại.",
});

export const STATUS_LABEL_MESSAGES = defineMessages(STATUS_LABEL, {
  draft: "Chưa có nội dung",
  in_review: "Bản nháp chờ duyệt",
  published: "Đã đăng",
  stale: "Lỗi thời (dữ kiện đã đổi)",
  unpublished: "Đã gỡ đăng",
});

export const STATUS_HINT_MESSAGES = defineMessages(STATUS_HINT, {
  draft: "Booking không hiện văn bản nào của CIS. Bản nháp được viết khi dữ kiện đến (hoặc bấm Tạo lại).",
  in_review: "Một bản nháp đang chờ duyệt. Booking không hiện văn bản nào của CIS cho đến khi có bản được đăng.",
  published: "Booking hiện phiên bản đã đăng.",
  stale:
    "Dữ kiện trong Booking đã đổi sau lần đăng gần nhất. Booking không hiện văn bản nào của CIS cho đến khi một phiên bản mới được đăng.",
  unpublished: "Đã ẩn khỏi Booking. Phiên bản đã duyệt vẫn được giữ và có thể đăng lại.",
});

export const TYPE_LABEL_MESSAGES = defineMessages(TYPE_LABEL, {
  hotel: "Khách sạn",
  service: "Dịch vụ",
});

/** The photo states' label and hint, keyed `<state>` and `<state>Hint`. */
export const PHOTO_STATE_MESSAGES = defineMessages(
  {
    ready: PHOTO_STATE.ready.label,
    readyHint: PHOTO_STATE.ready.hint,
    not_copied: PHOTO_STATE.not_copied.label,
    not_copiedHint: PHOTO_STATE.not_copied.hint,
    no_rights: PHOTO_STATE.no_rights.label,
    no_rightsHint: PHOTO_STATE.no_rights.hint,
    removed: PHOTO_STATE.removed.label,
    removedHint: PHOTO_STATE.removed.hint,
    unknown: PHOTO_STATE.unknown.label,
    unknownHint: PHOTO_STATE.unknown.hint,
  },
  {
    ready: "Đã được phép và đã sao chép",
    readyHint: "Có quyền sử dụng trong Booking và đã sao chép vào kho ảnh CIS: có thể đăng.",
    not_copied: "Chưa sao chép",
    not_copiedHint:
      "Đã có quyền sử dụng, nhưng việc sao chép vào kho ảnh CIS chưa xong (hoặc lỗi: xem job viết nháp).",
    no_rights: "Không có quyền sử dụng",
    no_rightsHint: "Booking cho biết AA không được dùng ảnh này. Phiên bản có ảnh này không thể đăng.",
    removed: "Đã gỡ trong Booking",
    removedHint: "Ảnh không còn trong dữ kiện của Booking. Phiên bản có ảnh này không thể đăng.",
    unknown: "Ảnh không rõ",
    unknownHint: "Ảnh này không còn thuộc các ảnh của mục.",
  },
);
