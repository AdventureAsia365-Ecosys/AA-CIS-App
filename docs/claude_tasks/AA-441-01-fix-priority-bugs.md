## Task cho Claude Code: Sửa 6 bug ưu tiên từ audit AA-438/439

Mục tiêu: sửa 6 bug đã confirm qua audit thật (code + live query), không phải giả thuyết — không cần STEP0 investigate lại, đi thẳng build. Verify từng bug độc lập trước khi coi Done.

Repo: AA-CIS-App
Branch hiện tại: main
Tạo branch mới: yes: feature/aa-441-fix-priority-bugs
Merge vào: main (trunk-based, không còn develop)

Files cần đọc trước:
- `api/middleware/rate_limit_middleware.py` (hoặc file tương đương chứa `rate_limit_middleware`): vai trò ghi `shared.tenant_api_usage`, đang skip path không bắt đầu `/v1/`
- Router/service đọc "Run Health" trên `/admin/dashboard` (tìm bằng grep `acp_runs`): đang query `acp_shared.acp_runs` (0 row) thay vì `acp_shared.acp_v2_runs` (bảng thật đang ghi)
- `services/export/handler.py` hoặc nơi chứa `PublishedCatalogRepository.insert()`: câu `INSERT ... ON CONFLICT (tour_id) DO UPDATE` hiện chỉ update 4/18 cột
- Frontend `BrandTab.tsx` (tenant portal T0): đang gọi `/api/admin/brand-identity/upload` — cần đổi sang route `/api/tenant/*` đã proxy đúng (xem cách GET/POST hiện tại của cùng file đang gọi đúng route nào, dùng làm mẫu)
- Router xử lý reject trong Review Queue (`admin_pipeline.py` hoặc tương đương, tìm hàm reject/`review_status='rejected'`): không reset `generated_content.status` về khỏi `'hitl'`
- `api/routers/v1_tours.py` (`/v1/tours/pool` — hàm `browse_pool` theo audit AA-438-03): so sánh với `acp_contract.v_trip_registry` để copy đúng cách filter `master_status`/`deleted_at`

Context:
- Đây là 6 bug độc lập nhau về code path — có thể sửa tuần tự trong cùng branch, nhưng PHẢI verify riêng từng bug (không gộp verify chung 1 lần).
- Không đụng vào data hiện có (nhiều data test sẽ bị xóa ở Data Cleanup Plan sắp tới — không cần sửa data cũ, chỉ sửa code/schema đi tới).
- Đọc kỹ ADR-2026-038 nếu cần đối chiếu route/schema convention (Notion `3c3b8a41-ec5d-8123-911f-e0c308841e79`), nhưng không bắt buộc cho 6 bug này vì đều đã root-cause rõ.

Steps:

**1. Pipeline Health — middleware bỏ qua /admin/***
- Sửa `rate_limit_middleware` để ghi `shared.tenant_api_usage` (hoặc bảng phù hợp) cho path `/admin/*` (admin activity) — không chỉ `/v1/*`.
- Cân nhắc: có thể cần phân biệt admin vs tenant traffic trong cùng bảng (thêm cột hoặc dùng giá trị đặc biệt cho tenant_id/actor khi request là admin) — quyết định cách phân biệt trước khi sửa, KHÔNG đoán, hỏi lại nếu schema không rõ actor là ai.
- Verify: gọi 1 admin endpoint thật (VD `/admin/dashboard` hoặc bất kỳ `/admin/*` GET), query lại bảng xem có row mới ghi nhận không.

**2. Run Health — đọc bảng chết**
- Đổi query đang đọc `acp_shared.acp_runs` sang đọc `acp_shared.acp_v2_runs`.
- Verify: sau khi đổi, dashboard/API Run Health phải trả về đúng 12 run thật (hoặc số hiện tại) thay vì rỗng.

**3. `published_tours` UPSERT thiếu cột**
- Sửa `ON CONFLICT (tour_id) DO UPDATE SET ...` để cover đủ 18 cột tương ứng bảng `published_tours` (không chỉ 4 cột hiện tại) — đối chiếu schema thật của bảng để liệt kê đủ cột cần update khi republish.
- Verify: republish 1 tour test (không phải data thật cần giữ) 2 lần với nội dung subtitle/SEO khác nhau — confirm lần republish thứ 2 ghi đè đúng, không còn giữ giá trị cũ.

**4. T0 Upload Brand Guide 401**
- Sửa `BrandTab.tsx` để gọi đúng route `/api/tenant/*` (route đã proxy đúng, dùng chung pattern với GET/POST hiện có trong cùng file) thay vì `/api/admin/brand-identity/upload`.
- Verify: dùng tenant JWT thật (mint trong container hoặc tenant test có sẵn), gọi upload thật qua HTTP — xác nhận không còn 401, request tới được backend.

**5. Reject không reset status**
- Sửa hàm reject trong Review Queue: khi set `review_status='rejected'`, đồng thời reset `generated_content.status` (rời khỏi `'hitl'` — xác nhận giá trị đích hợp lý, VD `'rejected'` nếu enum cho phép, hoặc giá trị khác đã dùng cho case tương tự — kiểm tra CHECK constraint trước khi chọn giá trị).
- Verify: reject 1 tour hitl test → confirm `generated_content.status` đổi đúng, tour xuất hiện đúng chỗ (không còn "biến mất" khỏi mọi filter Review Queue).
- Lưu ý: có 2 row thật hiện tại đang ở trạng thái lỗi này — có thể sửa data 2 row đó cùng lúc (không thuộc Data Cleanup Plan vì đây là bug thật cần fix, không phải rác test) — xác nhận với Nghiệp qua comment trước khi đụng data thật nếu không chắc.

**6. T1 `/v1/tours/pool` thiếu filter**
- Thêm filter `master_status='active'` và `deleted_at IS NULL` vào query của `browse_pool()` (hoặc tên hàm thật trong `v1_tours.py`), theo đúng mẫu đã có ở `acp_contract.v_trip_registry`.
- Verify: trash/deactivate 1 tour test ở Master Content → confirm tour đó KHÔNG còn hiện ở `/v1/tours/pool` cho tenant.

Verify tổng: chạy pytest suite hiện có (không được để regression), + verify riêng từng bug như mô tả trên. Liệt kê rõ bằng chứng verify cho từng bug trong báo cáo cuối (không chỉ nói "đã sửa").

Sau khi done:
- Lưu CHÍNH file task prompt này (nội dung bạn đang đọc) vào `docs/claude_tasks/AA-441-01-fix-priority-bugs.md` trong repo (chưa có sẵn trong repo, chỉ đang ở outputs tạm của Claude Chat) — làm bước đầu tiên trước khi bắt tay sửa code, không để tới cuối.
- Lưu báo cáo kết quả thực thi vào `docs/implementation-notes/AA-441-fix-priority-bugs.md` (không phải claude_audit vì đây là thực thi, không phải audit).
- git commit -m "fix: 6 priority bugs from AA-438/439 audit (pipeline health, run health, published_tours upsert, T0 401, reject status, T1 pool filter) [AA-441]" && git push
- Paste verify result về Claude Chat
- Linear: AA-441 → giữ nguyên status, Claude Chat sẽ verify qua comment trước khi đổi Done (theo ADR-2026-037, merge không tự Done)
