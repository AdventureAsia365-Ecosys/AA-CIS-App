// app/admin/ai-gateway/jevCredit.messages.ts — AI providers › Providers › Jev credit (AA-756 top-ups, AA-845 alerts;
// moved from Settings › Jev Credit by AA-804 UI part 2), English and Vietnamese. Notes and API errors stay as sent.

import { defineMessages } from "../../_kit/core/i18n";

export const JEV_CREDIT = defineMessages(
  {
    saveFailed: "Save failed",
    saving: "Saving…",
    estimate: "estimate",
    lowMailQueued: "low-credit e-mail queued",
    lowMailSkipped: "low-credit e-mail already sent today or not configured",
    testSent: (low: boolean, exhausted: boolean, mail: string) =>
      `Test alerts sent (bell: low ${low ? "yes" : "no"}, exhausted ${exhausted ? "yes" : "no"}; ${mail})`,
    testFailed: "Test alert failed",
    sendTest: "Send test alerts",
    sendTestHint: "Sends a [TEST] low-credit and a [TEST] exhausted alert (admin bell + e-mail to the alert recipients).",
    topupRecorded: "Top-up recorded",
    amountPositive: "Amount must be a positive number",
    date: "Date",
    amountUsd: "Amount (USD)",
    noteOptional: "Note (optional)",
    notePlaceholder: "e.g. topped up by the business owner",
    addTopup: "Add top-up",
    topupDeleted: "Top-up deleted",
    deleteFailed: "Delete failed",
    noTopupsTitle: "No top-ups recorded yet",
    noTopupsBody: "Add a top-up above to track Jev (TypeSafe) credit and see an estimated balance.",
    colAmount: "Amount",
    colNote: "Note",
    colAddedBy: "Added by",
    colActions: "actions",
    deleteTitle: "Delete this top-up?",
    delete: "Delete",
    deleteBody: (amount: string, day: string) =>
      `Remove the ${amount} top-up from ${day}? This only changes the recorded balance estimate; it does not touch the TypeSafe account.`,
    loadJevError: "Failed to load Jev credit",
    introA:
      "Jev (TypeSafe) has no balance API, so top-ups are recorded here by hand. The balance below is an",
    introB: "estimate",
    introC: (threshold: string) =>
      `: total topped up minus Jev spend since the first top-up. Admins get a bell and an e-mail when the estimate drops below the low-credit threshold${threshold}, and again if Jev reports the credit exhausted (HTTP 402).`,
    totalToppedUp: "Total topped up",
    since: (day: string) => `since ${day}`,
    spentJev: "Spent (Jev)",
    estimatedLeft: "Estimated left",
    belowThreshold: (threshold: string) =>
      `Estimated credit is below the ${threshold} alert threshold. Top up TypeSafe and record it below.`,
    lastLowAlert: (when: string) => ` Last low-credit alert: ${when}.`,
    lastExhausted: (when: string) => `Last exhausted alert: ${when}`,
    notDeployed: "The Jev credit endpoint is not deployed on this environment yet.",
  },
  {
    saveFailed: "Lưu thất bại",
    saving: "Đang lưu…",
    estimate: "ước tính",
    lowMailQueued: "đã xếp hàng e-mail số dư thấp",
    lowMailSkipped: "e-mail số dư thấp đã gửi hôm nay hoặc chưa được cấu hình",
    testSent: (low: boolean, exhausted: boolean, mail: string) =>
      `Đã gửi cảnh báo thử (chuông: số dư thấp ${low ? "có" : "không"}, hết số dư ${exhausted ? "có" : "không"}; ${mail})`,
    testFailed: "Gửi cảnh báo thử thất bại",
    sendTest: "Gửi cảnh báo thử",
    sendTestHint:
      "Gửi một cảnh báo [TEST] số dư thấp và một cảnh báo [TEST] hết số dư (chuông quản trị + e-mail tới người nhận cảnh báo).",
    topupRecorded: "Đã ghi lần nạp tiền",
    amountPositive: "Số tiền phải là số dương",
    date: "Ngày",
    amountUsd: "Số tiền (USD)",
    noteOptional: "Ghi chú (không bắt buộc)",
    notePlaceholder: "ví dụ: chủ doanh nghiệp đã nạp",
    addTopup: "Thêm lần nạp",
    topupDeleted: "Đã xoá lần nạp tiền",
    deleteFailed: "Xoá thất bại",
    noTopupsTitle: "Chưa ghi lần nạp tiền nào",
    noTopupsBody: "Thêm một lần nạp ở trên để theo dõi số dư Jev (TypeSafe) và xem số dư ước tính.",
    colAmount: "Số tiền",
    colNote: "Ghi chú",
    colAddedBy: "Người thêm",
    colActions: "thao tác",
    deleteTitle: "Xoá lần nạp tiền này?",
    delete: "Xoá",
    deleteBody: (amount: string, day: string) =>
      `Bỏ lần nạp ${amount} ngày ${day}? Thao tác này chỉ đổi số dư ước tính đã ghi; không động tới tài khoản TypeSafe.`,
    loadJevError: "Không tải được số dư Jev",
    introA:
      "Jev (TypeSafe) không có API số dư, nên các lần nạp tiền được ghi tay ở đây. Số dư bên dưới là",
    introB: "ước tính",
    introC: (threshold: string) =>
      `: tổng đã nạp trừ chi phí Jev kể từ lần nạp đầu. Quản trị nhận chuông và e-mail khi số ước tính xuống dưới ngưỡng số dư thấp${threshold}, và lần nữa nếu Jev báo hết số dư (HTTP 402).`,
    totalToppedUp: "Tổng đã nạp",
    since: (day: string) => `từ ${day}`,
    spentJev: "Đã chi (Jev)",
    estimatedLeft: "Ước tính còn lại",
    belowThreshold: (threshold: string) =>
      `Số dư ước tính đang dưới ngưỡng cảnh báo ${threshold}. Hãy nạp thêm TypeSafe và ghi lại bên dưới.`,
    lastLowAlert: (when: string) => ` Cảnh báo số dư thấp gần nhất: ${when}.`,
    lastExhausted: (when: string) => `Cảnh báo hết số dư gần nhất: ${when}`,
    notDeployed: "Endpoint số dư Jev chưa được triển khai trên môi trường này.",
  },
);
