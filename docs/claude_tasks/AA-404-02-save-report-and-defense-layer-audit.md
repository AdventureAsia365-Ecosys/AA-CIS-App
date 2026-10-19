## Task cho Claude Code: lưu F9 deep-dive report + audit mở rộng "defense-layer completeness" toàn N0-N8

Mục tiêu: 2 việc, làm phần 1 trước (nhanh, tránh mất dữ liệu), rồi phần 2 (điều tra mở rộng).

---

### Phần 1 — Lưu báo cáo (làm ngay, ưu tiên cao nhất)

File `F9-deep-dive-report.md` hiện đang nằm ở
`/tmp/claude-1000/-home-nghiep-projects-aa-cis-AA-CIS-App/3c344092-c283-4c73-8f63-39441918870c/scratchpad/F9-deep-dive-report.md`
— đây là scratchpad tạm của phiên, sẽ MẤT khi phiên kết thúc, KHÔNG commit vào repo.

Copy nội dung đầy đủ (không tóm tắt, không cắt bớt) vào
`docs/implementation-notes/AA-404-F9-deep-dive.md` trong repo AA-CIS-App (file mới, tách
riêng khỏi `AA-404.md` chính để không làm phình file đó quá mức — nhưng thêm 1 dòng link
tham chiếu ở cuối `AA-404.md` trỏ sang file mới này). Commit trực tiếp lên `main` (chỉ là
docs, không phải code — nếu team convention yêu cầu PR cho mọi thay đổi kể cả docs thì làm
PR nhỏ, tự merge được vì không phải code/schema).

---

### Phần 2 — Audit mở rộng: module nào khác có pattern giống F9 (chỉ giữ 1 tầng LLM, bỏ tầng deterministic)

Bối cảnh: F9 deep-dive vừa phát hiện 2 lỗ hổng nghiêm trọng — (1) brand rubric chỉ wire
vào JUDGE (F9), KHÔNG wire vào WRITER (E2/E3/E5) — 2 bên chấm/viết theo 2 chuẩn khác nhau;
(2) F9 (1 LLM judge call) đang một mình gánh việc của CẢ 6 tầng "Anti-AI-voice stack"
(CONTEXT.md §1.6) mà bản gốc Ms. Thư thiết kế tách riêng — trong đó tầng #1 (atom density
validator, deterministic, đòn bẩy cao nhất theo chính bà) HOÀN TOÀN CHƯA ĐƯỢC XÂY trong bản
port. Nghiệp hỏi: đây có phải hiện tượng RIÊNG của F9, hay là PATTERN LẶP LẠI ở các module
khác trong toàn chuỗi N0-N8?

Đã có 1 lần audit toàn chuỗi trước đó (xem `docs/implementation-notes/AA-404.md`, phần đối
chiếu module A-H với CONTEXT.md — kết quả đã ghi Linear AA-410/AA-411) — audit đó tập trung
"function có tồn tại tương ứng không" (tồn tại/không tồn tại/một phần). Audit LẦN NÀY khác:
tập trung "NHỮNG FUNCTION ĐÃ TỒN TẠI, có đủ CÁC TẦNG PHÒNG THỦ/KIỂM TRA mà spec gốc thiết kế
không, hay chỉ giữ lại 1 tầng và bỏ các tầng còn lại — giống chính xác pattern vừa thấy ở F9".

Đọc trước:
- `CONTEXT.md` bản gốc Ms. Thư TOÀN BỘ §1 (SYSTEM-WIDE LAWS) — đặc biệt §1.2 (closed-world
  generation — provenance tags), §1.5 (determinism boundary — liệt kê RÕ cái gì PHẢI
  deterministic, cái gì được phép LLM), §1.6 (anti-AI-voice stack, 6 tầng đã biết), §1.7
  (failure ladder).
- `F9-deep-dive-report.md` (vừa lưu ở Phần 1) — để hiểu đúng KHUÔN MẪU lỗ hổng cần tìm.
- Toàn bộ `aamc/*.py` bản gốc (đã đọc 1 phần trước, đọc lại đầy đủ nếu cần) — đối chiếu
  IMPLEMENTATION thật, không chỉ spec markdown.

Việc cần làm — với MỌI module/gate còn lại trong chuỗi N0-N8 (không chỉ F9), kiểm tra theo
đúng khuôn mẫu 2 câu hỏi đã lộ ra ở F9:

