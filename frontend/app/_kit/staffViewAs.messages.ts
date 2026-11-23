// app/_kit/staffViewAs.messages.ts — AA-1157: the owner's read-only "view as a lower role" (banner in the admin shell,
// start dialog on Roles & permissions), English and Vietnamese.

import { defineMessages } from "./core/i18n";

export const STAFF_VIEW_AS = defineMessages(
  {
    bannerTitle: (label: string) => `Viewing as ${label}`,
    bannerReadOnly: "Read only: nothing can be changed. Data that depends on your own assignments may be empty.",
    bannerEndsIn: (left: string) => `Ends in ${left}`,
    back: "Back",
    backing: "Going back…",
    startButton: "View as",
    startTitle: (label: string) => `View CIS as ${label}`,
    startIntro: "You see the pages and data this role sees, read only: every change is refused. Your own sign-in is "
      + "not changed. The view lasts at most 60 minutes and its start and end are recorded.",
    reasonLabel: "Reason",
    reasonHint: "Why you need to look (3 to 500 characters). Stored with the session.",
    minutesLabel: "Length",
    minutesOption: (n: number) => `${n} minutes`,
    start: "Start view",
    starting: "Starting…",
    cancel: "Cancel",
    errReason: "Give a reason of 3 to 500 characters.",
    errNotDownward: "This role can see more than you can, so you cannot view as it.",
    errOwner: "Only a platform owner can view as another role.",
    errUnknown: "This role cannot be viewed (unknown or archived).",
    startFailed: "The view could not be started",
  },
  {
    bannerTitle: (label: string) => `Đang xem với vai trò ${label}`,
    bannerReadOnly: "Chỉ xem: mọi thay đổi đều bị từ chối. Dữ liệu phụ thuộc vào phân công của chính bạn có thể trống.",
    bannerEndsIn: (left: string) => `Kết thúc sau ${left}`,
    back: "Quay lại",
    backing: "Đang quay lại…",
    startButton: "Xem như",
    startTitle: (label: string) => `Xem CIS với vai trò ${label}`,
    startIntro: "Bạn thấy các trang và dữ liệu vai trò này thấy, chỉ xem: mọi thay đổi đều bị từ chối. Phiên đăng nhập "
      + "của bạn không đổi. Phiên xem kéo dài tối đa 60 phút, lúc bắt đầu và kết thúc đều được ghi lại.",
    reasonLabel: "Lý do",
    reasonHint: "Vì sao bạn cần xem (3 đến 500 ký tự). Lưu cùng phiên.",
    minutesLabel: "Thời lượng",
    minutesOption: (n: number) => `${n} phút`,
    start: "Bắt đầu xem",
    starting: "Đang bắt đầu…",
    cancel: "Hủy",
    errReason: "Nhập lý do từ 3 đến 500 ký tự.",
    errNotDownward: "Vai trò này thấy nhiều hơn bạn, nên bạn không thể xem như vai trò đó.",
    errOwner: "Chỉ chủ sở hữu nền tảng mới xem được như vai trò khác.",
    errUnknown: "Không xem được vai trò này (không tồn tại hoặc đã lưu trữ).",
    startFailed: "Không bắt đầu được phiên xem",
  },
);
