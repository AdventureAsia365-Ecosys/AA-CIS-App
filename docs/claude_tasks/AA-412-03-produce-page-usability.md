## Task cho Claude Code: /admin/produce — trigger form availability hints + piece-level Gate C list + collapsed Gate Ledger

Mục tiêu: 3 cải tiến UX tiếp theo trên `/admin/produce` sau khi PR #166/#167 (modal
full-screen + Run History full-width) đã lên production và verify OK. Không đổi API
contract hiện có trừ khi thật sự cần API mới cho phần 1 (xem ghi chú).

Repo: AA-CIS-App
Branch hiện tại: main (verify bằng `git branch --show-current`)
Tạo branch mới: yes — `feature/aa-412-produce-page-usability`
Merge vào: main

Files cần đọc trước:
- Component "Trigger a Production Run" form (tenant/year/month/week selectors + nút "Run
  Production") trong `/admin/produce`.
- Component "Gate C — Packets Ready for Review" table (packet-level list hiện tại).
- Component modal "Review Packet Pieces" (vừa sửa ở PR #166 — full-screen, sticky
  header/footer/per-piece action).
- Trang/component "Quarter Plan (Gate B)" — tìm cách nó fetch + hiển thị approved plans
  theo tenant/year/month/week, để tái sử dụng cùng API/data source.
- `docs/implementation-notes/AA-412-ui-readability.md` — kết quả PR trước, tránh làm lại
  hoặc phá vỡ sticky header/footer/per-piece action vừa build.
- `docs/implementation-notes/AA-412.md` — bối cảnh gốc AA-412.

Context:
- Đã verify sống trên `aa-cis.lumiguides.it.com/admin/produce`: modal Review Packet
  Pieces giờ full-screen, sticky OK; bảng Run History full-width, không cuộn ngang. Cả 2
  đã confirm qua browser thật — giữ nguyên, chỉ build thêm.
- Vấn đề còn lại quan sát qua browser thật (không phải giả định):
  1. **Trigger a Production Run** — chọn tenant/year/month/week hiện tại là "chọn mù":
     không biết tuần nào của tenant nào đã có Quarter Plan (Gate B) approved trước khi
     bấm "Run Production". Nếu chưa approve, run sẽ fail với message rõ ràng (đã có
     validation phía sau) — nhưng người dùng phải thử mới biết, mất thời gian.
  2. **Gate C — Packets Ready for Review** — hiện gộp 1 dòng/packet (VD 4 piece trong 1
     packet chỉ hiện 1 dòng "6cadcbaa... / Adventure Asia Internal / 2026 W1 / 4 pieces /
     0/4 approved"). Muốn thấy ngay từng piece (channel, status, gate result) trước khi
     phải bấm "Review Pieces" mở modal xem chi tiết.
  2b. **BUG nghi ngờ — data/group sai theo tháng**: Nghiệp quan sát 2 packet của Week 1
     nhưng THUỘC 2 THÁNG KHÁC NHAU (VD 2026-08 W1 và 2026-09 W1) đang bị gộp/hiển thị
     chung như thể cùng 1 kỳ ("2026 / W1" — không phân biệt tháng). Đây là điều tra riêng,
     ưu tiên làm TRƯỚC Phần 2 vì có thể là bug hiển thị SAI DỮ LIỆU (khác với refactor
     UX ở Phần 2) — xem Steps — Phần 2a bên dưới.
  3. **Trong modal Review Packet Pieces** — mỗi piece hiện expand sẵn hiển thị CONTENT +
     GATE LEDGER (9 gate) + BRAND/SEO AUDIT (JSON) cùng lúc, dài, phải cuộn nhiều. Muốn
     Gate Ledger (badge PASS/FAIL từng gate, gọn) hiện ngay ngoài — nhìn lướt biết piece
     nào pass hết, piece nào có gate fail — mà KHÔNG cần bấm mở rộng. Content và
     Brand/SEO Audit (JSON dài) thì vẫn thu gọn, bấm mới xổ ra đọc.

Steps — Phần 1: Trigger form hiện availability theo Gate B
1. Xác định API/data source Quarter Plan (Gate B) approved đang dùng ở trang Quarter Plan
   — có khả năng đã có endpoint trả về list approved plans theo tenant, có year/month/week.
   Tái sử dụng endpoint đó, KHÔNG tạo bảng/logic trùng lặp.
2. Khi chọn Tenant trong dropdown: fetch/filter danh sách year+month+week nào của tenant
   đó đã có Gate B approved — hiển thị trực quan, ví dụ: disable hoặc đánh dấu rõ những
   tổ hợp year/month/week CHƯA approved trong dropdown Month/Week (hoặc hiện 1 dòng gợi ý
   "Approved slots: 2026-08 W1, W2..." ngay dưới form).
3. Nếu backend chưa có endpoint phù hợp (list approved theo tenant, không cần chọn sẵn
   year/month) — DỪNG lại, không tự ý thêm API mới, báo lại cho Nghiệp mô tả rõ endpoint
   cần thêm (path, params, response shape) trước khi code backend.
4. Vẫn giữ nguyên hành vi cũ: nếu người dùng cứ chọn tổ hợp chưa approved và bấm "Run
   Production", vẫn nhận error message rõ ràng như hiện tại (không rely 100% vào UI hint,
   đây chỉ là lớp hỗ trợ thêm).

Steps — Phần 2a: Điều tra + fix bug "2 Week 1 khác tháng bị gộp chung" (LÀM TRƯỚC Phần 2)
1. Đọc kỹ backend query/endpoint đang cấp data cho bảng "Gate C — Packets Ready for
   Review" (tìm bằng `grep -r "packets ready" -i` hoặc theo route `/admin/produce` gọi
   API nào — có thể là `GET .../produce/runs` hoặc tương tự nhắc ở AA-412 trước).
