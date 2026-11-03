// app/admin/tenant-qa/slate/messages.ts — AA-1039 (S266): the Slate page frame (Tenant content QA › Slate), English and
// Vietnamese. The section's own copy stays in atom-curation/messages.ts (AT), where it lived before the move.

import { defineMessages } from "../../../_kit/core/i18n";

export const SLATE_PAGE = defineMessages(
  {
    title: "Slate",
    description:
      "The content subjects each tenant's planner proposed per channel, and what became of them. Pick a tenant to see its slate.",
  },
  {
    title: "Slate",
    description:
      "Các chủ đề nội dung mà bộ lập kế hoạch của từng tenant đề xuất theo kênh, và kết quả của chúng. Chọn một tenant để xem slate.",
  },
);
