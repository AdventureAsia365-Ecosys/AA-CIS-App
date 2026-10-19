# AA-475 STEP0 — Xoá /admin/atomize + /admin/curation + 2551 atom owner_scope='platform'

Read-only investigation. Không sửa code, không DELETE gì. Repo: AA-CIS-App.
Ngày chạy: 2026-08-27. ECS/RDS đang live (`aa-cis-dev-api` desired=1/running=1, `aa-cis-dev-db` available)
— mọi query DB dưới đây chạy qua ECS exec pattern chuẩn (S3-mediated) trực tiếp trên RDS dev.

## KẾT LUẬN NGẮN GỌN

**CẦN DỪNG LẠI trước khi xoá dữ liệu (không phải "an toàn để xoá 100%").**

Code-side (routes/pages) AN TOÀN để xoá — không có caller T5/T6 nào bị ảnh hưởng, đã xác nhận bằng
grep + đọc code toàn bộ, không phải suy đoán.

Data-side (2551/2629 atom `owner_scope='platform'`) **KHÔNG an toàn để xoá thẳng** — phát hiện
**46/47 slot thật** trong `acp_shared.acp_v2_slots` (tenant `aa_internal`, `status='produced'` —
nghĩa là nội dung ĐÃ được sản xuất thật qua N7) có `payload->'atom_ids'` trỏ vào đúng các atom
`owner_scope='platform'` này. Đây không phải rác — đây là lịch sử sản xuất N7 thật của tenant
`aa_internal` (tenant nội bộ của AA, không phải tenant B2B ngoài, nhưng vẫn là dữ liệu vận hành
thật, có `status='produced'`), và một tính năng ĐANG SỐNG (`services/acp_shared/content_metrics.py
::rollup_atom_weights()`, AA-448 round 6 — vòng phản hồi atom-weight từ engagement thật) đọc lại
`tour_atoms` bằng đúng các `atom_id` này. Xoá ngay sẽ làm tính năng đó âm thầm no-op vĩnh viễn cho
toàn bộ lịch sử sản xuất của `aa_internal`, không có lỗi/exception nào báo hiệu (không có FK ràng
buộc — xem mục 3).

Chi tiết đầy đủ bên dưới.

---

## 1. Phân loại `api/routers/admin_atoms.py` — hàm nào xoá được, hàm nào phải giữ

`admin_atoms.py` có 5 endpoint. Auth thực tế của từng endpoint (đọc code trực tiếp, không suy đoán
từ docstring):

| Endpoint | Auth path | Caller thật (grep xác nhận) | Kết luận |
|---|---|---|---|
| `GET /admin/atoms` (`list_atoms`) | `_resolve_atom_owner_scope` — JWT tenant HOẶC X-Admin-Secret, `owner_scope` derive từ JWT | `frontend/app/admin/curation/page.tsx` (admin) **VÀ** `frontend/app/(tenant)/portal/_components/AtomsTab.tsx` (T6, qua `/api/tenant/admin/atoms`) | **PHẢI GIỮ** — hàm dùng chung thật, T6 đang sống dựa vào nó |
| `GET /admin/atoms/summary` | như trên | admin/curation/page.tsx **VÀ** AtomsTab.tsx | **PHẢI GIỮ** — dùng chung |
| `PATCH /admin/atoms/{atom_id}` | như trên | admin/curation/page.tsx **VÀ** AtomsTab.tsx (star/delete) | **PHẢI GIỮ** — dùng chung |
| `PATCH /admin/atoms/bulk` | như trên (owner_scope-scoped) | CHỈ `admin/curation/page.tsx` — AtomsTab.tsx xác nhận không có bulk (comment tự khai: "no bulk multi-select") | Xoá `/admin/curation` page thì endpoint này **mất caller thật duy nhất** → mồ côi. Task gốc không liệt kê nó, nhưng nên xoá cùng đợt (xem mục "Phát hiện thêm" bên dưới) — không bắt buộc phải xoá ngay, không rủi ro nếu giữ lại |
| `GET /admin/atoms/preview-slotgrid` | `verify_admin_secret` trực tiếp, KHÔNG có nhánh JWT | CHỈ `admin/curation/preview/page.tsx` | Không phải hàm dùng chung (chưa từng nhận owner_scope từ JWT) — mồ côi cùng lúc với trang preview. Là consumer của N4/N5/N6 (chiều phụ thuộc xác nhận ở mục 4), không tự nó touch dữ liệu atom platform nguy hiểm |

