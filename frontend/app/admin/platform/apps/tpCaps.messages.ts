// app/admin/platform/apps/tpCaps.messages.ts — the TripPlanner AI spend caps block (TpStageCaps.tsx), shown on the
// Apps board and on Cost Operations › AI & data spend. English and Vietnamese; stage codes stay as sent.

import { defineMessages } from "../../../_kit/core/i18n";

export const TP_CAPS = defineMessages(
  {
    title: "TripPlanner AI spend caps",
    description: "TripPlanner's customer-facing AI stops for the rest of the UTC day at its daily cap and falls back "
      + "to the simple version. A stage can also have its own cap.",
    combined: "Spent today, all customer stages",
    combinedOf: (cap: string) => `of the combined cap ${cap}`,
    nearCap: (pct: number) => `Near cap (${pct} %)`,
    notNear: "Below the alert level",
    colStage: "Stage",
    colSpent: "Spent today",
    colCalls: "Calls",
    colCap: "Stage cap",
    colUsed: "Used",
    noStageCap: "none: the combined cap applies",
    stateOk: "OK",
    stateWarn: "Near cap",
    stateCap: "Cap reached",
    mirrored: (pct: number) => `Caps are TripPlanner's own settings as CIS knows them; the alert level is ${pct} % of `
      + "a cap. No stage cap is set today, so the combined cap is the one that counts.",
    mirroredSet: (pct: number) => `Caps are TripPlanner's own settings as CIS knows them; the alert level is ${pct} % `
      + "of a cap. Both the combined cap and a stage's own cap apply.",
    unreadable: "TripPlanner's spend could not be read, so its AI is paused (it fails closed).",
    loadError: "The TripPlanner caps could not be loaded",
    open: "Open the Apps board",
  },
  {
    title: "Trần chi AI của TripPlanner",
    description: "AI phục vụ khách của TripPlanner dừng tới hết ngày UTC khi chạm trần ngày và trả bản đơn giản. Mỗi "
      + "stage cũng có thể có trần riêng.",
    combined: "Đã chi hôm nay, mọi stage phục vụ khách",
    combinedOf: (cap: string) => `trên trần chung ${cap}`,
    nearCap: (pct: number) => `Gần trần (${pct} %)`,
    notNear: "Dưới mức cảnh báo",
    colStage: "Stage",
    colSpent: "Đã chi hôm nay",
    colCalls: "Lần gọi",
    colCap: "Trần của stage",
    colUsed: "Đã dùng",
    noStageCap: "không có: áp trần chung",
    stateOk: "Ổn",
    stateWarn: "Gần trần",
    stateCap: "Chạm trần",
    mirrored: (pct: number) => `Mức trần là cài đặt riêng của TripPlanner theo CIS biết; mức cảnh báo là ${pct} % `
      + "của một trần. Hôm nay chưa đặt trần theo stage, nên trần chung là trần có hiệu lực.",
    mirroredSet: (pct: number) => `Mức trần là cài đặt riêng của TripPlanner theo CIS biết; mức cảnh báo là ${pct} % `
      + "của một trần. Cả trần chung và trần riêng của stage đều áp dụng.",
    unreadable: "Không đọc được số đã chi của TripPlanner, nên AI của nó đang tạm dừng (dừng an toàn).",
    loadError: "Không tải được trần chi của TripPlanner",
    open: "Mở bảng Ứng dụng",
  },
);
