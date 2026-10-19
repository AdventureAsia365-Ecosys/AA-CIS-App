# AA-444-01 — Task prompt (verbatim, saved before implementation)

Saved 23/08/2026, before any code was written, per repo convention (task prompt
snapshot precedes implementation).

---

## Task cho Claude Code: Build Marketplace view tổng hợp (AA-444)

Mục tiêu: build view/endpoint tổng hợp tenant_tour_versions + tour_atoms, FE trang read-only, deprecate marketplace_portfolios. AA-440 (Done) đã audit kỹ — đọc report đó trước, KHÔNG điều tra lại từ đầu.

Repo: AA-CIS-App
Branch hiện tại: main
Tạo branch mới: yes: feature/aa-444-marketplace-view
Merge vào: main

Files cần đọc trước:
- `docs/claude_audit/AA-440-marketplace-planning-produce-migration-audit.md` — toàn bộ findings, bảng mapping logic tái dùng/viết mới
- ADR-2026-038 mục 0.3 (Notion `3c3b8a41-ec5d-8123-911f-e0c308841e79`) — bản chất đúng của Marketplace
- Code path hiện đọc/ghi `marketplace_portfolios` (grep toàn repo) — cần biết hết mọi nơi trước khi deprecate, tránh bỏ sót
- `services/acp_planning/*` — catalog browse query + price/runway estimate functions AA-440 xác nhận pure/reusable
- 1 route `/admin/*` hoặc `/portal/*` hiện có làm mẫu style (tùy trang Marketplace mới đặt ở đâu — xem quyết định route bên dưới)

Context — quyết định đã chốt (23/08/2026):
- Marketplace KHÔNG phải bảng/trang lưu trữ riêng — là VIEW tổng hợp: `tenant_tour_versions WHERE tenant_id=X JOIN tour_atoms WHERE owner_scope=X`.
- KHÔNG cần bảng mới, KHÔNG cần migration schema — cả 2 bảng nguồn đã tenant-scoped sẵn (xác nhận AA-439-01/03).
- `marketplace_portfolios` (bảng cũ) DEPRECATE NGAY — ngừng đọc/ghi ở mọi code path, KHÔNG xóa dữ liệu (giữ nguyên trong DB).
- Trang FE mới CHỈ ĐỌC — không có logic chọn/lưu riêng như Marketplace kiểu cũ (đó là bản chất T1 rồi, không lặp lại ở đây).

Steps:

**1. Grep + liệt kê toàn bộ code path đọc/ghi `marketplace_portfolios`**
- Liệt kê đầy đủ file/hàm/endpoint đang dùng bảng này trước khi động vào gì.
- Với mỗi chỗ dùng, xác định: có thể chuyển sang view mới ngay không, hay cần giữ tạm (báo cáo rõ nếu có case nào không thể chuyển ngay, đừng tự ý bỏ qua).

**2. Build endpoint/view tổng hợp**
- Query `tenant_tour_versions` JOIN `tour_atoms` theo tenant, trả về tour đã chọn + atom curated của tenant đó.
- Tái dùng price/runway estimate functions đã pure (AA-440 xác nhận) nếu phù hợp cho output này — không viết lại logic đã có.
- Quyết định route/endpoint name theo convention hiện có (T-series hoặc `/api/tenant/marketplace` — chọn theo pattern gần nhất đã dùng cho tenant-facing read-only view, VD T6 curation UI).

**3. Deprecate `marketplace_portfolios`**
- Chuyển mọi code path đã liệt kê ở bước 1 sang dùng view mới (hoặc xóa hẳn nếu code path đó không còn cần thiết — báo cáo rõ quyết định).
- KHÔNG viết migration xóa bảng/cột — chỉ ngừng đọc/ghi ở tầng ứng dụng.
- Nếu có comment/docstring nào mô tả bảng này đang "sống", cập nhật ghi chú deprecated + trỏ tới thay thế (view mới), để người đọc sau không nhầm.

**4. FE trang read-only**
- 1 trang cho tenant xem tổng hợp tour+atom của chính họ — style theo trang tenant-facing gần nhất đã có (VD T6 curation UI nếu đã build, hoặc PoolTab.tsx).
- Không cần action button — chỉ hiển thị.

Verify:
- Query view mới với ít nhất 1 tenant thật có data (VD tenant test-agency hoặc test-n1-flow đã dùng ở AA-437) — xác nhận trả đúng tour+atom của đúng tenant đó, không lẫn tenant khác.
- Xác nhận không còn code path nào đọc/ghi `marketplace_portfolios` (grep lại sau khi sửa, phải về 0 — trừ nếu cố tình giữ lại 1 chỗ có lý do, báo cáo rõ).
- pytest suite hiện có: không regression.

Sau khi done:
- Lưu CHÍNH file task prompt này vào `docs/claude_tasks/AA-444-01-build-marketplace-view.md` trước khi bắt tay code.
- Lưu báo cáo thực thi vào `docs/implementation-notes/AA-444-marketplace-view.md`.
- git commit + push, tạo PR, KHÔNG tự merge.
- Paste kết quả verify về Claude Chat.
- Linear AA-444: giữ nguyên status, Claude Chat verify qua comment trước khi đổi Done (ADR-2026-037).
