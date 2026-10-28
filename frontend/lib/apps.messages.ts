// lib/apps.messages.ts — AA-994: the app names lib/apps.ts APP_REGISTRY carries (label, adminLabel), English and
// Vietnamese, keyed by app id: t(`${id}Label`) / t(`${id}Admin`) with t = useT(APP_MESSAGES). Product names (Booking,
// TripPlanner) stay as they are. The sidebar's app links (APP_LINKS) are in app/_kit/shell.messages.ts.

import { defineMessages } from "../app/_kit/core/i18n";

export const APP_MESSAGES = defineMessages(
  {
    cisLabel: "CIS (content + catalog)",
    cisAdmin: "CIS admin",
    bookingLabel: "Booking",
    bookingAdmin: "Booking admin",
    tripplannerLabel: "TripPlanner",
    tripplannerAdmin: "TripPlanner site",
    crmLabel: "Partner CRM",
    crmAdmin: "CRM agency app",
  },
  {
    cisLabel: "CIS (nội dung + danh mục)",
    cisAdmin: "Trang quản trị CIS",
    bookingLabel: "Booking",
    bookingAdmin: "Trang quản trị Booking",
    tripplannerLabel: "TripPlanner",
    tripplannerAdmin: "Trang TripPlanner",
    crmLabel: "Partner CRM",
    crmAdmin: "Ứng dụng CRM cho đại lý",
  },
);
