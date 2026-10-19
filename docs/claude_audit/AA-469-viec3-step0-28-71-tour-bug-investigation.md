# AA-469 Việc 3 — STEP0 Investigate: T1 pool chỉ hiện 28/71 published tours

Investigate only, không sửa code, không commit gì trong phiên này. Làm trên `main`, không dùng
worktree riêng (đọc-only). Đọc trực tiếp `api/routers/v1_tours.py` (hàm `browse_pool`),
`api/migrations/052_tour_lifecycle_adr018.sql`, `shared/repository/published_catalog_repository.py`,
`api/routers/admin.py` (3 write-site của `master_status`), `services/notifications.py`,
`frontend/app/(tenant)/portal/_components/PoolTab.tsx` phiên này + query DB thật qua S3-mediated
ECS exec pattern.

**Status: STEP0 xong — chưa sửa code, chưa đề xuất fix.** Đây là 1 trong 5 Việc AA-469 yêu cầu
điều tra riêng trước khi bất kỳ Việc nào được thiết kế/build — xem
`docs/claude_tasks/AA-469-redesign-t-series.md`.

---

## 1. Endpoint thật: `GET /pool`, hàm `browse_pool`

`api/routers/v1_tours.py:84-85`. Docstring tự flag: *"All active tenants can read the pool — RLS
bypassed via aa_internal filter."*

## 2. WHERE clause thật (`v1_tours.py:105-109`)

```python
conditions = [
    "pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid",
    "pt.master_status = 'active'",
    "pt.deleted_at IS NULL",
]
```

Comment tại chỗ (`v1_tours.py:107-111`, AA-441) tự ghi rõ đây là fix có chủ đích: *"was missing
the master_status/deleted_at gate that acp_contract.v_trip_registry already applies ... a tour an
admin trashes/deactivates in Master Content stayed fully visible and rewritable to every tenant
here."* Mặc định `page_size=20` (tối đa 100), `page=1`, `ORDER BY pt.published_at DESC` +
`LIMIT`/`OFFSET`.

## 3. Xác nhận số 71 vẫn đúng hôm nay (DB thật, RDS aa-cis-dev-db)

```sql
SELECT COUNT(*) FROM gold_aa_internal.published_tours;
```
→ **71**. Số liệu từ buổi họp vẫn chính xác tính đến 2026-08-29.

## 4. Chạy lại chính xác WHERE clause của `browse_pool` — xác nhận đúng con số 28

```sql
SELECT COUNT(*)
FROM gold_aa_internal.published_tours pt
LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
  AND pt.master_status = 'active'
  AND pt.deleted_at IS NULL
```
→ **post_where_total = 28** — khớp CHÍNH XÁC con số 28 được báo cáo. Đây KHÔNG phải bug pagination/
RLS — đúng là WHERE clause filter ra 28.

Tách từng điều kiện riêng để biết điều kiện nào chịu trách nhiệm chính:

| Điều kiện riêng | Kết quả |
|---|---|
| `tenant_id = '00000000-...-001'` (một mình) | 71 — điều kiện này KHÔNG lọc gì (toàn bộ 71 published_tours đều thuộc tenant_id này) |
| `master_status = 'active'` (một mình) | **28** — điều kiện chịu trách nhiệm chính, tự nó đã bằng đúng con số cuối cùng |
| `deleted_at IS NULL` (một mình) | 67 — chỉ loại 4 dòng, và 4 dòng đó đã nằm trong tập bị `master_status` loại rồi (không mất thêm khi kết hợp) |

## 5. Phân bố `master_status` thật trên toàn bộ 71 dòng

```sql
SELECT COALESCE(master_status::text, 'NULL') AS master_status,
       (deleted_at IS NOT NULL) AS has_deleted_at, COUNT(*) AS n
FROM gold_aa_internal.published_tours GROUP BY 1, 2 ORDER BY 3 DESC
```
→
```
{'master_status': 'inactive', 'has_deleted_at': False, 'n': 39}
{'master_status': 'active',   'has_deleted_at': False, 'n': 28}
{'master_status': 'trashed',  'has_deleted_at': True,  'n': 4}
```
28 + 39 + 4 = 71 ✓. `master_status` là enum Postgres cứng
(`gold_aa_internal.master_status_enum`, 3 giá trị `active`/`inactive`/`trashed`) — không có giá
trị NULL nào tồn tại thật (query đầu tiên chạy `COALESCE(master_status, 'NULL')` — thiếu `::text`
— bị `asyncpg.exceptions.InvalidTextRepresentationError` vì Postgres cố ép chuỗi `'NULL'` vào
kiểu enum; sửa lại `COALESCE(master_status::text, 'NULL')` chạy được, không có row nào rơi vào
nhánh NULL thật).

