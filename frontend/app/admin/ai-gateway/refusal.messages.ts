// app/admin/ai-gateway/refusal.messages.ts — S280 (AA-1096): the text of a refused AI providers write (useRefusalText).

import { defineMessages } from "../../_kit/core/i18n";

export const REFUSAL = defineMessages(
  {
    readOnly: "This is a read-only session: nothing was saved.",
    tightenOnly: "Your role may only tighten this limit (lower a cap, raise an alert floor, turn a stop on). An "
      + "administrator can loosen it. Nothing was saved.",
  },
  {
    readOnly: "Phiên này chỉ được xem: chưa lưu gì.",
    tightenOnly: "Vai trò của bạn chỉ được siết giới hạn này (giảm trần, nâng mức cảnh báo, bật dừng). Quản trị viên "
      + "mới nới được. Chưa lưu gì.",
  },
);
