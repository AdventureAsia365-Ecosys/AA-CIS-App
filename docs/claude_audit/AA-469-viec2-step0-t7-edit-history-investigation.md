# AA-469 Việc 2 — STEP0 Investigate: T7 Content Planning — edit sau finalize + xem lịch sử quý/tháng trước

Investigate only, không sửa code, không commit gì trong phiên này. Làm trên `main`, không dùng
worktree riêng (đọc-only). Đọc trực tiếp `api/routers/v1_planning.py`,
`api/migrations/092_acp_quarter_plan.sql`, `api/migrations/112_acp_shared_year_plan_and_metrics.sql`,
`services/acp_planning/quarter.py`, `services/acp_planning/allocator.py`,
`frontend/app/(tenant)/portal/_components/PlanningTab.tsx` toàn bộ phiên này (không tin lại mô tả
AA-448 dù đã Done) + query DB thật qua S3-mediated ECS exec pattern.

**Status: STEP0 xong — chưa build gì.** Đây là 1 trong 5 Việc AA-469 yêu cầu điều tra riêng trước
khi bất kỳ Việc nào được thiết kế/build — xem cross-Việc conflict note trong
`docs/claude_tasks/AA-469-redesign-t-series.md`.

---

## 1. Router thật — 8 endpoint, và phân biệt "khoá" (write-guard) vs. "ẩn" (không tồn tại)

`api/routers/v1_planning.py`, prefix `/v1/planning`:

| Endpoint | File:line | Vai trò |
|---|---|---|
| `POST /quarter-plan/preview` | `v1_planning.py:96-97` | Tính plan, KHÔNG lưu |
| `POST /quarter-plan` | `v1_planning.py:118-119` | Finalize — lưu + auto-approve (Gate B Option A) |
| `GET /quarter-plan` | `v1_planning.py:161-162` | Đọc plan đã finalize hiện tại |
| `GET /slot-grid` | `v1_planning.py:179-180` | Tính slot-grid tháng, đọc-only |
| `POST /metrics`, `POST /metrics/rollup` | `v1_planning.py:224-225`, `242-243` | Feedback loop (round 6), không liên quan Việc 2 |
| `GET /trip-reallocation/suggest`, `POST /trip-reallocation/confirm` | `v1_planning.py:250-251`, `266-267` | Round 6, không liên quan Việc 2 |

**409 lock chỉ tồn tại trong `finalize_quarter_plan` (`v1_planning.py:126-128`)**:
```python
if is_quarter_fully_locked(lock_status):
    raise HTTPException(status_code=409, detail=f"Q{body.quarter} {body.year} is fully locked ...")
```
`is_quarter_fully_locked` = "every week already produced-or-past" (module docstring, round-6
correction). Đây là **write-guard chặn re-finalize**, không phải read-restriction.

`get_quarter_plan` (`v1_planning.py:161-177`) **không có bất kỳ lock check nào** — `year`/`quarter`
là query param tự do, thất bại duy nhất là 404 nếu quý đó chưa từng finalize. **Backend đã sẵn sàng
đọc bất kỳ quý quá khứ nào đã finalize** — "khoá" (chặn re-finalize) và "ẩn" (không xem được) là 2
khái niệm khác nhau trong code hiện tại, đúng như giả thiết ban đầu của Nghiệp.

## 2. Không có endpoint edit nào — chỉ có preview (không lưu) và finalize (lưu, luôn tạo mới)

Không có PATCH/PUT nào trong router. `finalize_quarter_plan` (`v1_planning.py:118-159`) không có
guard "đã finalize rồi" — gọi lại `POST /quarter-plan` cho cùng year/quarter luôn tính lại từ dữ
liệu sống (trips/atoms/config hiện tại) rồi gọi `save_quarter_plan_version()` (tên hàm đã gợi ý:
tạo version mới, không phải patch).

## 3. Migration 092 — versioning được thiết kế đầy đủ TỪ NGÀY ĐẦU (S131, trước AA-448 rất lâu)

`api/migrations/092_acp_quarter_plan.sql` (comment tại dòng 9, `CREATE TABLE` tại dòng 33/43):

```sql
CREATE TABLE acp_shared.quarter_plan (
    plan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL, year INTEGER NOT NULL,
    quarter SMALLINT NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    current_version_id UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, year, quarter)
);
CREATE TABLE acp_shared.quarter_plan_version (
    version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES acp_shared.quarter_plan(plan_id),
    version_no INTEGER NOT NULL, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    source VARCHAR NOT NULL DEFAULT 'standard' CHECK (source IN ('standard', 'override')),
    approval_status VARCHAR NOT NULL DEFAULT 'pending' CHECK (approval_status IN ('pending','approved','rejected')),
    approved_by VARCHAR, approved_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (plan_id, version_no)
);
```

