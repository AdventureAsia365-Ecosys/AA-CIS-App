## Task cho Claude Code: Gate C review UI + Run History table — readability/layout fix

Mục tiêu: Sửa 2 màn hình trong `/admin/produce` (AA-CIS-App frontend) đang khó dùng vì
layout chật — không đổi logic nghiệp vụ, chỉ đổi UI/CSS/component structure.

Repo: AA-CIS-App
Branch hiện tại: main (verify bằng `git branch --show-current` trước khi bắt đầu)
Tạo branch mới: yes — `feature/aa-412-gate-c-ui-readability`
Merge vào: main (trunk-based, không còn develop — xem ADR-2026-023)

Files cần đọc trước:
- Component modal "Review Packet Pieces" (Gate C review, dưới tab "Trigger & Gate C" của
  `/admin/produce`) — tìm bằng `grep -r "Review Packet Pieces" --include=*.tsx` hoặc
  `grep -r "Advance Packet to Approve-to-Publish"`.
- Component bảng "Run History" (tab "Run History" cùng trang) — tìm bằng
  `grep -r "RUN HISTORY" --include=*.tsx` hoặc `grep -r "N7 PRODUCE"`.
- `docs/implementation-notes/AA-412.md` — bối cảnh AA-412 (Gate C per-piece approve +
  history view) vừa build xong, đây là task follow-up sửa UX của chính nó.

Context:
- Đã verify sống trên `aa-cis.lumiguides.it.com/admin/produce` (production build thật,
  không phải `next dev`) — 2 vấn đề UX cụ thể quan sát được qua screenshot:
  1. **Modal Review Packet Pieces**: modal nhỏ, cố định giữa màn hình, phải cuộn dọc rất
     dài (content + gate ledger 9 gate + brand/SEO audit JSON cho từng piece, x4 piece)
     mới thấy nút "Advance Packet to Approve-to-Publish" ở đáy. Nút Approve/Reject của
     từng piece nằm ngay dưới header piece đó nhưng trôi mất khi cuộn xuống piece sau.
  2. **Bảng Run History**: bảng có ~13 cột (TENANT, WEEK, STATUS, TRIGGERED, PIECES, rồi
     9 cột gate F1-F9 + output_rules) — bị tràn ngang, phải cuộn ngang mới thấy hết, dễ
     mất thông tin. Khi expand 1 row để xem per-piece gate ledger + held reason, hàng chi
     tiết cũng bị cắt ngang tương tự.
- Đây là internal admin tool (Nghiệp dùng hàng ngày để review Gate C trước khi publish),
  không phải customer-facing — ưu tiên dễ dùng/dễ đọc hơn thẩm mỹ.
- KHÔNG đổi API calls, KHÔNG đổi field nào trong request/response — chỉ đổi
  layout/CSS/component structure phía client.

Steps — Modal "Review Packet Pieces":
1. Mở rộng modal: đổi từ modal nhỏ cố định sang full-screen dialog/drawer (chiếm gần hết
   viewport, có thể dùng `max-w-full h-full` hoặc tương đương tuỳ UI kit đang dùng trong
   repo — kiểm tra xem đang dùng shadcn/ui, Radix Dialog, hay custom trước khi chọn cách).
2. Header của modal (tên packet, packet ID, nút đóng "X") — sticky, luôn hiển thị khi
   cuộn, không bị nội dung che.
3. Nút Approve/Reject của MỖI piece — sticky theo piece đó khi đang cuộn trong phần
   content của piece đó (không trôi mất, không cần cuộn ngược lên để bấm).
4. Footer — thanh chứa "x/4 pieces individually approved" + nút "Advance Packet to
   Approve-to-Publish" — sticky ở đáy modal, luôn hiển thị, không bị che bởi nội dung
   cuộn hay bị đẩy ra ngoài viewport.
