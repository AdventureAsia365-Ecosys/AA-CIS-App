// app/admin/tenants/_components/voiceMemory.messages.ts — AA-921 WM-29: the Tenant 360 "Voice memory" card, English and
// Vietnamese. Writer, kind and channel codes stay as the API sends them.

import { defineMessages } from "../../../_kit/core/i18n";

export const VM = defineMessages(
  {
    title: "Voice memory",
    help: "What the writers learned for this tenant: its active rules (and voice summary), its masked voice examples "
      + "and its latest release per writer. Admin only; the tenant never sees any of it.",
    loading: "Loading voice memory…",
    loadError: (msg: string) => `Could not load the voice memory: ${msg}`,
    activeRules: "Active rules",
    candidateRules: (n: number) => `${n} candidate(s) waiting`,
    summaries: "Voice summaries",
    summaryOff: "drafting needs the LLM path (off)",
    summaryReady: "drafting available",
    exemplars: "Voice examples",
    activeReleases: "Active tenant releases",
    noReleases: "No tenant release yet: the writers use the platform rules only.",
    colWriter: "Writer",
    colRelease: "Release",
    colStatus: "Status",
    colClass: "Content",
    colRules: "Rules",
    colActivated: "Activated",
    examplesByChannel: "Examples per writer and channel",
    noExamples: "No voice example for this tenant yet.",
    exampleCounts: (pinned: number, candidate: number, never: number) =>
      `${pinned} pinned · ${candidate} candidate · ${never} never use`,
    summaryHeading: "Voice summary",
    openWritingMemory: "Open Writing memory",
  },
  {
    title: "Bộ nhớ giọng văn",
    help: "Những gì writer đã học cho tenant này: các luật đang hoạt động (và tóm tắt giọng văn), các ví dụ giọng văn "
      + "đã che và bản phát hành gần nhất của mỗi writer. Chỉ admin thấy; tenant không bao giờ thấy.",
    loading: "Đang tải bộ nhớ giọng văn…",
    loadError: (msg: string) => `Không tải được bộ nhớ giọng văn: ${msg}`,
    activeRules: "Luật đang hoạt động",
    candidateRules: (n: number) => `${n} ứng viên đang chờ`,
    summaries: "Tóm tắt giọng văn",
    summaryOff: "soạn tóm tắt cần đường LLM (đang tắt)",
    summaryReady: "có thể soạn tóm tắt",
    exemplars: "Ví dụ giọng văn",
    activeReleases: "Bản phát hành tenant đang chạy",
    noReleases: "Chưa có bản phát hành cho tenant: writer chỉ dùng luật của nền tảng.",
    colWriter: "Writer",
    colRelease: "Bản phát hành",
    colStatus: "Trạng thái",
    colClass: "Nội dung",
    colRules: "Luật",
    colActivated: "Kích hoạt",
    examplesByChannel: "Ví dụ theo writer và kênh",
    noExamples: "Tenant này chưa có ví dụ giọng văn.",
    exampleCounts: (pinned: number, candidate: number, never: number) =>
      `${pinned} đã ghim · ${candidate} ứng viên · ${never} không dùng`,
    summaryHeading: "Tóm tắt giọng văn",
    openWritingMemory: "Mở Writing memory",
  },
);
