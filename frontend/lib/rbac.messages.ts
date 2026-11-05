// lib/rbac.messages.ts
// AA-994 — staff role names (lib/rbac.ts ROLE_LABEL, same keys) and the role refusal of staffErrorMessage(), in
// English and Vietnamese: `const t = useT(RBAC_MESSAGES); t(role)` / `staffErrorMessage(s, d, f, t("adminOnly"))`.

import { defineMessages } from "../app/_kit/core/i18n";

export const RBAC_MESSAGES = defineMessages(
  {
    admin: "Administrator",
    content: "Content Team",
    viewer: "Viewer (read-only)",
    content_lead: "Content Lead",
    tenant_qa: "Tenant Content QA",
    customer_success: "Customer Success",
    support: "Support",
    finance: "Finance",
    ai_ops: "AI Operations",
    adminOnly: "Admin only: your role cannot make this change.",
  },
  {
    admin: "Quản trị viên",
    content: "Nhóm nội dung",
    viewer: "Người xem (chỉ đọc)",
    content_lead: "Trưởng nhóm nội dung",
    tenant_qa: "Kiểm duyệt nội dung tenant",
    customer_success: "Chăm sóc khách hàng",
    support: "Hỗ trợ",
    finance: "Tài chính",
    ai_ops: "Vận hành AI",
    adminOnly: "Chỉ quản trị viên: vai trò của bạn không được thực hiện thay đổi này.",
  },
);
