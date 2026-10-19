# AA-507 — Điều tra hiện trạng UI T5→T11 (CHỈ ĐIỀU TRA, KHÔNG BUILD)

Ngày: 03/09/2026. Lệnh gốc: comment mới nhất trên AA-507 (02/09/2026 23:16 UTC).

## Ghi chú quan trọng về phương pháp — lỗi suýt xảy ra

Working tree ban đầu (branch `fix/aa-518-llm-audit-followup`) **thiếu 4 commit mới nhất trên
`origin/main`**, gồm đúng 3 sub-issue vừa merge trong ngày: `85fe6ef` (AA-512, #277), `d18f2f7`
(AA-513, #278), `028bbf3` (AA-514, #279). Nếu đọc thẳng file trên working tree, báo cáo T8 sẽ SAI
100% — sẽ kết luận nhầm "Channel vẫn chọn sau Angle, chưa đảo ngược" trong khi thực tế đã đảo
ngược xong trên `main`. Toàn bộ điều tra bên dưới đọc qua `git show origin/main:<path>` (hoặc xác
nhận file không đổi giữa 2 nhánh bằng `git diff HEAD origin/main --stat`), không dựa vào working
tree. Không sửa file nào, không checkout, không merge — chỉ đọc.

## Bảng tổng hợp T5 → T11

| Stage | UI tồn tại? | Đúng luồng MỚI hay còn luồng CŨ? | Đối chiếu repo Ms. Thư | Tenant tự vận hành được không? | Dở dang/sai chỗ nào |
|---|---|---|---|---|---|
| **T5 Atomize** | CÓ — nút "Atomize" trên `CatalogTab.tsx` (trang `/portal/t4-pool`, không phải trang T5 riêng), `POST /v1/tours/versions/{id}/atomize` | Backend đã có atom_id content-hash + cache theo ngày (AA-508, Done) — vô hình với UI, không cần đổi UI. Nút bấm thủ công, không phải chạy ngầm tự động | Repo Ms. Thư: `aa run atoms`, operator tự gõ lệnh (ADR 0015 "Folders are the interface" — CLI chạy stage, không phải auto-trigger). Nút bấm tenant-trigger là đúng tinh thần "ai đó chủ động chạy stage", không phải bản dịch sai | **CÓ** — tenant tự bấm Atomize trên tour đã rewrite xong | Copy sai lệch thực tế: `AtomsTab.tsx` (trang T6) có dòng EmptyState "*Atoms are generated automatically once one of your rewritten tours passes QA*" — SAI, atomize là nút bấm thủ công (AA-469 Việc 1), không tự động. Lỗi copy nhỏ, không phải lỗi luồng |
| **T6 Curation** | CÓ — `/portal/t6-atoms` (`AtomsTab.tsx`) | Đã group theo `segment_id` (code AA-509 đã LIVE trên `main`, dù Linear vẫn ghi "In Progress" — lệch trạng thái Linear vs code thật, không phải lệch chức năng) — hiển thị `canonical_place`/`canonical_action` làm header nhóm, số atom trong nhóm | Repo Ms. Thư không có UI (file/CLI) — không có tương đương trực tiếp để so | **CÓ** — star/xoá/filter/phân trang, giờ có thêm group Segment | 2 khoảng trống thật: (1) **KHÔNG hiển thị Atom Score / ranking** (stage mới AA-515, Done ở backend) — atom vẫn chỉ có badge HIGH/MED/LOW distinctiveness cũ, không có điểm ranking mới; (2) **KHÔNG hiển thị liên kết Route/Hub** (AA-510) — 1 Segment thuộc về Route nào phục vụ Blog thì tenant không thấy gì trên trang này. Ngoài ra: group Segment bị vỡ nếu 2 atom cùng Segment nằm ở 2 trang phân trang khác nhau (giới hạn tự ghi trong code, chưa phải bug nhưng nên biết) |
| **T7 Slate** | CÓ — `/portal/t7-planning`, `PlanningTab.tsx` render `<SlateTab/>` (đã xác nhận lại theo yêu cầu, không đào sâu lại) | Slate 8-tab Channel (5 nhịp tuần + 3 on-demand) LIVE, hiển thị `cleared_bar_reason`, nút "Chọn viết" tạo `angle_gate_request` với `subject_id`+`channel` sẵn rồi điều hướng sang T8 — đúng thiết kế AA-511 | Khớp ADR 0021 (propose subjects, not schedule) — 1 bar + arithmetic hiển thị, không gán ô lịch | **CÓ** | Vấn đề nhỏ về **kiến trúc trang**, không phải bug: `PlanningTab.tsx` vẫn hiển thị UI Quarter Plan CŨ (chọn năm/quý, Preview/Finalize, trip-reallocation) ở ĐẦU trang, Slate được nhúng THÊM vào bên dưới cùng 1 trang — 2 mô hình tư duy khác nhau (Quarter Plan = chọn trip nào chạy quý này; Slate = viết gì/tần suất nào) xếp chồng không phân cấp rõ. Tên trang/sidebar vẫn là "Content Planning" (nhãn cũ AA-448), không đổi thành "Slate" dù nội dung chính đã là Slate |
| **T8 Angle Gate** | CÓ — `/portal/t8-angle-gate`, `AngleGateTab.tsx` | **ĐÃ đảo ngược đúng theo AA-512** (PR #277, merged 02/09 15:58 UTC) — xác nhận bằng code thật trên `origin/main`: khi request có `subject_id` (đến từ Slate), luồng chỉ còn 3 bước Goal→Angle→Write, KHÔNG còn bước chọn Atom/Channel (đã cố định từ Subject) — `SubjectHeader` hiển thị Channel+Place+Score cố định. Angle card đã có badge đo được ("answers X/Y PAA", "N vi phạm avoid-list") thay vì chỉ text mô tả suông | Khớp ADR 0004 (measured, không phải LLM-opinion) | **CÓ**, đúng luồng mới | Luồng cũ 5-bước (Atom→...→Channel bước 4→Write) vẫn còn trong code, dùng khi vào thẳng `?atom_id=` — nhưng đường vào duy nhất của luồng cũ (`SlotPickerPanel.tsx`) đã là dead code không ai gọi (đã bị Slate thay thế, theo đúng quy ước "giữ code cũ, ngưng dùng, không xoá" của epic) — không phải gap thật, chỉ là code chết vô hại. Riêng: khối comment đầu file (dòng 1-100) vẫn mô tả luồng CŨ y như cũ, KHÔNG cập nhật để nói về nhánh subject-driven mới — nợ tài liệu (doc debt), không ảnh hưởng chức năng |
| **T9 Write** | Không có trang riêng — chạy inline trong `AngleGateTab.tsx` sau khi xác nhận Goal+Angle | Tenant hoàn toàn KHÔNG thấy gì trong lúc viết ngoài spinner "writing" — không progress bar %, không preview giữa chừng, không nút huỷ (grep "cancel/abort" = 0 kết quả). Polling 3s/lần, trần 180s | Repo Ms. Thư cũng KHÔNG có màn hình tiến trình cho bước viết — CLI chạy đồng bộ, operator ngồi chờ lệnh xong (ADR 0015). Về điểm "có progress UI hay không", AA-CIS không thua kém thiết kế gốc — nhưng đây vẫn là 1 khoảng trống UX thật với người dùng SaaS đa tenant (khác bản chất so với 1 operator dùng CLI riêng) | **KHÔNG can thiệp được giữa chừng** — chỉ chờ, đúng như câu hỏi đặt ra. Đây là thiết kế có chủ đích (AA-450: "ONE loading state") chứ không phải thiếu sót ai bỏ quên, nhưng KHÔNG có cơ sở nào (ADR/thiết kế gốc) nói rõ "không cần progress screen cho SaaS đa tenant" — cần Nghiệp/Claude Chat xác nhận có đủ hay cần thêm | **Gap thật xác nhận riêng (AA-513, route-aware write):** `route_segments` được backend dùng để build prompt (multi-Segment cho Blog Route) nhưng **KHÔNG bao giờ trả về frontend** — `_row_to_dict()` (`services/acp_content_writing/service.py`) không có field này. Nghĩa là tenant viết xong 1 bài Blog nhiều-Segment cũng KHÔNG thấy dấu hiệu gì phân biệt với bài 1-atom thường, ngay tại màn hình kết quả T9 |
| **T10 Gates** | CÓ — `/portal/t10-review`, `ReviewList.tsx` (AA-501) | Chỉ hiện `ready_state` (Ready / Writing… / Not Ready Yet) — code tự ghi rõ "DELIBERATELY never gate_ledger/repair_log/held_reason", quyết định của Nghiệp | ADR 0023 (Ms. Thư): "A Gate flags rather than blocks... every Piece ships... A Piece with Flags is the normal case, not a failure." ADR 0026 nói thẳng gate `promises_an_option` "sẽ thường xuyên gắn flag ở bản nháp đầu, và đó là chuyện bình thường". **AA-CIS làm KHÁC**: `gate_promises_an_option()` có `repairable=False` → fail lần đầu là `status='held'` NGAY (không phải "ship kèm flag" như bản gốc) → tenant chỉ thấy "Not Ready Yet" + text chung chung "Our team is on it", không thấy tên gate, không thấy câu bị flag | **KHÔNG** — không có cách nào tenant tự xem lý do hay tự sửa khi bị `held`. Người xử lý thật là AA STAFF qua `admin_a4.py` (đã có review queue đầy đủ `gate_ledger`/`held_reason` chi tiết per-gate — safety net này CÓ THẬT, đã kiểm chứng code) | **Câu hỏi kiến trúc thật, chưa có câu trả lời** — không tự bịa: AA-CIS's `content_piece` vẫn "block rồi chờ admin gỡ", trong khi ADR 0023/0026 gốc nói KHÔNG block cái gì cả. Đây có vẻ mâu thuẫn nhẹ với chính comment trong `AngleGateTab.tsx`: "*Adventure Asia never approves or blocks this for you*" — 1 piece bị `held` bởi `promises_an_option` (gate mới, dự đoán sẽ khá thường xuyên theo đúng lời ADR 0026) thực chất ĐANG bị 1 dạng gate/block chờ AA can thiệp. Cần Nghiệp/Claude Chat quyết định: giữ nguyên (an toàn hơn) hay đổi sang đúng tinh thần "ship kèm flag, không block" của bản gốc |
| **T11 Publish** | CÓ — `/portal/t11-publish/page.tsx` (AA-457/458), `PublishPendingList.tsx` | UI publish (title/content_preview/channel/date + nút Publish) — **không đổi gì** so với trước AA-507, xác nhận bằng `git diff HEAD origin/main --stat`: không file nào trong `t11-publish` hay `PublishPendingList.tsx` nằm trong diff của 8 sub-issue | Không có tương đương trong repo Ms. Thư ở dạng "1 nút publish" — output của repo là file HTML/Markdown, publish là hành động ngoài hệ thống (ADR 0015) | **CÓ**, publish hoạt động bình thường | **Xác nhận: đây LÀ khoảng trống thật, không phải "không cần đổi".** T11 là stage DUY NHẤT trong epic không có sub-issue — lý do thực tế không phải vì nó không cần gì, mà vì **route/hub metadata bị mất dọc đường trước khi tới T11**: T9 không trả `route_segments` về, T10 không lưu/hiện gì về Route, nên tới T11 thì hoàn toàn không còn dữ liệu để hiển thị dù muốn. `PublishPendingList.tsx` hiện 1 bài Blog route-aware (nhiều Segment) y hệt 1 bài atom đơn — tenant publish xong 1 bài multi-stop Route mà không hề biết đó là bài multi-Segment. Đây là gap thật, cần build (ít nhất: xuyên field route/hub qua T9→T10→T11 để hiển thị), không chỉ là "quên tạo issue" |

## Trả lời riêng các câu hỏi đặc biệt trong lệnh gốc

- **T5**: Atomize là nút bấm tenant tự chủ động, KHÔNG chạy ngầm tự động — xác nhận dứt khoát bằng code (`CatalogTab.tsx` dòng ~226-237, endpoint thật). Khớp tinh thần ADR 0015 của repo Ms. Thư (stage được "chạy", không tự khởi phát). Riêng copy trên trang T6 nói ngược lại ("generated automatically") là lỗi text, không phải lỗi luồng.
- **T6**: Ngoài thiếu group-by-Segment (đã có rồi, code AA-509 live) — còn thiếu Atom Score/ranking (AA-515) và liên kết Route/Hub (AA-510), cả hai đều KHÔNG hiển thị. Vẫn có nghĩa là chỉ list atom phẳng về mặt "độ ưu tiên/route" dù đã group phẳng-theo-Segment.
- **T7**: Xác nhận lại đúng như audit trước — 8-tab Channel, Slate live, không đào sâu thêm theo đúng yêu cầu. Có 1 điểm kiến trúc trang nhỏ ghi ở bảng trên (Slate nhúng dưới Quarter Plan cũ).
- **T8**: Đã đảo ngược đúng theo AA-512 — xác nhận bằng code thật trên `main` (không lặp lại phần audit riêng của AA-512, chỉ xác nhận kết luận: badge đo được có hiển thị đúng, Channel/Atom bị bỏ qua đúng khi có Subject).
- **T9**: Hoàn toàn async, tenant chỉ thấy loading rồi kết quả — đúng như dự đoán trong câu hỏi. Không có cơ sở rõ ràng nói đây là "đúng thiết kế" hay "thiếu 1 màn hình theo dõi" — bản thân repo Ms. Thư không có concept này (CLI đồng bộ), nên không dùng được làm bằng chứng cho cả 2 phía; đây là quyết định sản phẩm cần Nghiệp, không phải điều tra suy ra được.
- **T10**: Tenant KHÔNG thấy kết quả gate ở đâu cả (chỉ Ready/Writing/Not Ready Yet). Flag `promises_an_option` không hiện ở bất kỳ đâu tenant nhìn thấy — chỉ nằm trong `gate_ledger` mà AA staff xem qua `admin_a4.py`. Đã nêu rõ mâu thuẫn với ADR 0023/0026 gốc ở bảng trên — đây là câu hỏi kiến trúc cần quyết định, không tự kết luận đúng/sai.
- **T11**: Đã cập nhật gì chưa? CHƯA — 0 thay đổi. Có cần đổi theo Route/Segment mới không? CÓ — publish 1 bài Blog route-aware hiện tại publish y hệt bài thường, không hiển thị khác biệt gì, vì dữ liệu route đã bị rơi rớt từ T9. Đây LÀ 1 khoảng trống thật bị bỏ sót (T11 không có sub-issue trong epic), không phải "đúng là không cần đổi".

## Đề xuất kiến trúc điều hướng tổng thể (tham khảo, không bắt buộc)

Hiện trạng: Sidebar là 1 danh sách phẳng 11 mục (`Sidebar.tsx` NAV1) — Dashboard, Browse Pool,
My Catalog, Brand Identity, Atom Curation, Content Planning, Write Content, Review, Publish,
Marketplace, API Access — không có gì thể hiện đây là 1 pipeline có thứ tự
(T4→T5→T6→T7→T8→T9→T10→T11), không có trạng thái riêng từng bước (chưa tới/đang làm/xong/lỗi),
và như bảng trên cho thấy, 1 số bước (T9 gộp vào T8, T5 gộp vào T4) không có route riêng — đúng
tinh thần "1 wizard liền mạch" theo quyết định trước đây, nhưng khiến khó nhìn tổng thể theo yêu
cầu "cấu trúc phân cấp, chuyển trang đẹp hơn".

Đề xuất (để cân nhắc, không phải yêu cầu cứng):

1. **1 thanh tiến trình ngang (progress stepper) theo TOUR/SUBJECT**, không phải theo trang tĩnh —
   vì bản chất pipeline giờ không còn tuyến tính đều (T5/T9 không có trang riêng, T7 phân nhánh
   theo Channel). Stepper nên bám theo 1 đối tượng cụ thể tenant đang theo dõi (1 Subject hoặc 1
   content_piece), không phải 1 chuỗi 7 trang cố định — mỗi Subject/Piece tự có trạng thái riêng
   (proposed/picked/writing/held/approved/published) hiển thị dọc theo track.
2. Giữ nguyên các route hiện tại (không đổi URL, tránh phá vỡ bookmark/link đã có từ AA-430/448/449) —
   chỉ thêm 1 lớp điều hướng phụ (breadcrumb hoặc mini-stepper) TRÊN mỗi trang, chỉ ra đang ở bước
   nào trong track của 1 Subject cụ thể khi tenant đến từ 1 link có ngữ cảnh (`?tour_id=`,
   `?resume_request_id=`, tương tự cách `AtomsTab.tsx`/`CatalogTab.tsx` đã làm cho nhau).
3. Đổi nhãn sidebar "Content Planning" → "Slate" (khớp tên concept tenant thực sự thấy khi mở
   trang) — việc nhỏ, ăn theo lần build UI sắp tới thay vì để tên cũ tồn tại song song khái niệm mới.
4. Vá 3 điểm rơi dữ liệu Route/Hub (T6 không hiện, T9 không trả về, T11 không hiện) nên làm CHUNG
   1 đợt — vì cùng 1 field (`route_id`/`hub_name`) xuyên suốt, tách lẻ theo từng trang sẽ dễ vá nửa
   vời như đã thấy ở AA-513 (chỉ vá đến T9 backend, không xuyên tới response).

## Danh sách gap thật cần build (tổng hợp, ưu tiên theo mức nghẽn tenant)

1. **T10 — quyết định kiến trúc `promises_an_option` block-vs-flag** (mâu thuẫn ADR gốc, cần
   Nghiệp/Claude Chat quyết trước khi build, không phải lỗi code) — mức độ: cao, vì gate này dự
   kiến trigger thường xuyên theo đúng ADR 0026.
2. **Route/Hub metadata xuyên suốt T6→T9→T10→T11** (hiện mất tăm sau T7) — mức độ: trung bình,
   ảnh hưởng riêng nhánh Blog route-aware.
3. **T6 — hiện Atom Score (AA-515) trên UI** — mức độ: trung bình, backend đã có, chỉ thiếu field
   trả về + hiển thị.
4. **T9 — quyết định có cần progress screen không** (câu hỏi sản phẩm, không phải bug) — mức độ:
   thấp/trung bình.
5. **Việc nhỏ**: copy sai ở T6 EmptyState (T5 không tự động), nhãn sidebar T7 "Content Planning"
   → "Slate", comment đầu file `AngleGateTab.tsx` chưa cập nhật nhánh subject-driven.

---
*Chỉ điều tra + báo cáo theo đúng yêu cầu — không có file code/migration nào bị sửa trong lượt này
ngoài chính file báo cáo này.*
