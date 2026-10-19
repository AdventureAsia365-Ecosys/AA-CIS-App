## Task cho Claude Code: AA-351 tiếp — accept EULA GPT-5.6 trên acc3, thử làm judge F8/F9

Mục tiêu: AA-351 (khảo sát) xác nhận GPT-5.6 Sol/Terra/Luna khả dụng trên cả 3 account
nhưng cần accept model agreement (EULA) trước khi invoke được. AA-382 vừa xác nhận bằng
dữ liệu thật: Nova Pro (judge hiện tại cho F8/F9) tự mâu thuẫn giữa các vòng repair — flag
câu MỚI mỗi vòng dù câu cũ đã sửa đúng hướng dẫn, có trường hợp câu bị flag đọc lại đạt
chuẩn "GOOD" của chính rubric. Nghi ngờ liên quan tới bias đã ghi nhận ở AA-339 (Nova Pro
chấm 2 model "họ chất lượng cao" gần nhau bị lệch 17/29 tour). Thử GPT-5.6 làm judge thay
Nova Pro cho F8/F9 — vì là vendor khác hẳn writer (Claude) VÀ khác Nova, giảm rủi ro bias
theo cùng họ.

Repo: AA-CIS-App
Branch hiện tại: main (verify bằng `git branch --show-current`)
Tạo branch mới: yes — `feature/aa-351-gpt56-judge-trial`
Merge vào: main

**LƯU Ý — có thể chạy song song với AA-416-01 (fix event loop) trên session khác.** Rủi ro
đụng chung file `invoke_judge()` trong `shared/llm_client/` — PHẢI `git pull origin main`
trước khi tạo branch và trước khi push. Nếu AA-416-01 đã merge trước (đổi cách GỌI hàm
sang asyncio.to_thread), rebase để giữ cả 2 thay đổi (cách gọi mới CỘNG model mới) — không
ghi đè lẫn nhau. Nếu conflict phức tạp, DỪNG và báo Nghiệp.

BƯỚC 0: lưu file task prompt này vào `docs/claude_tasks/AA-351-02-gpt56-judge-f8-f9.md`
commit riêng đầu tiên trước khi code.

Files cần đọc trước:
- `docs/claude_audit/AA-351-gpt-bedrock-3accounts.md` — kết quả khảo sát: GPT-5.6 Sol/
  Terra/Luna khả dụng cả 3 account (acc1/2/3), cần accept EULA, giá ~$5.5/$33 per 1M input/
  output token.
- `docs/implementation-notes/AA-382-repair-rubric-context.md` — bằng chứng cụ thể Nova Pro
  tự mâu thuẫn giữa các vòng repair (flag câu mới mỗi vòng dù đã sửa đúng).
- `shared/llm_client/client.py`, `shared/llm_client/bedrock_satellite.py` — nơi định nghĩa
  `invoke_judge()` hiện tại (dùng Nova Pro qua acc2, RPM=25).
- Node judge F8/F9 trong graph N7 (`services/acp_produce/`).

Context — QUYẾT ĐỊNH ĐÃ CHỐT (không cần hỏi lại):
- Account dùng để accept EULA: **acc3** (`[acc3 id]`, profile `nghiep_aa365`) — satellite
  chính hiện tại, có fund $250 riêng, không đụng production acc2. ĐÂY LÀ HÀNH ĐỘNG KHÓ ĐẢO
  NGƯỢC (accept model agreement) — làm đúng 1 lần trên acc3, xác nhận qua
  `aws bedrock list-foundation-model-agreement-offers` trước và sau để chắc chắn chỉ tác
  động tới acc3.
- Đây là THỬ NGHIỆM (feature-flagged hoặc branch riêng), KHÔNG thay Nova Pro trong production
  ngay — cần so sánh song song trước khi quyết đổi hẳn.

**QUYẾT ĐỊNH BỔ SUNG trong session này (Nghiệp chọn qua câu hỏi trực tiếp, trước khi accept
EULA):** Sol/Terra/Luna hoá ra là 3 agreement/rate card TÁCH BIỆT (không chung 1 agreement
như task gốc giả định) — Sol $5.5/$33, Terra $2.2/$13.2, Luna $0.22/$1.32 mỗi 1M token
in/out (standard tier, xác nhận qua `list-foundation-model-agreement-offers` trên acc3,
16/08/2026). Nghiệp chọn **chỉ accept Sol** (flagship, không phải cả 3) — xem
`docs/implementation-notes/AA-351-gpt56-judge-trial.md` mục Decisions.

