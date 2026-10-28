// lib/rbac.messages.ts
// AA-994 — staff role names (lib/rbac.ts ROLE_LABEL, same keys) and the role refusal of staffErrorMessage(), in
// English and Vietnamese: `const t = useT(RBAC_MESSAGES); t(role)` / `staffErrorMessage(s, d, f, t("adminOnly"))`.

import { defineMessages } from "../app/_kit/core/i18n";

export const RBAC_MESSAGES = defineMessages(
  {
    admin: "Administrator",
    reviewer: "Reviewer",
    content: "Content Team",
    viewer: "Viewer (read-only)",
    adminOnly: "Admin only: your role cannot make this change.",
  },
  {
    admin: "Quản trị viên",
    reviewer: "Người duyệt",
    content: "Nhóm nội dung",
    viewer: "Người xem (chỉ đọc)",
    adminOnly: "Chỉ quản trị viên: vai trò của bạn không được thực hiện thay đổi này.",
  },
);
