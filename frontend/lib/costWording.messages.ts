// lib/costWording.messages.ts — AA-994: the cost wording of lib/costWording.ts (Overview "Cost per app" card and
// Platform › Cost per app) in English and Vietnamese: `useT(COST_WORDING)`, and
// `aiOnBillNote(ai, onBill, fmt, COST_WORDING[lang])`.

import { defineMessages } from "../app/_kit/core/i18n";
import {
  AI_ESTIMATE_LABEL,
  AI_NOTE_WORDS,
  AI_ON_BILL_LABEL,
  AI_ON_BILL_SRC,
  AI_OUTSIDE_LABEL,
  AI_OUTSIDE_SRC,
  AWS_BILL_LABEL,
  AWS_BILL_SRC,
} from "./costWording";

export const COST_WORDING = defineMessages(
  {
    awsBill: AWS_BILL_LABEL,
    aiOutside: AI_OUTSIDE_LABEL,
    aiEstimate: AI_ESTIMATE_LABEL,
    aiOnBill: AI_ON_BILL_LABEL,
    awsBillSrc: AWS_BILL_SRC,
    aiOutsideSrc: AI_OUTSIDE_SRC,
    aiOnBillSrc: AI_ON_BILL_SRC,
    ...AI_NOTE_WORDS,
  },
  {
    awsBill: "Hoá đơn AWS",
    aiOutside: "AI ngoài hoá đơn AWS",
    aiEstimate: "Ước tính AI",
    aiOnBill: "AI đã nằm trong hoá đơn AWS",
    awsBillSrc:
      "AWS Cost Explorer của tài khoản AWS chính, chia theo thẻ App của từng tài nguyên (làm mới khoảng mỗi ngày một lần)",
    aiOutsideSrc:
      "Ước tính từ nhật ký cuộc gọi của cổng AI cho phần AI không tính trên tài khoản AWS chính: các tài khoản Bedrock " +
      "khác, OpenAI và Jev",
    aiOnBillSrc:
      "Các cuộc gọi Bedrock trên tài khoản AWS chính: là một phần của ước tính AI nhưng đã nằm trong hoá đơn AWS, nên " +
      "được trừ ra một lần",
    aiNoneOnBill: (ai: string) => `Ước tính AI tổng cộng ${ai}; không có phần nào nằm trong hoá đơn AWS.`,
    aiSomeOnBill: (ai: string, onBill: string) =>
      `Ước tính AI tổng cộng ${ai}; trong đó ${onBill} là Bedrock trên tài khoản AWS chính, đã nằm trong hoá đơn AWS, ` +
      "nên chỉ tính một lần.",
  },
);