Sample 10 tour `master_status != 'active'` mới nhất cho thấy phần lớn là `'inactive'`
(`has_deleted_at=False`, KHÔNG phải trash), bao gồm cả tour publish RẤT gần đây
(`published_at`: 2026-08-22, 2026-08-01) — đây không giống hành vi "admin chủ động ẩn" theo mô tả
comment AA-441 (comment đó gộp chung "trashes/deactivates" thành 1 ý, nhưng 39 vs. 4 là quy mô rất
khác nhau).

## 6. Truy vết nguồn gốc 39 dòng `inactive` — KHÔNG đến từ INSERT, KHÔNG đến từ admin UI đã audit

**Cột `master_status` có DB DEFAULT = `'active'`** (`api/migrations/052_tour_lifecycle_adr018.sql`,
dòng 32: `ADD COLUMN master_status gold_aa_internal.master_status_enum NOT NULL DEFAULT 'active'`).

INSERT thật (không phải test/migrate-script) cho `published_tours` là
`shared/repository/published_catalog_repository.py:15-49`
(`PublishedCatalogRepository.insert()`, template `{self.gold}.published_tours`,
`ON CONFLICT (tour_id) DO UPDATE`). Danh sách cột INSERT/UPDATE hoàn toàn KHÔNG có
`master_status` — nghĩa là mọi lần publish (kể cả republish qua `ON CONFLICT DO UPDATE`) đều để
cột này rơi về DEFAULT `'active'`, không bao giờ tự set `'inactive'`.

Có đúng 2 endpoint admin-facing có thể set `master_status='inactive'`:
- `PATCH /admin/master/{tour_id}/status` (`toggle_master_status`, `admin.py:888-930`)
- `PATCH /admin/master/{tour_id}/deactivate` (`deactivate_master_tour`, `admin.py:1187-1224` — tên
  route đọc từ decorator gần dòng 1187, xác nhận qua đọc trực tiếp)

**Cả 2 đều emit `NotificationService(conn).emit(event_type=EventType.MASTER_DEACTIVATED, ...)`**
(`admin.py:906-911` và tương đương trong `deactivate_master_tour`) — ghi vào bảng audit thật
`shared.notifications` (`services/notifications.py:67`, `INSERT INTO shared.notifications`).

Query trực tiếp bảng audit:
```sql
SELECT COUNT(*) FROM shared.notifications WHERE event_type = 'MASTER_DEACTIVATED'
```
→ **0**. Chưa từng có 1 sự kiện `MASTER_DEACTIVATED` nào được ghi nhận, trong khi hiện có 39 tour
đang ở trạng thái `inactive`.

**Kết luận truy vết**: 39 dòng `inactive` này KHÔNG đến từ (a) giá trị mặc định lúc INSERT (default
là `active`), KHÔNG đến từ (b) bất kỳ hành động admin nào qua 2 endpoint đã audit (0 event ghi
nhận). Chúng phải đến từ 1 đường ghi dữ liệu nằm NGOÀI 2 endpoint này — khả năng cao nhất là 1 script
backfill/migration/UPDATE thủ công chạy trực tiếp lên DB (ngoài phạm vi app code), nhưng STEP0 này
KHÔNG thể xác định chính xác nguồn gốc chỉ từ code + audit log hiện có — cần hỏi trực tiếp Nghiệp
có từng chạy 1 script như vậy hay không, hoặc tra thêm nếu có audit log khác ngoài
`shared.notifications` (không tìm thấy trong phạm vi STEP0 này).

## 7. Frontend (`PoolTab.tsx`) — pagination đã cài đúng, KHÔNG phải nguyên nhân

`frontend/app/(tenant)/portal/_components/PoolTab.tsx`: có state `page`/`setPage` (dòng 30),
`PAGE_SIZE` constant, gọi `GET /api/tenant/v1/tours/pool?page=N&page_size=20&...` (dòng 68),
Prev/Next button dùng `total` trả về từ API để tính số trang và disable đúng lúc (dòng 176-180:
`disabled={page === 1}` / `disabled={page * PAGE_SIZE >= total}`). Với `total=28`, tenant hoàn toàn
xem được đủ 28 tour qua 2 trang (`ceil(28/20)=2`) — pagination hoạt động đúng THIẾT KẾ trên tập đã
lọc; vấn đề không nằm ở đây, mà ở việc tập đã lọc (28) nhỏ hơn tập thật (71) do `master_status`.

## Net conclusion — root cause thật