2. Kiểm tra CHÍNH XÁC: cột "WEEK" hiển thị "2026 / W1" có lấy đúng cả year + month + week
   từ DB hay chỉ lấy year + week (bỏ sót month)? Đối chiếu với field thật trong
   `acp_shared`/`acp_deliver` (dùng skill `aa-cis-schema` nếu cần tra tên cột chính xác —
   nhắc lại: "Week" ở đây LÀ 1-4 week-of-MONTH slot, không phải ISO week, theo AA-410).
3. Nếu bug ở TẦNG DATA (backend trả nhầm/group nhầm packet của tháng khác vào cùng key) —
   đây là bug nghiêm trọng hơn UI, vì Gate C có thể đang cho review NHẦM packet của tháng
   khác. Verify bằng query trực tiếp qua ECS Exec (S3-mediated pattern, xem skill
   `aa-cis-schema`) đối chiếu DB thật với những gì UI hiển thị cho 2 packet Week 1 khác
   tháng — xác nhận packet_id, tenant_id, year, month, week THỰC trong DB trước khi kết
   luận là bug frontend hay backend.
4. Nếu bug chỉ ở TẦNG HIỂN THỊ (label "2026 / W1" gộp năm+tuần nhưng bỏ month, trong khi
   data thực đằng sau đã đúng/riêng biệt theo từng packet) — sửa cột hiển thị: LUÔN show
   đủ Year — Month — Week riêng biệt, rõ ràng, không rút gọn gây nhầm 2 kỳ khác nhau
   trông giống nhau (VD "2026-08 / W1" và "2026-09 / W1", không phải chỉ "W1").
5. Áp dụng fix tương tự (nếu có) cho bảng Run History — kiểm tra cột WEEK ở đó có cùng
   vấn đề không (đã sửa full-width ở PR trước nhưng chưa kiểm tra riêng bug group-theo-
   tháng này).
6. Ghi rõ trong verify report: bug này là DATA hay DISPLAY, bằng chứng (query result +
   screenshot), và fix áp dụng ở tầng nào.

