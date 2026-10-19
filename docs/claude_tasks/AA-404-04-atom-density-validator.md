## Task cho Claude Code: build atom density validator (deterministic gate mới) [AA-404, ưu tiên cao]

Mục tiêu: Audit N0-N8 defense-layer xác nhận tầng #1 "Anti-AI-voice stack" (CONTEXT.md
§1.6, đòn bẩy cao nhất theo chính spec gốc Ms. Thư) HOÀN TOÀN CHƯA TỒN TẠI trong bản port.
Nguyên văn spec gốc: "Atom density validator: every 200–300 words of body must cite ≥1
atom/facts entry. Zero-atom paragraphs are flagged — that's where AI-voice lives. AI-sounding
text is an information-density problem, not a style problem: starve genericity out with
specifics." Đây là gate DETERMINISTIC (không LLM), lý giải trực tiếp code #1 nguyên nhân
blog fail F9 nhiều nhất (theo F9 deep-dive report).

QUAN TRỌNG — đọc trước, đừng lặp lại điều tra:
- `docs/implementation-notes/AA-404-F9-deep-dive.md` (đã merge, PR #159).
- File audit N0-N8 defense-layer trong `docs/claude_audit/` (task khác đang lưu song song
  — đọc nếu đã có, không chờ nếu chưa).
- `CONTEXT.md` bản gốc (`aa-marketing-v2/CONTEXT.md`) §1.5 (determinism boundary — xác nhận
  đây PHẢI là pure Python, never LLM) và §1.6.1 (atom density, nguyên văn ở trên).

Repo: AA-CIS-App
Branch mới: `feature/aa-404-atom-density-validator`
Merge vào: main

Việc cần làm:
1. Xác định đúng vị trí trong gate chain hiện tại (`gates.py`) — gate này thuộc nhóm
   DETERMINISTIC (F1-F7 theo naming hiện tại, xem code đặt nó ở đâu hợp lý — có thể là F2
   mới nếu số hiệu F2 hiện tại đang trống, hoặc thêm F-số mới nếu cần; KHÔNG đánh số trùng
   gate đã có — kiểm tra kỹ danh sách F1-F10 hiện tại trước khi đặt tên).
2. Logic: parse `body_tagged` (đã có `[R:atom_id]` provenance tags — xem cách F1_grounding
   đang parse chúng, tái dùng logic parse tag đã có, không viết parser mới).
3. Chia body thành các đoạn ~200-300 từ (theo đúng threshold spec gốc — xác nhận lại số
   chính xác trong CONTEXT.md, đừng đoán).
4. Với mỗi đoạn: đếm số `[R:atom_id]`/`[F:entry_id]` tag xuất hiện. Đoạn nào 0 tag → flag
   là zero-atom paragraph.
5. Quyết định threshold pass/fail: bao nhiêu % đoạn zero-atom thì fail toàn piece? (Spec
   gốc không nói rõ số cụ thể — cân nhắc: 1 đoạn zero-atom có nên fail cả piece ngay, hay
   cần tỷ lệ nhất định? Đề xuất hợp lý dựa trên tinh thần "every 200-300 words must cite" —
   có thể là: bất kỳ đoạn nào zero-atom đều fail, giữ nguyên tắc nghiêm ngặt của spec gốc.
   Nếu không chắc, implement với threshold rõ ràng, dễ điều chỉnh sau qua config, và NÊU RÕ
   trong PR description đây là quyết định cần Nghiệp xác nhận, không phải mặc định cứng).
6. Output: failure_code rõ ràng (VD `ZERO_ATOM_PARAGRAPH`), kèm vị trí đoạn vi phạm (để
   repair loop biết sửa đoạn nào — tái dùng cơ chế `flagged_phrases`/evidence đã dùng ở F9
   nếu hợp lý).
7. Wire vào gate chain — chạy Ở ĐÂU trong thứ tự (trước F9 hợp lý, vì đây là tầng "starve
   genericity out with specifics" nên chạy sớm hơn, cho phép repair sửa TRƯỚC KHI tới F9
   phải phán đoán "generic" một cách mơ hồ hơn).
8. QUAN TRỌNG: gate này CHỈ áp dụng piece nào có body dài đủ để chia đoạn (blog) — piece
   ngắn (Facebook 80-150 từ, TikTok script ngắn) có cần áp dụng không? Xem xét kỹ, có thể
   chỉ áp dụng cho blog (full-length), không áp dụng facebook/tiktok (đã có ràng buộc khác:
   single-atom ceiling từ PR #153/#154).

Test:
1. Unit test: đoạn có đủ atom → pass. Đoạn zero-atom → fail đúng code.
2. Test biên: đoạn đúng ranh giới 200/300 từ.
3. Test piece ngắn (facebook/tiktok) — xác nhận có bị áp gate này hay được exempt theo
   thiết kế đã chọn ở bước 8.
4. Full suite pass, flake8 sạch.

Sau khi done:
- `gh pr create`, KHÔNG tự merge (gate mới, ảnh hưởng toàn bộ future N7 run — Nghiệp cần
  review kỹ, đặc biệt quyết định threshold ở bước 5).
- PR description: nêu RÕ quyết định threshold đã chọn + lý do, để Nghiệp dễ duyệt/sửa.
- Paste PR link + verify về Claude Chat.
- KHÔNG tự chạy N7, không tự cập nhật Linear.
