## Task cho Claude Code: build lại UI Produce & Deliver — Gate C per-piece + History view [AA-412]

Mục tiêu: 2 gap UI gộp thành 1 task vì share backend/frontend (đọc kỹ AA-412 đầy đủ trên
Linear trước khi code — có phần audit chi tiết route/endpoint hiện tại).

Đọc trước, KHÔNG lặp lại điều tra:
- Linear AA-412 (toàn bộ nội dung — đã audit sẵn `frontend/app/admin/produce/page.tsx`,
  `api/routers/admin_produce.py`, xác nhận route nào tồn tại/không tồn tại).
- `docs/claude_audit/AA-404-n7-run6-results.md` (dữ liệu 7 lần chạy N7 thật, dùng làm test
  data cho history view — không cần chạy N7 mới để có dữ liệu test).
- `services/acp_produce/models.py` (schema `acp_deliver.pieces` — xác nhận cột `body_tagged`,
  `gate_ledger`, `brand_seo_audit` đã tồn tại sẵn, chỉ chưa serialize ra API).

Repo: AA-CIS-App
Branch mới: `feature/aa-412-produce-history-piece-detail`
Merge vào: main

---

### Phần A — Gate C tách piece, xem nội dung trước khi approve (per-piece, không phải per-packet)

1. Endpoint mới: `GET /api/admin/produce/packets/{packet_id}/pieces` — trả danh sách piece
   trong packet đó, mỗi piece gồm `piece_id`, `channel`, `body_tagged` (nội dung thật),
   `gate_ledger` (đầy đủ, không chỉ status), `brand_seo_audit` (nếu có).
2. Trang/modal mới hiển thị nội dung từng piece — có thể tham khảo cách `/admin/review`
   (AA-234/241) render content, nhưng dữ liệu lấy từ `acp_deliver.pieces` (KHÔNG phải
   `silver_aa_internal.review_queue` — 2 nguồn khác nhau, xem AA-412 đã làm rõ).
3. Approve chuyển từ per-packet sang **per-piece**: mỗi piece có nút Approve/Reject riêng,
   không phải 1 nút Approve gộp cả packet.
   - Cân nhắc: `trust_ramp.confirm_ramp_transition()` hiện nhận `packet_id` — cần xác nhận
     cơ chế ramp có hỗ trợ approve từng piece rồi mới move cả packet lên `veto_window_auto`
     khi ĐỦ piece đã approve, hay cần thiết kế lại logic này. Nếu ramp logic hiện tại chỉ
     hoạt động ở mức packet — có thể giữ "packet chỉ chuyển ramp khi 100% piece đã approve
     riêng lẻ", KHÔNG đổi ramp mechanism nếu không cần thiết (rủi ro cao, ngoài scope).
   - Nếu không chắc, dừng lại và mô tả rõ trade-off trong PR description thay vì tự quyết
     định đổi trust_ramp logic.
4. UI hiện tại (bảng packet-level) giữ nguyên như 1 lớp tổng quan, nhưng mỗi dòng giờ có
   link/expand để xem chi tiết per-piece thay vì chỉ nút Approve tĩnh.

### Phần B — History view: xem lại các lần N7 đã chạy

1. Endpoint mới: `GET /api/admin/produce/runs` — list các lần N7 run trước (run_id, tenant,
   year/month/week, triggered_at, status, gate_ledger tổng hợp — VD tổng pass/fail theo
   từng gate F1/F3/F4/F5/F8/F9).
2. Endpoint mới (hoặc mở rộng `GET /run/{id}` đã có): trả đầy đủ gate_ledger từng piece
   trong run đó — dùng cho drill-down.
3. Trang mới (hoặc tab trong `/admin/produce`) hiển thị danh sách run lịch sử, click vào
   1 run để xem chi tiết (tương tự bảng trong `docs/claude_audit/AA-404-n7-run6-results.md`
   — dùng chính báo cáo đó làm tham khảo format hiển thị).
4. Test bằng dữ liệu 7 run thật đã có trong DB (run_id liệt kê trong file audit trên) —
   không cần trigger N7 mới để có dữ liệu test.

---

Test:
1. Unit test cho 2 endpoint mới (packets/{id}/pieces, produce/runs).
2. Integration test: xác nhận endpoint trả đúng dữ liệu piece thật từ `acp_deliver.pieces`
   (dùng 1 trong 7 run_id thật đã biết).
3. Frontend: xác nhận build không lỗi, type-check pass (Next.js).
4. Full suite pass, flake8 sạch (backend); lint sạch (frontend).

Sau khi done:
- `gh pr create`, KHÔNG tự merge (thay đổi UI quan trọng, ảnh hưởng luồng approve thật —
  Nghiệp cần review + tự tay test qua UI trước khi merge).
- PR description: mô tả rõ quyết định ở Phần A bước 3 (approve per-piece vs ramp mechanism)
  nếu có trade-off cần Nghiệp xác nhận.
- Screenshot hoặc mô tả rõ luồng UI mới trong PR description.
- Paste PR link + verify về Claude Chat.
- KHÔNG tự cập nhật Linear (để Claude Chat làm sau khi xem PR).
