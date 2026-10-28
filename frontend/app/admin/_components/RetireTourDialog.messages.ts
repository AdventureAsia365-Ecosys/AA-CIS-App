// app/admin/_components/RetireTourDialog.messages.ts — AA-994: the retire / trash confirm dialog, English and
// Vietnamese.

import { defineMessages } from "../../_kit/core/i18n";

export const M = defineMessages(
  {
    titleRetire: "Retire this tour",
    titleTrash: "Trash this Master",
    retireFailed: "Failed to retire the tour",
    doneRetire: (name: string) => `"${name}" retired — tenants with a copy were warned`,
    doneTrash: (name: string) => `"${name}" moved to trash — tenants with a copy were warned`,
    counting: "Counting what this affects…",
    countFailed: "Could not count the effects",
    stopsServed: " stops being served by AA everywhere: Master Content, A3 (atoms, ranking, routes), the tenant pool "
      + "and the catalog that TripPlanner and Booking read. Tenants keep their own copies and are warned. You can "
      + "restore it later.",
    tenantsWarned: "Tenants warned",
    t2Copies: "Their T2 copies (kept)",
    slateSubjects: "Slate subjects",
    anglesPieces: "Angle requests / pieces (kept)",
    livePosts: "Live posts on tenant sites",
    livePostsNote: "Stay online (the tenant's site); the warning tells the tenant.",
    jobsCancelled: "Queued tenant jobs cancelled",
    reviewsDismissed: "Pending reviews dismissed",
    bookings: "Bookings",
    confirmRetire: "Retire tour",
    confirmTrash: "Move to trash",
  },
  {
    titleRetire: "Ngừng dùng tour này",
    titleTrash: "Chuyển Master này vào thùng rác",
    retireFailed: "Không ngừng dùng được tour",
    doneRetire: (name: string) => `Đã ngừng dùng "${name}" — các tenant có bản sao đã được cảnh báo`,
    doneTrash: (name: string) => `Đã chuyển "${name}" vào thùng rác — các tenant có bản sao đã được cảnh báo`,
    counting: "Đang đếm những gì bị ảnh hưởng…",
    countFailed: "Không đếm được ảnh hưởng",
    stopsServed: " sẽ không còn được AA phục vụ ở bất kỳ đâu: Master Content, A3 (atom, xếp hạng, route), kho của "
      + "tenant và danh mục mà TripPlanner và Booking đọc. Các tenant giữ bản sao của mình và được cảnh báo. Bạn có "
      + "thể khôi phục sau.",
    tenantsWarned: "Tenant được cảnh báo",
    t2Copies: "Bản sao T2 của họ (giữ lại)",
    slateSubjects: "Subject của Slate",
    anglesPieces: "Yêu cầu góc viết / bài viết (giữ lại)",
    livePosts: "Bài đang đăng trên site của tenant",
    livePostsNote: "Vẫn trực tuyến (site của tenant); cảnh báo sẽ báo cho tenant.",
    jobsCancelled: "Job của tenant đang chờ bị huỷ",
    reviewsDismissed: "Bài chờ duyệt bị bỏ qua",
    bookings: "Booking",
    confirmRetire: "Ngừng dùng tour",
    confirmTrash: "Chuyển vào thùng rác",
  },
);
