// lib/alertRecipients.messages.ts — AA-994: the words of lib/alertRecipients.ts (status badges, the resend wait)
// in English and Vietnamese: `statusLabel(r, ALERT_WORDS_MESSAGES[lang])`, `formatWait(s, ALERT_WORDS_MESSAGES[lang])`.

import { defineMessages } from "../app/_kit/core/i18n";
import { ALERT_WORDS } from "./alertRecipients";

export const ALERT_WORDS_MESSAGES = defineMessages(ALERT_WORDS, {
  confirmed: "Đã xác nhận",
  pending: "Đang chờ xác nhận",
  unsubscribed: "Đã huỷ đăng ký",
  expired: "Hết hạn (chưa xác nhận)",
  removed: "Đã gỡ",
  notSubscribed: "Chưa đăng ký",
  unknown: "Không rõ",
  waitNow: "ngay bây giờ",
  waitS: (s: number) => `${s} giây`,
  waitMin: (m: number) => `${m} phút`,
  waitHMin: (h: number, m: number) => `${h} giờ ${m} phút`,
});