Steps:
1. Accept model agreement cho GPT-5.6 (Sol, Terra, Luna — cả 3 variant nếu cùng 1 agreement,
   xác nhận qua `list-foundation-model-agreement-offers` trước) trên acc3 CHỈ, dùng
   `aws bedrock create-foundation-model-agreement` hoặc API tương đương xác nhận từ khảo
   sát AA-351.
2. Verify invoke thật thành công sau khi accept — 1 request test đơn giản, xác nhận model ID
   chính xác, response hợp lệ.
3. Thêm judge model MỚI (GPT-5.6, gọi qua acc3) làm LỰA CHỌN THAY THẾ cho `invoke_judge()`
   — dùng feature flag/env var (VD `JUDGE_MODEL=nova_pro|gpt56`) để chọn model judge, KHÔNG
   xoá/thay thế Nova Pro code path, chỉ thêm nhánh mới song song.
4. Đảm bảo prompt/rubric gửi cho GPT-5.6 judge giữ NGUYÊN Y HỆT rubric hiện dùng cho Nova
   Pro (F8 FRAMEWORK_RUBRICS, F9 brand/SEO rubric + good/bad anchor) — để so sánh công bằng,
   không đổi tiêu chí chấm, chỉ đổi model chấm.
5. Áp dụng route qua satellite acc3 (không phải acc2) cho judge call mới này — dùng lại
   pattern `invoke_claude(..., account="acc3")` đã có trong `bedrock_satellite.py` nếu cấu
   trúc tương tự áp dụng được cho OpenAI/GPT client, hoặc viết client tương đương nếu format
   API khác hẳn Bedrock Anthropic (GPT trên Bedrock có converse API riêng — kiểm tra kỹ).

Verify — SO SÁNH SONG SONG, không kết luận vội:
1. Chạy N7 thật 1 tuần MỚI (chưa từng chạy) — DÙNG feature flag chạy pipeline 2 LẦN trên
   CÙNG bộ content đã sinh (giữ nguyên draft, chỉ đổi judge model chấm F8/F9): 1 lần với
   Nova Pro (baseline hiện tại), 1 lần với GPT-5.6 — so sánh:
   a. Pass rate F8/F9 có khác nhau không.
   b. Với piece bị cả 2 flag fail — held_reason có nhất quán/cụ thể hơn không (ít "flag câu
      mới mỗi vòng" hơn).
   c. Cost per judge call (GPT-5.6 đắt hơn Nova Pro nhiều — ước tính chênh lệch thật).
   d. Latency per judge call.
2. Nếu không có cách chạy "cùng content, 2 judge" trong 1 lần N7 (do kiến trúc hiện tại
   không hỗ trợ) — ít nhất chạy 2 N7 run riêng biệt, gần nhau về thời gian, ghi rõ đây là
   so sánh gián tiếp (khác content, không hoàn toàn công bằng), không khẳng định chắc chắn.
3. KHÔNG tự ý đổi Nova Pro → GPT-5.6 trong production dù kết quả tốt — chỉ báo cáo số liệu
   so sánh, để Nghiệp quyết có đổi hẳn hay không.
4. Lưu report đầy đủ (bảng so sánh pass rate, cost, latency, held_reason mẫu) vào
   `docs/implementation-notes/AA-351-gpt56-judge-trial.md`.

Git context:
- Repo: AA-CIS-App
- Current branch: main
- Tạo branch mới: yes — feature/aa-351-gpt56-judge-trial
- Merge vào: main (feature-flagged, an toàn merge dù chưa quyết dùng chính thức — default
  flag vẫn là Nova Pro, không đổi hành vi production hiện tại)
- Sau khi done: `git pull origin main` trước, resolve nếu cần, rồi
  `git add . && git commit -m "feat: add GPT-5.6 as alternative judge for F8/F9, feature-flagged [AA-351]" && git push`,
  tự `gh pr create` (KHÔNG tự merge)

Sau khi done:
- Paste PR link + bảng so sánh Nova Pro vs GPT-5.6 (pass rate, cost, latency) về Claude Chat
- Trước khi ghi Linear, tự `get_issue` xác minh AA-351 title khớp bối cảnh trước khi post
  kết quả thử nghiệm — không tự đổi status, không tự quyết đổi judge production.
