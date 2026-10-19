# Task prompt: AA-351 — khảo sát GPT trên Bedrock, test cả 3 account (acc1/acc2/acc3)

(Saved verbatim as Step 0 of the task, 2026-08-16)

## Task cho Claude Code: AA-351 — khảo sát GPT trên Bedrock, test cả 3 account (acc1/acc2/acc3)

Mục tiêu: ĐÂY LÀ TASK KHẢO SÁT, KHÔNG PHẢI TASK CODE PRODUCTION. Xác định model GPT
(GPT-5.4/5.5/5.6 Sol/Terra/Luna, gpt-oss-120b/20b) có invoke được trên Bedrock ở CẢ 3 AWS
account đang dùng trong dự án hay không — mở rộng phạm vi so với issue gốc AA-351 (chỉ
định acc2), theo yêu cầu Nghiệp kiểm tra đủ cả acc1 và acc3.

Repo: AA-CIS-App (dùng để chạy script kiểm tra qua ECS Exec pattern nếu cần, nhưng đây là
task nghiên cứu — không tạo tính năng mới trong repo)
Branch hiện tại: main
Tạo branch mới: no (khảo sát, không code production — nếu cần script tạm để test invoke,
viết trong `/tmp` phía Claude Code session của bạn, KHÔNG commit script tạm vào repo, chỉ
commit báo cáo kết quả)

**LƯU Ý — chạy song song với AA-415-03 và AA-382-01 trên các session Claude Code khác.**
Task này KHÔNG code, chỉ đọc/gọi AWS API để khảo sát — rủi ro đụng file với 2 task kia gần
như bằng 0. Chỉ cần `git pull origin main` trước khi commit báo cáo cuối, vì 2 task kia có
thể đã merge PR trong lúc bạn khảo sát.

Context — Account Map (xem đầy đủ ở skill `ai-nghiep` §4, đã cập nhật 12/08/2026):
- **acc2** (`[acc2 id]`, profile `aa365-admin`, region `us-west-1`) — app chính, ECS/
  RDS/S3, hầu hết việc hàng ngày. ĐÃ BIẾT chặn hoàn toàn Anthropic (channel-program,
  `ValidationException: channel program accounts`) — cần xác nhận GPT có cùng số phận không.
- **acc3** (`[acc3 id]`, profile `nghiep_aa365`, region `us-west-1`) — Bedrock satellite
  CHÍNH (mới từ 12/08, thay acc1), fund $250, real cost — ưu tiên interactive + Batch.
- **acc1** (`[acc1 id]`, profile `pqnghiep-admin`, region `us-west-1`) — Bedrock
  satellite FALLBACK (khi acc3 lỗi/hết fund), real cost.
- Judge hiện tại cho N7/F8-F9 dùng Nova Pro — đang nghi bị position bias mạnh khi chấm 2
  bài "họ chất lượng cao" gần nhau (AA-339: 17/29 tour bias_flip). GPT-4.1 hiện dùng cho S1
  KHÔNG chạy trên Bedrock — gọi thẳng OpenAI API ngoài AWS (ADR-2026-031, T3 last-resort).
  Câu hỏi cần trả lời: nếu GPT-5.x invoke được trên Bedrock ở account nào đó, có thể dùng
  làm judge F8/F9 thay Nova Pro — lợi thế: (a) ở trong AWS (data residency tốt hơn so với
  gọi OpenAI ngoài), (b) vendor khác hẳn writer (Claude) VÀ khác Nova — giảm rủi ro bias
  theo đúng nguyên tắc "judge không nên cùng họ với writer".

Steps:
1. Verify quyền truy cập cả 3 profile trước khi bắt đầu:
   `aws sts get-caller-identity --profile aa365-admin --region us-west-1`
   `aws sts get-caller-identity --profile nghiep_aa365 --region us-west-1`
   `aws sts get-caller-identity --profile pqnghiep-admin --region us-west-1`
   Nếu profile nào cần MFA tương tác mà không có sẵn — báo rõ, không bỏ qua âm thầm, hỏi
   Nghiệp hỗ trợ nếu cần cho account đó.
2. Với MỖI account (acc1, acc2, acc3), dùng Bedrock API (`list-foundation-models` +
   `invoke-model` thử thật, theo đúng phương pháp AA-335 đã dùng để kiểm kê catalog) —
   kiểm tra các model GPT sau CÓ invoke được không:
   - GPT-5.4, GPT-5.5 (GA từ tháng 6/2026)
   - GPT-5.6 Sol/Terra/Luna (GA từ tháng 7/2026)
   - gpt-oss-120b, gpt-oss-20b (open-weight, lẽ ra có sẵn mọi Bedrock account từ 2025)
   Ghi rõ: invoke thành công, hay lỗi gì cụ thể (VD `ValidationException: channel program
   accounts` giống Anthropic, hay lỗi khác như quota/region-not-supported).
3. Nếu invoke được ở account nào — đo thêm (theo format bảng AA-335):
   a. Giá thật (on-demand rate) cho account đó/region đó.
   b. Rate limit thật (Service Quotas API, RPM/TPM).
   c. Có hỗ trợ Batch inference hay Prompt Caching không.
4. Nếu KHÔNG invoke được ở account nào — xác nhận lỗi cụ thể là gì, có phải cùng cơ chế
   channel-program chặn Anthropic không, hay lý do khác (VD GPT chưa GA ở us-west-1).
5. So sánh kết quả 3 account — có khả năng khác nhau theo account (VD acc2 chặn nhưng
   acc1/acc3 satellite lại được, tương tự cách acc1/acc3 hiện dùng cho Claude satellite).

Verify — vì đây là task khảo sát, không cần deploy:
1. Không tạo PR code production. Nếu cần script test tạm, xoá sau khi xong (không để lại
   rác trong `/tmp` hay repo).
2. Lưu báo cáo đầy đủ vào `docs/claude_audit/AA-351-gpt-bedrock-3accounts.md` — bảng kết
   quả invoke theo từng account × từng model, giá/rate-limit nếu có, kết luận rõ ràng.
3. Kết thúc bằng khuyến nghị: account nào (nếu có) khả thi để thử GPT-5.x làm judge thay
   Nova Pro cho F8/F9 — nhưng KHÔNG tự đổi judge production, đây chỉ là khảo sát (đúng
   ràng buộc gốc của AA-351: "KHÔNG đổi judge production hiện tại cho tới khi có quyết
   định rõ ràng từ Nghiệp").

Git context:
- Repo: AA-CIS-App
- Current branch: main
- Không tạo branch/PR code — chỉ commit trực tiếp báo cáo vào main qua
  `git pull origin main && git add docs/claude_audit/AA-351-gpt-bedrock-3accounts.md docs/claude_tasks/AA-351-01-gpt-bedrock-3accounts.md && git commit -m "docs: AA-351 GPT-on-Bedrock survey, 3 accounts [AA-351]" && git push`

Sau khi done:
- Paste báo cáo đầy đủ (bảng 3 account × model, kết luận) về Claude Chat
- Trước khi ghi Linear, tự `get_issue` xác minh AA-351 title khớp bối cảnh trước khi post
  kết quả khảo sát — không tự đổi status, để Nghiệp đọc và quyết bước tiếp theo (có nên
  thử GPT-5.x làm judge F8/F9 hay không).
