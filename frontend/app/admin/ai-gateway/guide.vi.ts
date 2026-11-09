// app/admin/ai-gateway/guide.vi.ts — AA-804: the Vietnamese twin of guide.ts (same section ids, blocks and controls;
// lib/guides.test.mjs checks the outline). The quoted labels are the page's Vietnamese words (messages.ts).
// Data only (the type import is erased), so node --test loads it directly.

import type { GuideContent } from "../../_kit/guideContent";

export const AI_GATEWAY_GUIDE_VI: GuideContent = {
  title: "Cách dùng trang Cổng AI",
  intro: "Một trang cho mọi nguồn AI và dữ liệu trả phí. Đọc trang để trả lời bốn câu: từng nguồn còn trả lời không, "
    + "đã tốn bao nhiêu, mỗi bước dùng model nào, có gì sắp chạm giới hạn không.",
  sections: [
    {
      id: "purpose",
      heading: "Trang này để làm gì",
      blocks: [
        { kind: "paragraph", text: "Nền tảng trả tiền cho vài dịch vụ bên ngoài: các model ngôn ngữ (qua AWS Bedrock "
          + "hoặc API của hãng), Jev cho các quyết định có / không, DataForSEO cho dữ liệu tìm kiếm và Geoapify cho "
          + "toạ độ địa điểm. Lần gọi AI riêng của CRM đối tác và bước kiểm dữ liệu cá nhân của nó (Bedrock "
          + "Guardrails) cũng có ở đây, cùng bản đồ của TripPlanner (Mapbox) chạy trên gói miễn phí. Trang này đặt "
          + "tất cả cạnh nhau." },
        { kind: "paragraph", text: "Mọi con số lấy từ sổ ghi của mình và các lần kiểm số dư đã lưu; mở trang không gọi "
          + "nhà cung cấp nào và không tốn tiền. Trang có tám tab: Nhà cung cấp (sức khoẻ, chi tiêu, số dư, kèm cài "
          + "đặt kết nối và sổ nạp Jev), Điều hướng (mỗi bước dùng model nào), Điều khiển route (thứ tự model và "
          + "nhà cung cấp, preset và phiên bản), Model và thử nghiệm (danh mục model và "
          + "so sánh chạy bóng), Ngân sách (mọi mức trần), Lần gọi (từng lần gọi đã ghi), Lộ trình (mọi lần thử của một yêu cầu, tỉ lệ mỗi bậc dự phòng đã phục vụ, cảnh báo route) và Hướng dẫn này. Cài đặt › "
          + "LLM Models, Cài đặt › Số dư Jev và Spend › Ngân sách đã dời về đây; link cũ mở đúng tab mới. "
          + "Tổng chi theo tenant, model và thị trường vẫn ở Spend, tab Chi phí AI và dữ liệu." },
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
          { term: "Hồ sơ ứng dụng", text: "Công tắc AI của từng ứng dụng (CIS, TripPlanner, CRM đối tác, AA-Booking). "
            + "Tắt AI: mọi lời gọi AI của ứng dụng đó bị từ chối trước khi gửi đi, có một dòng bị từ chối 0 USD ở tab "
            + "Calls, và job của nó chờ tới khi bật lại AI. Thẻ này cũng giữ việc tạm dừng tác vụ hàng loạt, ngân sách "
            + "ngày mặc định và điều ứng dụng hiển thị khi AI không dùng được. CIS, TripPlanner (trong vòng 20 giây) và "
            + "CRM đối tác áp dụng công tắc; TripPlanner và CRM còn dừng ở ngân sách ngày mặc định, và cảnh báo 80 % và "
            + "100 % của TripPlanner tới chuông và e-mail vận hành. Thay đổi cho CRM đối tác được gửi tới CRM trước "
            + "(cần nhóm quản trị nền tảng CRM). Tắt AI cần lý do và được ghi nhật ký kiểm toán." },
          { term: "Kết nối LLM", text: "Mỗi dòng là một đường nền tảng gọi tới model, ví dụ Bedrock trong một tài "
            + "khoản AWS (hiện bằng số tài khoản) hoặc API của OpenAI. Lần gọi AI của CRM đối tác có một dòng riêng, "
            + "đọc từ CRM bằng đăng nhập nhân viên của bạn; bật tắt chúng ở CRM, không phải ở đây." },
          { term: "Quyết định (Jev)", text: "Dòng của Jev. Số dư là ước tính: tổng nạp đã ghi ở Số dư Jev trừ chi tiêu "
            + "đã ghi, vì Jev không có dịch vụ xem số dư." },
          { term: "Nhà cung cấp dữ liệu", text: "DataForSEO (số dư từ lần kiểm hằng ngày), Geoapify (credit đã dùng "
            + "hôm nay so với giới hạn ngày; giới hạn đặt lại mỗi ngày UTC) và Mapbox cho bản đồ TripPlanner (gói "
            + "miễn phí, không ghi sổ: dòng của nó dẫn tới Mapbox console)." },
          { term: "Lớp bảo vệ", text: "Bedrock Guardrails, tìm dữ liệu cá nhân trong văn bản của CRM đối tác trước khi "
            + "model đọc. Số lần gọi và chi phí lấy từ sổ ghi của CRM." },
          { term: "Lỗi · độ trễ", text: "Trong 24 giờ và 7 ngày qua: bao nhiêu lần gọi tới được nhà cung cấp, phần trăm "
            + "bị lỗi (lỗi, câu trả lời bị cắt, hoặc tính tiền mà không dùng được), và thời gian trả lời: p50 là lần gọi "
            + "thường gặp, p95 là lần chậm nhất trong hai mươi lần. Lần gọi bị cổng của mình hoặc công tắc từ chối "
            + "trước khi gửi được đếm riêng, không tính là lỗi. Thời gian trả lời của Geoapify là thời gian trung bình "
            + "mỗi request của từng lô (lô trước 02/11/2026 không có). Dòng của CRM "
            + "đối tác lấy từ tổng theo ngày của CRM: hôm nay (UTC) và 7 ngày qua, lần bị từ chối tính là lỗi, không có "
            + "thời gian trả lời." },
          { term: "Sức khoẻ", text: "Với kết nối: lần trả lời gần nhất và loại lỗi gần nhất. Với Jev: số lần gọi và "
            + "lỗi trong 24 giờ qua, thời gian trả lời trung bình." },
          { term: "Ngân sách", text: "Ngân sách tháng của kết nối và trần ngày của nhà cung cấp, kèm phần đã dùng. "
            + "Dừng ở trần thì chặn lần gọi tiếp; chỉ cảnh báo thì chỉ báo." },
          { term: "Số dư / credit", text: "Phần còn lại ở nhà cung cấp, hoặc credit hôm nay với Geoapify." },
          { term: "Đường dự phòng · stage Claude", text: "API của Anthropic: đường độc lập tới Claude khi tài khoản AWS "
            + "đang chạy nó bị chặn. Chỉ được dùng khi đang bật, có tiền và có trong tuyến của stage." },
          { term: "Đường dự phòng · stage GPT", text: "API của OpenAI: tương tự cho các stage chấm bằng GPT." },
          { term: "Miền lỗi:", text: "Ai có thể chặn dòng này: loại dịch vụ, tài khoản và phạm vi dữ liệu. Hai dòng "
            + "cùng miền lỗi sẽ cùng hỏng; dự phòng chỉ có ích khi ở miền khác." },
          { term: "Truy cập:", text: "Lần gọi hoặc lần thăm dò gần nhất nói gì: đang trả lời, bị từ chối truy cập, "
            + "hết credit, chưa cài khoá, hoặc chưa kiểm." },
          { term: "Thăm dò gần nhất", text: "Lần thăm dò sức khoẻ hằng ngày gọi dòng này gần nhất (chưa thăm dò lần nào "
            + "cho tới khi bật thăm dò)." },
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
          { term: "Chưa có lượt gọi gần đây", text: "Kết nối LLM, hoặc dòng của CRM đối tác, không có lượt gọi được "
            + "trả lời trong 24 giờ qua. "
            + "Chưa thấy gì sai; tab Điều hướng dùng cùng chữ này." },
          { term: "Chưa rõ", text: "Không đọc được nguồn, hoặc chưa biết số dư hay lần nạp nào (Jev, DataForSEO). "
            + "Dòng đó ghi rõ lý do." },
          { term: "Không ghi sổ", text: "Nguồn được liệt kê nhưng không ghi sổ (Mapbox). Không tính là có vấn đề." },
        ] },
      ],
    },
    {
      id: "views",
      heading: "Bên trong tab Nhà cung cấp",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "Tổng quan", text: "Danh sách chỉ đọc ở trên: mỗi nguồn trả phí một dòng." },
          { term: "Kết nối", text: "Bật hoặc tắt một kết nối LLM, đổi timeout, số lần thử lại, giới hạn và ngân sách "
            + "tháng, và gửi một lần gọi thử nhỏ. Tên secret và base URL chỉ hiện, không sửa ở đây: chúng chỉ đổi qua "
            + "migration có duyệt." },
          { term: "Số dư Jev", text: "Ghi tay từng lần nạp Jev (Jev không có dịch vụ xem số dư), xem số dư ước tính "
            + "và gửi cảnh báo thử." },
          { term: "Số dư API", text: "Tương tự cho API OpenAI và Anthropic: ghi số dư đọc trên console của nhà cung "
            + "cấp và từng lần nạp, xem số dư ước tính (lần ghi số dư mới nhất cộng các lần nạp sau đó, trừ chi phí đã "
            + "ghi sổ từ lúc đó) và đặt ngưỡng cảnh báo. Dưới ngưỡng, admin nhận một thông báo chuông và một e-mail "
            + "mỗi ngày." },
        ] },
        { kind: "control", label: "Tắt", does: "Tắt Jev, DataForSEO hoặc Geoapify (hoặc bật lại bằng nút Bật), sau "
          + "một bước xác nhận. Khi đang tắt, không gửi gì tới nhà cung cấp: mỗi lần gọi bị từ chối được ghi với chi phí "
          + "0, Jev trả lời như khi đang sập, còn lượt chạy DataForSEO hay geocode dừng lại. Jev và DataForSEO nhận "
          + "thay đổi trong vòng một phút; job geocode đọc nó lúc bắt đầu.",
          cost: "Miễn phí. Tắt thì đỡ các lần gọi; bật lại thì các lần gọi tính giá như thường.",
          when: "Nhà cung cấp chạy sai, không được tiêu credit của nó, hoặc một bài thử cần tắt nó." },
        { kind: "control", label: "Đưa lại job bị từ chối", does: "Kiểm trước những job lỗi trong 14 ngày qua đã dừng vì "
          + "nhà cung cấp từ chối một tài khoản, hiện các con số, và chỉ sau khi bạn xác nhận mới đưa chúng về hàng đợi "
          + "với lượt thử mới, mỗi đợt 50 job (bấm Kiểm đợt tiếp cho phần còn lại). Job mà kết nối vẫn bị từ chối thì "
          + "vẫn để lỗi.",
          cost: "Bước kiểm miễn phí; job được đưa lại chạy và tốn như lần đầu.",
          when: "Một tài khoản từng bị chặn đã trả lời lại (dòng của nó Ổn)." },
      ],
    },
    {
      id: "routing",
      heading: "Điều hướng: mỗi bước dùng model nào",
      blocks: [
        { kind: "paragraph", text: "Mỗi bước của pipeline một dòng. Model chính trả lời trước; chuỗi dự phòng được thử "
          + "lần lượt khi nó lỗi; model chạy bóng gọi thêm một lượt trên một phần lượt gọi chỉ để so sánh (chi phí các "
          + "lượt đó tăng gấp đôi). Ô tình trạng LLM ở trên cho biết các model có trả lời gần đây không; các câu hỏi "
          + "Jev theo bước ở dưới cùng." },
        { kind: "terms", uiLabels: true, items: [
          { term: "Có dự phòng ở miền lỗi khác", text: "Nếu tài khoản của model đầu bị chặn, stage vẫn chạy được trên "
            + "một model ở miền lỗi khác." },
          { term: "Không có dự phòng ở miền lỗi khác", text: "Mọi model dùng được của tuyến nằm trong một miền lỗi, "
            + "hoặc không model nào chạy được lúc này: một tài khoản bị chặn là stage dừng. Chỉ báo cáo: không đổi gì." },
          { term: "Đang trả lời", text: "Các lượt gọi thật gần đây được trả lời. Khi một kết nối đang ngắt, nhãn cũng "
            + "ghi rõ, cùng chữ với tab Nhà cung cấp." },
        ] },
        { kind: "control", label: "Lưu", does: "Lưu model chính của một bước, cho mọi lượt gọi của bước đó.",
          cost: "Lưu không tốn tiền; các lượt gọi sau chạy trên model mới theo giá của nó.",
          when: "Một model bị ngừng, bị từ chối, hoặc một model rẻ hơn đã qua thử nghiệm." },
        { kind: "control", label: "Lưu tuyến", does: "Lưu chuỗi dự phòng và model chạy bóng của một bước.",
          cost: "Lưu không tốn tiền; chạy bóng làm chi phí các lượt được lấy mẫu tăng gấp đôi.",
          when: "Thiếu dự phòng, hoặc muốn so sánh một model mới trên lượt gọi thật." },
        { kind: "callout", tone: "note", title: "Tài khoản AWS theo bước",
          text: "Khi mọi bước đều chạy trong tài khoản LLM thì không có gì để chọn, nên ô chọn tài khoản bị ẩn; nó "
            + "chỉ hiện lại khi còn bước đi qua tài khoản khác." },
      ],
    },
    {
      id: "route-control",
      heading: "Điều khiển route: chuỗi, thứ tự nhà cung cấp, preset và phiên bản",
      blocks: [
        { kind: "paragraph", text: "Điều khiển route là nơi đổi mọi thiết lập định tuyến mà không cần deploy. Chuỗi của "
          + "một stage là danh sách model của nó: model đầu (model chính) phục vụ stage, các model sau lần lượt thay "
          + "khi nó lỗi, bị từ chối hoặc chạm trần chi. Một yêu cầu thử tối đa sáu lần tất cả." },
        { kind: "paragraph", text: "Nhiều model tới được bằng hơn một đường: Claude trên AWS (tài khoản LLM) hoặc qua "
          + "API Anthropic, GPT trên AWS hoặc qua API OpenAI. Thứ tự nhà cung cấp của một model cho biết đường nào "
          + "được thử trước. Kiểu thứ tự cho biết gateway đi theo chuỗi thế nào: cùng model trước (mọi nhà cung cấp "
          + "của một model rồi tới model kế; mặc định) hoặc AWS trước (mọi model AWS của chuỗi, rồi tới API của "
          + "hãng)." },
        { kind: "flow", steps: ["Đổi một chuỗi, một thứ tự nhà cung cấp hoặc kiểu thứ tự", "Xem trước (máy chủ kiểm, "
          + "miễn phí)", "Xem lại và lưu (một phiên bản mới)", "Gateway dùng nó trong khoảng 20 giây"] },
        { kind: "terms", uiLabels: true, items: [
          { term: "Phục vụ trước", text: "Model và nhà cung cấp sẽ được gọi đầu tiên cho stage lúc này, với thay đổi "
            + "bạn đang làm. Nó đọc trạng thái thật (đang tắt, bị từ chối hay vượt trần thì bị bỏ qua) và không gọi "
            + "model nào." },
          { term: "Không còn model sống", text: "Không model nào của chuỗi sẽ được gọi lúc này. Lần lưu để một stage "
            + "rơi vào cảnh này bị từ chối; stage vốn đã như vậy chỉ hiện cảnh báo." },
          { term: "Đang dùng", text: "Phiên bản gateway đang dùng." },
          { term: "Ngoài admin", text: "Thay đổi không làm qua trang này (ví dụ một migration cơ sở dữ liệu), được ghi "
            + "thành phiên bản riêng trước lần thay đổi kế tiếp từ admin." },
        ] },
        { kind: "subheading", text: "Quy tắc máy chủ kiểm mỗi lần lưu" },
        { kind: "list", items: [
          "Judge không bao giờ dùng model cùng họ với writer (nó sẽ chấm bài của chính mình).",
          "Hai bên của một cặp kiểm chéo (thẻ địa điểm, gộp địa điểm trùng) dùng model của hai hãng khác nhau.",
          "Một chuỗi thử tối đa sáu lần mỗi yêu cầu, tính mọi nhà cung cấp của mọi model.",
          "Mọi stage giữ ít nhất một model sẽ được gọi lúc này.",
          "Stage không được rời AWS thì không nhận model chỉ tới được qua API của hãng, và model như vậy nằm cuối "
            + "chuỗi (hoặc ngay sau chính model đó trên AWS).",
        ] },
        { kind: "paragraph", text: "Quy tắc đã bị vi phạm từ trước thay đổi của bạn chỉ hiện là cảnh báo, nên không "
          + "chặn một thay đổi không liên quan. Stage writer hoặc judge có chuỗi mới, hoặc bị thứ tự nhà cung cấp, "
          + "preset hay kiểu thứ tự chuyển sang model hoặc nhà cung cấp khác, cần eval run làm căn cứ, hoặc lý "
          + "do break glass (kèm cảnh báo cho admin), như ở tab Điều hướng." },
        { kind: "control", label: "Ưu tiên API Anthropic cho Claude (tài trợ)", does: "Đưa API Anthropic lên đầu cho "
          + "mọi model Claude và đặt kiểu thứ tự thành cùng model trước. Chuỗi không đổi.",
          cost: "Áp dụng miễn phí; lời gọi Claude sau đó tính tiền vào khoá API Anthropic khi kết nối đang bật.",
          when: "API Anthropic đang được tài trợ hoặc rẻ hơn AWS." },
        { kind: "control", label: "Ưu tiên AWS", does: "Đưa nhà cung cấp AWS lên đầu cho mọi model. Kiểu thứ tự và "
          + "chuỗi không đổi.", cost: "Áp dụng miễn phí.",
          when: "API của hãng lại chỉ nên là đường cuối cùng." },
        { kind: "control", label: "Xem lại và lưu", does: "Hiện mọi thay đổi, lỗi quy tắc và cảnh báo, rồi lưu chúng "
          + "thành một phiên bản mới kèm ghi chú của bạn.", cost: "Miễn phí; các lời gọi kế tiếp dùng route mới.",
          avoid: "Bản xem trước còn lỗi: sửa trước đã." },
        { kind: "control", label: "Hoàn tác", does: "Khôi phục chuỗi, thứ tự nhà cung cấp và kiểu thứ tự của một phiên "
          + "bản cũ thành một phiên bản mới (lịch sử giữ mọi bước).", cost: "Miễn phí.",
          when: "Một thay đổi làm mọi thứ tệ hơn." },
        { kind: "callout", tone: "note", title: "Một phiên bản chứa gì",
          text: "Kiểu thứ tự, chuỗi của mọi stage và thứ tự nhà cung cấp của mọi model. Model chạy bóng, công tắc "
            + "stage, phạm vi dữ liệu và trần chi có điều khiển riêng và không bị hoàn tác thay đổi. Mỗi lần gọi model "
            + "ghi lại phiên bản đang dùng lúc đó." },
      ],
    },
    {
      id: "models",
      heading: "Model và thử nghiệm",
      blocks: [
        { kind: "paragraph", text: "Danh mục model liệt kê mọi model nền tảng gọi được, kèm hãng, nơi chạy và giá. Giá "
          + "chỉ xem: chỉ đổi qua thay đổi cấu hình có duyệt. Một model bật hoặc tắt được; model chưa có giá thì "
          + "không bật được." },
        { kind: "paragraph", text: "Phần so sánh chạy bóng cho thấy, theo từng bước, model chạy bóng đồng ý với model "
          + "chính bao nhiêu lần, chênh lệch điểm, chi phí mỗi lượt và thời gian trả lời của model chạy bóng, kèm cỡ "
          + "mẫu." },
        { kind: "paragraph", text: "Thử nghiệm (chỉ xem) liệt kê các phiên bản prompt đã ghi (dấu vân tay ngắn, không "
          + "bao giờ có nội dung prompt) và các lượt đánh giá mới nhất: so sánh gì, trên bao nhiêu tour, kết luận và "
          + "chi phí. Lượt chạy do nhóm khởi động, không từ trang này." },
      ],
    },
    {
      id: "budgets",
      heading: "Ngân sách: mọi mức trần chi tiêu",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "Trần ngày theo nhà cung cấp", text: "Mức tối đa một nhà cung cấp được tiêu trong một ngày UTC trên "
            + "toàn nền tảng." },
          { term: "Trần theo job", text: "Trần của một loại job, mỗi lượt chạy và mỗi ngày. Số đã chi hôm nay đo đúng "
            + "như bộ chặn đo: cả ngày của nhà cung cấp với phần lớn job, riêng loại job này với vài job bị chặn theo "
            + "chi tiêu của chính nó (dòng dưới con số cho biết là loại nào)." },
          { term: "Ngân sách kết nối LLM", text: "Ngân sách tháng của từng kết nối; sửa ở Nhà cung cấp › Kết nối." },
          { term: "Ngân sách AI theo đại lý", text: "Ngân sách AI hằng tháng của từng đại lý có đặt, kèm chi tiêu tháng "
            + "này và trạng thái. Đặt hoặc sửa tại đại lý (Đại lý › đại lý đó › Chi phí). Chỉ nhân viên thấy. Hiện được "
            + "lưu và hiển thị; giữ job mới ở mức duyệt sẽ có khi cổng AI kiểm ngân sách." },
          { term: "Trần LLM chung", text: "Trần cho mọi lượt gọi model của một phạm vi, bất kể nhà cung cấp nào trả "
            + "lời: cả ứng dụng, một stage, một loại job (cả mỗi lượt job), các lượt eval hoặc các script ad-hoc; theo "
            + "ngày và theo tháng. Kiểm tra trước mỗi lượt gọi: trần có dừng sẽ từ chối lượt gọi trước khi gửi gì đi "
            + "(một dòng bị từ chối, chi phí 0, trong Lần gọi). Trần mới bắt đầu ở chế độ chỉ cảnh báo; trên Dev giữ "
            + "chỉ cảnh báo." },
          { term: "Độ phủ theo điểm gọi", text: "Mỗi dòng là một stage có gọi model, kèm các trần chung nó nằm dưới, "
            + "các loại job đã gọi nó và chi tiêu. Dòng chưa có trần chung vẫn bị chặn bởi ngân sách tháng của kết "
            + "nối." },
          { term: "Dừng ở trần", text: "Lượt chạy kết thúc khi lượt gọi kế tiếp sẽ vượt trần." },
          { term: "Chỉ cảnh báo", text: "Không bao giờ dừng; gửi cảnh báo cho quản trị ở % cảnh báo." },
        ] },
        { kind: "control", label: "Thêm trần", does: "Tạo một trần cho cả nhà cung cấp hoặc cho một loại job.",
          cost: "Miễn phí.", when: "Một nguồn trả phí hiện Không trong danh sách có trần chưa." },
        { kind: "control", label: "Thêm trần chung", does: "Tạo một trần cho mọi lượt gọi model của một phạm vi; Gỡ "
            + "trong khung sửa sẽ bỏ nó.",
          cost: "Miễn phí.", when: "Một điểm gọi chưa có trần chung và chi tiêu của nó cần có chuông báo." },
      ],
    },
    {
      id: "calls",
      heading: "Lần gọi: từng lần gọi đã ghi",
      blocks: [
        { kind: "paragraph", text: "Lần gọi mới nhất ở trên: mỗi lần thử gọi model (cả lần bị từ chối, chi phí 0, có "
          + "loại lý do dưới kết quả), mỗi phán quyết Jev, mỗi yêu cầu DataForSEO và geocode, và các lần gọi AI riêng "
          + "của CRM đối tác. Dòng CRM cần đăng nhập nhân viên; khi không đọc được, tab nói lý do và hiện tổng của "
          + "CRM, tính theo trọn ngày UTC (nên có thể xa hơn khoảng đã chọn)." },
        { kind: "terms", uiLabels: true, items: [
          { term: "Khoảng", text: "Danh sách lùi xa tới đâu; mặc định 24 giờ qua." },
          { term: "Sổ", text: "Dòng đến từ sổ nào: LLM, Jev, DataForSEO, Geocode hoặc CRM." },
          { term: "Kết quả", text: "đã trả lời, bị từ chối (nhà cung cấp hoặc chốt chặn của mình từ chối, không tính "
            + "tiền), lỗi, bị cắt (câu trả lời chạm giới hạn độ dài) hoặc tính tiền, không dùng được." },
          { term: "Độ trễ của các lần gọi đang liệt kê", text: "Mỗi lần gọi trên màn hình một cột, đỏ khi bị từ chối "
            + "hoặc lỗi, kèm trung vị, 5 % chậm nhất và lần chậm nhất." },
          { term: "Chỉ job này", text: "Giữ các dòng của job của dòng đó; Bỏ lọc để xoá bộ lọc." },
        ] },
        { kind: "control", label: "Tải lần gọi cũ hơn", does: "Thêm 100 dòng cũ hơn trong khoảng.",
          cost: "Miễn phí: chỉ đọc sổ ghi." },
      ],
    },
    {
      id: "trace",
      heading: "Xem các lần gọi của một job hay một tour (Truy vết)",
      blocks: [
        { kind: "flow", steps: ["Jobs", "Bấm một job", "Mở truy vết", "Đọc các lần gọi theo thời gian"] },
        { kind: "paragraph", text: "Truy vết liệt kê mọi lần gọi trả phí đã ghi của job: gọi model, quyết định Jev, "
          + "yêu cầu DataForSEO và yêu cầu geocode, kèm lúc nào, việc gì, kết quả, chi phí và thời gian trả lời, dạng "
          + "Dòng thời gian (mặc định) hoặc Bảng. Dòng trên cùng là tổng theo từng sổ ghi. Truy vết của một tour "
          + "(Master Content › một tour › Mở truy vết AI) gom mọi job của tour đó và mọi lần gọi gắn với tour; trang "
          + "Jev decisions cũng nối job của từng phán quyết theo cách này." },
        { kind: "control", label: "Mở truy vết", does: "Mở trang này kèm ngăn truy vết của job.",
          cost: "Miễn phí: chỉ đọc sổ ghi.", when: "Một job tốn hơn dự kiến, hoặc lỗi và muốn biết lần gọi nào sai." },
        { kind: "control", label: "Làm mới", does: "Đọc lại mọi nguồn.", cost: "Miễn phí." },
      ],
    },
    {
      id: "limits",
      heading: "Những gì trang chưa hiện được",
      blocks: [
        { kind: "callout", tone: "warning", title: "Lần gọi trước 31/10/2026",
          text: "Lần gọi ghi trước 31/10/2026 không có kết quả hay thời gian trả lời, và lần bị từ chối trước ngày đó "
            + "không được ghi. Từ đó mọi lần thử đều có dòng, lần bị từ chối với chi phí 0." },
        { kind: "list", items: [
          "Lần gọi model của TripPlanner hiện từ sổ ghi chung; trần theo stage của nó ở Spend › Chi phí AI "
            + "và dữ liệu và bảng Ứng dụng.",
          "Mapbox không ghi sổ: dòng của nó không có chi tiêu hay thời gian trả lời; xem mức dùng trên Mapbox console.",
          "Các dòng của CRM đối tác cần đăng nhập nhân viên của bạn. Chúng chỉ đọc tổng theo ngày của CRM (trọn ngày "
            + "UTC, không có thời gian trả lời); từng lần gọi của CRM xem ở tab Lần gọi.",
          "Các lần chạy thử model mới (thử nhanh, kiểm model chấm, so sánh) chưa có trên tab Model và thử nghiệm.",
          "Chi tiêu là số mình ghi theo giá của mình. Hoá đơn AWS có thể lệch chút; Spend hiện hoá đơn.",
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
    { term: "Dự phòng", text: "Model kế tiếp một bước thử khi model chính lỗi." },
    { term: "Chuỗi", text: "Danh sách model có thứ tự mà một stage thử: model chính, rồi các model dự phòng." },
    { term: "Thứ tự nhà cung cấp", text: "Nhà cung cấp nào của cùng một model được thử trước (AWS hay API của hãng)." },
    { term: "Phiên bản route", text: "Một trạng thái đã lưu của mọi chuỗi, thứ tự nhà cung cấp và kiểu thứ tự." },
    { term: "Chạy bóng", text: "Model thứ hai trả lời cùng lượt gọi chỉ để so sánh; câu trả lời của nó không được dùng." },
  ],
};