Tóm lại: **`list_atoms`, `atoms_summary`, `patch_atom` (3 hàm) phải giữ nguyên** vì T6
(`/portal/t6-atoms`) đang gọi thật qua đúng các hàm này (không phải bản sao) — xoá sẽ làm T6 vỡ
ngay lập tức. `patch_atoms_bulk` và `preview_slotgrid` không có caller nào ngoài 2 trang admin sắp
xoá, nhưng **không nằm trong yêu cầu gốc** của AA-475 — liệt kê để Nghiệp quyết định có gộp vào
cùng đợt xoá hay để lại (giữ lại không gây hại gì, chỉ là code chết).

## 2. Danh sách chính xác phạm vi xoá (code)

**An toàn xoá — đã grep xác nhận 0 caller khác ngoài các trang/route liệt kê:**

- `frontend/app/admin/atomize/page.tsx` (route `/admin/atomize`)
- `frontend/app/admin/curation/page.tsx` (route `/admin/curation`)
- `frontend/app/admin/curation/preview/page.tsx` (route `/admin/curation/preview`)
- `frontend/app/admin/_components/AdminSidebar.tsx` — 2 dòng nav entry ("Atomize (N2)", "Atom Curation")
- `frontend/middleware.ts` — 2 dòng `PROTECTED_ROUTES` (`/admin/curation`, `/admin/atomize`)
- `api/routers/admin_pipeline.py::get_tours_for_atomization()` — `GET /admin/tours-for-atomization`
- `api/routers/admin_pipeline.py::admin_decompose_atoms()` — `POST /admin/atoms/decompose` (alias,
  gọi `v1_atoms.decompose()` verbatim)
- `api/routers/v1_atoms.py` **toàn bộ file** — `POST /v1/atoms/decompose` + `_decompose_inline()` +
  Bedrock Batch path. Xác nhận: `_get_tenant()` của file này CÓ CHẤP NHẬN JWT tenant về mặt code,
  nhưng grep toàn bộ frontend (admin lẫn tenant portal) không tìm thấy bất kỳ caller tenant thật
  nào — T5 (`services/acp_produce/tenant_pipeline.py::run_t5_atomize`) là một pipeline atomize
  HOÀN TOÀN RIÊNG, tự viết `INSERT INTO acp_contract.tour_atoms` trực tiếp với
  `owner_scope=tenant_id`, không gọi qua `v1_atoms.py` ở bất kỳ bước nào. Caller thật duy nhất của
  `v1_atoms.decompose()` là admin alias ở trên.
- `shared/services/atom_decompose_poller.py` — **phát hiện thêm**, không có trong yêu cầu gốc:
  poller cho `acp_contract.atom_decompose_jobs` (job Bedrock Batch của N2). Grep xác nhận **0
  caller** (không cron, không Lambda, không test wiring) — đã là code chết từ trước, gắn chặt với
  N2 decompose, nên đưa vào cùng phạm vi cân nhắc.
- Test files gắn riêng với các trang trên: `tests/e2e/aa300-curation*.spec.ts`,
  `tests/e2e/aa345-round*-curation*.spec.ts`, `tests/unit/test_aa300_admin_atoms.py` (phần
  preview_slotgrid/bulk), `tests/unit/test_aa345_atomize.py`, `tests/verify_scripts/aa345_round*_*.mjs`

**KHÔNG được đụng vào** (dùng chung với T6, xem mục 1): `list_atoms`, `atoms_summary`, `patch_atom`
trong `admin_atoms.py`, và toàn bộ `services/acp_produce/tenant_pipeline.py::run_t5_atomize` (T5,
pipeline atomize hiện hành, hoàn toàn độc lập với code sắp xoá).

## 3. FK graph + tham chiếu chéo — PHÁT HIỆN CHẶN

Query trực tiếp trên RDS dev (`pg_constraint`, không chỉ `information_schema`), theo đúng phương
pháp AA-473 Phần 2 đã rút kinh nghiệm (không chỉ quét theo tên cột):

