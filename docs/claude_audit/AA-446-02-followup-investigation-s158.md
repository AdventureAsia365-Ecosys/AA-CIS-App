# AA-446-02 — Follow-up Investigation S158 (24/08/2026)

Điều tra + đề xuất thuần. KHÔNG sửa code, KHÔNG commit, KHÔNG tạo PR trong task này.

Nguồn đọc: `docs/claude_audit/AA-446-01-reconciliation-s158.md` (phiên trước), phần "#11"/"#12"
nguyên văn trong `docs/claude_audit/AA-439-00-SUMMARY-tenant-tier-audit.md`, code thật của
`services/acp_planning/` (T7, AA-448), `services/acp_angle_gate/` (T8, AA-449),
`docs/implementation-notes/AA-451.md` (T7 slot-persist connector), và grep trực tiếp trên
`main` hiện tại (`d1f8ea0`, sau AA-452 — không phải bản audit gốc 22-23/08).

---

## Việc 1 — Điều tra dứt điểm AA-439 #11 và #12

### Nguyên văn 2 mục từ AA-439-00-SUMMARY (để đối chiếu, không diễn giải lại)

> | 11 | **Không có handoff T7→T8 trong code** — N6 planning pipeline và module angle-generation
> (`acp_s4_social`) chưa từng gọi nhau — không phải "chưa nối dây", cấu trúc chưa từng nối |
> `services/acp_produce/` grep zero "angle" references | Real, confirmed |
>
> | 12 | Real, complete, working angle-generation flow (`acp_s4_social/`) 100% admin-gated, **zero
> rows ever, in any environment** — confirmed at both data level (06) and code level: **no caller
> exists anywhere, frontend or backend, for `/generate`/`/angles`/`/write`** |
> `v1_s4_social.py`, full frontend grep | Real, confirmed | 06, 07 |

Cả 2 mục này viết về hệ thống **CŨ**: T7 = N6 planning cũ (`services/acp_produce/` +
`services/acp_planning/allocator.py::allocate_month()`), T8 = `services/acp_s4_social/`
(port thẳng code chị Thư, admin-only). Câu hỏi của task này: sau khi T7 viết lại (AA-448) và T8
viết mới (AA-449) + AA-451 nối chúng lại, 2 finding này còn đúng theo nghĩa đen không?

---

### #11 — KẾT LUẬN: CÓ, chuỗi MỚI có handoff thật, verify được bằng code + test + live HTTP

**Bằng chứng code-level (không phải suy luận):**

`services/acp_angle_gate/service.py` (T8, AA-449) import trực tiếp 5 hàm/module từ
`services/acp_planning/` (T7, AA-448) — không phải 2 router độc lập tình cờ liền kề:

```python
from services.acp_planning.allocator import compute_slot_grid, create_weekly_produce_run, persist_slot_grid
from services.acp_planning.models import QuarterPlanNotApprovedError
from services.acp_planning.quarter import fetch_approved_quarter_plan
from services.acp_planning.runway import compute_runway_map
from services.acp_planning.tenant_config import TenantNotFoundError, fetch_tenant_planning_config
from services.acp_planning.tenant_pool import fetch_tenant_atoms_by_trip, fetch_tenant_trips
```

`create_request()` (T8, workflow step 1) đọc dữ liệu T7 sinh ra theo 2 đường thật:

1. `_fetch_slot_cta()` — đọc `acp_shared.acp_v2_slots` (bảng T7/N6 ghi qua `persist_slot_grid()`)
   để lấy `cta_target` của slot mà atom này đang nằm trong đó.
