// app/admin/ai-gateway/guide.vi.ts — AA-804: the Vietnamese twin of guide.ts (same section ids, blocks and controls;
// lib/guides.test.mjs checks the outline). The quoted labels are the page's Vietnamese words (messages.ts).
// Data only (the type import is erased), so node --test loads it directly.

import type { GuideContent } from "../../_kit/guideContent";

export const AI_GATEWAY_GUIDE_VI: GuideContent = {
  title: "Cách dùng trang Nhà cung cấp AI",
  intro: "Một trang cho mọi nguồn AI và dữ liệu trả phí. Đọc trang để trả lời ba câu: từng nguồn còn trả lời không, "
    + "đã tốn bao nhiêu, có gì sắp chạm giới hạn không.",
  sections: [
    {
      id: "purpose",
      heading: "Trang này để làm gì",
      blocks: [
        { kind: "paragraph", text: "Nền tảng trả tiền cho vài dịch vụ bên ngoài: các model ngôn ngữ (qua AWS Bedrock "
          + "hoặc API của hãng), Jev cho các quyết định có / không, DataForSEO cho dữ liệu tìm kiếm và Geoapify cho "
          + "toạ độ địa điểm. Trang này đặt tất cả cạnh nhau." },
        { kind: "paragraph", text: "Trang chỉ đọc. Mọi con số lấy từ sổ ghi của mình và các lần kiểm số dư đã lưu; "
          + "mở trang không gọi nhà cung cấp nào và không tốn tiền. Bật / tắt kết nối và sửa giới hạn vẫn làm ở Cài "
          + "đặt › LLM Models; ngân sách vẫn đặt ở Cost Operations." },
      ],
    },
    {
      id: "tiles",
      heading: "Ba ô tóm tắt",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "Chi tháng này", text: "Mọi lần gọi trả phí đã ghi từ ngày đầu tháng (UTC), cộng mọi nguồn." },
          { term: "Chi hôm nay", text: "Như trên nhưng từ nửa đêm UTC. Trần theo ngày đặt lại lúc đó." },
          { term: "Cần xem", text: "Số nguồn không Ổn và không đang tắt. Bằng 0 là mọi thứ ổn." },
        ] },
      ],
    },
    {
      id: "columns",
      heading: "Đọc một dòng thế nào",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "Kết nối LLM", text: "Mỗi dòng là một đường nền tảng gọi tới model, ví dụ Bedrock trong một tài "
            + "khoản AWS (hiện bằng số tài khoản) hoặc API của OpenAI." },
          { term: "Quyết định (Jev)", text: "Dòng của Jev. Số dư là ước tính: tổng nạp đã ghi ở Cài đặt trừ chi tiêu "
            + "đã ghi, vì Jev không có dịch vụ xem số dư." },
          { term: "Nhà cung cấp dữ liệu", text: "DataForSEO (số dư từ lần kiểm hằng ngày) và Geoapify (credit đã dùng "
            + "hôm nay so với giới hạn ngày; giới hạn đặt lại mỗi ngày UTC)." },
          { term: "Sức khoẻ", text: "Với kết nối: lần trả lời gần nhất và loại lỗi gần nhất. Với Jev: số lần gọi và "
            + "lỗi trong 24 giờ qua, thời gian trả lời trung bình." },
          { term: "Ngân sách", text: "Ngân sách tháng của kết nối và trần ngày của nhà cung cấp, kèm phần đã dùng. "
            + "Dừng ở trần thì chặn lần gọi tiếp; chỉ cảnh báo thì chỉ báo." },
          { term: "Số dư / credit", text: "Phần còn lại ở nhà cung cấp, hoặc credit hôm nay với Geoapify." },
        ] },
      ],
    },
    {
      id: "statuses",
      heading: "Trạng thái",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "Ổn", text: "Đang trả lời, chưa gần giới hạn nào." },
          { term: "Suy giảm", text: "Vẫn trả lời nhưng gần đây có lần lỗi (với Jev: phần lớn lần gọi 24 giờ qua)." },
          { term: "Đang ngắt", text: "Nhiều lần gọi liên tiếp bị lỗi nên cổng tạm bỏ qua kết nối này và dùng đường "
            + "kế tiếp. Cổng tự thử lại." },
          { term: "Sắp hết", text: "Số dư dưới ngưỡng cảnh báo, hoặc đã dùng quá 80 % credit hôm nay." },
          { term: "Chạm trần", text: "Ngân sách, số dư hoặc credit hôm nay đã hết. Lần gọi dừng hoặc chuyển đường dự "
            + "phòng cho tới khi nâng trần hoặc hết kỳ." },
          { term: "Tắt", text: "Cố ý tắt. Không tính là có vấn đề." },
          { term: "Chưa rõ", text: "Chưa có lần gọi gần đây, chưa đọc số dư, hoặc không đọc được nguồn. Dòng đó ghi "
            + "rõ lý do." },
        ] },
      ],
    },
    {
      id: "trace",
      heading: "Xem các lần gọi của một job (Truy vết)",
      blocks: [
        { kind: "flow", steps: ["Jobs", "Bấm một job", "Mở truy vết", "Đọc các lần gọi theo thời gian"] },
        { kind: "paragraph", text: "Truy vết liệt kê mọi lần gọi trả phí đã ghi của job: gọi model, quyết định Jev, "
          + "yêu cầu DataForSEO và yêu cầu geocode, kèm lúc nào, việc gì, kết quả, chi phí và (với Jev) thời gian trả "
          + "lời. Dòng trên cùng là tổng theo từng sổ ghi." },
        { kind: "control", label: "Mở truy vết", does: "Mở trang này kèm ngăn truy vết của job.",
          cost: "Miễn phí: chỉ đọc sổ ghi.", when: "Một job tốn hơn dự kiến, hoặc lỗi và muốn biết lần gọi nào sai." },
        { kind: "control", label: "Làm mới", does: "Đọc lại mọi nguồn.", cost: "Miễn phí." },
      ],
    },
    {
      id: "limits",
      heading: "Những gì trang chưa hiện được",
      blocks: [
        { kind: "callout", tone: "warning", title: "Lần gọi bị từ chối",
          text: "Lần gọi bị nhà cung cấp từ chối chưa được ghi, nên không hiện trong truy vết hay chi tiêu. Cột Sức "
            + "khoẻ vẫn hiện lỗi. Sổ ghi mới sẽ thêm các dòng này với chi phí 0." },
        { kind: "list", items: [
          "Thời gian trả lời của từng lần gọi model chưa được ghi; chỉ Jev có.",
          "Bedrock Guardrails và Mapbox chưa ghi sổ nên chưa có dòng.",
          "Chi tiêu là số mình ghi theo giá của mình. Hoá đơn AWS có thể lệch chút; Cost Operations hiện hoá đơn.",
        ] },
      ],
    },
  ],
  glossary: [
    { term: "Kết nối", text: "Một đường tới nhà cung cấp model: một tài khoản AWS có Bedrock, hoặc một khoá API của "
      + "hãng." },
    { term: "Cổng", text: "Phần code chung mà mọi lần gọi model đi qua; nó chọn đường, ghi lần gọi và giữ ngân sách." },
    { term: "Trần ngày", text: "Mức tối đa một nhà cung cấp được tốn trong một ngày UTC cho cả nền tảng." },
    { term: "Truy vết", text: "Mọi lần gọi trả phí đã ghi của một job, theo thời gian." },
  ],
};
