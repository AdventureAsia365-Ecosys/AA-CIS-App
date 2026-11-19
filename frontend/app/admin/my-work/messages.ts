// app/admin/my-work/messages.ts — AA-1156: the My work page (content queues of the signed-in role), English and
// Vietnamese. The tile keys match lib/myWork.ts MY_WORK_TILES (and the backend's TILES).

import { defineMessages } from "../../_kit/core/i18n";

export const MY_WORK = defineMessages(
  {
    title: "My work",
    description: "What is waiting for you in the content queues. Each tile opens its page with the same filter, so the "
      + "number there matches the number here.",
    refresh: "Refresh",
    refreshing: "Refreshing…",
    contentBand: "Content",
    contentBandDesc: "The queues your role works. A tile shows how many items wait and how long the oldest has waited.",
    bookingLink: "Booking: My work",
    bookingLinkTitle: "Open the Booking staff dashboard (same sign-in)",
    loadError: "Could not load your work.",
    nothing: "Your role has no content queues.",
    nothingDesc: "My work shows the queues of the content roles. Use the menu to open the pages your role reads.",
    allClear: "Nothing is waiting.",
    totalWaiting: (n: number) => `${n} item${n === 1 ? "" : "s"} waiting in all.`,
    oldest: (age: string) => `Oldest: ${age}`,
    noneWaiting: "Nothing waiting",
    unavailable: "Not available yet",
    open: "Open",
    updated: (at: string) => `Updated ${at}`,
    review_waiting: "Review Queue: waiting",
    review_waiting_info: "Rewrites the quality gate held for a person (the Review Queue's default view: pending).",
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
      + "auto_checked).",
    compliance_holds: "Compliance holds",
    compliance_holds_info: "Open flags of agency content: no Adventure Asia subject, or a post about a retired tour.",
  },
  {
    title: "Việc của tôi",
    description: "Những việc đang chờ bạn trong các hàng đợi nội dung. Mỗi ô mở đúng trang với cùng bộ lọc, nên con số "
      + "ở đó khớp với con số ở đây.",
    refresh: "Làm mới",
    refreshing: "Đang làm mới…",
    contentBand: "Nội dung",
    contentBandDesc: "Các hàng đợi vai trò của bạn xử lý. Mỗi ô cho biết số mục đang chờ và mục cũ nhất đã chờ bao lâu.",
    bookingLink: "Booking: Việc của tôi",
    bookingLinkTitle: "Mở bảng điều khiển nhân viên Booking (cùng một lần đăng nhập)",
    loadError: "Không tải được danh sách việc.",
    nothing: "Vai trò của bạn không có hàng đợi nội dung.",
    nothingDesc: "Việc của tôi hiện hàng đợi của các vai trò nội dung. Dùng menu để mở các trang vai trò của bạn được xem.",
    allClear: "Không có việc nào đang chờ.",
    totalWaiting: (n: number) => `Tổng cộng ${n} mục đang chờ.`,
    oldest: (age: string) => `Cũ nhất: ${age}`,
    noneWaiting: "Không có gì đang chờ",
    unavailable: "Chưa có",
    open: "Mở",
    updated: (at: string) => `Cập nhật ${at}`,
    review_waiting: "Review Queue: đang chờ",
    review_waiting_info: "Các bản viết lại bị cổng chất lượng giữ lại chờ người duyệt (mặc định của Review Queue: "
      + "pending).",
    supply_drafts: "Nội dung NCC: bản nháp chờ duyệt",
    supply_drafts_info: "Khách sạn và dịch vụ có bản nháp đang chờ duyệt, gồm cả bản nháp đầu và bản viết lại của nội "
      + "dung đã xuất bản hoặc đã cũ (bộ lọc: Có bản nháp chờ duyệt).",
    supply_no_description: "Khách sạn và dịch vụ chưa có mô tả",
    supply_no_description_info: "Booking chưa hiện chữ nào của CIS và chưa từng có bản nháp (trạng thái: No content "
      + "yet). Mục đã cũ (stale) đang chờ bản viết lại tự động nên không tính ở đây.",
    aa_content_drafts: "AA content: bản nháp",
    aa_content_drafts_info: "Các mục AA content có bản nháp chờ xuất bản (trạng thái: Draft).",
    place_cards_to_review: "Place card cần xem",
    place_cards_to_review_info: "Thẻ do mô hình soạn và tự kiểm, chưa có người xác minh (trạng thái: auto_checked).",
    compliance_holds: "Nội dung bị giữ (Compliance)",
    compliance_holds_info: "Cờ đang mở trên nội dung của đại lý: không có chủ đề Adventure Asia, hoặc bài về tour đã "
      + "ngừng bán.",
  },
);