**43 tour "biến mất" khỏi T1 pool vì `browse_pool()`'s WHERE clause lọc `pt.master_status =
'active'`, và 39/43 tour đang ở `master_status='inactive'` (4 còn lại là `'trashed'` hợp lệ, có
`deleted_at`).** Filter bản thân nó là fix có chủ đích từ AA-441 (đúng thiết kế, không phải bug
logic SQL). Nhưng **nguồn gốc của 39 dòng `inactive`** — phần chiếm phần lớn (39/43) — KHÔNG giải
thích được bằng bất kỳ code path/action nào tìm thấy trong app: default DB là `active`, INSERT
thật không đụng cột này, và CẢ 2 admin endpoint có thể set `inactive` đều để lại audit trail 0 sự
kiện. Đây là **unexplained data state**, nhiều khả năng do 1 thao tác ngoài luồng ứng dụng (script
DB trực tiếp/backfill/migration thủ công) — không phải bug pagination, không phải bug RLS/tenant
scoping (tenant_id filter tự nó không loại tour nào), không phải bug JOIN.

Không đề xuất fix trong phiên này theo đúng yêu cầu — quyết định tiếp theo (có nên tự động
active hoá lại 39 tour này, hay cần review từng cái trước, hay hỏi thêm Nghiệp về nguồn gốc) cần
Nghiệp/Claude Chat quyết trước khi build bất kỳ thay đổi nào.

---

## Việc 3 re-open (30/08/2026) — câu hỏi hẹp hơn: trong số tour ĐANG active, `browse_pool()` có
## hiện đủ, đúng không? KHÔNG điều tra lại nguồn gốc 39 tour inactive (đã chốt ở trên).

**Phương pháp**: (1) đếm trực tiếp SQL số tour `master_status='active'` — độc lập, không tái dùng
lại WHERE clause của `browse_pool()`. (2) Gọi THẬT endpoint `GET /v1/tours/pool` qua HTTP thật
(trong container, `localhost:8000`, cùng process FastAPI đang chạy live, JWT tenant thật), phân
trang qua HẾT tất cả các trang (không chỉ trang 1), gom toàn bộ `tour_id` trả về. (3) Diff.

**Kết quả (live, 30/08/2026, ECS task `aa-cis-dev-api:169`)**:
```
Direct SQL active count: 28
Distinct active tour_ids (direct SQL): 28
published_at ties among active tours (rows with duplicate timestamps): 0

page=1 got=20 total=28 pages=2
page=2 got=8 total=28 pages=2
Total rows returned across all pages: 28
Distinct tour_ids returned across all pages: 28
Duplicate rows across pages (should be 0): 0

Missing from browse_pool (should be empty): []
Extra in browse_pool not active (should be empty): []
RESULT: PASS
```

**KHỚP CHÍNH XÁC — không có bug.** 28 = 28, không thiếu, không thừa, không trùng lặp giữa các
trang. Đã kiểm tra riêng cả 3 khả năng đầu bài nêu, không khả năng nào xảy ra thật:
- **RLS**: không áp dụng cho endpoint này — docstring `browse_pool()` tự ghi rõ "RLS bypassed via
  aa_internal filter" (WHERE clause hardcode `tenant_id = '00000000-...-001'::uuid`, tenant gọi
  API không ảnh hưởng đến tập kết quả, chỉ ảnh hưởng cờ `already_rewritten` per-row).
- **JOIN**: `LEFT JOIN silver_aa_internal.raw_tours` — LEFT JOIN không thể làm mất dòng bên trái
  (pt) dù không khớp `rt`; xác nhận qua đọc code, không cần fix.
- **Pagination**: công thức `offset = (page-1)*page_size` và index tham số SQL (`LIMIT
  $N+1 OFFSET $N+2`, `tid_idx = N+3`) đúng, xác nhận qua chạy thật 2 trang cho đúng 20+8=28. Kiểm
  tra thêm rủi ro tiềm ẩn: `ORDER BY pt.published_at DESC` không có tiebreaker phụ (vd. `id`) —
  về lý thuyết CÓ THỂ gây trùng/thiếu dòng giữa các trang nếu 2 tour active trùng
  `published_at` y hệt nhau; kiểm tra thật cho thấy **0 cặp trùng `published_at`** trong 28 tour
  active hiện tại, nên rủi ro này không xảy ra hôm nay — flag như một điểm có thể hardening sau
  này (thêm `pt.id` làm tiebreaker phụ trong `ORDER BY`), KHÔNG fix vì đầu bài yêu cầu không đoán
  bug khi chưa có bằng chứng thật, và đây chỉ là rủi ro lý thuyết chưa từng xảy ra.

**Kết luận Việc 3 (câu hỏi hẹp) — ĐÓNG, không cần code change.** Tenant thấy và chọn được đầy đủ
tất cả tour đang `active` trong Master Content, không thiếu tour nào do bug riêng ngoài filter
`master_status` đã biết. Không commit gì (không có thay đổi code), file STEP0 này cập nhật tại
chỗ theo đúng quy ước.
