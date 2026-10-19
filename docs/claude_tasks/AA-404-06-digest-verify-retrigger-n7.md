## Task cho Claude Code: digest verify + retrigger N7 lần 6 [AA-404]

Mục tiêu: 4 PR batch mới (#160 audit docs, #161 wire brand rubric writer-side, #162 atom
density validator F5, #163 hub/PAS + TikTok + atom-per-section opt-in) đã được Nghiệp merge
tay vào `main`. Trước khi tin deploy đã lên, verify digest ECR `:latest` khớp ECS task đang
chạy (đúng ADR-2026-037 pattern, không suy đoán từ "đã merge" là đủ). Sau đó chạy N7 lần 6
để đo tác động thật của các fix.

Đọc trước, KHÔNG lặp lại điều tra:
- `docs/claude_audit/AA-404-N0-N8-defense-layer-audit.md`
- `docs/implementation-notes/AA-404-F9-deep-dive.md`
- `docs/implementation-notes/AA-404-writer-side-brand-wire.md` (PR #161)
- Nội dung PR #162 (atom density validator) và #163 (batch 2: hub/PAS, TikTok, atom-per-section)

Repo: AA-CIS-App
Branch hiện tại: main (không tạo branch mới — đây là verify + trigger, không phải code change)

---

### Bước 1 — Digest verify (bắt buộc trước khi trigger N7)

1. Xác nhận `main` đã có đủ 4 PR (#160/#161/#162/#163) qua `git log --oneline -20`.
2. Xác nhận GitHub Actions đã build + push image mới lên ECR sau merge cuối cùng (`gh run
   list` cho workflow deploy, trạng thái success).
3. So sánh digest `image:latest` trên ECR với digest image ECS task đang chạy
   (`aws ecs describe-services` → `taskDefinition` → `describe-task-definition` → image
   digest, đối chiếu `aws ecr describe-images`).
4. Nếu digest KHÔNG khớp (ECS chưa pull image mới) — force new deployment:
   `aws ecs update-service --cluster aa-cis-dev-cluster --service aa-cis-dev-api
   --force-new-deployment --profile aa365-admin --region us-west-1`, chờ tới khi service
   ổn định (`aws ecs wait services-stable` hoặc poll `describe-services`).
5. Verify lại digest khớp — CHỈ tiếp tục Bước 2 khi đã khớp thật, không suy đoán.

### Bước 2 — Retrigger N7 lần 6

1. Dùng đúng flow đã dùng ở 5 lần trước (xem `docs/implementation-notes/AA-404.md` để
   biết cách trigger — qua UI `/admin/produce` hoặc API endpoint tương ứng).
2. Chạy N7 cho CÙNG slot/tenant đã dùng ở 5 lần chạy trước (để so sánh táo với táo — không
   đổi test subject giữa các lần).
3. Theo dõi tới khi hoàn tất (thành công hoặc fail) — ghi lại toàn bộ gate ledger như các
   lần trước.

### Bước 3 — Phân tích kết quả

1. So sánh với lần chạy thứ 5 (baseline): pass rate tổng, đặc biệt F9 (brand_seo_audit) và
   F5 (atom density, gate MỚI lần đầu xuất hiện trong run thật).
2. Nếu F5 (atom density) fail nhiều — đây là tín hiệu quan trọng: gate mới có đang hoạt
   động đúng thiết kế không, hay quá nghiêm/quá lỏng? Ghi nhận cụ thể, không tự sửa threshold.
3. Nếu vẫn còn piece fail F9 — so sánh chất lượng thất bại với lần 5 (có "gần pass hơn"
   không, hay vẫn cùng loại lỗi).
4. KHÔNG tự động chạy thêm repair/retry ngoài luồng pipeline tự nhiên đã có.

Output yêu cầu: báo cáo đầy đủ gate ledger 6 lần chạy so sánh cạnh nhau (bảng), kết luận
rõ ràng "cải thiện đáng kể / cải thiện nhẹ / không đổi / xấu đi" cho từng gate quan trọng
(F1, F5 mới, F8, F9), và khuyến nghị bước tiếp theo cho demo Ms. Thư 17/08.

Lưu báo cáo vào `docs/claude_audit/AA-404-n7-run6-results.md` (đúng quy tắc mới — không để
`/tmp`/scratchpad). Paste toàn bộ nội dung báo cáo trong response về Claude Chat, không chỉ
nói "đã lưu file".

KHÔNG tự cập nhật Linear — để Claude Chat làm sau khi xem kết quả.
