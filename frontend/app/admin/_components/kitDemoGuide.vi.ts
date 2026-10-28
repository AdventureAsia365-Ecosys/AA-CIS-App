// app/admin/_components/kitDemoGuide.vi.ts — AA-994: the Vietnamese twin of kitDemoGuide.ts (same section ids).

import type { GuideContent } from "../../_kit";

export const DEMO_GUIDE_VI: GuideContent = {
  title: "Cách dùng trang mẫu",
  intro: "Mọi trang admin có tab Hướng dẫn đều hiển thị GuideContent của nó bằng GuidePanel: cùng một giao diện ở mọi nơi.",
  sections: [
    { id: "purpose", heading: "Trang này dùng để làm gì", blocks: [
      { kind: "paragraph", text: "Một đoạn văn bằng lời lẽ đơn giản." },
      { kind: "flow", steps: ["Nháp", "Kiểm tra", "Duyệt", "Đang chạy"] },
      { kind: "callout", tone: "note", text: "Một ghi chú: điều nên biết." },
    ] },
    { id: "buttons", heading: "Các nút", blocks: [
      { kind: "control", label: "Chạy kiểm tra", does: "Nút này làm gì.", cost: "$0. Khoảng một phút.",
        when: "Khi nào nên dùng.", avoid: "Khi nào không nên dùng." },
      { kind: "callout", tone: "warning", title: "Một cái bẫy", text: "Một cảnh báo: một khoản chi phí, một cái bẫy, một chỗ còn thiếu đã biết." },
    ] },
    { id: "labels", heading: "Đọc các nhãn", blocks: [
      { kind: "subheading", text: "Trạng thái" },
      { kind: "terms", uiLabels: true, items: [{ term: "đang chạy / tạm dừng", text: "Các nhãn trang hiển thị, dạng chip." }] },
      { kind: "list", ordered: true, items: ["Một bước có đánh số.", "Một bước khác."] },
    ] },
  ],
  glossary: [{ term: "Thuật ngữ", text: "Nghĩa của nó." }],
};
