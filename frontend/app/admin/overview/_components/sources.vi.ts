// app/admin/overview/_components/sources.vi.ts
// AA-994 — the Vietnamese twin of sources.ts (same shape, checked by tsc through `Sources`): where every number on
// /admin/overview comes from, in plain business words. The same rule as the English side: no table, view or column
// names, migration numbers, account names or internal codes. Read through useSources() in sources.ts.

import { COST_WORDING } from "../../../../lib/costWording.messages";
import type { AppHealth } from "./opsTypes";
import type { Sources } from "./sources";

const CW = COST_WORDING.vi;

const OPS_5MIN = "máy chủ cập nhật mỗi 5 phút (bấm Làm mới để tải số mới nhất)";

const HEALTH_REFRESH = `24 giờ qua, ${OPS_5MIN}`;

const APP_SUBJECT: Record<AppHealth["app"], string> = {
  cis: "API của CIS",
  booking: "API của Booking",
  tripplanner: "các hàm của TripPlanner (duyệt và ghép chuyến đi)",
};

const isTp = (app: AppHealth["app"]) => app === "tripplanner";
const UNIT = (app: AppHealth["app"]) => (isTp(app) ? "lượt chạy" : "yêu cầu");
const ERRORS = (app: AppHealth["app"]) => (isTp(app) ? "lượt chạy thất bại" : "lỗi máy chủ (5xx)");

const HEALTH_SRC: Sources["HEALTH_SRC"] = {
  card:
    "AWS CloudWatch: số yêu cầu (với TripPlanner là số lượt chạy hàm), lỗi và thời gian phản hồi của từng ứng dụng cùng " +
    `trạng thái các cảnh báo của nó, ${HEALTH_REFRESH}`,
  requests: (app) => `AWS CloudWatch, ${UNIT(app)} của ${APP_SUBJECT[app]}, ${HEALTH_REFRESH}`,
  errors: (app) =>
    `AWS CloudWatch, ${ERRORS(app)} của ${APP_SUBJECT[app]}, ${HEALTH_REFRESH}; % là số lỗi chia cho số ${UNIT(app)}`,
  speed: (app) =>
    app === "tripplanner"
      ? `AWS CloudWatch, thời gian chạy của ${APP_SUBJECT[app]}: 95% lượt chạy xong trong khoảng thời gian này (lấy hàm ` +
        `chậm hơn trong hai hàm), ${HEALTH_REFRESH}`
      : `AWS CloudWatch, thời gian phản hồi của ${APP_SUBJECT[app]}: 95% yêu cầu được trả lời trong khoảng thời gian này, ` +
        HEALTH_REFRESH,
  clientErrors: (app) =>
    `AWS CloudWatch, yêu cầu tới ${APP_SUBJECT[app]} bị từ chối vì dữ liệu sai hoặc chưa đăng nhập (4xx), ` +
    `${HEALTH_REFRESH}; không phải lỗi của ứng dụng`,
  throttles: (app) =>
    `AWS CloudWatch, lượt chạy của ${APP_SUBJECT[app]} bị từ chối vì có quá nhiều lượt chạy cùng lúc, ${HEALTH_REFRESH}`,
  hourly: (app) =>
    `AWS CloudWatch, ${UNIT(app)} mỗi giờ của ${APP_SUBJECT[app]} ` +
    `(${isTp(app) ? "lượt chạy thất bại" : "lỗi máy chủ"} màu đỏ), ${HEALTH_REFRESH}`,
  alarms: (app) => `Các cảnh báo AWS CloudWatch thiết lập cho ${APP_SUBJECT[app]}, trạng thái hiện tại, ${OPS_5MIN}`,
  shared: `Các cảnh báo AWS CloudWatch của dịch vụ dùng chung (mạng, đăng nhập nhân sự, e-mail), trạng thái hiện tại, ${OPS_5MIN}`,
};

const TP_WINDOW = `7 ngày qua (ngày theo UTC), ${OPS_5MIN}; mức thay đổi so với 7 ngày trước đó`;

