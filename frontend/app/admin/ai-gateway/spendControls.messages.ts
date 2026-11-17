// app/admin/ai-gateway/spendControls.messages.ts — AA-1126 (S283): the Spend controls section of AI gateway › Budgets
// (the connection cap mode, the anomaly breaker and the run caps of this environment, read from Platform › Settings),
// English and Vietnamese. Setting labels come from the settings registry as sent.

import { defineMessages } from "../../_kit/core/i18n";

/** The state of one app's anomaly breaker (lib/spendControls BreakerState). */
export const BREAKER_STATE = defineMessages(
  { ok: "Normal", tripped_alert: "Over the line (alert only)",
    not_enforced: "Over the line (alert only: not enforced for this app yet)", refusing: "Refusing new AI calls" },
  { ok: "Bình thường", tripped_alert: "Vượt ngưỡng (chỉ cảnh báo)",
    not_enforced: "Vượt ngưỡng (chỉ cảnh báo: app này chưa áp dụng chặn)", refusing: "Đang từ chối lệnh gọi AI mới" },
);

/** Where a value comes from (settings reader Resolution.source). */
export const VALUE_SOURCE = defineMessages(
  { default: "Default", platform: "Approved value", tenant: "Tenant value", env: "Emergency override" },
  { default: "Mặc định", platform: "Giá trị đã duyệt", tenant: "Giá trị của tenant", env: "Ghi đè khẩn cấp" },
);

