## Task cho Claude Code: chuyển file audit N0-N8 vào docs/claude_audit/ [nhanh, ưu tiên]

File hiện ở scratchpad tạm:
`/tmp/claude-1000/-home-nghiep-projects-aa-cis-AA-CIS-App/3c344092-c283-4c73-8f63-39441918870c/scratchpad/N0-N8-defense-layer-audit.md`
— sẽ mất khi phiên kết thúc.

**Quy tắc mới từ Nghiệp (áp dụng từ giờ cho MỌI báo cáo audit tương lai, không chỉ file
này):** lưu vào `docs/claude_audit/` trong repo (không phải `/tmp`), tên file có PREFIX là
issue liên quan để dễ tra cứu.

Việc cần làm:
1. Tạo thư mục `docs/claude_audit/` nếu chưa có.
2. Copy nội dung đầy đủ (không tóm tắt) của file trên vào
   `docs/claude_audit/AA-404-N0-N8-defense-layer-audit.md`.
3. Commit lên main (docs-only, không phải code/schema — tự merge được, giống cách xử lý
   `AA-404-F9-deep-dive.md` ở PR #159 trước đó).
4. Thêm 1 dòng note ở đầu file mới: nguồn gốc (task/ngày), và link sang
   `docs/implementation-notes/AA-404-F9-deep-dich.md` (báo cáo gốc dẫn tới audit này).

Ghi nhớ quy tắc mới này cho các lần tạo báo cáo audit sau trong phiên — không cần đợi
Nghiệp nhắc lại mỗi lần.

Báo cáo về Claude Chat: xác nhận đã lưu xong, đường dẫn cuối cùng trong repo.
