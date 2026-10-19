# AA-547 — Điều tra lại AA-469's 5 việc: trạng thái THẬT hôm nay (06/09/2026)

Không code, không sửa. Mọi kết luận có bằng chứng file:line/query/commit cụ thể.

## Phát hiện bao trùm cả Part 1 và Part 2 (mẫu lặp lại 2 lần)

**AA-469's Việc 1 và Việc 2 ĐÃ ĐƯỢC BUILD ĐÚNG, ĐÃ LIVE-VERIFY THẬT (29-30/08/2026) — nhưng sau
đó ĐÃ BỊ XOÁ bởi 2 quyết định kiến trúc LỚN HƠN, MUỘN HƠN (AA-526 04-05/09, AA-519 03/09), không
phải vì AA-469 làm sai.** Đây không phải "chưa build" — là "build đúng rồi, kiến trúc đổi hướng
sau đó khiến nó không còn chỗ đứng". Quan trọng để Claude Chat không thiết kế nhầm dựa trên câu
hỏi "sao Việc 1/2 chưa xong" — câu hỏi đúng là "kiến trúc đã đổi, cần thiết kế LẠI theo hướng mới".

## Phần 1 — Việc 1 (tách T4/T5): đã build, đã bị AA-526 xoá bỏ hoàn toàn

`find "frontend/app/(tenant)/portal" -name page.tsx"` — **không có trang T5 riêng nào** (15 trang
thật, không có `t5-*`).

**Lịch sử thật (đọc trực tiếp AA-469 Linear issue)**: Việc 1 SHIPPED 29/08 (PR #259, task def
:166, live-verify browser+HTTP thật đầy đủ) — không phải trang riêng, mà 1 NÚT "Atomize" trong
`CatalogTab.tsx` (route `/portal/t4-pool`) + endpoint mới `POST /v1/tours/versions/{version_id}/
atomize`, tenant tự bấm.

**Đã bị AA-526 (04-05/09) xoá hoàn toàn**, xác nhận 3 lớp bằng chứng:
1. `grep -c atomize frontend/app/(tenant)/portal/_components/CatalogTab.tsx` = **0** — nút không
   còn tồn tại trong code.
2. `grep "versions/{version_id}/atomize" api/routers/v1_tours.py` — chỉ còn 1 dòng COMMENT (dòng
   649) nhắc lại route CŨ đã bị xoá, không còn `@router.post` decorator nào cho path này.
3. `v1_tours.py` dòng 649 tự ghi rõ: *"atomize (T5) no longer runs on tenant-rewritten content at
   all, here or via the (now-removed) standalone POST /v1/tours/versions/{version_id}/atomize
   AA-469 Việc 1 introduced"* — chính code thừa nhận đã xoá route AA-469 xây.

