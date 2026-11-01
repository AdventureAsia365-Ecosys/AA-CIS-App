// lib/crmConsole.messages.ts — AA-994: the words of lib/crmConsole.ts (Platform › Tenants › Agencies) in English and
// Vietnamese. The English side is the lib's own constants; app/admin/platform/agencies/useCrmText.ts binds them to the
// UI language. CRM codes (states, reasons, features, error codes) stay the catalogue keys.

import { defineMessages } from "../app/_kit/core/i18n";
import {
  CRM_ERROR_TEXT as E,
  CRM_WORDS,
  DATA_REQUEST_STATUS_LABEL as DS,
  FEED_STATE_LABEL as FS,
  LOCK_SOURCE_LABEL as LS,
  STATE_LABEL,
} from "./crmConsole";

export const CRM_WORDS_MESSAGES = defineMessages(CRM_WORDS, {
  lagNone: "chưa có lần lấy thành công",
  lagS: (n: number) => `${n} giây`,
  lagMin: (n: number) => `${n} phút`,
  lagH: (h: number) => `${h} giờ`,
  lagHMin: (h: number, m: number) => `${h} giờ ${m} phút`,
  lagD: (d: number) => `${d} ngày`,
  lagDH: (d: number, h: number) => `${d} ngày ${h} giờ`,
  pullNone: "chưa có",
  pullAgo: (lag: string) => `${lag} trước`,
  lockNoSourceLabel: "Đã khoá (không rõ nguồn)",
  lockNoSourceShort: "không rõ nguồn",
  lockNoSourceDetail: "CRM không cho biết ai đã khoá.",
  lockOtherLabel: (src: string) => `Đã khoá (${src})`,
  lockOtherShort: (src: string) => `bởi ${src}`,
  checkForm: (msgs: string) => `Kiểm tra lại biểu mẫu: ${msgs}`,
  refused: (status: number) => `Yêu cầu bị từ chối (${status}).`,
  adminOnly: "Chỉ dành cho quản trị viên: vai trò CIS của bạn không làm được việc này.",
  failed: (status: number) => `Yêu cầu thất bại (${status}).`,
  network: "Lỗi mạng: chưa gửi gì cả. Hãy thử lại.",
});

export const CRM_STATE = defineMessages(
  {
    active: STATE_LABEL.active.label,
    locked: STATE_LABEL.locked.label,
    offboarding: STATE_LABEL.offboarding.label,
    offboarded: STATE_LABEL.offboarded.label,
  },
  { active: "Đang hoạt động", locked: "Đã khoá", offboarding: "Đang ngừng hợp tác", offboarded: "Đã ngừng hợp tác" },
);

/** Keyed `<source>`, `<source>Short`, `<source>Detail`. */
export const CRM_LOCK_SOURCE = defineMessages(
  {
    aa_ops: LS.aa_ops.label,
    aa_opsShort: LS.aa_ops.short,
    aa_opsDetail: LS.aa_ops.detail,
    cis_tenant: LS.cis_tenant.label,
    cis_tenantShort: LS.cis_tenant.short,
    cis_tenantDetail: LS.cis_tenant.detail,
    cis_membership: LS.cis_membership.label,
    cis_membershipShort: LS.cis_membership.short,
    cis_membershipDetail: LS.cis_membership.detail,
  },
  {
    aa_ops: "Khoá bởi AA",
    aa_opsShort: "bởi nhân sự AA",
    aa_opsDetail: "Khoá từ console này (hoặc trước khi CRM ghi nhận ai khoá). AA mở khoá là gỡ được.",
    cis_tenant: "Khoá bởi CIS: tenant bị khoá hoặc bị xoá",
    cis_tenantShort: "bởi vòng đời tenant CIS",
    cis_tenantDetail: "CIS đã khoá hoặc xoá tenant này, và luồng thành viên đã chuyển sang CRM. CIS mở khoá là gỡ được.",
    cis_membership: "Khoá bởi CIS: không còn chủ sở hữu đang hoạt động",
    cis_membershipShort: "bởi CIS: không còn chủ sở hữu",
    cis_membershipDetail:
      "CIS đã gỡ chủ sở hữu đang hoạt động cuối cùng của đại lý. Khôi phục chủ sở hữu hoặc AA mở khoá là gỡ được.",
  },
);

