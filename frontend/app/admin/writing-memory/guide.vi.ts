// app/admin/writing-memory/guide.vi.ts — AA-994: the Vietnamese twin of guide.ts (the Guide tab of Writing memory).
// Same section ids and the same blocks in the same order (lib/guides.test.mjs checks the outline); the English file is
// the original, so change it first and this one in the same PR. Terms in `uiLabels` blocks are the page's Vietnamese
// words (messages.ts, releases.messages.ts, lib/writingMemory.messages.ts). Data only, so node --test loads it.

import type { GuideContent } from "../../_kit/guideContent";

export const WRITING_MEMORY_GUIDE_VI: GuideContent = {
  title: "Cách dùng Bộ nhớ viết",
  intro: "Hãy đọc một lần từ đầu đến cuối. Sau đó, chỉ cần làm theo quy trình hằng tuần.",
  sections: [
    {
      id: "purpose",
      heading: "Trang này dùng để làm gì",
      blocks: [
        { kind: "paragraph", text: "Các trình viết của chúng ta (tour A1, tour T2 của tenant, bài blog và mạng xã hội "
          + "T9) lặp lại cùng một lỗi nhiều lần, và người ta phải sửa tay. Trang này gom các lần sửa đó lại và tìm ra "
          + "những lỗi lặp lại. Sau đó nó biến chúng thành các quy tắc ngắn để trình viết làm theo ở lần sau." },
        { kind: "paragraph", text: "Không gì ở đây tự thay đổi một trình viết. Quy tắc chỉ đến với trình viết sau một "
          + "lượt kiểm và một cú bấm của người. Tenant không bao giờ thấy trang này hay các quy tắc của nó. Họ chỉ nhận "
          + "được văn bản cần sửa ít hơn." },
        { kind: "callout", tone: "note", title: "Con đường duy nhất đang chạy thật hôm nay",
          text: "Hôm nay chỉ một con đường thật sự thay đổi những gì trình viết viết ra: một quy tắc từ vựng (cấm hoặc "
            + "thay một cụm từ) cho blog hoặc mạng xã hội T9, qua Chạy R0 rồi Kích hoạt (L0). Mọi thứ khác trên trang "
            + "chỉ đang gom bằng chứng cho sau này." },
      ],
    },
    {
      id: "loop",
      heading: "Cách hoạt động, trong 6 bước",
      blocks: [
        { kind: "flow", steps: [
          "Ghi nhận (lần sửa, lỗi gate → tín hiệu)",
          "Bộ lọc dữ kiện",
          "Gom cụm",
          "Soạn (Khai thác quy tắc)",
          "Kiểm (lint + R0)",
          "Chạy thật (Kích hoạt (L0) hoặc một bản phát hành)",
        ] },
        { kind: "list", ordered: true, items: [
          "Ghi nhận. Một admin cắt hoặc viết lại một câu của bài T2 hay T9, hoặc sửa một trường A1. Mỗi thay đổi được "
            + "lưu ngay thành một tín hiệu. Tenant sửa một bài T9 trong portal cũng tạo một tín hiệu, một cách lặng lẽ. "
            + "Admin từ chối, bỏ qua hoặc tạo lại một dòng duyệt chỉ tạo tín hiệu khi chọn một chip lý do. Ba nguồn "
            + "không bao giờ được ghi nhận trực tiếp. Chúng chỉ đến qua Nạp bù tín hiệu: các lần sửa T2 của tenant, các "
            + "lần thử T9 thất bại và bài học kiểm thương hiệu A1.",
          "Bộ lọc dữ kiện. Mỗi ngày một lần, hoặc khi bạn bấm Chạy bộ lọc dữ kiện, mỗi tín hiệu mới được đánh dấu đạt, "
            + "xám hoặc trượt. Trượt: lần sửa thêm một dữ kiện mà nguồn không có, nên ta không bao giờ học từ nó. Xám: "
            + "lần sửa thêm một cái tên mà nguồn không có, nên admin quyết định. Đạt: ta có thể học từ nó.",
          "Gom cụm. Các tín hiệu đạt trong 90 ngày gần nhất cùng chỉ vào một vấn đề tạo thành một cụm. Cụm lặp lại đủ "
            + "nhiều thì 'vượt ngưỡng'.",
          "Soạn (Khai thác quy tắc, tính phí). Mỗi cụm sẵn sàng được một lượt gọi model soạn một quy tắc ngắn. Một lượt "
            + "kiểm tự động, lint, thử bản soạn. Bản soạn đạt được lưu thành ứng viên; bản soạn trượt được lưu là bị lint "
            + "từ chối. Có lượt gọi tính phí không lưu gì cả: model trả lời không có quy tắc nào hợp (NONE), câu trả lời "
            + "không dùng được, hoặc cơ sở dữ liệu từ chối dòng đó. Khi ấy không có quy tắc nào, cụm vẫn giữ nhãn "
            + "'lần chạy tới sẽ soạn', và lần chạy tới lại trả tiền cho nó.",
          "Kiểm (miễn phí). Admin đọc ứng viên, rồi sửa hoặc từ chối nó. Sau đó admin chạy R0: phát lại trên văn bản và "
            + "tín hiệu đã lưu. Nó cho thấy quy tắc sẽ bật ở đâu và những lỗi cũ nào nó lẽ ra đã tránh được. R0 không gọi "
            + "model.",
          "Chạy thật. Quy tắc từ vựng T9 có R0 đạt: bấm Kích hoạt (L0), và lần viết T9 tới sẽ dùng nó. Mọi quy tắc khác "
            + "phải vào một bản phát hành: đánh giá, rồi một lần người duyệt, rồi kích hoạt, ở tab Bản phát hành.",
        ] },
        { kind: "subheading", text: "Hoàn tác (quay lui)" },
        { kind: "list", items: [
          "Quy tắc L0: mở nó trong Ứng viên (bộ lọc trạng thái: đang hoạt động), nhập lý do, bấm Quay lui. Lần viết tới "
            + "không còn nhận nó.",
          "Bản phát hành: mở nó trong Bản phát hành, nhập lý do, bấm Quay lui. Bản phát hành trước hoạt động trở lại. "
            + "Không cần deploy. Bản phát hành 0 không có bản trước nên không quay lui được: hãy bấm Tắt bộ nhớ cho "
            + "trình viết đó.",
        ] },
      ],
    },
    {
      id: "tabs",
      heading: "Các tab và nút",
      blocks: [
        { kind: "subheading", text: "Các ô ở trên cùng" },
        { kind: "list", items: [
          "Tín hiệu: tổng số, rồi bao nhiêu đạt, xám, trượt và chờ.",
          "Ứng viên: các quy tắc đang chờ người, và bao nhiêu đã bị từ chối.",
          "Chi phí vòng học tháng này: số tiền khai thác và các lần chạy học khác đã tiêu, so với trần $25 mỗi tháng và "
            + "trần $1 mỗi lần khai thác.",
          "Lần chạy tự động tới: tắt, đang chờ hoặc đến hạn. Khai thác tự động mặc định là tắt, nên 'tắt' là bình thường.",
          "Dòng dưới các ô: trạng thái của job cuối cùng mỗi loại. Chỉ trạng thái, không có số. Mở Nền tảng › Jobs để "
            + "xem số liệu.",
        ] },
        { kind: "callout", tone: "warning", title: "Ô 'Lần chạy tự động tới' có thể sai",
          text: "Nó đọc công tắc trên service API, nhưng các job chạy trên service worker. Nếu công tắc chỉ được đặt trên "
            + "worker, ô này vẫn hiện 'tắt'." },

        { kind: "subheading", text: "Tab Tín hiệu" },
        { kind: "list", items: [
          "Cụm vượt ngưỡng: Điều lặp lại (rê chuột để xem nhãn), Bằng chứng (tín hiệu, bài, tenant, người duyệt), Cơ "
            + "sở, Trạng thái và một Mẫu đã che. Xem thì không tốn gì.",
          "Tín hiệu: 200 tín hiệu mới nhất, có bộ lọc theo trạng thái dữ kiện. Không có trang tiếp theo. Mỗi dòng có "
            + "Thời điểm, Phạm vi, Trình viết, Hành động (đã làm gì, ai làm, mã lý do, gate), Trước → sau, và Dữ kiện.",
          "Tín hiệu xám có nút Đạt và Trượt ở cột Dữ kiện. Nó xám vì nguồn không có cái tên mà lần sửa đã thêm, nên nguồn "
            + "không phân xử được. Bấm Đạt khi cái tên được thêm là đúng và không phải dữ kiện bịa. Ngược lại thì bấm "
            + "Trượt. Tín hiệu có mã lý do fact_not_in_source (hiện nguyên văn ở cột Hành động) không bao giờ đặt được "
            + "thành đạt.",
        ] },
        { kind: "callout", tone: "note",
          text: "Phạm vi hiện 'Nền tảng' (tenant chủ của AA) hoặc 'Tenant' cùng 8 ký tự đầu của id. Các danh sách Tín "
            + "hiệu, Ứng viên và Quy tắc không bao giờ hiện tên tenant. Chỉ tab Bản phát hành nêu tên tenant sở hữu "
            + "một bản phát hành." },

        { kind: "subheading", text: "Tab Ứng viên" },
        { kind: "paragraph", text: "Các quy tắc mà bộ khai thác đã soạn. Bộ lọc trạng thái bắt đầu ở ứng viên. Chuyển "
          + "sang đang hoạt động để xem các quy tắc đang chạy, hoặc bị từ chối để xem những gì đã trượt. Các cột: Ngày "
          + "soạn, Phạm vi, Trình viết, Loại, Quy tắc (→ từ thay thế), Bằng chứng, Lint, Trạng thái. Bấm một dòng để mở "
          + "ngăn chi tiết." },
        { kind: "paragraph", text: "Trong ngăn chi tiết:" },
        { kind: "list", items: [
          "Sửa (chỉ ứng viên): đổi quy tắc (≤ 25 từ), từ thay thế (chỉ quy tắc từ vựng; để trống nghĩa là cấm cụm từ) và "
            + "các cụm từ khớp (mỗi dòng một cụm, ≤ 5). Với quy tắc từ vựng, cụm từ bị cấm chính là văn bản Quy tắc, "
            + "không phải các cụm từ khớp. 'Lưu và lint' chạy lint lại. Nếu lint trượt, quy tắc thành bị từ chối, ghi là "
            + "do bạn từ chối (không phải do lint). Vì vậy lần Khai thác quy tắc tới sẽ soạn lại cụm của nó, với một "
            + "lượt gọi tính phí mới. Nếu lần sửa có văn bản của tenant, tên thương hiệu tenant hoặc từ bị cấm, hoặc quá "
            + "dài, sẽ không lưu gì và một thông báo lỗi nói lý do.",
          "Từ chối (ứng viên): nhập lý do (ít nhất 3 ký tự), rồi bấm Từ chối.",
          "Chạy R0: miễn phí, không gọi model. Với quy tắc T9, nó phát lại quy tắc trên 300 bài T9 đã lưu mới nhất (đã "
            + "duyệt hoặc bị giữ, 90 ngày gần nhất) và các tín hiệu của trình viết. Với quy tắc A1 hoặc T2, nó chỉ đọc "
            + "tín hiệu: không phát lại bài nào. Nó chạy trên worker và mất vài phút. Khi job xong, hãy đóng rồi mở lại "
            + "ngăn chi tiết để xem kết quả.",
          "Kích hoạt (L0): chỉ cho quy tắc từ vựng của t9_blog hoặc t9_social. Nút bị mờ, kèm 'Chưa kích hoạt được: …', "
            + "cho tới khi mọi điều kiện kích hoạt đều thỏa (liệt kê ở 'Đọc trạng thái và nhãn'). Sau cú bấm, lần viết "
            + "T9 tới nhận quy tắc. Quy tắc có Phạm vi Nền tảng đến với các lần viết T9 của mọi tenant, nên cần chủ "
            + "chương trình đồng ý trước khi bạn bấm. Trang không kiểm điều này.",
          "Quay lui (chỉ quy tắc đang hoạt động): nhập lý do (ít nhất 3 ký tự), rồi bấm. Quy tắc thành bị từ chối, và "
            + "lần viết tới không còn nhận nó.",
          "Nguồn gốc: mọi tín hiệu đứng sau quy tắc, với nguồn và trước/sau của nó. Hãy đọc trước khi kích hoạt bất cứ "
            + "thứ gì.",
        ] },
        { kind: "callout", tone: "note",
          text: "Mọi lần sửa đều làm kết quả R0 cũ hết hiệu lực. Chạy R0 lại trước khi Kích hoạt (L0)." },

        { kind: "subheading", text: "Một quy tắc L0 đang hoạt động làm gì" },
        { kind: "list", items: [
          "Cụm từ được thêm vào danh sách từ 'Không bao giờ dùng' của thương hiệu cho các lần viết T9. Quy tắc nền tảng "
            + "đến với các lần viết T9 của mọi tenant. Quy tắc tenant chỉ đến với tenant đó. Khi quy tắc liệt kê kênh, "
            + "nó chỉ áp dụng trên các kênh đó.",
          "Khi có từ thay thế, trình viết còn được dặn 'Viết X thay vì Y'.",
          "Nếu cụm từ vẫn xuất hiện, gate từ vựng gắn cờ chặn, và bước sửa có thể sửa nó.",
          "Nó bị bỏ qua khi cụm từ là một từ đặc trưng của tenant, hoặc là một phần của CTA, tên thương hiệu hay tên "
            + "chuyến đi. Quy tắc riêng của tenant thắng quy tắc nền tảng.",
          "Nó dẫn hướng trình viết trước các gate. Nó không bao giờ tự đổi chữ trong văn bản sau đó.",
        ] },

        { kind: "subheading", text: "Tab Bản phát hành" },
        { kind: "paragraph", text: "Trình viết, ở trên cùng: mỗi trình viết một dòng với công tắc Bộ nhớ (bật hoặc tắt), "
          + "số của bản phát hành nền tảng đang hoạt động, bao nhiêu bản phát hành tenant đang hoạt động, và tháng mà "
          + "lịch phát hành đã đóng tới. Bên dưới: mọi bản phát hành, số mới nhất trước, có bộ lọc theo trình viết và "
          + "trạng thái. Các cột: Trình viết, Chủ sở hữu, Số, Trạng thái, Nội dung (loại và số quy tắc), Bằng chứng, "
          + "Người soạn / người duyệt, Ngày tạo, Ngày kích hoạt, Prompt (phần đầu mã băm prompt mà lần phát lại R0 đã "
          + "đo). Chủ sở hữu là 'Nền tảng' hoặc tên tenant (slug khi không có tên, hoặc 'Tenant' cùng 8 ký tự đầu của "
          + "id khi danh sách tenant không biết nó; rê chuột để xem đủ id). Bấm một dòng để mở ngăn chi tiết: nó cũng "
          + "hiện đủ id của tenant, để hỗ trợ." },
        { kind: "list", items: [
          "Tắt bộ nhớ / Bật bộ nhớ (Trình viết): công tắc ngắt, một cú bấm và một xác nhận. Tắt: lần viết tới của trình "
            + "viết đó bỏ qua mọi bản phát hành. Muốn bật cần một bản phát hành nền tảng đang hoạt động. Nó không ảnh "
            + "hưởng quy tắc L0.",
          "Đóng tháng (Trình viết): chọn một tháng. Sau đó không bản phát hành thường kỳ nào của tháng đó hoặc trước đó "
            + "được kích hoạt. Bản phát hành từ vựng, khẩn cấp và quay lui không bao giờ bị chặn. Không mở lại được ở đây.",
          "Bản nháp mới: chọn trình viết và chủ sở hữu (Nền tảng, hoặc Một tenant: khi đó chọn tenant theo tên trong "
            + "danh sách Tenant, có tìm kiếm khi danh sách dài), tuỳ chọn id quy tắc. Không có id quy tắc thì tạo một "
            + "bản phát hành rỗng. Ngăn chi tiết của bản nháp mới sẽ mở ra.",
          "Quét cần kiểm lại: gắn cờ mọi bản phát hành đang hoạt động có golden set, tuyến model hoặc gate đã đổi kể từ "
            + "bằng chứng của nó. Bản bị gắn cờ vẫn tiếp tục phục vụ.",
          "Gắn bằng chứng (ngăn chi tiết, nháp hoặc đã đánh giá): các id lần chạy mà bản phát hành cần. Với bản rỗng, "
            + "'Dùng các lần chạy của bản phát hành nền tảng đang hoạt động' điền sẵn lần phát lại R0 và baseline của "
            + "bản đang chạy của trình viết đó. Khi đã gắn đủ mọi lần chạy cần có, bản phát hành thành đã đánh giá.",
          "Duyệt (ngăn chi tiết, đã đánh giá): một cú bấm của bạn, với tư cách admin đã đăng nhập. Tham chiếu duyệt là "
            + "tuỳ chọn: một đường dẫn tới quyết định.",
          "Kích hoạt (ngăn chi tiết, đã duyệt): bị mờ, kèm 'Chưa kích hoạt được: …', khi bằng chứng chưa đủ hoặc trình "
            + "viết không đọc được loại bản phát hành đó (T2 đọc bản rỗng và bản có quy tắc, không đọc bản chỉ có gợi ý "
            + "sửa; không trình viết nào đọc bản chỉ có từ vựng, loại này chạy thật qua Kích hoạt (L0)). Sau khi xác "
            + "nhận, bản trước đó bị thay và bản này phục vụ lần viết tới.",
          "Kiểm ngay (ngăn chi tiết, bản nào từng hoạt động): chạy lượt kiểm sau kích hoạt hằng giờ một lần, dưới dạng "
            + "chạy thử. Nó đọc các văn bản viết từ khi bản phát hành chạy thật và hiện kết luận (đạt, đang chờ, vi "
            + "phạm, hoặc không áp dụng cho bản rỗng), các con số và, khi vi phạm, id của các văn bản lỗi. Không gọi "
            + "model, không tốn tiền, không thay đổi gì: chỉ lượt kiểm hằng giờ mới quay lui.",
          "Quay lui (ngăn chi tiết, đang hoạt động, có bản trước) và Từ chối (nháp, đã đánh giá hoặc đã duyệt): nhập "
            + "lý do ít nhất 3 ký tự trước. Bản đã quay lui không bao giờ trở lại: muốn sửa thì làm bản mới.",
        ] },
        { kind: "paragraph", text: "Ngăn chi tiết còn hiện từng lần chạy bằng chứng (loại, kết luận, đầu vào, khác biệt "
          + "byte, mã băm đã lưu tái tạo được, mã băm prompt) và dấu vân tay mà lượt quét cần kiểm lại so sánh: golden "
          + "set, phân loại gate, model viết và model judge, prompt." },

        { kind: "subheading", text: "Một bản phát hành hoạt động thế nào" },
        { kind: "flow", steps: ["nháp", "đã đánh giá", "đã duyệt", "đang hoạt động"] },
        { kind: "list", items: [
          "Bản phát hành là một gói quy tắc có phiên bản cho một trình viết và một chủ sở hữu (nền tảng, hoặc một "
            + "tenant).",
          "Kích hoạt một bản phát hành sẽ thay bản trước đó. Bản phát hành có thể bị từ chối ở bất kỳ bước nào trước "
            + "khi hoạt động.",
          "Đã đánh giá nghĩa là đã gắn bằng chứng. Bản rỗng: một lần phát lại R0 với 0 khác biệt trên ít nhất 20 đầu "
            + "vào, cộng baseline đánh giá. Bản chỉ có từ vựng: R0. Bản có quy tắc khác: lần chạy R2 và shadow.",
          "Mọi bản phát hành cần một lần duyệt bởi admin đã đăng nhập (Duyệt ở tab Bản phát hành). Không bản nào tự "
            + "chạy thật. Một ngoại lệ: bản phát hành rỗng (không có quy tắc), bất kể số hay chủ sở hữu, có thể được "
            + "agent duyệt khi nó mang tham chiếu tới quyết định của chủ chương trình. Hai công cụ dòng lệnh làm việc "
            + "này: công cụ release 0 (đã duyệt bản phát hành nền tảng 0) và bài diễn tập quay lui (đã duyệt bản 0 và 1 "
            + "của tenant thử nghiệm).",
          "Mỗi tháng một bản phát hành thường kỳ cho mỗi trình viết và chủ sở hữu (lịch phát hành tháng). Quy tắc từ "
            + "vựng, khẩn cấp và quay lui không phải chờ lịch này.",
          "Bản đang chạy bị gắn cờ 'cần kiểm lại' khi golden set, một tuyến model hoặc các gate đã đổi kể từ bằng chứng "
            + "của nó. Nó vẫn phục vụ cho tới khi được kiểm lại.",
          "Bản đang chạy có quy tắc được kiểm mỗi giờ sau khi chạy thật: văn bản mới phải mang bản phát hành, không có "
            + "dấu vết kiểm chất lượng và không có văn bản của tenant khác. Một văn bản trượt sẽ quay lui trình viết về "
            + "bản trước đó ngay lập tức, kèm cảnh báo ở chuông và qua e-mail. Lượt kiểm này không tốn gì (không gọi "
            + "model).",
          "Bản phát hành tenant chỉ chứa quy tắc của tenant đó. Quy tắc nền tảng dựng từ tín hiệu tenant cần ít nhất 3 "
            + "tenant. Mọi quy tắc phạm vi nền tảng, trong bản phát hành hay qua Kích hoạt (L0), cần chủ chương trình "
            + "đồng ý trước (ADR 0010 quyết định 10). Trang không bắt buộc điều này.",
        ] },
        { kind: "callout", tone: "note",
          text: "Bản phát hành 0 là bản rỗng: khi nó là bản đang hoạt động, đầu ra của trình viết giống hệt như khi "
            + "không có bộ nhớ." },

        { kind: "subheading", text: "Tab Ví dụ mẫu" },
        { kind: "paragraph", text: "Ví dụ giọng văn cho T2 và T9 (A1 không bao giờ có). Một ví dụ là một đoạn trích "
          + "của bài mà admin đã đánh dấu \"dùng làm ví dụ\" trong Content quality, với địa danh, tên, số, giá và giờ "
          + "đã được che. Một bản phát hành có luật ghim tối đa hai ví dụ cho mỗi writer và kênh lúc tạo bản nháp: "
          + "ví dụ đã ghim trước, rồi đến ứng viên mới nhất. Writer chỉ đọc chúng để lấy giọng văn và không được "
          + "dùng lại dữ kiện nào." },
        { kind: "list", items: [
          "Tìm ứng viên (chạy thử) đếm số ví dụ mà Thêm ứng viên sẽ thêm. Thêm ứng viên thêm một ví dụ đã che cho mỗi "
            + "tín hiệu \"dùng làm ví dụ\" đã qua bộ lọc dữ kiện. Cả hai đều không tốn tiền (không gọi model).",
          "Bản nháp tới: chọn một writer và một chủ sở hữu để xem bản nháp có luật sẽ ghim gì lúc này, theo từng kênh.",
          "Bấm một dòng để mở ví dụ. Ghim giữ nó trong bản nháp tới, Bỏ ghim trả nó về ứng viên, Không dùng loại nó "
            + "vĩnh viễn. Mỗi thao tác cần một lý do.",
          "Bản phát hành của một tenant chỉ ghim ví dụ của chính tenant đó. Khi tenant có ít hơn hai ví dụ cho một "
            + "kênh, ví dụ của bản phát hành nền tảng bù vào, không bao giờ lấy của tenant khác.",
        ] },

        { kind: "subheading", text: "Tab Chỉ số" },
        { kind: "paragraph", text: "Báo cáo hằng tuần, đo mỗi thứ Hai cho tuần trước. Không tốn tiền. Dấu gạch nghĩa là "
          + "không đo được writer đó (không có dữ liệu), không bao giờ là số không." },
        { kind: "list", items: [
          "Mỗi writer: Số bài, Bị gắn cờ (khoảng 95%), Lỗi lặp lại, Tỷ lệ sửa nhẹ, Phút để sửa, Tenant sửa / bài, "
            + "Có lý do (xanh từ 80%), Chi phí / bài, Tỷ lệ khớp replay và Cặp sạch.",
          "Điểm dừng mềm: với mỗi bản phát hành có nội dung đang chạy, các điểm dừng mềm đã chạm từ khi chạy và Job đã "
            + "làm gì. Một điểm dừng mềm đưa bản phát hành ra khỏi vận hành, trừ khi có người bấm Giữ lại và ghi lý do "
            + "(từ 1 đến 30 ngày).",
          "Luật không được xác nhận lại bởi chỉnh sửa mới sẽ hết hạn sau 60 ngày. Luật dừng dự án được gắn cờ khi một "
            + "writer có hai bản phát hành không cải thiện.",
          "Đo ngay (chạy thử) cho xem số liệu tuần này mà không lưu hay đổi gì. Chạy job hằng tuần ngay là lần chạy "
            + "thật.",
          "Hàng đợi bất đồng: 5-10 trường hợp mỗi tuần mà judge hoặc cổng kiểm và nhãn của người không khớp. Bấm Judge "
            + "đúng, Người đúng hoặc Chưa rõ.",
        ] },

        { kind: "subheading", text: "Các nút ở đầu trang" },
        { kind: "paragraph", text: "Làm mới chỉ tải lại dữ liệu trang. Mọi nút khác xếp một job nền, và một thông báo "
          + "hiện 'Đã xếp hàng (job …); xem Nền tảng › Jobs'. Kết quả số liệu nằm ở Nền tảng › Jobs, không phải trên "
          + "trang này." },
        { kind: "control", label: "Nạp bù (chạy thử)",
          does: "Đếm những gì Nạp bù tín hiệu sẽ chép, theo nguồn và theo tenant. Bốn nguồn: các lần sửa câu cũ của "
            + "admin, các lần sửa T2 của tenant, các lần thử T9 thất bại và bài học kiểm thương hiệu A1. Nó không chèn "
            + "gì.",
          cost: "$0. Tới khoảng 15 phút.",
          when: "Trước lần nạp bù thật đầu tiên, hoặc để kiểm xem dữ liệu cũ có bị thiếu không." },
        { kind: "control", label: "Nạp bù tín hiệu",
          does: "Chép mọi tín hiệu chưa được ghi nhận, cũ hay mới, từ bốn nguồn trên. Sau đó nó tự xếp bộ lọc dữ kiện. "
            + "Chạy hai lần vẫn an toàn: không gì bị thêm hai lần.",
          cost: "$0. Tới khoảng 15 phút.",
          when: "Mỗi tuần, là bước đầu của quy trình. Các lần sửa T2 của tenant, lần thử T9 thất bại và bài học kiểm "
            + "thương hiệu A1 chỉ đến trang này bằng cách này." },
        { kind: "control", label: "Chạy bộ lọc dữ kiện",
          does: "Đánh dấu các tín hiệu đang chờ là đạt, xám hoặc trượt (tới 5000 mỗi lần chạy). Nó cũng kiểm điều kiện "
            + "khai thác tự động, nhưng chỉ bắt đầu lần chạy tính phí khi khai thác tự động được bật (mặc định là tắt).",
          cost: "$0. Tới khoảng 10 phút.",
          when: "Có tín hiệu đang chờ và bạn không muốn đợi lần chạy hằng ngày." },
        { kind: "control", label: "Baseline R0",
          does: "Lượt 'kiểm tra R0'. Nó chạy lại các gate trên văn bản blog và mạng xã hội T9 đã lưu mà không có quy "
            + "tắc nào, và so kết quả với những gì các gate đã nói lúc viết. Nó không ghi gì. Độ trùng khớp cao nghĩa là "
            + "bạn có thể tin kết quả R0.",
          cost: "$0. Tốn CPU của worker, tới khoảng 20 phút.",
          when: "Sau một thay đổi ở các gate, hoặc trước lần đầu bạn dựa vào kết quả R0.",
          avoid: "Đang có wave chạy." },
        { kind: "control", label: "Khai thác quy tắc",
          does: "Trước tiên nó cho hết hạn các ứng viên và quy tắc đã duyệt quá 60 ngày (trừ quy tắc mà một bản phát "
            + "hành nháp, đã đánh giá hoặc đã duyệt đang giữ). Sau đó nó dựng các cụm và gọi model tối đa một lần cho "
            + "mỗi cụm có nhãn 'lần chạy tới sẽ soạn'. Nó bỏ qua cụm có bản soạn cuối bị lint từ chối khi tín hiệu chưa "
            + "đổi, dù nhãn vẫn hiện 'lần chạy tới sẽ soạn'. Mỗi lượt gọi kết thúc theo một trong ba cách: một ứng viên, "
            + "một bản soạn bị lint từ chối, hoặc không lưu gì (model trả lời NONE, câu trả lời không dùng được, hoặc cơ "
            + "sở dữ liệu từ chối dòng đó). Lượt gọi không lưu gì vẫn bị tính tiền, và lần chạy tới lại trả tiền cho cụm "
            + "đó. Một hộp xác nhận hiện chi phí tháng này trước.",
          cost: "Tính phí. Tối đa 50 lượt gọi và $1 mỗi lần chạy, và $2 mỗi ngày cho riêng khai thác. Trần $25 mỗi "
            + "tháng tính cho cả vòng học (khai thác và các lần chạy đánh giá). Chi phí thật khoảng $0.001 mỗi lượt gọi, "
            + "nên một lần chạy đầy đủ khoảng $0.05. Tới khoảng 30 phút. Lần chạy lỗi không bao giờ tự chạy lại.",
          when: "Bảng Cụm có ít nhất một 'lần chạy tới sẽ soạn' mà bản soạn cuối không bị lint từ chối trên cùng các tín "
            + "hiệu (xem Ứng viên với bộ lọc trạng thái là bị từ chối).",
          avoid: "Không cụm nào hiện 'lần chạy tới sẽ soạn', hoặc mỗi cụm đều đang chờ một bản soạn bị lint từ chối mà "
            + "chưa có tín hiệu mới (lần chạy sẽ không soạn gì mà vẫn dùng một trong 3 lần chạy của tháng), hoặc các cụm "
            + "giống hệt đã quay lại mà lần chạy trước không lưu gì (model không tìm được quy tắc: lần chạy tới gần như "
            + "chắc sẽ trả tiền cho cùng câu trả lời), hoặc đang có wave chạy." },
        { kind: "callout", tone: "warning", title: "3 lần khai thác mỗi tháng",
          text: "Tháng tính theo UTC. Mọi lần chạy đã bắt đầu đều được tính, kể cả lần không soạn được gì. Lần thứ 4 bị "
            + "từ chối. Khi tháng đã vượt trần chi phí, lần chạy vẫn bắt đầu, dừng trước lượt gọi tính phí đầu tiên, và "
            + "vẫn tính là một trong 3 lần." },
        { kind: "callout", tone: "warning", title: "Wave và deploy",
          text: "Không có gì trong code chặn Khai thác quy tắc, R0 hay Baseline R0 khi đang có wave. Không chạy chúng lúc "
            + "đó là quy ước của chính chúng ta, vì chúng dùng CPU của worker. Ngoài ra, mọi job đang chờ hoặc đang chạy "
            + "đều giữ lại một lần deploy backend, nên hãy bắt đầu job dài khi không có deploy nào được lên kế hoạch." },
        { kind: "callout", tone: "note",
          text: "Baseline R0 và Chạy R0 theo từng quy tắc dùng cùng một loại job, nên dòng job cuối có thể hiện cái nào "
            + "cũng được." },
      ],
    },
    {
      id: "labels",
      heading: "Đọc trạng thái và nhãn",
      blocks: [
        { kind: "subheading", text: "Dữ kiện (trạng thái dữ kiện của một tín hiệu)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "chờ", text: "Chưa được kiểm. Bộ lọc dữ kiện hằng ngày sẽ kiểm nó." },
          { term: "xám", text: "Lần sửa thêm một cái tên mà nguồn không có. Nó chờ bạn bấm Đạt hoặc Trượt." },
          { term: "đạt", text: "Dùng được để học. Các lần cắt, sửa văn phong và tín hiệu chỉ có lý do đều đạt." },
          { term: "trượt", text: "Lần sửa thêm một con số, thời gian, giá hoặc từ hứa hẹn ('included', 'free', "
            + "'pick-up'…) mà nguồn không có, hoặc mã lý do của nó là fact_not_in_source. Không bao giờ được học." },
        ] },
        { kind: "subheading", text: "Trạng thái (của một cụm)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "đã có quy tắc", text: "Đã có một quy tắc đang mở (ứng viên, đã duyệt hoặc đang hoạt động) cho nó." },
          { term: "lần chạy tới sẽ soạn", text: "Nó có ít nhất 3 tín hiệu và chưa có quy tắc đang mở. Thường lần Khai "
            + "thác quy tắc tới sẽ gọi một lượt tính phí cho nó. Hai ngoại lệ mà nhãn không hiện: nếu lint đã từ chối "
            + "bản soạn cuối và tín hiệu chưa đổi kể từ đó, lần chạy bỏ qua nó; và nếu lượt gọi tính phí cuối không lưu "
            + "gì (NONE, câu trả lời không dùng được, hoặc dòng bị cơ sở dữ liệu từ chối), nhãn vẫn giữ và lần chạy tới "
            + "lại trả tiền." },
          { term: "cần 3 tín hiệu", text: "Nó đã vượt ngưỡng nhưng chỉ có 2 tín hiệu. Nó chờ, không tốn gì. Bộ soạn "
            + "thấy 1 tín hiệu, còn 2 được giữ riêng để thử bản soạn." },
        ] },
        { kind: "paragraph", text: "Cụm có bản soạn bị lint từ chối trong một lần khai thác sẽ bị bỏ qua cho tới khi "
          + "tập tín hiệu của nó thay đổi: một tín hiệu mới gia nhập, hoặc một tín hiệu cũ rời khỏi cửa sổ 90 ngày. Cụm "
          + "được soạn lại ở lần chạy tới, với lượt gọi tính phí mới, khi admin đã từ chối hoặc quay lui quy tắc của nó, "
          + "khi quy tắc của nó trượt lint sau một lần admin sửa, hoặc khi lượt gọi cuối không lưu gì." },
        { kind: "subheading", text: "Ngưỡng (khi nào một cụm vượt ngưỡng)" },
        { kind: "paragraph", text: "Chỉ tính tín hiệu đạt của 90 ngày gần nhất. Tín hiệu của người là các lần sửa và cả "
          + "tín hiệu chỉ có lý do: từ chối, bỏ qua hoặc tạo lại kèm chip lý do. Mọi cụm, dù cơ sở là gì, cần ít nhất 2 "
          + "tín hiệu trên ít nhất 2 bài. Ngoài ra:" },
        { kind: "list", items: [
          "Quy tắc tenant: ít nhất 2 tín hiệu của người (admin hoặc tenant) trên ít nhất 2 bài của tenant đó.",
          "Quy tắc nền tảng từ tín hiệu tenant: cùng vấn đề ở ít nhất 3 tenant khác nhau.",
          "Quy tắc nền tảng từ tín hiệu của người phía AA (A1, và các bài T2, T9 của chính tenant chủ AA): ít nhất 2 "
            + "người duyệt khác nhau.",
          "Quy tắc nền tảng từ lỗi gate A1: trên ít nhất 2 bài khác nhau, hoặc trên 5% số bài A1 của 30 ngày gần nhất "
            + "(làm tròn lên), lấy số lớn hơn.",
        ] },
        { kind: "callout", tone: "warning", title: "Code và ADR khác nhau ở đây",
          text: "ADR 0010 chỉ nêu A1 cho ngưỡng người duyệt. Code tính mọi tín hiệu của người trên các bài của chính AA, "
            + "bất kể trình viết, nên các lần sửa T2 và T9 của chính AA cũng có thể dựng một quy tắc nền tảng." },
        { kind: "subheading", text: "Trạng thái (của một quy tắc)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "ứng viên", text: "Đã soạn, lint đạt, đang chờ người. Hết hạn sau 60 ngày." },
          { term: "đã duyệt", text: "Đã được duyệt cho một bản phát hành. Chưa có nút nào đặt trạng thái này." },
          { term: "đang hoạt động", text: "Trình viết đang dùng. Không bao giờ hết hạn." },
          { term: "bị từ chối", text: "Bị lint hoặc admin từ chối, hoặc đã quay lui. Ngăn chi tiết cho biết lý do." },
          { term: "hết hạn", text: "Một ứng viên hoặc quy tắc đã duyệt bị để quá 60 ngày. Được kiểm khi một lần khai "
            + "thác bắt đầu." },
        ] },
        { kind: "subheading", text: "Trạng thái (của một bản phát hành)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "nháp", text: "Đã tạo; chưa gắn đủ các lần chạy bằng chứng." },
          { term: "đã đánh giá", text: "Đã gắn đủ mọi lần chạy bằng chứng cần có. Chờ một admin bấm Duyệt." },
          { term: "đã duyệt", text: "Đã được một admin đăng nhập duyệt. Bấm Kích hoạt để chạy thật." },
          { term: "đang hoạt động", text: "Bản phát hành mà trình viết đọc (khi bộ nhớ của nó bật). Mỗi trình viết và "
            + "chủ sở hữu một bản." },
          { term: "đã thay", text: "Bị một bản mới hơn thay thế, hoặc đã quay lui. Bản đã quay lui không bao giờ trở lại." },
          { term: "bị từ chối", text: "Dừng lại trước khi chạy thật, kèm lý do." },
          { term: "cần kiểm lại", text: "Golden set, một tuyến model hoặc các gate của nó đã đổi kể từ bằng chứng. Nó "
            + "vẫn phục vụ cho tới khi được kiểm lại." },
        ] },
        { kind: "subheading", text: "Loại (của một quy tắc)" },
        { kind: "terms", items: [
          { term: "lexicon", text: "Cấm một cụm từ, hoặc thay nó bằng cách nói trung tính. Loại duy nhất có thể chạy "
            + "thật hôm nay (T9, qua L0)." },
          { term: "writer_instruction", text: "Một chỉ dẫn ngắn cho trình viết. Cần một bản phát hành." },
          { term: "repair_hint", text: "Một gợi ý cho bước sửa. Cần một bản phát hành." },
          { term: "preference_summary", text: "Khẩu vị của một tenant trong tối đa 5 câu. Đã lên kế hoạch; bộ khai thác "
            + "chưa soạn loại này." },
        ] },
        { kind: "subheading", text: "Nhãn lint (xanh = đạt, đỏ = trượt; rê chuột để xem chi tiết)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "Kích thước", text: "Nằm trong giới hạn từ, ký tự và cụm từ." },
          { term: "Chỉ giới hạn", text: "Quy tắc chỉ kìm trình viết lại. Nó trượt nếu nói thêm, nhắc tới, bao gồm, hứa "
            + "hẹn… mà không có 'không'." },
          { term: "Từ thay không thêm gì", text: "Từ thay thế không thêm con số, thời gian, giá, tên hay từ hứa hẹn "
            + "nào. 'lunch' → 'lunch included' là trượt." },
          { term: "Không chép văn bản tenant", text: "Quy tắc nền tảng không chép chuỗi 5 từ liền nào của văn bản tenant "
            + "và không nêu thương hiệu hay từ cấm của tenant nào. Quy tắc tenant không nêu thương hiệu hay từ cấm của "
            + "tenant khác. Nếu không tải được tên hay văn bản cần thiết, nó trượt, cho an toàn." },
          { term: "Giải thích được các sửa giữ lại", text: "Các cụm từ của quy tắc giải thích được ít nhất 2 lần sửa đã "
            + "giữ riêng." },
        ] },
        { kind: "paragraph", text: "Quy tắc chỉ là ứng viên khi mọi nhãn đều xanh." },
        { kind: "subheading", text: "Nhãn trong ngăn chi tiết" },
        { kind: "list", items: [
          "Trạng thái, kích thước tính bằng token, ngày hết hạn, và model đã soạn nó (id của model, không phải một "
            + "người).",
          "Các quy tắc đã duyệt và đang hoạt động của một trình viết và một chủ sở hữu được giữ tối đa 400 token cho "
            + "nền tảng và 250 cho mỗi tenant. Nút Kích hoạt không kiểm điều này: khi vượt trần, cơ sở dữ liệu từ chối "
            + "cú bấm và thông báo lỗi ghi token_cap. Mọi lần từ chối khác của một lượt kiểm cơ sở dữ liệu lúc đó cũng "
            + "được báo là token_cap.",
        ] },
        { kind: "subheading", text: "Bảng R0" },
        { kind: "terms", uiLabels: true, items: [
          { term: "L0 đạt / L0 trượt", text: "Những gì lần phát lại đã chứng minh: cụm từ hợp lệ, từ thay không thêm gì, "
            + "cụm từ có trong văn bản đã lưu, biến mất sau khi thay, và không có điểm chặn cứng mới. Dòng đỏ bên dưới "
            + "nói vì sao không. Riêng 'L0 đạt' chưa đủ để bật Kích hoạt (L0): lint cũng phải đạt và từ thay không được "
            + "dùng từ nào mà trình viết bị dặn không bao giờ dùng." },
          { term: "còn lặp lại (đủ điều kiện R2) / dưới k", text: "Vấn đề có còn lặp lại đủ nhiều trong các tín hiệu gần "
            + "đây không (k là ngưỡng khai thác)." },
          { term: "Cụm từ trong văn bản đã lưu", text: "Cụm từ xuất hiện bao nhiêu lần trong văn bản đã lưu hiện tại, và "
            + "sau quy tắc." },
          { term: "điểm chặn cứng mới", text: "Các lỗi gate mà quy tắc sẽ gây ra mới. Nên là không có." },
          { term: "Văn bản sẽ bị gắn cờ mới", text: "Bao nhiêu văn bản đã lưu (văn bản cuối, lần thử bị bỏ, văn bản "
            + "trước khi sửa) mà quy tắc sẽ gắn cờ mới, trên số văn bản phát lại từ các bài đã tải. Một bài cho tới 6 văn "
            + "bản, nên con số này có thể lớn hơn số bài. Luôn là 0 với quy tắc A1 và T2: không phát lại bài nào cho "
            + "chúng." },
          { term: "Lỗi cũ lẽ ra đã tránh được", text: "Các lỗi gate trước đây mà quy tắc lẽ ra đã tránh được." },
          { term: "Tín hiệu khớp", text: "Các tín hiệu mà quy tắc giải thích được." },
          { term: "Baseline", text: "Lần phát lại khớp với kết quả gate đã lưu tới mức nào, khi tắt quy tắc." },
          { term: "đã cũ: quy tắc đã đổi sau lần chạy này", text: "Quy tắc đã được sửa sau R0. Chạy R0 lại." },
        ] },
        { kind: "subheading", text: "'Chưa kích hoạt được: …'" },
        { kind: "paragraph", text: "Thông báo nêu điều đầu tiên còn thiếu. Tất cả các điều sau phải thỏa:" },
        { kind: "list", items: [
          "một quy tắc từ vựng, cho trình viết T9 (t9_blog hoặc t9_social);",
          "trạng thái ứng viên hoặc đã duyệt;",
          "lint đạt;",
          "một cụm từ hợp lệ: chính văn bản Quy tắc (không phải các cụm từ khớp), có từ thật, tối đa 6 từ và ít nhất 3 "
            + "ký tự, không chỉ toàn từ nhỏ như 'the' hay 'a', và không có token bị che như [NAME];",
          "một từ thay không thêm gì;",
          "R0 đã chạy trên văn bản hiện tại của quy tắc, và L0 đạt;",
          "từ thay không dùng từ nào mà trình viết bị dặn không bao giờ dùng (mã replacement_forbidden).",
        ] },
        { kind: "paragraph", text: "Vì sao L0 hay trượt nhất:" },
        { kind: "list", items: [
          "'the phrase appears in no stored text of this writer and owner: nothing to prove'. Hay gặp với cụm từ mới: "
            + "R0 chỉ chứng minh được quy tắc trên văn bản có cụm từ đó. Chờ tới khi bài đã lưu có nó, rồi chạy R0 lại.",
          "'the phrase is still there … after the replacement': từ thay vẫn giữ cụm từ (ví dụ nó chứa cụm từ đó). Hãy "
            + "đổi từ thay.",
          "'new hard stops: …': văn bản sau khi thay sẽ trượt một gate chặn mà trước đó nó đạt. Hãy đổi từ thay, hoặc "
            + "từ chối quy tắc.",
        ] },
        { kind: "paragraph", text: "Chỗ trống dưới trần token không nằm trong lượt kiểm này. Nút vẫn bật, và khi vượt "
          + "trần thì cú bấm lỗi kèm thông báo (token_cap)." },
      ],
    },
    {
      id: "routine",
      heading: "Quy trình hằng tuần",
      blocks: [
        { kind: "list", ordered: true, items: [
          "Bấm Nạp bù tín hiệu. Nó lấy các lần sửa T2 của tenant, lần thử T9 thất bại và bài học kiểm thương hiệu A1 kể "
            + "từ lần chạy trước (chúng không bao giờ được ghi nhận trực tiếp), rồi xếp bộ lọc dữ kiện. Chờ tới khi cả "
            + "hai job xong ở Nền tảng › Jobs, rồi bấm Làm mới.",
          "Mở tab Tín hiệu và nhìn ô Tín hiệu: bao nhiêu đang chờ và xám.",
          "Vẫn còn tín hiệu chờ sau khi job bộ lọc dữ kiện xong? Bấm Chạy bộ lọc dữ kiện.",
          "Đặt bộ lọc là xám. Với mỗi dòng, quyết định xem cái tên được thêm có đúng và không phải dữ kiện bịa không, "
            + "rồi bấm Đạt hoặc Trượt.",
          "Xem Cụm vượt ngưỡng. Có 'lần chạy tới sẽ soạn' nào không? Trong Ứng viên (trạng thái bị từ chối), kiểm rằng "
            + "bản soạn cuối của nó không bị lint từ chối trên cùng các tín hiệu: cụm như vậy sẽ bị bỏ qua. Nếu cùng cụm "
            + "đó đã hiện 'lần chạy tới sẽ soạn' sau lần chạy trước, lượt gọi đó không lưu gì: thêm một lần chạy nữa "
            + "nhiều khả năng sẽ trả tiền cho cùng kết quả. Kiểm rằng không có wave nào đang chạy và tháng này còn lượt "
            + "chạy (tối đa 3). Rồi bấm Khai thác quy tắc và xác nhận.",
          "Khi job khai thác đã xong (Nền tảng › Jobs), mở Ứng viên. Với mỗi quy tắc, đọc nó và Nguồn gốc của nó. Sửa "
            + "nếu cách diễn đạt chưa ổn. Từ chối, kèm lý do, nếu nó sai.",
          "Với ứng viên từ vựng T9 mà bạn đồng ý: Chạy R0 (ngoài lúc có wave), mở lại ngăn chi tiết, và xem bảng R0. "
            + "Nếu Phạm vi là Nền tảng, hãy xin chủ chương trình đồng ý trước: mọi quy tắc nền tảng đều cần, và trang "
            + "không bắt buộc điều này. Nếu 'Chưa kích hoạt được' đã biến mất và quy tắc sẽ không bật sai chỗ, bấm Kích "
            + "hoạt (L0).",
          "Đặt bộ lọc trạng thái là đang hoạt động. Kiểm rằng các quy tắc đang chạy vẫn hợp lý. Quay lui những quy tắc "
            + "gây hại.",
          "Liếc qua ô chi phí.",
        ] },
        { kind: "subheading", text: "Thỉnh thoảng" },
        { kind: "list", items: [
          "Sau một thay đổi ở các gate: Baseline R0.",
          "Để xem trước những gì lần nạp bù sẽ chép (theo nguồn và tenant): Nạp bù (chạy thử).",
          "Ứng viên hết hạn sau 60 ngày, nên hãy duyệt chúng trước đó.",
          "Sau khi đổi tuyến model hoặc golden set: Quét cần kiểm lại ở tab Bản phát hành, rồi tìm các nhãn 'cần kiểm "
            + "lại'.",
          "Cuối tháng, nếu không còn bản phát hành thường kỳ nào nên chạy thật cho tháng đó: Đóng tháng cho từng trình "
            + "viết.",
        ] },
      ],
    },
    {
      id: "not-in-ui",
      heading: "Chưa có trên trang này",
      blocks: [
        { kind: "paragraph", text: "Một số bước chưa có nút. Theo yêu cầu của chủ chương trình, một agent làm chúng qua "
          + "API hoặc công cụ dòng lệnh." },
        { kind: "subheading", text: "Hôm nay do agent làm" },
        { kind: "list", items: [
          "Phát lại và duyệt bản phát hành 0, bài diễn tập quay lui và nhập golden set (công cụ dòng lệnh).",
          "Lần chạy đánh giá R2 và lần chạy shadow có chọn lọc (công cụ dòng lệnh xếp một job nền tính phí, có trần chi "
            + "phí cứng). Lần chạy của chúng sau đó được gắn làm bằng chứng của bản phát hành ở tab Bản phát hành.",
        ] },
        { kind: "subheading", text: "Chưa được xây ở đâu cả" },
        { kind: "list", items: [
          "Duyệt một ứng viên (ứng viên → đã duyệt). Vì vậy các quy tắc ngoài quy tắc từ vựng T9 chưa đi tiếp được.",
          "Trình viết đọc quy tắc từ một bản phát hành. Hôm nay chỉ bản phát hành rỗng mới hoạt động được.",
          "Tên tenant, số liệu của lần chạy thử hoặc baseline (chỉ ở Nền tảng › Jobs), và hơn 200 tín hiệu trong danh "
            + "sách.",
        ] },
        { kind: "callout", tone: "warning", title: "Công tắc bộ nhớ không dừng quy tắc L0",
          text: "T9 đọc trực tiếp các quy tắc từ vựng đang hoạt động. Muốn dừng một quy tắc, hãy quay lui nó trong ngăn "
            + "chi tiết." },
      ],
    },
  ],
  glossary: [
    { term: "Tín hiệu", text: "Một bằng chứng đã lưu về đầu ra của trình viết: một lần người sửa, một lần từ chối "
      + "hoặc tạo lại kèm lý do, hoặc một lỗi gate." },
    { term: "Bộ lọc dữ kiện", text: "Lượt kiểm miễn phí hằng ngày đánh dấu mỗi tín hiệu đạt, xám hoặc trượt." },
    { term: "Cụm", text: "Một nhóm tín hiệu đạt cùng chỉ vào một vấn đề." },
    { term: "Ngưỡng", text: "Một vấn đề phải lặp lại bao nhiêu lần trước khi một quy tắc được soạn (xem 'Đọc trạng "
      + "thái và nhãn')." },
    { term: "Quy tắc", text: "Một chỉ dẫn ngắn (tối đa 25 từ) cho một trình viết." },
    { term: "Ứng viên", text: "Một quy tắc đã soạn, đã qua lint và đang chờ người." },
    { term: "Lint", text: "Lượt kiểm tự động trên mọi quy tắc được soạn hoặc sửa (năm nhãn)." },
    { term: "R0", text: "Lần phát lại miễn phí một quy tắc trên văn bản (T9) và tín hiệu đã lưu. Không gọi model." },
    { term: "Baseline R0", text: "R0 không có quy tắc: kiểm rằng lần phát lại khớp với kết quả gate đã lưu." },
    { term: "L0", text: "Con đường nhanh: quy tắc từ vựng T9 đã được R0 chứng minh chạy thật bằng một cú bấm." },
    { term: "R2 / shadow", text: "Các lần chạy đánh giá mà bản phát hành có quy tắc khác cần. R2 chạy golden set "
      + "(tối đa $10 một lần); shadow viết các đầu vào thật gần đây mà không ai thấy (20, hoặc 50 cho T2 và Facebook; "
      + "bản phát hành mạng xã hội cho mọi kênh: 50 Facebook + 20 kênh khác). Chỉ lần chạy của chính bản phát hành đó "
      + "mới được tính là bằng chứng của nó." },
    { term: "Bản phát hành", text: "Một gói quy tắc có phiên bản cho một trình viết và một chủ sở hữu. Nó cần một lần "
      + "duyệt trước khi chạy thật." },
    { term: "Golden set", text: "Một tập đầu vào thử cố định (tour, bài blog và mạng xã hội, cộng các bẫy) dùng để "
      + "đánh giá một bản phát hành." },
    { term: "Công tắc bộ nhớ", text: "Theo từng trình viết: bật nghĩa là trình viết đọc bản phát hành đang hoạt động "
      + "của nó. Tắt nghĩa là nó viết như không có bộ nhớ. Không ảnh hưởng quy tắc L0." },
    { term: "Phạm vi", text: "Một tín hiệu hay quy tắc thuộc về ai: Nền tảng (AA, mọi tenant) hoặc một tenant." },
    { term: "Nguồn gốc", text: "Các tín hiệu mà một quy tắc được soạn từ đó." },
    { term: "Wave", text: "Một lần chạy lại có kế hoạch của pipeline nội dung. Theo quy ước của chúng ta, chỉ bắt đầu "
      + "Khai thác quy tắc, R0 và Baseline R0 ngoài lúc có wave; không có gì trong code bắt chúng phải chờ." },
    { term: "Chi phí vòng học", text: "Tiền chi cho khai thác và các lần chạy đánh giá, trần $25 mỗi tháng. Riêng khai "
      + "thác còn có trần $1 mỗi lần chạy và $2 mỗi ngày." },
  ],
};