5. Đảm bảo mọi thông tin hiện có (content, gate ledger 9 gate, brand/SEO audit JSON) vẫn
   hiển thị đầy đủ, không ẩn/collapse mất chi tiết nào — chỉ đổi cách bố trí để dễ đọc
   hơn trong không gian rộng hơn, không cắt bớt thông tin.

Steps — Bảng "Run History":
1. Bảng full width theo viewport, bỏ cuộn ngang — người dùng phải thấy hết cột mà không
   cần scroll trái/phải.
2. Vẫn hiển thị đầy đủ, chi tiết tất cả thông tin hiện có (không gộp/ẩn cột gate F1-F9,
   không tóm tắt mất chi tiết) — nghĩa là cần layout khác để chứa hết mà không tràn:
   cân nhắc responsive column width, font-size nhỏ hơn cho vùng gate, hoặc horizontal
   scroll CHỈ trong vùng bảng con (không phải toàn trang) nếu thực sự không đủ chỗ —
   nhưng ưu tiên số 1 vẫn là full-width fit trang trước khi rơi về scroll cục bộ.
3. Phần expand row (per-piece gate ledger + held reason) hiện tại cũng bị cắt ngang
   tương tự — áp dụng cùng nguyên tắc fit full-width, đầy đủ thông tin.

Verify:
1. `npm run build` pass sạch (không lỗi type/lint).
2. Chạy production build thật (`npm run build && npm start`, KHÔNG dùng `next dev`) —
   tự chụp screenshot hoặc dùng Playwright để xác nhận: (a) modal Review Packet Pieces mở
   full-screen, header+footer sticky, nút Approve/Reject từng piece không bị trôi mất khi
   cuộn; (b) bảng Run History không bị cuộn ngang ở viewport chuẩn (1920x1080 và
   1440x900), toàn bộ cột gate hiển thị đủ.
3. Deploy Dev qua CI như thường lệ, verify ECS task digest khớp `:latest` sau deploy
   (theo pattern đã dùng ở AA-412).
4. Lưu kết quả verify (screenshot trước/sau, digest, URL) vào
   `docs/implementation-notes/AA-412-ui-readability.md` — KHÔNG lưu vào `/tmp` hay
   scratchpad tạm (sẽ mất khi phiên kết thúc).
5. **Lưu ý quan trọng**: "Done" chỉ được xác nhận sau khi Nghiệp tự bấm qua browser thật
   (production build). Sau khi deploy xong, dừng lại và báo cho Nghiệp tự test qua
   `aa-cis.lumiguides.it.com/admin/produce`, không tự đánh dấu Done.

Git context:
- Repo: AA-CIS-App
- Current branch: main
- Tạo branch mới: yes — feature/aa-412-gate-c-ui-readability
- Merge vào: main
- Sau khi done: `git add . && git commit -m "fix: improve Gate C review modal + Run History table readability [AA-412]" && git push`, tự `gh pr create` (KHÔNG tự merge — merge là human-only, Nghiệp tự bấm)

Sau khi done:
- Paste PR link + verify result (screenshot, digest) về Claude Chat
- Linear: AA-412 → cập nhật comment với kết quả, KHÔNG đổi status (issue AA-412 vẫn open
  cho D3 — reject flow chưa có, xem `docs/implementation-notes/AA-412.md`)
- **Trước khi ghi bất cứ gì vào Linear issue AA-412, tự `get_issue` xác minh title khớp
  bối cảnh này (Gate C UI) trước khi post** — tránh lặp lỗi ghi nhầm issue đã xảy ra ở S150.

---

## Mid-turn amendment (received while task was in progress)

User overrode the PR-merge step above: after PR is created, monitor CI — if CI green,
merge it directly (`gh pr merge --auto --squash`, per the repo's existing solo-operator
auto-merge convention — see [reference_pr_auto_merge_not_automatic]/CLAUDE.md "CI/CD —
solo-operator mode"), then monitor deploy through completion, and report back for Nghiệp
to test on the real UI. This supersedes the original task's "KHÔNG tự merge" instruction
for this specific PR only — it does not change the general repo rule for other work.
