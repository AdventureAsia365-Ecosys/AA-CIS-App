// app/admin/ai-gateway/experiments.messages.ts — AI providers › Models & trials › Experiments (AA-900 UI, S262 slice
// C4), English and Vietnamese. Stage codes, versions, pipelines and verdict codes stay as the API sends them.

import { defineMessages } from "../../_kit/core/i18n";

export const EXPERIMENTS = defineMessages(
  {
    title: "Experiments",
    intro: "What was tried before a change ships: the prompt versions on record, the evaluation runs on a fixed set of "
      + "tours, and (above) the shadow comparisons on real calls. Read-only: runs are started by the team, not from "
      + "this page.",
    promptsTitle: "Prompt versions",
    promptsHelp: "One line per stage and version: when it was recorded and a short fingerprint of the prompt. The "
      + "prompt text itself is not stored here.",
    promptsNotReady: "The prompt register is not in the database yet; it arrives with a database update.",
    promptsEmpty: "No prompt version is recorded yet",
    promptsEmptyBody: "Versions are recorded when a prompt change is reviewed.",
    runsTitle: "Evaluation runs",
    runsHelp: (n: number) => `The newest ${n} runs: what was compared, on how many tours, the verdict and the cost.`,
    runsEmpty: "No evaluation run yet",
    runsNotReady: "The evaluation runs could not be read.",
    colStage: "Stage",
    colVersion: "Version",
    colFingerprint: "Fingerprint",
    colRecorded: "Recorded",
    colNotes: "Notes",
    colRun: "Run",
    colCompared: "Compared",
    colTours: "Tours",
    colScore: "Mean score",
    colGates: "Gates pass / fail",
    colVerdict: "Verdict",
    colCost: "Cost",
    colWhen: "Started",
    vsBaseline: (v: string) => `vs ${v}`,
    regression: "regression",
    running: "running",
    loadError: "Could not load the experiments",
  },
  {
    title: "Thử nghiệm",
    intro: "Những gì đã thử trước khi một thay đổi được đưa vào: các phiên bản prompt đã ghi, các lượt đánh giá trên một "
      + "bộ tour cố định, và (ở trên) so sánh chạy bóng trên lượt gọi thật. Chỉ xem: lượt chạy do nhóm khởi động, không "
      + "từ trang này.",
    promptsTitle: "Phiên bản prompt",
    promptsHelp: "Mỗi dòng là một bước và một phiên bản: ghi lúc nào và dấu vân tay ngắn của prompt. Nội dung prompt "
      + "không lưu ở đây.",
    promptsNotReady: "Sổ đăng ký prompt chưa có trong cơ sở dữ liệu; nó đến cùng một lần cập nhật cơ sở dữ liệu.",
    promptsEmpty: "Chưa ghi phiên bản prompt nào",
    promptsEmptyBody: "Phiên bản được ghi khi một thay đổi prompt được duyệt.",
    runsTitle: "Lượt đánh giá",
    runsHelp: (n: number) => `${n} lượt mới nhất: so sánh gì, trên bao nhiêu tour, kết luận và chi phí.`,
    runsEmpty: "Chưa có lượt đánh giá nào",
    runsNotReady: "Không đọc được các lượt đánh giá.",
    colStage: "Bước",
    colVersion: "Phiên bản",
    colFingerprint: "Dấu vân tay",
    colRecorded: "Ghi lúc",
    colNotes: "Ghi chú",
    colRun: "Lượt",
    colCompared: "So với",
    colTours: "Tour",
    colScore: "Điểm trung bình",
    colGates: "Cổng đạt / trượt",
    colVerdict: "Kết luận",
    colCost: "Chi phí",
    colWhen: "Bắt đầu",
    vsBaseline: (v: string) => `so với ${v}`,
    regression: "thụt lùi",
    running: "đang chạy",
    loadError: "Không tải được phần thử nghiệm",
  },
);