Comment gốc (dòng 9): *"Decision (Nghiep, S131): full versioning from day one (phuong an B), not
minimal-then-migrate ... Re-approving a quarter NEVER overwrites — it always creates a new version;
old versions stay queryable."*

`payload` là 1 JSONB blob duy nhất cho MỖI version — toàn bộ `QuarterPlan` đã tính được snapshot
nguyên khối, không phải per-slot row. Migration 112 (`api/migrations/112_acp_shared_year_plan_and_metrics.sql`)
**KHÔNG đụng đến `quarter_plan_version`** — chỉ thêm `year_plan_id` (wrapper gom nhóm 4 quý/năm)
và bảng riêng `content_metric_snapshot` (feedback loop, không liên quan Việc 2).

Hai trục "lịch sử" khác nhau cần phân biệt rõ khi thiết kế:
1. **Lịch sử giữa các quý khác nhau** (Q1 2026 vs Q3 2025) — mỗi `(tenant_id, year, quarter)` là 1
   row `quarter_plan` riêng, đã đọc được qua `GET /quarter-plan?year=&quarter=` (mục 1).
2. **Lịch sử version trong CÙNG 1 quý** (re-finalize nhiều lần) — bảng `quarter_plan_version` hỗ
   trợ nhiều row/`plan_id` theo thiết kế, nhưng câu hỏi có expose được version cũ (không phải
   version hiện tại) hay không phụ thuộc vào code ở mục 5.

## 4. `compute_quarter_plan()`/`compute_slot_grid()` là pure full-recompute — xác nhận đúng lo ngại "sửa 1 slot có thể bị ghi đè"

Cả 2 hàm (`services/acp_planning/quarter.py:148`, `services/acp_planning/allocator.py` — hàm
`compute_slot_grid`) đều tự ghi trong docstring: *"Pure computation — no DB, no LLM, 100%
unit-testable."* Không hàm nào đọc lại state đã lưu để seed chính nó — mỗi lần gọi tính lại từ đầu
theo `trips`/`atoms_by_trip`/`config`/`runway`. **Không có cơ chế override per-field/per-slot nào
bên trong.**

Tiền lệ duy nhất cho "manual override sống sót qua recompute" là `specials`/`excluded_trip_ids`
(`QuarterPlanRequest.specials`, `.excluded_trip_ids`, truyền vào body request, feed lại vào
`compute_quarter_plan()` mỗi lần gọi) — đây là pattern thật một UI "edit" cần tổng quát hoá (lưu
override, replay làm input mỗi lần tính), KHÔNG PHẢI patch trực tiếp lên output đã tính. Ở tầng
slot-grid (tháng), không có tham số override tương đương nào tồn tại.

Versioning (`save_quarter_plan_version()`, `quarter.py:359-423`) xác nhận triết lý "edit = version
mới" nhất quán: mỗi lần gọi luôn `INSERT` version mới (`next_version_no = MAX(version_no)+1`),
không bao giờ `UPDATE` payload cũ. `approve_quarter_plan_version()` (`quarter.py:425-467`) cũng
không bao giờ thu hồi `approval_status` của version cũ khi version mới được approve — chỉ di
chuyển `quarter_plan.current_version_id`.

## 5. Phát hiện quan trọng: 2 hàm cho đúng nhu cầu Việc 2(b) ĐÃ TỒN TẠI, nhưng chưa được T7 tenant router dùng

```python
async def fetch_current_version_no(tenant_id, year, quarter, pool) -> Optional[int]:  # quarter.py:497
    """AA-323 round 5 — Việc 2. ... lets the Preview screen show which version it's reading —
    with several approved versions accumulating per tenant/quarter..."""

async def fetch_quarter_plan_version(version_id: UUID, pool) -> Optional[dict]:  # quarter.py:521
    """AA-323 round 6, Phần A — read one SPECIFIC persisted version by its version_id,
    regardless of approval_status ... lets the Preview screen resolve a historical version
    from just its id in the URL."""
```

Cả 2 hàm được export trong `quarter.py`'s `__all__`, docstring tự nói rõ mục đích cho "Preview
screen" (đã build cho **AA-323's admin-side** version-history UI, không phải T7 tenant
self-service — `fetch_quarter_plan_version`'s docstring nói rõ caller là `admin_atoms.py`'s
preview-slotgrid). `v1_planning.py`'s import list (dòng 44-45) chỉ pull
`approve_quarter_plan_version, compute_quarter_plan, fetch_approved_quarter_plan,
save_quarter_plan_version` — **KHÔNG import 2 hàm trên**. Nghĩa là 1 khả năng "xem 1 version lịch
sử cụ thể qua id" đã có sẵn, đã test, đang sống trong codebase — chỉ cần 1 route tenant-facing
mới tái sử dụng, không cần logic backend mới.

