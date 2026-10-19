# AA-500 STEP0 — N1 tenant onboarding: thật sự đang sống hay đã chết? (2026-09-01)

Read-only, không sửa code. Trigger: `fetch_trips()` (`services/acp_planning/runway.py:205`)
còn marker `REVISIT WHEN N1 SHIPS — see AA-309`, nhưng Linear AA-309 Done bị archive cùng epic
cha AA-297 (Cancel, kiến trúc N0-N8 cũ). Câu hỏi: logic N1 có đang chạy sống trên production
không — dựa bằng chứng code+DB thật, không dựa trạng thái Linear.

## Kết luận

**N1 đã ship (PR #119, 20/04/2026) và CHẠY SỐNG một thời gian — nhưng đã bị xoá có chủ đích bởi
2 đợt refactor sau (AA-472, AA-473), là hệ quả trực tiếp của ADR-2026-038 (A/T split). N1 hiện
KHÔNG còn sống trên production.** Đây là case 2 trong 3 case STEP0 đặt ra ("N1 đã ship nhưng đã bị
thay thế/chết theo refactor sau"), không phải case 1 (đang sống) hay case 3 (chưa từng chạy thật —
nó CÓ chạy thật, để lại dữ liệu thật, chỉ là không còn ai gọi tới nữa).

Hệ quả trực tiếp cho AA-500/AA-511: marker `REVISIT WHEN N1 SHIPS` trong `runway.py` đã **lỗi thời
theo một cách khác với giả định ban đầu** — không phải "N1 chưa ship nên chưa revisit được", mà là
"cơ chế N1 định làm (Gate-A-approved licensing, D3/D4 marketplace) đã bị huỷ hướng hoàn toàn, sẽ
không bao giờ ship dưới hình hài đó nữa". Marker nên được viết lại hoặc xoá, không phải "chờ".

## Bằng chứng

### 1. `acp_shared.tenant_atom_state` — tồn tại, có dữ liệu thật, nhưng mồ côi

Query trực tiếp RDS thật (`aa-cis-dev-db`, qua ECS exec pattern, 01/09/2026):

- Bảng **tồn tại** (`information_schema.tables`).
- **18 row**, **2 tenant_id phân biệt**: `WanderLux Travel` (9 row, toàn bộ `assigned_angle=
  culture_craft`) và `ExploreAsia Co.` (1 row trong sample, `assigned_angle=physical_terrain`).
- Cả 2 tenant có `created_at` **giống hệt nhau tới micro-giây**
  (`2026-04-20 15:05:34.175731+00`) — dấu hiệu rõ đây là seed/demo data từ lúc build AA-309
  (20/04/2026), không phải 2 khách hàng B2B thật onboard độc lập.
- Tenant **mới nhất thật** trong `shared.tenants` — `test 1` (`slug=test-1`, tạo
  `2026-08-30 14:14:56`) — có **0 row** trong `tenant_atom_state`. Tenant được tạo sau khi N1's
  writer code đã bị xoá (AA-472, 26/08/2026) không hề ghi gì vào bảng này.
- `grep -rn "tenant_atom_state" --include=*.py .` (loại test) toàn repo chỉ ra **đúng 2 chỗ**,
  cả hai đều là comment lịch sử, không phải code thật:
  - `services/acp_planning/runway.py:214-215` (chính marker đang xét)
  - `api/routers/admin.py:191` — comment nói rõ bảng "no longer part of the N1 onboarding flow"
  - **Không còn INSERT/UPDATE/SELECT thật nào** vào bảng này ở bất kỳ router hay service nào.

→ Bảng là **artifact chết, mồ côi** — dữ liệu thật từ lần chạy build AA-309, nhưng không ai viết
hay đọc nó nữa kể từ 26/08/2026.

### 2. `assigned_angle` (cơ chế chống cannibalization) — code đã bị xoá hẳn

`grep -rln "assigned_angle" --include="*.py" .` → **0 kết quả** trong toàn bộ code (chỉ còn trong
dữ liệu DB ở mục 1, và trong docstring migration 098). Xác nhận qua git:

- PR #232 (`ca70dc2`, "AA-472: remove mandatory portfolio+angle from tenant onboarding") xoá
  `seed_tenant_atoms()`, `assign_tenant_angle()`, `get_tenant_mirror()`, và cả file
  `admin_marketplace.py` — khớp với memory `project_aa472_portfolio_angle_removed`.
- Comment tại `api/routers/admin.py:190-192`: *"AA-472: `seeded`/`seeded_tour_count`/
  `angle_assigned` are removed from this response -- acp_shared.tenant_atom_state is no longer
  part of the N1 onboarding flow (portfolio seeding and per-tenant angle assignment were both
  removed, Hướng B)."*
- Migration 119 (`api/migrations/119_tenant_onboarding_portfolio_optional.sql`, applied live
  26/08/2026 14:49:55 UTC) drop NOT NULL trên `tenant_onboarding.portfolio_id` — xác nhận
  `create_tenant()` giờ tự seed `portfolio_id=NULL`, không còn bước
  `POST /admin/tenants/{id}/seed-atoms` (route đã bị xoá) trước khi Gate A duyệt.

### 3. Gate A (Ms. Thư duyệt Mirror) — không còn tồn tại, kể cả bảng DB

- `grep -rn "gate-a" .` (toàn repo, .py/.ts/.tsx) → **0 kết quả thật** (2 match là false-positive
  regex — "gate-agnostic" và "gate-at-cap", không liên quan).
- `acp_shared.tenant_onboarding` (bảng Gate A dùng để track approval) — **query DB xác nhận:
  KHÔNG còn tồn tại** (`tenant_onboarding_exists: false`).
- Migration 120 (`api/migrations/120_drop_gate_a.sql`) — `DROP TABLE IF EXISTS
  acp_shared.tenant_onboarding` — **đã áp dụng live 28/08/2026 02:12:33 UTC**
  (`shared.schema_versions` version 120 xác nhận).
- Commit `f22337c` (PR #234, "fix: remove Gate A tenant-onboarding approval entirely [AA-473]"):
  xoá `approve_gate_a()`, `get_gate_a_status()`, bảng `tenant_onboarding`, guard kích hoạt trong
  `update_tenant()`, response field `pending_tenants` trong `list_tenants()`, và **tab
  "Onboarding" + pending-tenant cards trên admin UI** (`frontend/app/admin/tenants/page.tsx`).
  Frontend hiện có comment ngay đầu file xác nhận: *"immediately, no onboarding approval step
  left. The 'Onboarding' tab and its pending-tenant..."*
- Lý do ghi trong migration: *"Tenant giờ is_active=true ngay khi tạo... theo ADR-2026-038 §0.2
  (AA không gate hoạt động tenant, chỉ hậu-kiểm A4)."*
- **Có tenant nào từng được duyệt qua Gate A trước khi nó bị xoá không?** Không còn cách xác nhận
  trực tiếp — bảng chứa lịch sử approval (`tenant_onboarding`) đã bị DROP, không phải archive.
  Bằng chứng gián tiếp mạnh: comment trong `runway.py:214` (viết lúc AA-323 round 6, 13/08/2026 —
  **trước khi Gate A bị xoá**) tự xác nhận *"a tenant with a real, Gate-A-approved N1 onboarding
  (acp_shared.tenant_atom_state had 6 valid tour_ids for it)"* — tức có ít nhất 1 tenant thật
  từng qua Gate A tại thời điểm đó. Sau khi bị xoá, không truy lại được ai/khi nào nữa.

### 4. `shared.tenants` hiện tại — is_active mặc định true, không gate

Toàn bộ 4 tenant thật trong DB đều `is_active=true`:

| tenant | slug | created_at | is_active |
|---|---|---|---|
| Adventure Asia Internal | aa_internal | 2026-04-20 | true |
| WanderLux Travel | wanderlux-travel | 2026-04-20 | true |
| ExploreAsia Co. | exploreasia-co | 2026-04-20 | true |
| test 1 | test-1 | 2026-08-30 | true |

`create_tenant()` (`api/routers/admin.py:78-173`) insert `is_active=True` trực tiếp — không còn
bước duyệt riêng biệt nào. Không có tenant `is_active=false` để so sánh trạng thái "pending".

### 5. PR #119 (AA-CIS-App) — code còn hay đã bị thay/xoá?

`git log --grep="AA-309"` xác nhận PR #119 merge (`f521972`) 20/04/2026, tựa "N1 tenant
onboarding — seed/angle/Mirror/Gate A". Đối chiếu với main hiện tại (`b18166d`):

| Thành phần PR #119 | Trạng thái hiện tại | Xoá bởi |
|---|---|---|
| `acp_shared.tenant_atom_state` (bảng) | Tồn tại, mồ côi, 18 row cũ | (chưa bị DROP) |
| `seed_tenant_atoms()` | **Đã xoá** | AA-472 / PR #232 |
| `assign_tenant_angle()` (chống cannibalization) | **Đã xoá** | AA-472 / PR #232 |
| `get_tenant_mirror()` | **Đã xoá** | AA-472 / PR #232 |
| `admin_marketplace.py` (file) | **Đã xoá** | AA-472 / PR #232 |
| `acp_shared.tenant_onboarding` (bảng Gate A) | **Đã DROP (migration 120, live)** | AA-473 / PR #234 |
| `approve_gate_a()` / `get_gate_a_status()` | **Đã xoá** | AA-473 / PR #234 |
| Admin UI "Onboarding" tab | **Đã xoá** | AA-473 / PR #234 |
| Marketplace catalog/portfolio UI (AA-330, phần còn lại của PR #119) | Còn sống nhưng đã đổi hướng — xem AA-384 (posts_per_week free-input, Mirror info-only) và AA-444 (`GET /v1/marketplace` thay `marketplace_portfolios` làm nguồn view Marketplace thật của tenant) | AA-384 + AA-444 |

→ Không phải "revert" theo nghĩa git revert PR #119 — mà là 2 refactor có chủ đích (AA-472,
AA-473) tháo dỡ từng phần cơ chế licensing/gate của N1, đúng theo ADR-2026-038 Hướng B/§0.2 (AA
không gate hoạt động tenant nữa, chỉ hậu-kiểm qua A4). Phần UI Marketplace/catalog (AA-330 nửa
kia của PR #119) vẫn sống nhưng đã được AA-384/AA-444 viết lại hướng đi, không còn là "N1 gate"
nữa.

## Khuyến nghị cho AA-500/AA-511

1. Xoá hoặc viết lại marker `REVISIT WHEN N1 SHIPS — see AA-309` ở `runway.py:214-224` — không
   phải "chờ N1 ship" mà là "N1's licensing/gate direction đã bị huỷ, cần quyết định hướng khác
   nếu muốn giới hạn trip theo tenant (D3/D4 PRD ACP v2 vẫn treo, chưa có thay thế)".
2. `acp_shared.tenant_atom_state` là bảng mồ côi thật (18 row cũ, không caller) — ứng viên cho một
   đợt dọn dẹp kiểu AA-477/AA-481 (drop dead table) nếu không có kế hoạch hồi sinh cơ chế
   angle-assignment dưới hình hài khác.
3. Không cần build gì thêm cho câu hỏi STEP0 gốc — đây là investigation thuần, chưa commit
   (chờ gộp PR sau theo yêu cầu).