Steps — Phần 2: Gate C packet list → piece-level list
1. Đổi bảng "Gate C — Packets Ready for Review" từ 1 dòng/packet sang 1 dòng/piece. Cột
   gợi ý: Packet (rút gọn ID, vẫn giữ liên kết ngầm để biết piece nào cùng packet — có
   thể group visual bằng packet ID lặp lại hoặc separator), Tenant, Week, Channel (blog/
   facebook/tiktok...), Status (PENDING REVIEW/APPROVED/REJECTED), Gate (PASSED/FAILED),
   Reviewed by (nếu có).
2. Click 1 row (1 piece) → mở thẳng modal Review Packet Pieces đã scroll/focus tới đúng
   piece đó (dùng lại modal full-screen hiện có từ PR #166, không xây modal mới) — không
   bắt phải bấm "Review Pieces" ở packet-level rồi tự tìm piece trong modal.
3. Nếu 1 packet có nhiều piece cùng trạng thái, cân nhắc group theo packet bằng header
   con hoặc màu nền xen kẽ để dễ phân biệt packet nào với packet nào — nhưng đơn vị hiển
   thị chính vẫn là piece, không phải packet.
4. Giữ 1 điểm vào tổng quan packet (VD packet ID + "x/y approved" tổng) — có thể làm
   sticky group header phía trên nhóm piece cùng packet, để không mất ngữ cảnh packet.

Steps — Phần 3: Modal — Gate Ledger hiện ngoài, Content/Brand-SEO Audit thu gọn
1. Trong modal Review Packet Pieces (component từ PR #166), đổi layout mỗi piece:
   - Gate Ledger (bảng 9 gate + PASS/FAIL) hiển thị NGAY, không cần bấm mở rộng — đây là
     phần quyết định nhanh approve/reject.
   - Content (bài viết đầy đủ) và Brand/SEO Audit (JSON) — thu gọn mặc định (collapsed),
     có nút/toggle rõ ràng để bấm xổ ra đọc chi tiết khi cần.
2. Giữ nguyên sticky header/footer/per-piece action bar đã build ở PR #166 — không phá
   layout đó.
3. Nếu Gate Ledger có gate FAIL — auto-expand Content/Audit cho piece đó (gợi ý người
   dùng cần xem lý do fail), các piece PASS hết thì giữ collapsed mặc định.

Verify:
1. `npm run build` pass sạch.
2. Chạy production build thật, xác nhận qua Playwright hoặc chụp screenshot: (a) trigger
   form hiện rõ tổ hợp tenant/year/month/week đã approved Gate B; (b) 2 packet Week 1 của
   2 tháng khác nhau giờ hiển thị RÕ RÀNG KHÁC NHAU (không còn gộp/trông giống nhau) trên
   cả Gate C list lẫn Run History; (c) Gate C list hiện từng piece, click piece mở đúng
   modal đúng vị trí; (d) modal hiện Gate Ledger ngoài, Content/Audit collapsed mặc định,
   auto-expand khi có FAIL.
3. Deploy Dev qua CI, verify ECS task digest khớp `:latest`.
4. Lưu verify report vào `docs/implementation-notes/AA-412-produce-page-usability.md`.
5. KHÔNG tự đánh dấu Done — dừng lại sau khi deploy, báo Nghiệp tự test qua
   `aa-cis.lumiguides.it.com/admin/produce`.

Git context:
- Repo: AA-CIS-App
- Current branch: main
- Tạo branch mới: yes — feature/aa-412-produce-page-usability
- Merge vào: main
- Sau khi done: `git add . && git commit -m "feat: add Gate B availability hints, piece-level Gate C list, collapsed gate ledger UX [AA-412]" && git push`, tự `gh pr create` (KHÔNG tự merge)

Sau khi done:
- Paste PR link + verify result về Claude Chat
- Trước khi ghi Linear, tự `get_issue` xác minh AA-412 title khớp bối cảnh (Gate C UI)
  trước khi post comment — không đổi status (vẫn giữ In Progress, D3 reject-flow chưa
  làm).