const TP_SRC: Sources["TP_SRC"] = {
  card: `Cơ sở dữ liệu TripPlanner (số đếm hoạt động theo ngày, không có thông tin khách) và nhật ký cuộc gọi AI, ${OPS_5MIN}`,
  planner_session_started: `Cơ sở dữ liệu TripPlanner, số phiên lập kế hoạch mới của khách, ${TP_WINDOW}`,
  trip_started: `Cơ sở dữ liệu TripPlanner, số bản nháp chuyến đi mới, ${TP_WINDOW}`,
  inquiry_sent: `Cơ sở dữ liệu TripPlanner, số yêu cầu "gửi cho tư vấn viên" đã chuyển sang Booking, ${TP_WINDOW}`,
  ai_search:
    `Nhật ký cuộc gọi AI, số lượt tìm kiếm TripPlanner có gọi AI, ${TP_WINDOW}; lượt tìm lặp lại được trả lời từ bộ nhớ đệm ` +
    "không được tính",
  chart: `Cơ sở dữ liệu TripPlanner, cùng các số đếm theo ngày trong 14 ngày qua (ngày theo UTC), ${OPS_5MIN}`,
};

const BK_8W = `tổng của 8 tuần qua, ${OPS_5MIN}`;
const BK_WEEK = "trong từng tuần (thứ Hai đến Chủ nhật, UTC)";

const BOOKING_SRC: Sources["BOOKING_SRC"] = {
  card: `Cơ sở dữ liệu Booking (số đếm theo ngày và trạng thái hồ sơ chuyến đi hiện tại, không có thông tin khách), ${OPS_5MIN}`,
  steps: {
    inquiry_received: `Cơ sở dữ liệu Booking, yêu cầu tư vấn nhận từ TripPlanner và trang web, ${BK_8W}`,
    trip_case_created: `Cơ sở dữ liệu Booking, hồ sơ chuyến đi được mở (không tính hồ sơ đã xoá), ${BK_8W}`,
    proposal_sent: `Cơ sở dữ liệu Booking, hồ sơ chuyến đi lần đầu đến bước "đã gửi đề xuất", ${BK_8W}`,
    confirmed: `Cơ sở dữ liệu Booking, hồ sơ chuyến đi lần đầu đến bước "đã xác nhận", ${BK_8W}`,
  },
  weeks: {
    inquiry_received: `Cơ sở dữ liệu Booking, yêu cầu tư vấn nhận được ${BK_WEEK}`,
    trip_case_created: `Cơ sở dữ liệu Booking, hồ sơ chuyến đi được mở ${BK_WEEK}`,
    proposal_sent: `Cơ sở dữ liệu Booking, hồ sơ chuyến đi lần đầu đến bước "đã gửi đề xuất" ${BK_WEEK}`,
    confirmed: `Cơ sở dữ liệu Booking, hồ sơ chuyến đi lần đầu đến bước "đã xác nhận" ${BK_WEEK}`,
  },
  cancelled: `Cơ sở dữ liệu Booking, hồ sơ chuyến đi bị huỷ lần đầu ${BK_WEEK}`,
  attention: "Cơ sở dữ liệu Booking, các yêu cầu tư vấn đang chờ hoặc lỗi trong hộp thư Booking ngay lúc này",
  openCases:
    "Cơ sở dữ liệu Booking, hồ sơ chuyến đi theo trạng thái hiện tại (không tính hồ sơ đã xoá); các nhãn liệt kê mọi " +
    "trạng thái có hồ sơ, Hoàn tất và Đã huỷ là đã đóng và hiện màu xám",
};

const COST_SRC: Sources["COST_SRC"] = {
  card:
    "Hoá đơn AWS qua Cost Explorer, chia theo thẻ App của từng tài nguyên, từ đầu tháng đến nay (lấy khoảng mỗi ngày một " +
    "lần, bản thân hoá đơn trễ khoảng hai ngày), cộng nhật ký cuộc gọi AI cho phần AI tính ngoài hoá đơn AWS (máy chủ " +
    "cập nhật mỗi 15 phút, bấm Làm mới để tải số mới nhất); cùng số liệu với trang Chi phí theo ứng dụng",
  total: `${CW.awsBill} + ${CW.aiOutside}, cho cùng ứng dụng và cùng tháng`,
  prevMonth: (month: string) => `Cùng các nguồn đó cho toàn bộ ${month}`,
  aiNote: `Nhật ký cuộc gọi AI, tháng này: toàn bộ ước tính AI. ${CW.aiOnBillSrc}`,
};

const CIS_DB = "Cơ sở dữ liệu CIS";
const LIVE = "đếm trực tiếp, làm mới mỗi phút";

