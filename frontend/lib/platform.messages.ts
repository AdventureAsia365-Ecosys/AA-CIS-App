// lib/platform.messages.ts — AA-994: the words of lib/platform.ts in English and Vietnamese. App status labels
// (APP_STATUS_LABEL, the one wording for Platform › Apps and the Overview tiles) keyed by status code, and the
// basisText() phrasings: `basisText(basis, BASIS_MESSAGES[lang])`.

import { defineMessages } from "../app/_kit/core/i18n";
import { APP_STATUS_LABEL, BASIS_WORDS } from "./platform";

export const APP_STATUS_MESSAGES = defineMessages(
  {
    ok: APP_STATUS_LABEL.ok.label,
    degraded: APP_STATUS_LABEL.degraded.label,
    down: APP_STATUS_LABEL.down.label,
    unknown: APP_STATUS_LABEL.unknown.label,
  },
  { ok: "Ổn định", degraded: "Cần xem lại", down: "Ngừng hoạt động", unknown: "Không rõ" },
);

export const BASIS_MESSAGES = defineMessages(BASIS_WORDS, {
  noAnswerTraffic: "kiểm tra sức khoẻ không phản hồi; lưu lượng 24 giờ qua vẫn được hiển thị",
  noAnswer: "kiểm tra sức khoẻ không phản hồi (hiện chưa có số liệu lưu lượng)",
  probeTraffic: "dựa trên kiểm tra sức khoẻ và lưu lượng 24 giờ qua",
  traffic: "dựa trên lưu lượng 24 giờ qua (AWS CloudWatch); ứng dụng không có kiểm tra sức khoẻ",
  probe: "dựa trên kiểm tra sức khoẻ (hiện chưa có số liệu lưu lượng)",
});
