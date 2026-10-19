# AA-469 — Redesign T-series (5 Việc) — task log

Linear: AA-469.

**Status: STEP0 toàn bộ 5 việc AA-469 đã hoàn tất (Việc 1-5 đều Done) — sẵn sàng tổng hợp thiết kế cross-Việc.**

## Audit files

- Việc 1 (tách T4/T5 thành 2 trigger độc lập): `docs/claude_audit/AA-469-viec1-step0-t4-t5-split-investigation.md`
- Việc 2 (T7 — edit sau finalize + xem lịch sử quý/tháng trước): `docs/claude_audit/AA-469-viec2-step0-t7-edit-history-investigation.md`
- Việc 3 (T1 pool chỉ hiện 28/71 tour): `docs/claude_audit/AA-469-viec3-step0-28-71-tour-bug-investigation.md` — **STEP0 Done, root cause = `browse_pool()`'s `master_status='active'` filter (đúng thiết kế, AA-441) loại 43 tour có `master_status IN ('inactive','trashed')`; 39/43 là `'inactive'` và nguồn gốc KHÔNG giải thích được bằng bất kỳ code path nào (DB default=`active`, INSERT không đụng cột này, cả 2 admin toggle endpoint đều 0 audit event) — unexplained data state, không phải bug pagination/RLS/JOIN, cần hỏi Nghiệp về nguồn gốc trước khi quyết fix.**
- Việc 4 (xác nhận chuỗi T8→T9→T10→T11): `docs/claude_audit/AA-469-viec4-step0-t8-t11-chain-investigation.md` — **STEP0 Done. (c) DFS/PAA KHÔNG vào angle generation ở bất kỳ layer nào (gap thật, chưa build). (d) Blog chưa có multi-atom input, vẫn dùng chung cơ chế 1-atom với 7 channel khác (cần thiết kế riêng). (a) claim "fail 2 lần vẫn ép ra T11 kèm error tag" SAI — T11's endpoint chặn cứng `status != 'approved'`, `held` piece cấu trúc không thể tới T11. (b) xác nhận đúng: T8-T10 là linear wizard 1 trang, back-navigation duy nhất là "Start over" (xoá sạch); T11 còn tách biệt hơn — không cùng flow/component với T8-T10 luôn.**
- Việc 5 (A4 feedback-loop reach across LLM steps): `docs/claude_audit/AA-469-viec5-step0-a4-feedback-loop-investigation.md` — **STEP0 Done. A4 thật có 4 endpoint (không phải 2 — thêm `GET/POST publish-log` từ AA-455). T2 (rewrite→T3 QA-gate): structured error CÓ + ĐÃ A4-reachable (không phải gap — `escalate_detail` ghi thẳng vào bảng A4 đã đọc). T5 (atomize): error tồn tại trong code nhưng chỉ log CloudWatch, chưa từng persist — gap sâu nhất. T6/T7 "scoring": KHÔNG liên quan LLM cả 2 (cả `score_distinctiveness()` và `score_dfs_relevance()` đều pure/deterministic) — giải xong uncertainty của Nghiệp. T9/T10: structured error tốt nhất (`content_piece.gate_ledger`/`held_reason`) nhưng ZERO A4 path — gap dễ vá nhất vì dữ liệu đã sẵn, chỉ thiếu 1 route đọc. Không có shared error-log pattern nào giữa các stage.**

## Note

Nghiệp yêu cầu STEP0 đầy đủ cho cả 5 Việc trước khi bất kỳ Việc nào được thiết kế/build — **không
bắt đầu build 1 Việc đơn lẻ nào trước khi cả 5 đã được điều tra**, để tránh xung đột giữa các Việc.
Xung đột đã phát hiện qua STEP0 (đã xác nhận đầy đủ, cả 5 Việc đã điều tra xong):
- **T5/atomize chạm cả Việc 1 VÀ Việc 5 — xác nhận thật, không còn là khả năng.** Việc 1 muốn tách
  T4/T5 thành 2 trigger; Việc 5 phát hiện T5's error hiện chỉ log CloudWatch, chưa từng persist —
  nếu Việc 1 build trước (thêm nút Atomize riêng) mà không tính luôn việc Việc 5 cần T5 ghi lỗi
  vào DB, sẽ phải sửa lại `run_t5_atomize()` 2 lần. Nên gộp thiết kế 2 việc này khi build.
- T7 (quarter plan) chạm Việc 2 (edit/history) — KHÔNG chạm Việc 4/5 như lo ngại ban đầu: Việc 4
  xác nhận T8's angle generation không đọc DFS/seo_context ở bất kỳ layer nào (gap riêng, độc lập
  khỏi T7), và Việc 5 xác nhận T7's `compute_quarter_plan()`/scoring hoàn toàn không dùng LLM nên
  không nằm trong phạm vi feedback-loop của Việc 5. Lo ngại này đã được loại trừ.
- **Mới phát hiện ở Việc 5**: Việc 4 (T8-T11 UX flow) và Việc 5 (A4 feedback loop) cùng chạm
  `content_piece`/T9/T10 — nếu Việc 4 build lại UI T8-T11 thành 1 flow liền mạch có back-nav,
  nên tính sẵn chỗ hiển thị dữ liệu `gate_ledger`/`held_reason` mà Việc 5 sẽ cần A4 đọc, tránh
  2 lần đổi cùng 1 vùng dữ liệu.

## Commit decision (Nghiệp, 29/08/2026)
Hold off committing docs/claude_audit/ + docs/claude_tasks/ files for AA-469 until
ALL 5 Việc have completed STEP0. Then bundle into a single docs-only PR (same pattern
as PR #239) rather than one PR per Việc. Files stay local/untracked-in-git until then
— this is intentional, not an oversight if seen in a future session.

Phát hiện phụ trong lúc bàn Việc 2 — mô hình atom/angle/piece reuse N:N, cần issue Linear riêng
(chưa tạo), STEP0 riêng đã chạy, xem docs/claude_audit/atom-angle-piece-reuse-model-investigation.md

AA-494 (angle_gate_request.status redesign, phát sinh từ atom/angle/piece reuse investigation ở
trên) — STEP0 bản đồ phụ thuộc đầy đủ của status column (backend+frontend, 2 kịch bản so sánh chi
phí) đã chạy, xem docs/claude_audit/AA-494-status-dependency-map.md

AA-494 design document complete — docs/claude_tasks/AA-494-design-atom-angle-piece-reuse.md.
Ready for build next session. Build order and open questions inside.

AA-494 year-scope legacy code STEP0 done — old N4 runway.py is LIVE (not dead), already wired
into T7's real v1_planning.py + T8's angle-gate + trip_reallocation.py; Decision 6 of the design
doc needs revision (the "connector layer" it calls undesigned already exists live in
allocator.py::compute_slot_grid()). See docs/claude_audit/AA-494-year-scope-legacy-code-investigation.md

AA-494 Decision 6 follow-up STEP0 done — compute_slot_grid() DOES hard-lock atom+week together
(round-robin, arbitrary), but T9 (content_writing/angle_gate) never reads Slot.week at all, so
the live tenant write path already has the "any week this month" flexibility Nghiệp wants; the
lock only matters for N7's still-untriggered automated weekly pipeline. See
docs/claude_audit/AA-494-decision6-slot-grid-week-flexibility-investigation.md
