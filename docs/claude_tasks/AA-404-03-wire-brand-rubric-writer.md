## Task cho Claude Code: wire brand rubric thật vào E2-E5 (writer side) [AA-404, ưu tiên cao]

Mục tiêu: F9 deep-dive xác nhận PR #158 chỉ wire brand rubric thật (`tenant_brand_rules`)
vào phía JUDGE (F9). Writer (`generation.py` E2, `adapt.py` E3, `faq.py` E4, `repair.py`
E5) vẫn hardcode `AA_BRAND_IDENTITY_PROMPT` generic — writer viết theo chuẩn cũ trong khi
judge chấm theo chuẩn mới cụ thể hơn. Đây là nguyên nhân chính khiến fix #1 (PR #158) chưa
cải thiện blog nhiều. Fix lần này: cho cả 4 module writer đọc CÙNG nguồn `fetch_brand_rubric_text()`
mà judge đang dùng.

QUAN TRỌNG — trước khi code, đọc kỹ 2 file audit vừa merge (đừng lặp lại điều tra):
- `docs/implementation-notes/AA-404-F9-deep-dive.md` (PR #159, đã merge vào main).
- File audit N0-N8 defense-layer (đang được lưu vào `docs/claude_audit/` bởi 1 task khác
  chạy song song — nếu file đó đã có khi bạn bắt đầu, đọc nó; nếu chưa, tiếp tục không cần
  chờ, nội dung chính đã tóm tắt đủ ở đây).

Bối cảnh kiến trúc quan trọng (từ audit): spec gốc Ms. Thư có `build_context()` — 1 điểm
assembly DUY NHẤT lắp mọi context (role/corpus/tenant/upstream/task/output) cho MỌI lời gọi
LLM, đảm bảo writer và judge luôn đọc chung 1 nguồn brand/rubric. Bản port KHÔNG có cơ chế
này — mỗi module tự ý fetch riêng. Case đối chứng sạch: E1 (outline) và F4 (brief_compliance)
CÙNG đọc 1 object `Brief` → không hề lệch chuẩn. Đây là bằng chứng: fix đúng hướng là đảm
bảo E2-E5 và F9 cùng đọc `fetch_brand_rubric_text()` (đã có sẵn từ PR #158, không viết mới).

Repo: AA-CIS-App
Branch mới: `feature/aa-404-wire-brand-writer-side`
Merge vào: main

Files cần sửa:
1. `services/content_generation/generation.py` (E2 draft_sections) — tìm chỗ hiện đang
   hardcode/import `AA_BRAND_IDENTITY_PROMPT`, thay bằng gọi `fetch_brand_rubric_text(db,
   tenant_id)` (hàm đã có từ PR #158 trong `slot_runner.py` — cân nhắc: có nên di chuyển
   hàm này lên 1 module dùng chung, VD `services/acp_produce/brand.py`, để mọi module import
   được mà không tạo layering inversion? Xem PR #158 đã tự lưu ý về vấn đề layering
   `api.routers → services` — áp dụng đúng nguyên tắc tương tự ở đây).
2. `services/acp_produce/adapt.py` (E3 channel adapt) — tương tự.
3. `services/acp_produce/faq.py` (E4 FAQ) — tương tự, nếu module này cũng có brand voice
   instruction riêng.
4. `services/acp_produce/repair.py` (E5 repair) — ĐÃ CÓ `PieceInvariants` object từ PR #154
   truyền xuyên suốt repair loop — thêm brand_rubric_text vào invariants object đó (tái
   dùng cơ chế đã có, không tạo tham số mới rời rạc) để mọi vòng repair đều nhận đúng brand
   rubric thật.
5. Đảm bảo fetch 1 lần/slot (như đã làm ở PR #158 cho F9), reuse cho cả E2/E3/E4/E5 trong
   cùng slot — KHÔNG fetch lại nhiều lần lãng phí.

Fallback: nếu row brand rules thiếu/rỗng cho tenant nào đó (không phải aa_internal) — fallback
về `AA_BRAND_IDENTITY_PROMPT` generic, có log warning (không im lặng), giống pattern PR #158
đã làm cho F9.

Test:
1. Unit test xác nhận E2/E3/E4/E5 đều gọi đúng `fetch_brand_rubric_text()`.
2. Unit test fallback khi row rỗng/thiếu.
3. Full suite pass, flake8 sạch.

Sau khi done:
- `gh pr create`, KHÔNG tự merge (thay đổi lan rộng 4 module chính, Nghiệp cần review).
- Paste PR link + verify (test count, flake8) về Claude Chat.
- KHÔNG tự chạy N7 — để bước riêng sau merge, đúng chuỗi đã làm.
- KHÔNG tự cập nhật Linear.