**Nguyên nhân**: AA-526 (Nghiệp, 04/09/2026) chuyển hẳn Atomize (T5) sang chạy 1 lần, platform-wide,
tại A3 (`services/export/handler.py`, ngay khi tour trở thành Master Content) — tenant **không
còn atomize nữa, không thấy atom, không tự bấm gì cả** ("tenants never create or see atoms
directly anymore, curation moves to AA-admin" — comment gốc AA-526). Việc 1 nguyên bản ("tenant tự
bấm atomize như 1 hành động riêng") **không còn ý nghĩa để build lại** — không phải gap cần lấp,
mà là 1 tính năng đã bị kiến trúc mới loại bỏ có chủ đích.

**Kết luận Phần 1**: KHÔNG CÒN VIỆC GÌ ĐỂ LÀM cho "T5 trang riêng, tenant tự bấm" — premise này đã
lỗi thời do AA-526. Nếu Claude Chat soạn spec UI T-series tiếp theo, KHÔNG được giả định T5 cần 1
trang/nút cho tenant.

## Phần 2 — Việc 2 (T7 sửa được + xem lịch sử): đã build, đã bị AA-519 xoá phần lớn

Đọc `frontend/app/(tenant)/portal/t7-planning/page.tsx` → delegate `_components/PlanningTab.tsx`
(183 dòng). Đọc trực tiếp comment đầu file (dòng 4-26):

> *"AA-519 Việc 3 — this page used to open with a Quarter Plan preview/trip-table/finalize/
> history UI (AA-448, extended AA-469 Việc 2) ABOVE `<SlateTab/>`; Nghiệp's explicit call... is
> that this was fully superseded by the Slate (AA-511) and should come out... removed here."*

**Lịch sử thật**: Việc 2 SHIPPED 29/08 (PR #258) — `GET /v1/planning/quarter-plan/history` +
`GET /v1/planning/quarter-plan/versions/{version_id}` (2 route MỚI, nối dây hàm backend có sẵn từ
AA-323), cộng "edit sau khi finalize" qua cơ chế `specials`/`excluded_trip_ids` có sẵn (không phải
sửa từng slot). Live-verify thật trên dữ liệu `wanderlux-travel` (2 version Q4 2026).

**Hiện trạng thật hôm nay — trang `t7-planning` KHÔNG CÒN cả 2 tính năng này**:
- `PlanningTab.tsx` hiện chỉ render `<SlateTab/>` (T7 Slate picker, AA-511) + `<ReallocationSection/>`
  (suggest/apply/reject phân bổ tour quý sau — KHÔNG PHẢI "sửa quarter plan hiện tại/quá khứ").
- 2 route backend `GET /v1/planning/quarter-plan/history` + `.../versions/{version_id}` **VẪN CÒN
  TỒN TẠI THẬT** trong `api/routers/v1_planning.py` (dòng 220-268, chưa bị xoá) — nhưng
  `grep -rn "quarter-plan/history\|quarter-plan/versions" frontend/` = **0 kết quả** — **2 endpoint
  thật, hoạt động, hoàn toàn mồ côi (orphaned), không FE nào gọi**.
- `v1_planning.py` (`grep "@router\.\(get\|post\|patch\|put\)"`) — **0 endpoint PATCH/PUT** cho
  quarter-plan — xác nhận đúng câu hỏi Phần 2 của issue: chỉ có GET (đọc) + POST (preview/finalize/
  reallocation-confirm), không có "sửa" theo nghĩa PATCH 1 plan đã tồn tại.

**Nguyên nhân**: AA-519 (03/09/2026, Nghiệp xác nhận trực tiếp trong issue AA-519) quyết định Slate
(AA-511) đã thay thế hoàn toàn khái niệm "xem/sửa Quarter Plan dạng bảng" — không phải bug, là
redesign có chủ đích.

**Kết luận Phần 2**: "T7 sửa được + xem lịch sử" theo đúng nghĩa họp 26/08 (bảng trip, preview,
finalize, xem lại version cũ) **KHÔNG CÒN TỒN TẠI trên UI hôm nay**, dù backend history vẫn sống.
Nếu vẫn cần "xem lịch sử" theo tinh thần cũ, cách rẻ nhất là nối lại UI vào 2 route đã có sẵn
(không cần build lại backend) — nhưng cần Nghiệp xác nhận đây còn là nhu cầu thật sau khi Slate đã
thay thế, không tự suy đoán.

## Phần 3 — Việc 4: T8/T11 theo đúng 3 tiêu chí ADR-2026-037 (query thật trên RDS dev, 06/09/2026)

### T8 (`acp_shared.angle_gate_request`/`angle_gate_option`)

| Tiêu chí ADR-2026-037 | Kết quả thật |
|---|---|
| 1. Query đếm > 0 row | **22** `angle_gate_request` (10 approved, 7 pending_choice, 5 pending_goal), **51** `angle_gate_option` (10 chosen) |
| 2. Đọc tay 1 row có nghĩa | `request_id=be56ce3f-...`, tenant WanderLux Travel, channel=facebook, goal=promotion, status=approved, created 2026-09-05 03:23 — dữ liệu hợp lý, không rác |
| 3. Xác nhận đường đọc ra | Bảng này đã được chính AA-545 (06/09) đọc lại độc lập (`angle_gate_request.route_segment_ids` FK check) — xác nhận đây là dữ liệu ĐANG SỐNG, không phải cô lập |

**Kết luận**: gap Nghiệp lo (comment AA-478 28/08: "T8 yếu hơn, không có row-count tường minh") đã
được LẤP ĐẦY bởi dữ liệu thật tích luỹ từ các task SAU AA-449 (AA-451/512/513/528...) — nhưng
**KHÔNG có 1 task nào TỪNG chạy đủ 3 tiêu chí ADR-2026-037 và ghi lại tường minh** cho T8 tính đến
trước investigation này. Investigation AA-547 này là lần đầu tiên cả 3 tiêu chí được chạy và ghi
nhận cùng lúc cho T8 — coi như đóng gap **về mặt bằng chứng**, không phải "gap chưa từng đóng".

### T11 (`acp_shared.publish_log`, `shared.tenant_integrations`)

| Tiêu chí ADR-2026-037 | Kết quả thật |
|---|---|
| 1. Query đếm > 0 row | **`publish_log` = 0 hàng** (không status nào, kể cả `failed`/`unpublished`). **`tenant_integrations` = 0 hàng.** |
| 2. Đọc tay 1 row có nghĩa | Không thể — không có hàng nào để đọc |
| 3. Xác nhận đường đọc ra | N/A |

**Kết luận: GAP VẪN TREO, THẬT SỰ TỆ HƠN mô tả cũ trong CLAUDE.md.** CLAUDE.md (AA-458) ghi
"positive path chỉ chứng minh bằng unit test, chưa từng publish thật thành công" — **nhưng đọc DB
trực tiếp hôm nay cho thấy còn tệ hơn**: không chỉ chưa có publish THÀNH CÔNG thật, mà **KHÔNG CÓ
BẤT KỲ HÀNG NÀO** trong `publish_log` (kể cả 1 lần thử thất bại), và **KHÔNG CÓ TENANT NÀO từng kết
nối WordPress thật** (`tenant_integrations` = 0). T11 chưa từng được một tenant thật (hay cả 1
phiên live-verify) chạm tới ở mức tối thiểu là "thử kết nối" — không chỉ "chưa publish thành
công". Đây là case rõ nhất "Done nhưng chưa persist" (ADR-2026-037) trong toàn bộ T-series, xác
nhận đúng và còn nghiêm trọng hơn Nghiệp lo ngại ở comment 28/08.

## Phần 4 — Bug 28/71 tour (Việc 3): đã đóng dứt điểm 30/08/2026, không phải "chưa sửa"

Đọc trực tiếp lịch sử AA-469 (comment nội bộ, không phải suy đoán):
- Điều tra lần 1 (29/08): 28/71 = filter `master_status='active'` đúng thiết kế (AA-441) — 39 tour
  "inactive" là rác test cũ, không rõ nguồn gốc qua audit log, Nghiệp xác nhận không cần điều tra
  sâu (sẽ mất khi reset AA-495).
- Điều tra lần 2, câu hỏi hẹp hơn (30/08, do Nghiệp tự mở lại): trong số tour ĐANG active thật,
  `browse_pool()` có hiển thị đủ 100% không? **Kết quả: PASS, không có bug** — SQL đếm trực tiếp
  `master_status='active'` = 28, `GET /v1/tours/pool` qua hết trang = 28, khớp tuyệt đối
  (`docs/claude_audit/AA-469-viec3-step0-28-71-tour-bug-investigation.md`). Đã kiểm cả RLS/JOIN/
  phân trang — không có gì sai. **Không có PR, không có migration, không có code fix** — vì không
  có bug nào cần sửa.

**Kết luận Phần 4**: Việc 3 đóng đúng ("không có bug" là kết luận hợp lệ, không phải "chưa sửa").
Không cần điều tra lại.

## Phần 5 — Tổng hợp: UI T-series còn thiếu gì THẬT SỰ (dựa trên Phần 1-4, không suy đoán từ mô tả họp 26/08 đã lỗi thời)

1. **T5 (Atomize) không còn là khái niệm tenant-facing** — mọi thiết kế UI T-series tiếp theo phải
   bỏ hẳn "trang T5 cho tenant" ra khỏi phạm vi (đã bị AA-526 loại bỏ có chủ đích).
2. **T7 "xem lịch sử"** — backend đã có sẵn (`GET /v1/planning/quarter-plan/history` +
   `.../versions/{id}`), hoàn toàn mồ côi phía FE. Nếu vẫn còn nhu cầu thật (cần Nghiệp xác nhận,
   không tự suy đoán — có thể Slate đã đủ thay thế), đây là việc RẺ NHẤT có thể làm (chỉ cần build
   FE, backend đã xong, đã live-verify từ 29/08).
3. **T7 "sửa được"** theo nghĩa gốc (bảng trip, preview/finalize từng dòng) — đã bị thay thế hoàn
   toàn bởi Slate, không có backend nào để nối lại nữa (bị xoá cùng UI). Nếu vẫn cần, phải thiết kế
   MỚI, không phải "nối dây lại".
4. **T8** — không còn gap về bằng chứng (đã đóng bởi chính investigation này) — không cần hành
   động thêm cho mục đích evidence, chỉ cần ghi vào implementation-notes cho task nào tiếp theo
   đụng T8.
5. **T11 — gap thật, nghiêm trọng nhất, ưu tiên cao nếu có build tiếp**: cần ít nhất 1 tenant thật
   kết nối WordPress + 1 lần publish thành công thật được ghi vào `publish_log.status='published'`
   trước khi coi T11 là "hoạt động", bất kể trạng thái Linear/code review nói gì. Đây không phải
   việc UI — là việc XÁC MINH VẬN HÀNH, có thể cần 1 site WordPress test không bị chặn bot (khác
   `aa-wordpress.rf.gd`, theo đúng gap CLAUDE.md đã ghi).
6. **2 gap phụ tìm được ngoài phạm vi hỏi trực tiếp của issue** (đáng ghi vào backlog, không phải
   phát minh mới): comment `t1-rewrite/page.tsx` dòng 3-4 ("triggers... T2-T5") đã lỗi thời (không
   còn T5 trong chuỗi đó); các gap hiển thị Tenant→Market ở AA-546's investigation (trang
   `atom-curation`) — không liên quan T-series nhưng cùng loại "code cũ chưa theo kịp kiến trúc
   mới".
