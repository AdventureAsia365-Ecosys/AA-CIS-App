// app/admin/platform/apps/tpCaps.messages.ts — the TripPlanner AI spend caps block (TpStageCaps.tsx), shown on the
// Apps board and on Cost Operations › AI & data spend. English and Vietnamese; stage codes stay as sent.

import { defineMessages } from "../../../_kit/core/i18n";

export const TP_CAPS = defineMessages(
  {
    title: "TripPlanner AI spend caps",
    description: "Under the dev spend rules TripPlanner's customer-facing AI stops for the rest of the UTC day at its "
      + "daily cap and falls back to the simple version; under the prod spend rules a reached cap only alerts. A "
      + "stage can also have its own cap (Platform › Settings, TripPlanner tab).",
    ruleStops: "A reached cap stops the AI (dev spend rules)",
    ruleAlertOnly: "A reached cap only alerts (prod spend rules)",
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
    mirrored: (pct: number) => `The stage caps and the alert level (${pct} % of a cap) are the settings TripPlanner `
      + "reads (Platform › Settings, TripPlanner tab); the combined cap is the daily budget on AI gateway › App "
      + "profiles. No stage cap is set, so the combined cap is the one that counts.",
    mirroredSet: (pct: number) => `The stage caps and the alert level (${pct} % of a cap) are the settings TripPlanner `
      + "reads (Platform › Settings, TripPlanner tab). Both the combined cap and a stage's own cap apply.",
    unreadable: "TripPlanner's spend could not be read, so its AI is paused (it fails closed).",
    unreadableProd: "TripPlanner's spend could not be read; under the prod spend rules its AI goes on and staff get a "
      + "bell.",
    loadError: "The TripPlanner caps could not be loaded",
    open: "Open the Apps board",
  },
  {
    title: "Trần chi AI của TripPlanner",
    description: "Với quy tắc chi tiêu dev, AI phục vụ khách của TripPlanner dừng tới hết ngày UTC khi chạm trần ngày "
      + "và trả bản đơn giản; với quy tắc prod, chạm trần chỉ cảnh báo. Mỗi stage cũng có thể có trần riêng "
      + "(Platform › Settings, tab TripPlanner).",
    ruleStops: "Chạm trần thì dừng AI (quy tắc chi tiêu dev)",
    ruleAlertOnly: "Chạm trần chỉ cảnh báo (quy tắc chi tiêu prod)",
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
    mirrored: (pct: number) => `Trần theo stage và mức cảnh báo (${pct} % của một trần) là các cài đặt TripPlanner `
      + "đọc (Platform › Settings, tab TripPlanner); trần chung là ngân sách ngày ở AI gateway › App profiles. Chưa "
      + "đặt trần theo stage, nên trần chung là trần có hiệu lực.",
    mirroredSet: (pct: number) => `Trần theo stage và mức cảnh báo (${pct} % của một trần) là các cài đặt TripPlanner `
      + "đọc (Platform › Settings, tab TripPlanner). Cả trần chung và trần riêng của stage đều áp dụng.",
    unreadable: "Không đọc được số đã chi của TripPlanner, nên AI của nó đang tạm dừng (dừng an toàn).",
    unreadableProd: "Không đọc được số đã chi của TripPlanner; với quy tắc chi tiêu prod, AI vẫn chạy và staff nhận "
      + "chuông cảnh báo.",
    loadError: "Không tải được trần chi của TripPlanner",
    open: "Mở bảng Ứng dụng",
  },
);