## 6. Slot-grid tenant-facing hiện là ephemeral compute, không đụng bảng persist thật

`GET /slot-grid` (`v1_planning.py:179-222`) gọi `compute_slot_grid()` trực tiếp — **không bao giờ
đọc `acp_shared.acp_v2_slots`**. Bảng persist thật (ghi qua `persist_slot_grid()`,
`allocator.py`, dùng `ON CONFLICT (slot_id) DO NOTHING` nên không bị ghi đè khi re-run) chỉ được
chạm tới qua đường N7 weekly-trigger riêng (`allocate_and_persist_week()`, admin/Lambda-triggered).
T7 tenant month-view hiện tại hoàn toàn tách biệt khỏi cơ chế persist idempotent này.

## 7. Frontend (`PlanningTab.tsx`, 419 dòng) — pure view/compute, không có form edit nào, kể cả ẩn

- Year picker (`PlanningTab.tsx:177`) đã cho chọn `[DEFAULT_YEAR-1, DEFAULT_YEAR, DEFAULT_YEAR+1]`
  — nhưng "Preview" luôn `POST /quarter-plan/preview` (tính sống theo trip/atom HIỆN TẠI), **không
  bao giờ gọi `GET /quarter-plan` hay `GET /slot-grid`** — chọn năm trước rồi Preview chỉ cho thấy
  kết quả tính GIẢ ĐỊNH hôm nay, không phải plan thật đã finalize khi đó.
- `specials`/`excluded_trip_ids` bị hardcode `[]` ở cả `runPreview()` (`PlanningTab.tsx:94`) và
  `finalize()` (`PlanningTab.tsx:108`) — không có checkbox nào trên bảng trip (dù bảng đã render
  đủ field per-trip để gắn checkbox ngay).
- Không có version indicator nào — `finalizedVersionId` chỉ tồn tại tạm trong session hiện tại
  của component, bị clear ngay khi preview lại, không bao giờ fetch lại từ lịch sử.

## 8. Xác nhận qua DB thật (S3-mediated ECS exec, RDS aa-cis-dev-db)

- Tenant WanderLux (`a1b2c3d4-0001-4000-8000-000000000001`) — Q4 2026 đã có `version_no=2`
  (`source='override'`), Q3 2026 có `version_no=1` — bằng chứng trực tiếp: finalize lần 2 tạo
  version mới thật, không overwrite. Cả 2 đều ngày 2026-08-24, cùng ngày với dữ liệu Việc 1's
  atomize test.
- Tenant test khác (`00000000-0000-0000-0000-000000000001`) — Q3 2026 riêng đã có ít nhất 5 version
  (v1-v5), TẤT CẢ đều `approval_status='approved'` cùng lúc, `current_version_id` trỏ tới 1 version
  còn mới hơn (`98af1ff5-...`) không nằm trong 20 row đầu — xác nhận thực nghiệm đúng claim của
  `fetch_current_version_no()`'s docstring: "several approved versions accumulating per
  tenant/quarter".
- Toàn bộ `approved_by` quan sát được: `admin`, `nghiep`, `tenant:<uuid>`, hoặc note phiên Claude
  Code — 100% traffic build/verify, không phải sử dụng tổ chức thật (cùng caveat với Việc 1).

## Net conclusion

- **Việc 2(b) — xem lịch sử quý trước**: backend gần như đã sẵn sàng — `GET /quarter-plan` đã hỗ
  trợ query bất kỳ quý nào, `fetch_quarter_plan_version()`/`fetch_current_version_no()` đã có sẵn
  cho xem version cụ thể. Việc chính còn lại là **routing/wiring** (thêm route tenant-facing dùng
  2 hàm có sẵn + sửa frontend gọi `GET` thay vì luôn `POST /preview`), không phải backend logic mới.
- **Việc 2(a) — edit sau finalize**: cần **thiết kế mới thật sự** — không có primitive edit
  per-slot/per-field nào tồn tại, chỉ có pattern `specials`/`excluded_trip_ids` (override đầu vào,
  replay mỗi lần tính) để tổng quát hoá. Chưa quyết trong STEP0 này: edit có nghĩa là gì cụ thể
  (loại trừ/thêm trip vào danh sách hiện có — mở rộng pattern override sẵn có; hay sửa trực tiếp
  `destination_shares`/slot content — cần thiết kế mới hoàn toàn).

Theo yêu cầu của Nghiệp, Việc 2 KHÔNG nên được build trước khi cả 5 Việc AA-469 đã qua STEP0 — xem
`docs/claude_tasks/AA-469-redesign-t-series.md` cho cross-Việc conflict note.
