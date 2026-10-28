// lib/writingReasons.messages.ts
// AA-994 — the reason chips (lib/writingReasons.ts REASON_LABEL / REASON_HINT, same codes) in English and Vietnamese:
// `useT(REASON_LABEL_MESSAGES)(code)`, `useT(REASON_HINT_MESSAGES)(code)`.

import { defineMessages } from "../app/_kit/core/i18n";
import { REASON_HINT, REASON_LABEL } from "./writingReasons";

export const REASON_LABEL_MESSAGES = defineMessages(REASON_LABEL, {
  fact_not_in_source: "Dữ kiện không có trong nguồn",
  optional_as_included: "Tuỳ chọn ghi là đã bao gồm",
  wrong_place_or_time: "Sai địa điểm / thời gian",
  voice_off_brand: "Lệch giọng thương hiệu",
  ai_phrasing: "Văn phong AI",
  format_channel: "Định dạng / kênh",
  repair_residue: "Sót lại sau sửa",
  style_only: "Chỉ văn phong",
});

export const REASON_HINT_MESSAGES = defineMessages(REASON_HINT, {
  fact_not_in_source: "một dữ kiện mà nguồn không đưa ra",
  optional_as_included: "một mục tuỳ chọn được ghi là đã bao gồm",
  wrong_place_or_time: "sai địa điểm hoặc thời gian",
  voice_off_brand: "cách viết lệch giọng thương hiệu",
  ai_phrasing: "văn phong AI chung chung",
  format_channel: "định dạng hoặc độ dài không hợp kênh",
  repair_residue: "phần sót lại của một bước sửa",
  style_only: "một thay đổi văn phong",
});
