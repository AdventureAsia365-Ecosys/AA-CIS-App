## Task cho Claude Code: fix 3 lệch chuẩn writer/judge còn lại (mục 4, 5, 6 — audit N0-N8)

Mục tiêu: Audit N0-N8 defense-layer (`docs/claude_audit/AA-404-N0-N8-defense-layer-audit.md`
— đọc file này TRƯỚC, đừng lặp lại điều tra) tìm ra 3 cặp writer/judge lệch chuẩn khác
ngoài F9, cùng bản chất: writer không được instruct đủ chi tiết, judge lại chấm theo tiêu
chuẩn đầy đủ hơn. Fix cả 3 trong 1 PR (cùng nhóm vấn đề, cùng pattern sửa).

Đọc trước, KHÔNG lặp lại điều tra:
- `docs/claude_audit/AA-404-N0-N8-defense-layer-audit.md` (toàn bộ).
- `docs/implementation-notes/AA-404-F9-deep-dive.md` (để hiểu pattern gốc).

Repo: AA-CIS-App
Branch mới: `feature/aa-404-writer-judge-alignment-batch2`
Merge vào: main

---

### Mục 4 — hub/PAS framework: bare-label vs FRAMEWORK_RUBRICS đầy đủ

Trước đây CỐ Ý HOÃN việc đưa full framework spec vào writer prompt (E1/E2) — quyết định
này giờ có dữ liệu: F8 repair success chỉ 14.6% cho case liên quan (thấp bất thường so với
gate khác). Review lại quyết định hoãn:

1. Tìm chỗ E1 (outline)/E2 (draft) hiện đang chỉ đưa writer bare label kiểu
   `"FRAMEWORK: hub"` — xác nhận đúng vị trí code.
2. Tìm `FRAMEWORK_RUBRICS` đầy đủ mà F8 judge đang dùng để chấm (hub/skyscraper, PAS, BAB,
   AIDA, 4Ps... theo CONTEXT.md §4 framework table).
3. Đưa ĐẦY ĐỦ framework spec liên quan (không chỉ tên) vào writer prompt (E1 outline +
   E2 draft) — writer cần biết CHÍNH XÁC cấu trúc framework đang viết (VD hub cần "skyscraper
   structure + FAQ always", PAS cần "Problem-Agitate-Solve", AIDA cần "Attention-Interest-
   Desire-Action woven into body, NO FAQ block").
4. Xác nhận: quyết định hoãn trước đây có lý do cụ thể gì đã ghi lại không (tìm trong
   implementation-notes cũ) — nếu có lý do chính đáng vẫn còn đúng (VD lo ngại prompt quá
   dài), NÊU RÕ trong PR description để Nghiệp cân nhắc, không tự ý bỏ qua lý do gốc.

### Mục 5 — TikTok hook_beats_payoff: writer thiếu instruct "beats"/"payoff"

1. Tìm E3 (adapt.py, tiktok channel) hiện chỉ yêu cầu dòng HOOK ≤15 từ.
2. Tìm F8 judge (`hook_beats_payoff` rubric) đang chấm CẢ "timed beats" và "payoff lands" —
   2 tiêu chí writer chưa từng được yêu cầu tạo ra.
3. Mở rộng E3 tiktok prompt: thêm instruction rõ ràng cho "timed beats" (theo CONTEXT.md
   §5 E3: "TikTok (shoot kit: hook / timed beats / payoff / paste-ready caption + on-screen
   text / shoot notes / reuse notes)") và "payoff" — writer cần tạo đủ 3 phần, không chỉ hook.
4. Lưu ý: audit ghi "chưa có failure thật" cho gap này — vẫn nên fix để đóng trước khi có
   failure, không cần chờ bằng chứng.

### Mục 6 — atom-per-section: F1 grounding không theo section

1. Tìm E1 (outline) — atom được gán CỤ THỂ theo từng section (outline schema có atom_ids
   per section, theo CONTEXT.md §5 E1: "outline schema (H2s, atoms per section...)").
2. Tìm F1_grounding hiện tại — `valid_ids` đang lấy TOÀN BỘ atom của piece (không phân biệt
   section nào dùng atom nào), thay vì kiểm tra ĐÚNG atom đã gán cho ĐÚNG section đó.
3. Sửa F1_grounding: khi kiểm tra 1 section, chỉ coi hợp lệ các `[R:atom_id]` đã được gán
   CHO SECTION ĐÓ trong outline (không phải toàn bộ atom của piece) — siết chặt đúng theo
   spec gốc "every section ≥1 atom or explicitly marked connective" (CONTEXT.md §5 E1 DET
   validation).
4. Cẩn thận: đây là thay đổi SIẾT CHẶT một gate đang chạy — có rủi ro làm tăng fail rate ở
   nơi trước đây pass nhầm. Chạy thử trên dữ liệu piece cũ (không phải trigger N7 mới) để
   ước tính tác động trước khi merge, nếu có thể (đọc `body_tagged` của piece cũ, giả lập
   chạy gate mới qua nó, so sánh kết quả cũ/mới) — báo cáo rõ trong PR nếu tìm được impact.

---

Test cho cả 3 mục:
1. Unit test riêng cho mỗi thay đổi.
2. Full suite pass, flake8 sạch.
3. Nếu mục 6 có rủi ro tăng fail rate — thêm test case cụ thể minh hoạ.

Sau khi done:
- `gh pr create`, KHÔNG tự merge (nhiều thay đổi gộp, đặc biệt mục 6 siết chặt gate đang
  chạy — Nghiệp cần review kỹ trước khi merge).
- PR description: 3 phần rõ ràng theo mục 4/5/6, mỗi phần nêu rõ thay đổi + rủi ro (nếu có).
- Paste PR link + verify về Claude Chat.
- KHÔNG tự chạy N7, không tự cập nhật Linear.
