## Task cho Claude Code: STEP0 investigate — A4 Cross-Tenant Oversight

Mục tiêu: điều tra hạ tầng THẬT có sẵn trước khi build A4 (Cross-Tenant Oversight) — A4 chưa có code nào tồn tại (0% built), cần biết chính xác cái gì đã có, cái gì phải xây mới, trước khi soạn task build. KHÔNG viết code, KHÔNG sửa gì — thuần investigate.

Repo: AA-CIS-App
Branch hiện tại: main
Tạo branch mới: yes: feature/aa-437-a4-step0-investigate (giữ pattern nhánh audit trước, commit local, chưa push tới khi có task build tổng hợp)
Merge vào: main (không áp dụng phiên này — investigate only)

Files cần đọc trước:
- ADR-2026-038 mục 0.1 (Notion `3c3b8a41-ec5d-8123-911f-e0c308841e79`) — nguồn thiết kế A4 đầy đủ nhất
- `docs/claude_audit/AA-436-*.md` — báo cáo T3 auto-pass, nơi `review_queue`/`escalate_detail` được ghi
- `services/*/trust_ramp.py` — đã đọc ở AA-439-06/07, cần đọc lại tập trung vào A4 use case (không phải T8)
- Comment Linear AA-255→259 (Command Center backlog) — xem đã có schema/design nào cho oversight dashboard chưa

Context:
- A4 KHÔNG phải content gate — vai trò giám sát hậu-kiểm, có khả năng can thiệp (flag/suspend/force-unpublish), không chặn trước publish.
- 2 use case đã xác nhận cần có (theo ADR mục 0.1 + memory.md S155 "phiên sau bắt đầu từ đâu"):
  (a) đọc `review_queue`/`escalate_detail` — T3 auto-pass log theo tenant, để AA phát hiện pattern lỗi lặp lại (VD FORBIDDEN_WORD fail nhiều lần = prompt/brand-rule hệ thống sai, không phải lỗi riêng tenant)
  (b) Trust Ramp dashboard — xem tenant nào đang ở mức nào, khả năng đẩy `suggest_ramp_transition()` thật (hiện 0 caller, chỉ staff tay đổi qua `confirm_ramp_transition()`)
- Nối vào Command Center backlog có sẵn (AA-255→259) theo ý định ban đầu — cần xác nhận có tái dùng được gì không, hay Command Center đó scope khác hẳn (infra/cost, không phải content oversight).

Steps:

1. **Đọc schema thật của `review_queue`** — liệt kê đầy đủ cột, đặc biệt `escalate_detail` (JSON shape thật, không suy đoán từ code viết). Query trực tiếp DB dev, lấy vài row thật (T3 auto-pass log từ AA-436 đã sống production) làm ví dụ cụ thể.

2. **Đọc schema + code thật của Trust Ramp** — bảng nào lưu trạng thái ramp hiện tại của mỗi tenant, `suggest_ramp_transition()` + `confirm_ramp_transition()` đầy đủ logic, `audit_log` bảng ghi `publish_mode_transition` (đã biết 0 row từ AA-439-06 — verify lại còn đúng không, có thể đã đổi từ lúc đó).

3. **Đọc AA-255→259 (Command Center backlog)** — xác nhận scope thật (infra/cost hay có phần content/tenant oversight nào không), có FE shell/route nào đã tồn tại không (kể cả chưa hoàn thiện) mà A4 có thể gắn vào thay vì xây route mới hoàn toàn.

4. **Kiểm tra route/FE admin hiện có** — có route `/admin/*` nào gần giống oversight dashboard chưa (kể cả không đúng tên), tránh xây trùng.

5. **Xác định gap thật cần build** — liệt kê rõ: (a) endpoint nào cần mới, (b) FE page/route nào cần mới, (c) có cần thêm cột/bảng nào không (VD nếu muốn filter theo pattern lỗi lặp lại, có cần aggregate view hay group-by trực tiếp trên `review_queue` là đủ).

6. **Đề xuất scope cho task build tiếp theo** (không tự quyết định kiến trúc — chỉ liệt kê phương án nếu có điểm cần quyết định, giống cách AA-436 STEP0 đã làm) — đặc biệt nếu phát hiện gì đó thay đổi giả định ban đầu của ADR (như trường hợp T3 từng phát hiện route `/portal/t3-review` chưa từng tồn tại).

Verify: không cần verify code (không có code viết ra) — verify là bằng chứng thật cho từng phát hiện (query result, code path thật, comment Linear liên quan).

Sau khi done:
- Lưu CHÍNH file task prompt này vào `docs/claude_tasks/AA-437-01-step0-a4-investigate.md` trước khi bắt tay điều tra.
- Lưu báo cáo investigate vào `docs/claude_audit/AA-437-01-a4-step0-audit.md` (đây là audit/investigate, không phải implementation).
- Paste tóm tắt kết quả về Claude Chat.
- Linear: AA-437 giữ nguyên status Backlog — Claude Chat sẽ đọc báo cáo, quyết định kiến trúc cùng Nghiệp, rồi soạn task build riêng (AA-437-02).