export const SPEND_CONTROLS = defineMessages(
  {
    title: "Spend controls of this environment",
    help: "What the gateway does when a connection reaches its monthly budget, when an app's AI spend today jumps far "
      + "above a normal day (the anomaly breaker), and the hard cap of one batch run (a tour rewrite in a wave, an "
      + "eval run, an ad-hoc run). Each value is a setting of this environment: change it on Platform › Settings "
      + "(area Spend), where every change is approved and audited. This page only shows what is in force.",
    editAll: "Edit on Platform › Settings",
    edit: "Edit",
    colSetting: "Setting",
    colValue: "In force",
    colDefault: "Default",
    colSource: "Source",
    missing: (n: number) => `${n} ${n === 1 ? "setting is" : "settings are"} not set up on this server yet: the `
      + "gateway keeps its earlier behaviour for them.",
    breakerTitle: "Anomaly breaker per app",
    breakerHelp: (mode: string) => `Mode ${mode}. An app trips when its AI spend today (UTC, or since its last reset) `
      + "is above the multiple of its median day and above the floor. In mode enforce a tripped app's new AI calls "
      + "are refused until it is reset or the UTC day ends; in mode alert the admin bell rings once a day.",
    breakerOff: "The anomaly breaker is off in this environment.",
    colApp: "App",
    colToday: "Spent today",
    colLine: "Trip line",
    colMedian: "Median day",
    colState: "State",
    colReset: "Last reset",
    lineOf: (multiple: string, floor: string) => `${multiple} × median, at least ${floor}`,
    reset: "Reset",
    resetTitle: (app: string) => `Reset the anomaly breaker of ${app}?`,
    resetBody: "Its AI spend is counted from now for the rest of the UTC day, so its calls go on. Reset only after "
      + "you have checked why the spend jumped. The reset is audited.",
    resetConfirm: "Reset the breaker",
    resetDone: (app: string) => `The anomaly breaker of ${app} was reset.`,
    resetTighten: "Resetting a breaker lets an app spend again: an administrator does it.",
    loadError: "Could not load the spend controls.",
    sourceErrors: "Part of the spend controls could not be read; the values shown are still the ones in force.",
    capStops: "Stops at the cap",
    capFailsOver: "Fails over at the cap",
    capAlertOnly: "Alert only",
    capModeNote: (mode: string) => `What a connection at its budget does in this environment: ${mode}.`,
    runCapNote: "The run caps above are the only per-run caps of those runs. The day and month caps of the same runs "
      + "are central caps below (S1 rewrite job, eval, ad-hoc).",
    breakerEnforcedNote: "Only CIS refuses calls in mode enforce today: TripPlanner and the CRM show their state and "
      + "ring the bell, their own gateways do not read the breaker yet.",
    runCapFromSettings: "Set in Platform › Settings",
    runCapEditorNote: (value: string) => `The per-run cap of this scope is ${value}, a Platform › Settings value (one `
      + "place to change it, approved and audited). Day and month caps are set here.",
    errRunSetting: "The per-run cap of this scope is set on Platform › Settings, not here.",
  },
  {
    title: "Kiểm soát chi tiêu của môi trường này",
    help: "Cổng AI làm gì khi một kết nối chạm ngân sách tháng, khi chi tiêu AI hôm nay của một app tăng vọt so với "
      + "một ngày bình thường (breaker bất thường), và trần cứng của một lần chạy hàng loạt (viết lại một tour trong "
      + "wave, một lần eval, một lần chạy adhoc). Mỗi giá trị là một cài đặt của môi trường này: sửa ở Nền tảng › Cài "
      + "đặt (nhóm Chi phí), nơi mọi thay đổi được duyệt và ghi nhật ký. Trang này chỉ hiển thị giá trị đang áp dụng.",
    editAll: "Sửa ở Nền tảng › Cài đặt",
    edit: "Sửa",
    colSetting: "Cài đặt",
    colValue: "Đang áp dụng",
    colDefault: "Mặc định",
    colSource: "Nguồn",
    missing: (n: number) => `${n} cài đặt chưa có trên máy chủ này: cổng AI giữ cách xử lý cũ cho chúng.`,
    breakerTitle: "Breaker bất thường theo app",
    breakerHelp: (mode: string) => `Chế độ ${mode}. Một app bị ngắt khi chi tiêu AI hôm nay (UTC, hoặc từ lần reset `
      + "gần nhất) vượt bội số của ngày trung vị và vượt mức sàn. Ở chế độ enforce, lệnh gọi AI mới của app bị ngắt "
      + "sẽ bị từ chối tới khi reset hoặc hết ngày UTC; ở chế độ alert, chuông admin kêu một lần mỗi ngày.",
    breakerOff: "Breaker bất thường đang tắt ở môi trường này.",
    colApp: "App",
    colToday: "Đã tiêu hôm nay",
    colLine: "Ngưỡng ngắt",
    colMedian: "Ngày trung vị",
    colState: "Trạng thái",
    colReset: "Reset gần nhất",
    lineOf: (multiple: string, floor: string) => `${multiple} × trung vị, tối thiểu ${floor}`,
    reset: "Reset",
    resetTitle: (app: string) => `Reset breaker bất thường của ${app}?`,
    resetBody: "Chi tiêu AI của app được tính lại từ bây giờ cho phần còn lại của ngày UTC, nên lệnh gọi chạy tiếp. Chỉ "
      + "reset sau khi đã kiểm vì sao chi tiêu tăng vọt. Lần reset được ghi nhật ký.",
    resetConfirm: "Reset breaker",
    resetDone: (app: string) => `Đã reset breaker bất thường của ${app}.`,
    resetTighten: "Reset breaker cho phép app tiêu tiếp: việc này do quản trị viên làm.",
    loadError: "Không tải được phần kiểm soát chi tiêu.",
    sourceErrors: "Một phần kiểm soát chi tiêu không đọc được; các giá trị hiển thị vẫn là giá trị đang áp dụng.",
    capStops: "Dừng khi chạm trần",
    capFailsOver: "Chuyển kết nối khi chạm trần",
    capAlertOnly: "Chỉ cảnh báo",
    capModeNote: (mode: string) => `Kết nối chạm ngân sách làm gì ở môi trường này: ${mode}.`,
    runCapNote: "Các trần mỗi lượt ở trên là trần mỗi lượt duy nhất của các lần chạy đó. Trần ngày và trần tháng của "
      + "cùng các lần chạy là trần trung tâm ở bên dưới (job viết lại S1, eval, adhoc).",
    breakerEnforcedNote: "Hiện chỉ CIS từ chối lệnh gọi ở chế độ enforce: TripPlanner và CRM hiện trạng thái và báo "
      + "chuông, cổng AI riêng của chúng chưa đọc breaker.",
    runCapFromSettings: "Đặt ở Nền tảng › Cài đặt",
    runCapEditorNote: (value: string) => `Trần mỗi lượt của phạm vi này là ${value}, một giá trị ở Nền tảng › Cài đặt `
      + "(một nơi duy nhất để sửa, có duyệt và ghi nhật ký). Trần ngày và trần tháng đặt ở đây.",
    errRunSetting: "Trần mỗi lượt của phạm vi này đặt ở Nền tảng › Cài đặt, không đặt ở đây.",
  },
);
