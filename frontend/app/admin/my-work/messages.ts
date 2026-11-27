// app/admin/my-work/messages.ts — AA-1156 / AA-1190: the My work page (the landing page: what waits for the signed-in
// person, in bands), English and Vietnamese. The tile keys match lib/myWork.ts MY_WORK_TILES (and the backend's TILES).

import { defineMessages } from "../../_kit/core/i18n";

export const MY_WORK = defineMessages(
  {
    title: "My work",
    description: "What is waiting for you. Each tile opens its page with the same filter, so the number there matches "
      + "the number here.",
    bandApprove: "Needs your approval",
    bandApproveDesc: "Changes another person made that you may approve. Your own proposals are left out while a second "
      + "person must approve them.",
    bandWork: "Content work",
    bandWorkDesc: "The content queues your role works.",
    bandPlatform: "Platform",
    bandPlatformDesc: "Problems the status page sees open now. Open Overview to handle them.",
    bandWatch: "Keep an eye on",
    bandWatchDesc: "Backlog and items that wait for a policy decision. Not counted in the total above.",
    emptyQueues: (n: number) => `Nothing waiting in ${n} queue${n === 1 ? "" : "s"}`,
    settings_to_approve: "Settings to approve",
    settings_to_approve_info: "Open proposals on Platform › Settings that you may approve. The link opens the app tab of "
      + "the oldest proposal with only the settings that have one.",
    compliance_exceptions_to_approve: "Compliance exceptions to approve",
    compliance_exceptions_to_approve_info: "Proposed exceptions (accepted risks) on Compliance readiness › Exceptions "
      + "that you may approve.",
    erasure_to_approve: "Data erasure to approve",
    erasure_to_approve_info: "Tenant offboarding requests whose DRY RUN plan waits for an administrator's approval.",
    ops_open_problems: "Platform problems open",
    ops_open_problems_info: "Alarms firing, services below their desired count and failed rollouts that the status "
      + "sampler sees open now. Overview shows each one.",
    review_held_by_policy: "Held by policy",
    review_held_by_policy_info: "Review Queue rows held by the elephant-riding policy: a decision of policy, not "
      + "counted in the total (filter: Elephant riding policy).",
    refresh: "Refresh",
    refreshing: "Refreshing…",
    contentBand: "Content",
    contentBandDesc: "The queues your role works. A tile shows how many items wait and how long the oldest has waited.",
    bookingLink: "Booking: My work",
    bookingLinkTitle: "Open the Booking staff dashboard (same sign-in)",
    loadError: "Could not load your work.",
    nothing: "Nothing for your role here yet.",
    nothingDesc: "My work shows the queues and approvals your role may act on. Use the menu to open the pages your role "
      + "reads.",
    allClear: "Nothing is waiting.",
    totalWaiting: (n: number) => `${n} item${n === 1 ? "" : "s"} waiting in all.`,
    oldest: (age: string) => `Oldest: ${age}`,
    noneWaiting: "Nothing waiting",
    unavailable: "Not available yet",
    open: "Open",
    updated: (at: string) => `Updated ${at}`,
    review_waiting: "Review Queue: ready to review",
    review_waiting_info: "Rewrites the quality gate held for a person, without the rows held by a policy (filter: "
      + "pending, Without a policy hold).",
    supply_drafts: "Supply copy: drafts to review",
    supply_drafts_info: "Hotels and services with a written draft that waits for review, first drafts and redrafts "
      + "of published or stale content alike (filter: Draft waiting for review).",
    supply_no_description: "Hotels and services with no description",
    supply_no_description_info: "Booking shows no CIS text for these and no draft was ever written (status: No content "
      + "yet). Stale items wait for their automatic redraft and are not counted here.",
    aa_content_drafts: "AA content: drafts",
    aa_content_drafts_info: "AA content items with a draft waiting to be published (status: Draft).",
    place_cards_to_review: "Place cards to review",
    place_cards_to_review_info: "Cards the models drafted and checked that no person has verified yet (status: "
      + "auto_checked). Backlog: not counted in the total.",
    compliance_holds: "Compliance holds",
    compliance_holds_info: "Open flags of agency content: no Adventure Asia subject, or a post about a retired tour.",
  },
  {
    title: "Việc của tôi",
    description: "Những việc đang chờ bạn. Mỗi ô mở đúng trang với cùng bộ lọc, nên con số ở đó khớp với con số ở đây.",
    bandApprove: "Cần bạn duyệt",
    bandApproveDesc: "Thay đổi do người khác lập mà bạn được duyệt. Đề xuất của chính bạn không tính khi cần người thứ "
      + "hai duyệt.",
    bandWork: "Việc nội dung",
    bandWorkDesc: "Các hàng đợi nội dung vai trò của bạn xử lý.",
    bandPlatform: "Nền tảng",
    bandPlatformDesc: "Sự cố trang trạng thái đang thấy mở. Mở Tổng quan để xử lý.",
    bandWatch: "Theo dõi",
    bandWatchDesc: "Tồn đọng và mục chờ quyết định chính sách. Không tính vào tổng ở trên.",
    emptyQueues: (n: number) => `Không có gì đang chờ ở ${n} hàng đợi`,
    settings_to_approve: "Cài đặt chờ duyệt",
    settings_to_approve_info: "Đề xuất đang mở ở Platform › Settings mà bạn được duyệt. Liên kết mở tab ứng dụng của đề "
      + "xuất cũ nhất, chỉ hiện các cài đặt có đề xuất.",
    compliance_exceptions_to_approve: "Ngoại lệ tuân thủ chờ duyệt",
    compliance_exceptions_to_approve_info: "Ngoại lệ (rủi ro chấp nhận) đang đề xuất ở Compliance readiness › "
      + "Exceptions mà bạn được duyệt.",
    erasure_to_approve: "Xoá dữ liệu chờ duyệt",
    erasure_to_approve_info: "Yêu cầu offboarding tenant có kế hoạch DRY RUN đang chờ quản trị viên duyệt.",
    ops_open_problems: "Sự cố nền tảng đang mở",
    ops_open_problems_info: "Cảnh báo đang kêu, dịch vụ thiếu task và lần rollout lỗi mà bộ lấy mẫu trạng thái đang thấy "
      + "mở. Tổng quan hiện từng mục.",
    review_held_by_policy: "Bị giữ theo chính sách",
    review_held_by_policy_info: "Dòng Review Queue bị giữ theo chính sách cưỡi voi: chờ quyết định chính sách, không "
      + "tính vào tổng (bộ lọc: Elephant riding policy).",
    refresh: "Làm mới",
    refreshing: "Đang làm mới…",
    contentBand: "Nội dung",
    contentBandDesc: "Các hàng đợi vai trò của bạn xử lý. Mỗi ô cho biết số mục đang chờ và mục cũ nhất đã chờ bao lâu.",
    bookingLink: "Booking: Việc của tôi",
    bookingLinkTitle: "Mở bảng điều khiển nhân viên Booking (cùng một lần đăng nhập)",
    loadError: "Không tải được danh sách việc.",
    nothing: "Chưa có gì cho vai trò của bạn ở đây.",
    nothingDesc: "Việc của tôi hiện hàng đợi và việc duyệt vai trò của bạn được làm. Dùng menu để mở các trang vai trò "
      + "của bạn được xem.",
    allClear: "Không có việc nào đang chờ.",
    totalWaiting: (n: number) => `Tổng cộng ${n} mục đang chờ.`,
    oldest: (age: string) => `Cũ nhất: ${age}`,
    noneWaiting: "Không có gì đang chờ",
    unavailable: "Chưa có",
    open: "Mở",
    updated: (at: string) => `Cập nhật ${at}`,
    review_waiting: "Review Queue: sẵn sàng duyệt",
    review_waiting_info: "Các bản viết lại bị cổng chất lượng giữ lại chờ người duyệt, trừ dòng bị giữ theo chính sách "
      + "(bộ lọc: pending, Without a policy hold).",
    supply_drafts: "Nội dung NCC: bản nháp chờ duyệt",
    supply_drafts_info: "Khách sạn và dịch vụ có bản nháp đang chờ duyệt, gồm cả bản nháp đầu và bản viết lại của nội "
      + "dung đã xuất bản hoặc đã cũ (bộ lọc: Có bản nháp chờ duyệt).",
    supply_no_description: "Khách sạn và dịch vụ chưa có mô tả",
    supply_no_description_info: "Booking chưa hiện chữ nào của CIS và chưa từng có bản nháp (trạng thái: No content "
      + "yet). Mục đã cũ (stale) đang chờ bản viết lại tự động nên không tính ở đây.",
    aa_content_drafts: "AA content: bản nháp",
    aa_content_drafts_info: "Các mục AA content có bản nháp chờ xuất bản (trạng thái: Draft).",
    place_cards_to_review: "Place card cần xem",
    place_cards_to_review_info: "Thẻ do mô hình soạn và tự kiểm, chưa có người xác minh (trạng thái: auto_checked). "
      + "Tồn đọng: không tính vào tổng.",
    compliance_holds: "Nội dung bị giữ (Compliance)",
    compliance_holds_info: "Cờ đang mở trên nội dung của đại lý: không có chủ đề Adventure Asia, hoặc bài về tour đã "
      + "ngừng bán.",
  },
);