/** Keyed `<state>` and `<state>Detail`. */
export const CRM_FEED = defineMessages(
  {
    ok: FS.ok.label,
    okDetail: FS.ok.detail,
    pending: FS.pending.label,
    pendingDetail: FS.pending.detail,
    stale: FS.stale.label,
    staleDetail: FS.stale.detail,
    failing: FS.failing.label,
    failingDetail: FS.failing.detail,
    unknown: FS.unknown.label,
    unknownDetail: FS.unknown.detail,
  },
  {
    ok: "Ổn",
    okDetail: "CRM đang lấy các thay đổi thành viên từ CIS.",
    pending: "Đang chờ",
    pendingDetail: "Chưa có lần lấy thành công (kể cả khi luồng chưa được cấu hình).",
    stale: "Cũ",
    staleDetail: "Không có lần lấy thành công nào trong 10 phút.",
    failing: "Đang lỗi",
    failingDetail: "Các lần lấy gần nhất đều thất bại.",
    unknown: "Không rõ",
    unknownDetail: "CRM không đọc được trạng thái của luồng.",
  },
);

export const CRM_LOCK_REASON = defineMessages(
  {
    owner_request: "The owner asked for it",
    payment: "Payment problem",
    abuse: "Abuse",
    security: "Security incident",
    test: "Test",
  },
  {
    owner_request: "Chủ sở hữu yêu cầu",
    payment: "Vấn đề thanh toán",
    abuse: "Lạm dụng",
    security: "Sự cố bảo mật",
    test: "Thử nghiệm",
  },
);

export const CRM_FEATURE = defineMessages(
  {
    ai_assist: "AI writing assist",
    whatsapp_sms: "WhatsApp and SMS",
    stripe_connect: "Stripe payments (Connect)",
    campaigns: "E-mail campaigns",
    custom_domain: "Custom domain",
    insights_export: "Insights export",
  },
  {
    ai_assist: "AI hỗ trợ viết",
    whatsapp_sms: "WhatsApp và SMS",
    stripe_connect: "Thanh toán Stripe (Connect)",
    campaigns: "Chiến dịch e-mail",
    custom_domain: "Tên miền riêng",
    insights_export: "Xuất số liệu phân tích",
  },
);

export const CRM_RECOVERY_REASON = defineMessages(
  {
    owner_left: "The owner left the agency",
    owner_unreachable: "The owner cannot be reached",
    no_owner: "The agency has no owner",
  },
  {
    owner_left: "Chủ sở hữu đã rời đại lý",
    owner_unreachable: "Không liên lạc được với chủ sở hữu",
    no_owner: "Đại lý không có chủ sở hữu",
  },
);

export const CRM_DATA_KIND = defineMessages(
  { agency_export: "Export the agency's data", agency_deletion: "Delete the agency (offboard)" },
  { agency_export: "Xuất dữ liệu của đại lý", agency_deletion: "Xoá đại lý (ngừng hợp tác)" },
);

export const CRM_DATA_STATUS = defineMessages(
  {
    awaiting_owner_confirmation: DS.awaiting_owner_confirmation.label,
    confirmed: DS.confirmed.label,
    export_downloaded: DS.export_downloaded.label,
    rejected: DS.rejected.label,
    cancelled: DS.cancelled.label,
    failed: DS.failed.label,
    done: DS.done.label,
  },
  {
    awaiting_owner_confirmation: "Đang chờ chủ sở hữu",
    confirmed: "Chủ sở hữu đã xác nhận",
    export_downloaded: "Đã tải về; chủ sở hữu chưa đóng yêu cầu",
    rejected: "Chủ sở hữu đã từ chối",
    cancelled: "Đã huỷ",
    failed: "Thất bại",
    done: "Xong",
  },
);