**Câu hỏi 1 — "writer vs judge lệch chuẩn":** Với mọi cặp (nơi TẠO nội dung, nơi KIỂM TRA
nội dung) trong pipeline — 2 bên có cùng đọc 1 nguồn config/rubric/rule hay đang dùng 2
nguồn khác nhau (1 bên đã update, 1 bên còn cũ)? Kiểm tra cụ thể:
- E1 (outline) vs F5 (brief_compliance) — outline có tuân theo CHÍNH XÁC brief đã compile
  (C3) hay outline tự generate độc lập rồi F5 mới kiểm tra sau?
- E1/E2 vs F2 (atom density, NẾU tồn tại — xem câu hỏi 2 bên dưới) — writer có được instruct
  rõ "phải cite atom mỗi 200-300 từ" hay chỉ generate tự do rồi gate mới đếm sau?
- Brief compiler (C3) vs generation (E1/E2) — Brief có atoms/facts đã assign theo section,
  writer có thực sự dùng ĐÚNG atom đã assign hay tự chọn atom khác?
- Bất kỳ cặp writer/judge nào khác phát hiện được trong lúc đọc code.

**Câu hỏi 2 — "6 tầng anti-AI-voice, tầng nào tồn tại/tầng nào thiếu":** Với TỪNG tầng
trong CONTEXT.md §1.6 (1. Atom density validator, 2. Banned-pattern lexicon, 3. Structural
variance rules, 4. Brand rejects/audit rubric, 5. Human seam, 6. Regression signal/AI-detector
trend) — xác nhận CÓ/KHÔNG/MỘT PHẦN trong bản port, dùng đúng tên function/file cụ thể nếu
có. (Đã biết: #2 banned-pattern = F2 tồn tại; #4 brand rejects = F9 tồn tại nhưng gánh quá
nhiều; #1 atom density = KHÔNG tồn tại, xác nhận ở deep-dive vừa xong — verify lại chắc
chắn, không nhầm với F1_grounding vốn là gate khác; #3 structural variance = F3 tồn tại;
#5 human seam = chưa xác nhận; #6 AI-detector trend = chưa xác nhận).

**Câu hỏi 3 (mở rộng câu 2, spec gốc §1.5 determinism boundary):** Spec gốc liệt kê RÕ danh
sách "deterministic, never LLM": "runway math, lead-time offsets, slot allocation, funnel-mix
validation, budgets/caps, grounding checks, banned-pattern scans, atom cooldowns, schema
emission (FAQ JSON-LD), date math, metric roll-ups, confidence gates". Với TỪNG mục trong
danh sách này, xác nhận bản port có đúng implement DETERMINISTIC (pure Python, không gọi
LLM) hay đã âm thầm đổi sang gọi LLM ở đâu đó (vi phạm §1.5 invariant #4: "No number from
prose — code computes, the LLM narrates computed values only").

Output yêu cầu:
- Bảng tổng hợp: mỗi module/tầng/hạng mục kiểm tra → CÓ/KHÔNG/MỘT PHẦN/LỆCH CHUẨN, kèm
  bằng chứng code cụ thể (file:line).
- Xếp hạng mức độ nghiêm trọng: hạng mục nào GIỐNG HỆT pattern F9 (1 LLM gánh việc nhiều
  tầng, hoặc writer/judge lệch chuẩn) là NGHIÊM TRỌNG NHẤT — vì đã có bằng chứng thật (F9)
  cho thấy hậu quả là pipeline chạy nhiều lần vẫn không hội tụ.
- Nếu phát hiện thêm bug/gap cụ thể tương tự F9 — liệt kê rõ, đề xuất Linear issue (không
  tự tạo issue, để Nghiệp/Claude Chat quyết định sau khi xem báo cáo).

KHÔNG tự sửa code — đây là audit/investigation. Ưu tiên ĐỘ RỘNG (quét hết mọi module) hơn
ĐỘ SÂU (không cần điều tra kỹ như F9 deep-dive cho từng module — chỉ cần xác nhận có/không/
lệch chuẩn, đủ để Nghiệp biết mức độ rủi ro tổng thể).

Báo cáo về Claude Chat: bảng tổng hợp đầy đủ + kết luận tổng quan (đây có phải pattern hệ
thống lặp lại nhiều nơi, hay F9 là trường hợp đặc biệt xui nhất). Paste toàn bộ nội dung
trong response, không chỉ nói "đã lưu file" — giống cách đã làm ở F9 deep-dive report.