**Số liệu chính xác hiện tại** (khác một chút so với con số Nghiệp nhớ — 2551 vẫn đúng cho "atom
sống"):
```
owner_scope breakdown (toàn bộ acp_contract.tour_atoms):
  'platform'                              -> 2629 rows
  'a1b2c3d4-0001-4000-8000-000000000001'  -> 7 rows   (tenant thật, T5)

platform, NOT deleted AND NOT is_empty_marker : 2551   <- khớp đúng con số Nghiệp nhắc
platform, TOTAL (kể cả deleted/marker)        : 2629
```
Chỉ có 2 giá trị `owner_scope` tồn tại trong toàn bộ bảng — không có owner_scope thứ 3 nào khác bị bỏ sót.

**Formal FK (pg_constraint) trỏ vào `acp_contract.tour_atoms.atom_id`: KHÔNG CÓ.**
`tour_atoms.atom_id` chỉ có PRIMARY KEY, không table nào có FK chính thức trỏ vào nó. Grep mọi FK
constraint definition chứa chuỗi "atom" trong toàn DB cũng không ra thêm gì ngoài PK đó và 2 CHECK
constraint không liên quan (`prompt_eval_runs_pipeline_check`, `unknown_ledger_kind_check`).

**Tham chiếu free-text/JSONB (không có FK, nhưng có thể là tham chiếu thật) — kiểm tra từng bảng:**

| Nguồn | Kết quả |
|---|---|
| `acp_shared.angle_gate_request.atom_id` (TEXT, không FK) | **0 hit** — không request nào trỏ vào atom platform |
| `acp_shared.quarter_plan_version.payload->'big_rocks'[].atom_ids` (JSONB) | **0 hit** — không version nào embed atom platform |
| `acp_shared.marketplace_portfolios.atom_snapshot` (JSONB) | Không chứa atom_id list — chỉ chứa `{total_atoms, runway_months, posts_per_week}` (số đếm, xác nhận bằng cách đọc migration 097 + sample 5 row thật). Đã grep code: 0 reader nào còn đọc field này (AA-472 đã xoá `seed_tenant_atoms` — reader duy nhất trước đây). An toàn tuyệt đối. |
| **`acp_shared.acp_v2_slots.payload->'atom_ids'` (JSONB)** | **46 slot / 47 slot tổng (tenant `aa_internal`) CÓ tham chiếu tới atom platform, status='produced'** — xem chi tiết dưới |

### Phát hiện chặn: `acp_v2_slots` — dữ liệu sản xuất N7 thật, không phải rác

```
acp_v2_slots: tổng 47 row, 2 tenant riêng biệt.
46/47 row của tenant 00000000-0000-0000-0000-000000000001 (slug 'aa_internal'),
status='produced', có payload->'atom_ids' chứa atom_id thuộc owner_scope='platform'.
```

Nguồn gốc (đã xác nhận bằng đọc code, không suy đoán): `acp_v2_slots` được ghi bởi
`services/acp_planning/allocator.py::persist_slot_grid()` — gọi từ N7 (real weekly-run flow),
KHÔNG phải từ `admin_atoms.py::preview_slotgrid()` (endpoint đó tự khai trong docstring "nothing
here writes a NEW row", đã xác nhận đúng). `persist_slot_grid()` nhận `SlotGrid` từ
`allocate_month()`/`allocate_month_from_db()`, hai hàm này gọi `fetch_atoms_by_trip()` — hàm mà
AA-461 (đã ship, xem `services/acp_planning/quarter.py` dòng ~303) từng sửa vì trước đó filter sai
theo `raw_tours.tenant_id` thay vì `tour_atoms.owner_scope`. **Trước khi AA-461 fix**, N7 chạy cho
tenant `aa_internal` (mà `raw_tours.tenant_id = aa_internal` cho toàn bộ catalog platform) đã kéo
theo TẤT CẢ atom `owner_scope='platform'` gắn với các tour đó vào SlotGrid thật — và
`persist_slot_grid()` đã ghi các atom_id đó vào DB thật, `status='produced'`. Đây chính là dữ liệu
sản xuất N7 lịch sử của `aa_internal`, không phải test data ngẫu nhiên.

**Consumer đang sống, đang đọc lại các atom_id này** (không phải giả thuyết — đọc trực tiếp code):
`services/acp_shared/content_metrics.py::rollup_atom_weights()` (AA-448 round 6, vòng phản hồi
atom-weight từ engagement thật do người dùng nhập tay) join
`acp_deliver.pieces.slot_id → acp_v2_slots.payload->'atom_ids'` rồi
`UPDATE acp_contract.tour_atoms SET weight = ... WHERE atom_id = $1`. Nếu xoá 2551 atom platform,
mọi lần rollup chạy cho các piece lịch sử của `aa_internal` gắn với 46 slot này sẽ **âm thầm
UPDATE 0 row** — không lỗi, không cảnh báo, tính năng chỉ lặng lẽ ngừng có tác dụng cho đúng phần
lịch sử đó. `services/acp_produce/trust_ramp.py::packet_has_bofu_piece()` cũng join qua
`acp_v2_slots.payload` nhưng đọc key khác (`funnel_stage`), không phụ thuộc atom_id — không bị ảnh
hưởng.

**Không table nào khác** trong `acp_shared`/`acp_contract`/`acp_deliver` có cột tên chứa "atom"
ngoài các bảng đã kiểm ở trên (đã liệt kê đầy đủ qua `information_schema.columns`,
`s1_from_atom_runs.atoms_available/atoms_used_count` chỉ là số đếm int, không phải danh sách id).

## 4. `preview-slotgrid` / N4-N5-N6 — chiều phụ thuộc

Xác nhận lại bằng đọc code (`services/acp_planning/quarter.py`, `allocator.py`, `runway.py`):

- N4 (`runway_map`/`fetch_trips`) **không đụng `tour_atoms` chút nào** — chỉ đọc trips.
- N5 (`compute_quarter_plan`/`plan_quarter`) và N6 (`allocate_month`/`allocate_month_from_db`) đều
  lấy atom qua **một hàm duy nhất**: `fetch_atoms_by_trip(tenant_id, pool)` —
  `WHERE owner_scope = $1` với `$1 = str(tenant_id)` (một UUID string thật). Vì `'platform'`
  (string literal) không bao giờ bằng bất kỳ UUID tenant nào (kể cả `aa_internal`'s
  `00000000-0000-0000-0000-000000000001`), **N5/N6 từ sau AA-461 KHÔNG THỂ nhận atom
  `owner_scope='platform'` cho bất kỳ tenant nào, kể cả `aa_internal`** — đây là guarantee ở tầng
  code (SQL filter), không phụ thuộc dữ liệu.
- `admin_atoms.py::preview_slotgrid()` chỉ là CALLER của N4→N5→N6 (đúng như AA-473 STEP0 từng ghi),
  không phải ngược lại, và bản thân nó không bao giờ ghi (không có INSERT nào trong hàm này).

**Kết luận mục 4**: xoá atom platform-scoped **an toàn tuyệt đối cho N4/N5/N6 từ nay về sau** — kể
cả khi preview-slotgrid gọi lại chúng cho `aa_internal`. Rủi ro DUY NHẤT không nằm ở N4/N5/N6/
preview-slotgrid, mà ở **dữ liệu đã persist trước AA-461** trong `acp_v2_slots` (mục 3) — một
đường ghi khác (N7 thật, không phải preview) đã tồn tại TRƯỚC KHI fix, và hậu quả (46 slot) vẫn còn
nằm trong DB tới hôm nay.

## 5. DRY RUN (số liệu chính xác, không xoá gì)

Nếu xoá theo đúng phạm vi 2551 atom (`owner_scope='platform', NOT deleted, NOT is_empty_marker`)
mà KHÔNG xử lý phát hiện ở mục 3 trước:

- 2551 row `acp_contract.tour_atoms` bị xoá thẳng (không FK nào cascade — vì không có FK).
- 0 row ở `angle_gate_request`/`quarter_plan_version` bị ảnh hưởng (đã xác nhận sạch).
- **46 row `acp_v2_slots`** (trong tổng 47) sẽ có `payload->'atom_ids'` trỏ vào atom_id không còn
  tồn tại — không lỗi DB (JSONB không ràng buộc), nhưng **`rollup_atom_weights()` sẽ mất khả năng
  cập nhật `tour_atoms.weight` cho các atom này vĩnh viễn** (dù chính các atom đó cũng đã bị xoá
  nên "mất khả năng" ở đây có nghĩa là join `acp_v2_slots → tour_atoms` cho các atom_id này luôn
  trả về 0 dòng — hệ quả cuối là lịch sử sản xuất N7 của `aa_internal` không còn traceable ngược về
  atom gốc).
- Nếu tính luôn 2629 (bao gồm cả deleted/marker), thêm 78 row nữa (marker rows/soft-deleted) không
  ai tham chiếu, xoá được không tranh cãi.

## KẾT LUẬN

**CẦN DỪNG LẠI** ở phần data (2551/2629 atom), không phải "an toàn để xoá":

- Phần **code/route/UI** (mục 1-2): AN TOÀN ĐỂ XOÁ, ranh giới đã xác định chính xác, T6/T5 không bị
  ảnh hưởng — có thể tiến hành bất cứ lúc nào Nghiệp duyệt.
- Phần **data** (2551 atom `owner_scope='platform'`): cần Nghiệp quyết định 1 trong các hướng sau
  trước khi xoá, vì 46 slot sản xuất thật của `aa_internal` (tenant nội bộ AA, không phải test) sẽ
  mất khả năng truy vết ngược atom qua tính năng rollup atom-weight đang sống:
  1. Xoá atom nhưng migrate/snapshot lại nội dung 46 slot đó trước (ví dụ denormalize text atom
     vào chính `acp_v2_slots.payload` trước khi xoá), hoặc
  2. Chấp nhận mất traceability lịch sử đó (rollup_atom_weights sẽ no-op âm thầm cho đúng 46 slot
     này, không phải toàn bộ tính năng) và xoá thẳng, hoặc
  3. Giữ lại đúng các atom_id nào đang được 46 slot đó tham chiếu (một tập con nhỏ trong 2551, có
     thể liệt kê chính xác), xoá phần còn lại.

Không tự chọn hướng nào — chờ xác nhận trước khi build.