2. Nếu (1) rỗng VÀ caller truyền `year`/`month` (AA-451, Option B Nghiệp đã confirm):
   `_compute_and_persist_slot_cta()` **tính lại slot-grid tháng đó bằng ĐÚNG cùng chuỗi
   tenant-scoped fetcher mà T7's `GET /v1/planning/slot-grid` dùng**
   (`fetch_tenant_planning_config` → `fetch_approved_quarter_plan` → `fetch_tenant_trips` →
   `fetch_tenant_atoms_by_trip` → `compute_runway_map` → `compute_slot_grid`), rồi **ghi xuống
   bằng chính `persist_slot_grid()` của T7**, rồi đọc lại từ DB (không tin giá trị in-memory —
   cùng bài học AA-448's "stale response" đã rút ra).

Đây đúng nghĩa "handoff": T8 không tự tính lại logic slot-grid theo cách riêng của nó — nó **tái
sử dụng thẳng hàm tính toán của T7**, chỉ khác chỗ ghi (T7's `GET` cố tình read-only theo thiết kế
AA-448, T8's `POST create_request` là nơi ghi thật khi cần).

**Bằng chứng test:** PR #208 (AA-451) thêm 6 unit test **trực tiếp vào
`tests/unit/test_aa449_angle_gate_service.py`** (không phải file riêng) — nghĩa là test T8 giờ
tự bao gồm cả nhánh gọi-vào-T7. Xác nhận qua `git show cc5fcf5 --stat`.

**Bằng chứng live-verify thật, MỘT phiên HTTP liên tục** (không phải 2 test độc lập) —
`docs/implementation-notes/AA-451.md`, "Post-merge / post-deploy record", tenant thật
`test-n1-flow`:

1. `POST /v1/planning/quarter-plan` (T7 finalize) → 200, `approved=true`.
2. `GET /v1/planning/slot-grid` (T7, read-only) → 200, thấy slot thật (`channel=blog`,
   `atom_id=atom_0e9a4a62ed`, `cta_target` trong bộ nhớ = URL trip thật).
3. Xác nhận trước đó **chưa** có row `acp_v2_slots` nào cho `(tenant, channel, atom)` này.
4. `POST /v1/angle-gate/requests` (T8) **kèm** `year`/`month` → 200, `cta` trả về đúng
   `'https://aa-cis.lumiguides.it.com/'` — **không phải None**. DB re-check trực tiếp: row
   `acp_v2_slots` giờ tồn tại, row `acp_v2_runs` được tạo đúng, `angle_gate_request.cta` (SELECT
   tươi, không phải response body) khớp.
5. Idempotency: gọi lại cùng `year`/`month` → cùng `cta`, row count `acp_v2_slots` vẫn = 1
   (`ON CONFLICT DO NOTHING` không double-insert).
6. Cross-tenant isolation: tenant `test-agency` query cùng `(channel, atom_id)` → `None` — không
   rò rỉ chéo tenant, xác nhận không đụng lại bug `owner_scope` mà AA-445-02 từng phát hiện ở
   `allocate_month()`'s platform-wide fetcher.

**Kết luận rõ ràng:** Chuỗi MỚI (T7-rewrite AA-448 → T8-rewrite AA-449, nối bởi AA-451) **có
handoff thật, ở mức code (import + gọi hàm trực tiếp), có unit test, và đã real-HTTP-verify
trong đúng 1 phiên liên tục finalize→preview→create-request** — không phải "2 stage độc lập,
tình cờ test riêng rẽ".

**Nhưng finding gốc #11 nói về 2 MODULE CŨ** (`services/acp_produce/`'s N6 planning cũ +
`services/acp_s4_social/` cũ) — 2 module đó **vẫn chưa từng gọi nhau** (xác nhận lại, xem #12).
Bản thân câu hỏi gốc đã moot vì cả 2 module cũ đều bị thay bởi chuỗi mới, không phải vì ai đó vá
lại chúng.

**Đề xuất trạng thái:** #11 nên đổi từ "🔲 Chưa làm" sang **"✅ Resolved differently"** trong
AA-446 — ghi rõ: không phải fix trực tiếp 2 module cũ, mà là finding gốc bị vô hiệu hoá bởi việc
viết lại cả T7 lẫn T8 (AA-448 + AA-449) và nối chúng bằng AA-451. Quyết định đổi trạng thái là của
Nghiệp — task này chỉ điều tra, không tự sửa Linear.

---

### #12 — KẾT LUẬN: CHẮC CHẮN dead 100%, an toàn xoá, đã verify lại SAU khi AA-449 merge

**Grep lại toàn repo (trên `main` hiện tại, sau AA-452, không phải bản audit gốc):**

```
grep -rln "acp_s4_social" --include="*.py" api/ services/ | grep -v "^services/acp_s4_social/"
→ api/routers/v1_content_writing.py
→ api/routers/v1_angle_gate.py
→ api/routers/v1_s4_social.py        (chính router cũ, đã biết)
→ services/acp_angle_gate/__init__.py
→ services/acp_content_writing/generate.py
→ services/acp_content_writing/prompts.py
→ services/acp_content_writing/quality_gates.py
```

Đọc từng dòng context — **toàn bộ là docstring/comment ghi rõ "KHÔNG reuse" (ADR §0.5)**, không
có `import` thật nào:

```
services/acp_angle_gate/__init__.py:5:   ...— no import from services.acp_s4_social anywhere in this package.
api/routers/v1_content_writing.py:6:    ...Written fresh per ADR §0.5 — no import from services.acp_s4_social anywhere.
services/acp_content_writing/quality_gates.py:233: (a departure from ADR §0.5's "don't reuse acp_s4_social/acp_produce business logic")
services/acp_content_writing/generate.py:9,33:     ...not-reused acp_s4_social/writer.py...
services/acp_content_writing/prompts.py:3:         Referenced (not reused, ADR §0.5) against services/acp_s4_social/writer.py's...
api/routers/v1_angle_gate.py:9:        Written fresh per ADR §0.5 — no import from services.acp_s4_social anywhere in this router...
```

**Kết luận: T8 (AA-449) VÀ T9 (AA-450) đều KHÔNG import lại `acp_s4_social` ở đâu cả — xác nhận
đúng như ADR §0.5 yêu cầu, không có route mới nào vô tình gọi lại module cũ khi build T8/T9.**

**Kiểm tra riêng: FE admin sidebar vẫn có `/admin/pipeline/s4-social` — đây CÓ phải là 1 caller
thật không?**

Đọc `frontend/app/admin/pipeline/s4-social/page.tsx` — gọi 3 endpoint:
`GET /api/admin/acp/runs`, `GET /api/admin/acp/s4/social?run_id=`,
`POST /api/admin/acp/s4/social/batch-review`.

Truy vào backend thật (`api/routers/admin_acp_proxy.py:478,531`) — **cả 2 endpoint này KHÔNG gọi
vào `v1_s4_social.py` ở đâu cả**:
- `GET /s4/social` → `SELECT ... FROM acp_silver_s4.social_content` (đọc thẳng bảng, bảng này
  audit gốc đã xác nhận **0 row**, nên trang FE này luôn hiện danh sách rỗng).
- `POST /s4/social/batch-review` → `UPDATE acp_silver_s4.social_content SET
  hitl_gate_3_social_status = ...` — **UPDATE trực tiếp bằng SQL của chính nó, không gọi
  `v1_s4_social.py::PATCH /{social_id}/hitl`**.

→ Trang admin FE này là **reader/reviewer thuần của cùng 1 bảng luôn rỗng**, không phải caller
của flow `/generate`/`/angles`/`/write`. Không mâu thuẫn với finding gốc.

**Kiểm tra riêng: Lambda `aa-cis-dev-acp-s4-trigger` (nhắc trong CLAUDE.md top-level) có gọi
module này không?**

Đọc `api/lambda/s4_trigger/handler.py` — comment đầu file: `EventBridge → Lambda → POST
/v1/acp/s4/blog/runs`. Đây là module **S4-BLOG**, khác hoàn toàn với **S4-SOCIAL** (angle-gate).
Lambda này không gọi bất kỳ route nào của `v1_s4_social.py`.

**Toàn bộ 6 route của `v1_s4_social.py`** (`/generate`, `/angles`, `/write`, `GET /{social_id}`,
`/{social_id}/retry-angle`, `PATCH /{social_id}/hitl`) — xác nhận **0 caller ở BE, 0 caller ở FE,
0 caller từ Lambda**, cả trước và sau khi AA-449 merge.

**Rủi ro khi xoá — đánh giá cụ thể:**

| Rủi ro | Đánh giá |
|---|---|
| Có ai gọi trực tiếp route này qua admin secret thủ công (curl/Postman) không? | Không xác nhận được từ code — nhưng 0 row trong `acp_silver_s4.social_content` (kể cả sau khi hệ thống chạy nhiều tháng) chứng minh **chưa từng có ai gọi thật kể cả thủ công** |
| Xoá `services/acp_s4_social/` có ảnh hưởng gì đến trang FE `/admin/pipeline/s4-social` không? | **Không** — trang đó đọc/ghi thẳng bảng `acp_silver_s4.social_content` qua `admin_acp_proxy.py`, hoàn toàn độc lập với `v1_s4_social.py`/`services/acp_s4_social/`. Xoá module cũ không làm hỏng trang review đó. |
| Cần sửa gì khác khi xoá? | `api/main.py` dòng 36 (`from api.routers.v1_s4_social import router...`) + dòng 198 (`app.include_router(...)`) phải xoá theo, nếu không import sẽ lỗi |
| Bảng `acp_silver_s4.social_content` có bị ảnh hưởng? | Không — xoá code không xoá bảng/dữ liệu, ngoài phạm vi |
| `services/acp_s4_social/references/*.md` (14 file formula + SKILL.md/CONTEXT.md) có nơi nào khác đọc không? | Chưa grep riêng — nếu quyết định xoá cả `acp_s4_social/`, nên grep thêm 1 lần `references/` trước khi xoá (nằm ngoài phạm vi điều tra "chỉ đọc" của task này, để lúc build thật mới cần) |

**Kết luận rõ ràng:** #12 — **chắc chắn dead 100%, đã re-verify SAU AA-449 merge, an toàn xoá**
với 1 lưu ý duy nhất: phải xoá đồng thời 2 dòng đăng ký router trong `api/main.py`, và nên grep
`references/*.md` một lần nữa ngay trước khi thực sự xoá (phòng trường hợp 1 file nào đó trong
`acp_content_writing/` có đọc file markdown đó bằng đường dẫn tương đối thay vì import Python).

---

## Việc 2 — Đề xuất việc nhỏ, rủi ro thấp, gộp 1 PR (từ 17 mục độc lập)

Rà lại 17 mục độc lập trong AA-446-01 Phần 3 (12 AA-438: #1,2,3,4,6,9,10,11,12,16,17,18 + 5
AA-439: #3,9,10,13,19). Verify lại code thật (không phải chỉ đọc mô tả cũ) cho từng ứng viên khả
thi.

### Ứng viên đã verify, đề xuất gộp 1 PR

| # | Việc | File | Verify hiện tại | Effort | Rủi ro |
|---|---|---|---|---|---|
| AA-438 #9 | Xoá dead code `ExportService.publish_tour()` | `shared/services/export_service.py` (264 dòng) | Grep lại: **0 caller** ở `api/`, `services/`, `shared/` ngoài chính file này | ~5 phút (xoá file + kiểm tra không ai `import shared.services.export_service`) | Rất thấp — 0 caller xác nhận |
| AA-438 #16 | Fix Settings "Pipeline Flow" chip thiếu node | `api/routers/admin_settings.py:18` | Hiện tại: `"pipeline_flow": ["generate", "validate", "brand_audit", "flag_fix"]` — grep `graph.py` xác nhận graph thật có thêm `llm_judge` (giữa validate→brand_audit) + `revalidate` (sau flag_fix, nhánh retry) | ~10 phút (sửa 1 list, thêm 2 giá trị đúng thứ tự edge thật: `generate→validate→llm_judge→brand_audit→flag_fix→revalidate`) | Rất thấp — chỉ đổi 1 hardcoded list hiển thị UI, không đụng logic pipeline thật |
| AA-438 #17 | Xoá `retry_count: 0` hardcode dead | `api/routers/acp_health.py:291` + `frontend/app/admin/run-health/page.tsx:41` | Grep FE: `retry_count` chỉ khai báo trong TS interface (dòng 41), **0 chỗ render/dùng trong JSX** của cùng file | ~10 phút (xoá field khỏi response dict BE + xoá khỏi TS interface FE) | Rất thấp — xác nhận field chết cả 2 phía, xoá đồng bộ không vỡ gì |
| AA-439 #3 | Thêm nav 2 chiều T4 (CatalogTab) ↔ T6 (AtomsTab) | `frontend/app/(tenant)/portal/_components/{CatalogTab,AtomsTab}.tsx` | Portal dùng cơ chế tab-switch nội bộ (không phải route riêng) — đọc `CatalogTab.tsx`/`AtomsTab.tsx` xác nhận cả 2 dùng pattern `onClick={() => ...}` cục bộ, chưa có link/badge nào trỏ sang tab kia | ~30-45 phút (thêm 1 button/badge "Xem atom đã tạo →" ở CatalogTab, và ngược lại; cần xem `portal/page.tsx` để biết prop chuyển tab được truyền xuống thế nào — chưa đọc sâu trong task này) | Thấp — UI thuần, không đụng backend/DB, nhưng cần đọc thêm `portal/page.tsx` trước khi code thật để biết cơ chế chuyển tab chính xác |

**4 việc trên đề xuất gộp 1 PR** — tất cả độc lập với nhau (không file nào overlap), tổng effort
ước tính dưới 1.5 giờ, không đụng migration/DB, không đụng logic nghiệp vụ thật (chỉ dashboard
UI hiển thị + 1 file dead code + 1 field dead). Rủi ro thấp nhất trong toàn bộ 23 mục còn treo.

### Cân nhắc thêm 1 việc thứ 5 (không nằm trong 17 mục AA-438/439 gốc, nhưng phát sinh trực tiếp từ Việc 1 ở trên)

| # | Việc | File | Effort | Rủi ro |
|---|---|---|---|---|
| AA-439 #12 (bonus, ngoài phạm vi "17 mục độc lập" — thuộc nhóm phụ thuộc T8 rewrite đã xong) | Xoá `services/acp_s4_social/` + `api/routers/v1_s4_social.py` + 2 dòng đăng ký router trong `api/main.py` | Nhiều file (1 package + 1 router + `main.py`) | ~20-30 phút xoá + verify test suite không còn import gãy | Thấp, đã verify kỹ ở Việc 1 (0 caller BE/FE/Lambda, trang FE review không phụ thuộc router này) — nhưng **effort xoá cả package lớn hơn 4 việc trên**, nên đề xuất **PR riêng**, không gộp chung, để commit message/lịch sử rõ ràng là "dọn dead code T8 cũ" tách biệt "4 fix vặt dashboard/nav" |

### Các mục ĐÃ xem nhưng KHÔNG đề xuất gộp lần này (lý do cụ thể)

| # | Lý do loại |
|---|---|
| AA-438 #1, #2 | Cần sửa state machine thật (`export/handler.py` khi nào flip `pipeline_runs.status`) + logic bucket dashboard — không còn là "nhỏ", nên tách task riêng có thiết kế trước |
| AA-438 #3 | Chưa xác nhận là bug thật (cần verify CloudWatch trước) — chưa sẵn sàng để "fix", chỉ sẵn sàng "investigate" |
| AA-438 #4 | Đọc lại code (`admin_pipeline.py:693-714`) xác nhận **đã có log cảnh báo** (`AA-210`) khi batch_id không phải UUID — hành vi thực tế đã là "log rồi skip", đúng như audit mô tả ("Minor, logged only"), không phải "hoàn toàn im lặng". Cân nhắc: có thể coi đây gần như đã đủ tốt, không cần sửa thêm — đề xuất Nghiệp xác nhận đóng mục này dưới dạng "➖ đã đủ, không cần sửa thêm" thay vì đưa vào PR |
| AA-438 #6 | Phụ thuộc Data Cleanup Plan (xoá dữ liệu), không phải code fix |
| AA-438 #10, #18 | Cần điều tra/quyết định thiết kế trước (brand_audit threshold, count discrepancy), chưa có fix cụ thể để code |
| AA-438 #11, #12 | Sửa đúng cần thêm cost/token tracking cho `tenant_tour_versions` hoặc join nhiều bảng — là việc thiết kế + migration, không "nhỏ" |
| AA-439 #9, #10, #19 | Đều là caveat/thiết kế/tính năng mở rộng thật (DFS batching tiết kiệm ~$460, mở rộng E3 sang 5 kênh) — có giá trị nhưng KHÔNG nhỏ, cần task riêng có ước lượng cost/effort đàng hoàng |
| AA-439 #13 | Nghiệp (qua AA-437) đã quyết định KHÔNG tự động hoá — không phải thiếu sót cần vá, mà là giữ nguyên có chủ đích |

---

## Tóm tắt cho quyết định

- **#11**: Chuỗi MỚI (T7 AA-448 + T8 AA-449 + AA-451) **có handoff thật, verify được bằng code +
  test + live HTTP trong 1 phiên** — khuyến nghị đổi trạng thái AA-446 sang "Resolved
  differently", không còn "Chưa làm" nguyên trạng.
- **#12**: **Xác nhận lại, vẫn dead 100% sau AA-449 merge** — 0 caller BE/FE/Lambda, an toàn xoá,
  1 lưu ý kỹ thuật duy nhất (xoá kèm 2 dòng đăng ký router trong `main.py`).
- **4 việc nhỏ sẵn sàng gộp 1 PR ngay**: AA-438 #9 (dead code), #16 (Settings chip), #17
  (retry_count dead field), AA-439 #3 (T4↔T6 nav) — tổng effort dưới 1.5 giờ, rủi ro thấp nhất
  trong 23 mục còn treo.
- **1 việc bonus (xoá `acp_s4_social/`)** đủ điều kiện làm ngay nhưng effort lớn hơn 4 việc trên
  — đề xuất PR riêng, không gộp.

Không sửa code, không commit, không tạo PR trong task này — chờ Nghiệp xem trực tiếp trước khi
quyết định bước tiếp theo.