const PIPELINE_SRC: Sources["PIPELINE_SRC"] = {
  card: `${CIS_DB}, mọi tour theo từng quốc gia qua pipeline nội dung, ${LIVE}`,
  raw: `${CIS_DB}, tour đã nhập và còn hoạt động ở nguồn; +n/7d = nhập trong 7 ngày qua`,
  generated: `${CIS_DB}, tour đang hoạt động có ít nhất một bản nháp viết lại; +n/7d = tour có bản nháp mới trong 7 ngày qua`,
  review: `${CIS_DB}, tour đang hoạt động đang chờ trong Review Queue ngay lúc này`,
  published: `${CIS_DB}, tour đang hoạt động đã xuất bản sang Master Content (không tính tour trong thùng rác); +n/7d = xuất bản trong 7 ngày qua`,
  atomized: `${CIS_DB}, tour đã xuất bản đang hoạt động được tách thành các mẩu nội dung (atom) đang hoạt động`,
};

const INTEL_SRC: Sources["INTEL_SRC"] = {
  atoms: `${CIS_DB}, các mẩu nội dung (atom) cắt từ tour, không tính atom đã xoá và atom rỗng, ${LIVE}`,
  segments: `${CIS_DB}, các segment chứa ít nhất một atom trong số đó, ${LIVE}`,
  routes: `${CIS_DB}, các tuyến hiện hành (không tính phiên bản đã bị thay), ${LIVE}`,
  hubs: `${CIS_DB}, các hub có ít nhất một tuyến hiện hành, ${LIVE}`,
  scores: `${CIS_DB}, điểm xếp hạng atom hiện hành (không tính điểm đã bị thay), ${LIVE}`,
  research: `${CIS_DB}, các địa điểm đã nghiên cứu cho segment; cũ = có ít nhất một thị trường nghiên cứu lần cuối hơn 30 ngày trước, ${LIVE}`,
};

const TENANT_SRC: Sources["TENANT_SRC"] = {
  active: `${CIS_DB}, tài khoản tenant đang bật, ${LIVE}`,
  rewrites: `${CIS_DB}, phiên bản tour tenant tạo trong 7 ngày qua, ${LIVE}`,
  pieces: `${CIS_DB}, mẩu nội dung theo kênh tạo cho tenant trong 7 ngày qua, ${LIVE}`,
  published: `${CIS_DB}, mẩu nội dung đã đăng lên một kênh của tenant trong 7 ngày qua, ${LIVE}`,
  quotaTours: `${CIS_DB}, số tour đã dùng trong tháng thanh toán này theo % hạn mức tour của gói; liệt kê tenant từ 80% trở lên`,
  quotaCalls: `${CIS_DB}, số lượt gọi API trong tháng thanh toán này theo % hạn mức gọi của gói; liệt kê tenant từ 80% trở lên`,
};

const JOBS_SRC: Sources["JOBS_SRC"] = {
  counts: `Hàng đợi job của CIS, job tạo trong 30 ngày qua theo trạng thái, ${LIVE}`,
  workers:
    "Hàng đợi job của CIS, worker được thấy trong 60 giây qua và chưa dừng (cùng quy tắc với trang Ứng dụng và bước kiểm tra deploy)",
  waiting: "Hàng đợi job của CIS, job đang xếp hàng và chưa bắt đầu, ngay lúc này",
  running: "Hàng đợi job của CIS, job đang chạy ngay lúc này",
};

const PROVIDER_COST_SRC: Sources["PROVIDER_COST_SRC"] = {
  today:
    "Nhật ký cuộc gọi AI (AWS Bedrock, OpenAI, Jev) và nhật ký cuộc gọi DataForSEO, chi phí từ nửa đêm UTC hôm nay; màu đỏ " +
    "khi chạm mức cảnh báo của ngân sách",
  month:
    "Nhật ký cuộc gọi AI, tháng này (cùng ước tính AI với trang Chi phí theo ứng dụng); DataForSEO: nhật ký cuộc gọi của nó, tháng này",
  budget: "Ngân sách chi tiêu đặt trong CIS (Nhà cung cấp AI, Ngân sách), hạn mức theo ngày cho từng nhà cung cấp; được đếm khi chi hôm nay chạm mức cảnh báo",
  aiMonth: "Nhật ký cuộc gọi AI, mọi nhà cung cấp AI, tháng này (UTC)",
  dfsBalance: "Số dư tài khoản DataForSEO từ lần kiểm tra số dư được lưu gần nhất (không gọi trực tiếp)",
};

export const SOURCES_VI: Sources = {
  HEALTH_SRC,
  TP_SRC,
  BOOKING_SRC,
  COST_SRC,
  PIPELINE_SRC,
  INTEL_SRC,
  TENANT_SRC,
  JOBS_SRC,
  PROVIDER_COST_SRC,
};