export const CRM_ERRORS = defineMessages(
  {
    cognito_session_required: E.cognito_session_required,
    missing_group: E.missing_group,
    wrong_audience: E.wrong_audience,
    crm_unavailable: E.crm_unavailable,
    crm_bad_response: E.crm_bad_response,
    crm_path_not_allowed: E.crm_path_not_allowed,
    tenant_not_found: E.tenant_not_found,
    tenant_locked: E.tenant_locked,
    tenant_status: E.tenant_status,
    has_travellers: E.has_travellers,
    owner_exists: E.owner_exists,
    tenant_exists: E.tenant_exists,
    owner_invite_pending: E.owner_invite_pending,
    member_limit_reached: E.member_limit_reached,
    invalid_feature: E.invalid_feature,
    offboarding_open: E.offboarding_open,
    legal_hold: E.legal_hold,
    recovery_pending: E.recovery_pending,
    recovery_not_due: E.recovery_not_due,
    recovery_closed: E.recovery_closed,
    recovery_not_found: E.recovery_not_found,
    candidate_inactive: E.candidate_inactive,
    candidate_is_owner: E.candidate_is_owner,
    member_not_found: E.member_not_found,
    job_not_found: E.job_not_found,
    synthetic_required: E.synthetic_required,
    synthetic_not_allowed: E.synthetic_not_allowed,
    synthetic_flag_mismatch: E.synthetic_flag_mismatch,
    invalid_display_name: E.invalid_display_name,
    invalid_locale: E.invalid_locale,
    invalid_ticket_ref: E.invalid_ticket_ref,
    owner_invite_not_found: E.owner_invite_not_found,
    not_synthetic: E.not_synthetic,
    agency_name_mismatch: E.agency_name_mismatch,
    has_members: E.has_members,
  },
  {
    cognito_session_required: "Hãy đăng nhập bằng tài khoản nhân sự (Cognito) để dùng console CRM.",
    missing_group: "Tài khoản nhân sự của bạn không thuộc nhóm crm-platform-admin.",
    wrong_audience: "Lần đăng nhập này không gọi được console CRM (sai app client).",
    crm_unavailable: "CRM đối tác không phản hồi. Hãy thử lại sau một phút.",
    crm_bad_response: "CRM đối tác trả về một câu trả lời không mong đợi.",
    crm_path_not_allowed: "Thao tác này không thuộc console L1.",
    tenant_not_found: "Đại lý này không tồn tại trong CRM.",
    tenant_locked: "Đại lý đang bị khoá. Hãy mở khoá trước.",
    tenant_status: "Không thực hiện được ở trạng thái hiện tại của đại lý.",
    has_travellers: "Đại lý đã có khách du lịch hoặc yêu cầu tư vấn, nên AA không thể mời chủ sở hữu đầu tiên.",
    owner_exists: "Đại lý đã có chủ sở hữu.",
    tenant_exists: "Trong CRM đã có một đại lý với id này. Không có gì thay đổi.",
    owner_invite_pending: "Đã có một lời mời chủ sở hữu đang chờ.",
    member_limit_reached: "Đại lý không còn chỗ trống.",
    invalid_feature: "Một trong các tính năng không phải mã đã biết.",
    offboarding_open:
      "Đại lý đã có một yêu cầu xuất hoặc xoá dữ liệu đang mở (bản xuất đã tải về vẫn mở cho đến khi chủ sở hữu đóng lại).",
    legal_hold: "Đang có lệnh giữ dữ liệu theo pháp lý, nên không xoá được đại lý.",
    recovery_pending: "Đã có một yêu cầu khôi phục chủ sở hữu đang chờ.",
    recovery_not_due: "Thời gian chờ 14 ngày chưa kết thúc.",
    recovery_closed: "Yêu cầu khôi phục này đã bị huỷ hoặc đã hoàn tất.",
    recovery_not_found: "Yêu cầu khôi phục này không tồn tại.",
    candidate_inactive: "Ứng viên không còn là thành viên đang hoạt động.",
    candidate_is_owner: "Ứng viên đã là chủ sở hữu.",
    member_not_found: "Thành viên này không tồn tại.",
    job_not_found: "Job này không tồn tại, chưa thất bại, hoặc thuộc về đại lý khác.",
    synthetic_required: "Môi trường này chỉ nhận đại lý giả lập (thử nghiệm).",
    synthetic_not_allowed: "Môi trường production không nhận đại lý giả lập.",
    synthetic_flag_mismatch: "Đại lý đã tồn tại với thiết lập giả lập ngược lại.",
    invalid_display_name: "Tên đại lý không hợp lệ.",
    invalid_locale: "Ngôn ngữ và khu vực phải là en-AU, en-GB, en-US hoặc vi-VN.",
    invalid_ticket_ref: "Mã ticket phải có dạng SUP-1024.",
    owner_invite_not_found: "Không có lời mời chủ sở hữu đầu tiên nào để rút lại (không có lời mời đang chờ, hoặc lời mời đã được chấp nhận).",
    not_synthetic: "Ở đây chỉ xoá được đại lý thử nghiệm (giả lập). Đại lý thật do chính chủ sở hữu xoá.",
    agency_name_mismatch: "Tên vừa nhập không phải tên đại lý. Hãy nhập đúng như đang hiển thị.",
    has_members: "Đại lý vẫn còn thành viên đang hoạt động hoặc đang được mời.",
  },
);
