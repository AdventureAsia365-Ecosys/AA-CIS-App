// app/admin/spend/ceFreshness.messages.ts — AA-1127 (C2 "spend truth"): the freshness badge of the stored AWS Cost
// Explorer figures on AI & spend › Spend (the forecast and the By app tab), in English and Vietnamese.

import { defineMessages } from "../../_kit/core/i18n";

export const CE_FRESHNESS = defineMessages(
  {
    fresh: "AWS figures fresh",
    stale: "AWS figures stale",
    age: (hours: string) => `last fetched ${hours} h ago`,
    noData: "no AWS figures stored yet",
    staleAfter: (hours: string) => `stale after ${hours} h`,
    dailyAt: (hour: string) => `daily check at ${hour}:00 UTC`,
    dailyOff: "daily check off",
    title:
      "How old the newest stored AWS Cost Explorer fetch is (the older of the per-service and the App-tag fetch). The daily check and the stale window are settings on Platform › Settings (area spend).",
  },
  {
    fresh: "Số AWS mới",
    stale: "Số AWS đã cũ",
    age: (hours: string) => `lấy lần cuối ${hours} giờ trước`,
    noData: "chưa lưu số AWS nào",
    staleAfter: (hours: string) => `coi là cũ sau ${hours} giờ`,
    dailyAt: (hour: string) => `kiểm hằng ngày lúc ${hour}:00 UTC`,
    dailyOff: "đã tắt kiểm hằng ngày",
    title:
      "Lần lấy số AWS Cost Explorer mới nhất đã lưu cũ bao lâu (lấy lần cũ hơn trong hai lần: theo dịch vụ và theo App tag). Lượt kiểm hằng ngày và mốc coi là cũ là các setting trên Platform › Settings (nhóm spend).",
  },
);
