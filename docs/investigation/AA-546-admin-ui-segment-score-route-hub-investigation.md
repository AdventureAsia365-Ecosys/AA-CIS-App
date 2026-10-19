# AA-546 — Điều tra hiện trạng + nhu cầu UI Admin giám sát Segment/Score/Route/Hub platform-wide

STEP0 thuần túy — không code, không thiết kế UI. Mọi kết luận có bằng chứng file:line/query thật.

## Phát hiện quan trọng nhất (đảo ngược premise ban đầu của issue)

**Issue này giả định UI Admin giám sát Segment/Score/Route/Hub CHƯA tồn tại, chỉ mới có 2 backend
endpoint (`/admin/dashboard/routes`, `/admin/dashboard/score`) chưa có UI gọi. Điều đó SAI.**

Có 1 trang admin THẬT, ĐANG SỐNG, ĐÃ CÓ TRONG NAV, tiêu thụ đúng 4 endpoint của `admin_dashboard.py`
(gồm cả `/segments` và `/slate`, không chỉ 2 cái issue nêu):

- **`frontend/app/admin/atom-curation/page.tsx`** (883 dòng) — route `/admin/atom-curation`,
  có icon `Puzzle` trong `AdminSidebar.tsx:228-230`, thuộc nhóm nav chính (không phải
  `CONTENT_AUTHORING_NAV` cũ).
- Xây tại **AA-527 "bổ sung"** (05/09/2026, comment "Quyết định bố cục — Phương án C"), PR **#312**,
  **đã merge vào `main`** (`git log`: commit `2b7eb13`, "feat: Atom Curation → 8-section T5-T11
  dashboard shell (Phương án C) [AA-527] (#312)"). Memory nội bộ trước đó ghi PR #312 "chưa merge"
  — **đã lỗi thời, cần sửa lại memory này** (không phải việc của issue này, chỉ ghi nhận).
- Trang có sidebar phụ (nested trong `AdminSidebar` toàn site) liệt kê đúng **8 section T5-T11**
  (`page.tsx:67-74`): `01 · Atomize`, `02 · Segment`, `03 · Score`, `04 · Route/Hub`, `05 · Slate`,
  `06 · Write/Gate`, `07 · Review`, `08 · Publish` — header có dropdown chọn 1 Tour làm anchor lọc
  cho cả trang (section 01 duy nhất không cần chọn Tour).
- Gọi đúng 4 endpoint: `GET /api/admin/dashboard/segments|score|routes|slate?tour_id=` (đã ghi
  trong docstring file, dòng 19-22) + tái dùng `admin_a4.py`'s `content-log`/`publish-log` cho
  Write-Gate/Review/Publish.

→ **Câu hỏi cốt lõi Phần 1 của issue** ("2 endpoint đã build ở AA-545 đang gọi từ đâu, hay chỉ mới
có backend") **đã có câu trả lời dứt khoát**: KHÔNG PHẢI "chỉ mới có backend" — có UI thật, đã
merge, đã trong nav, TRƯỚC CẢ khi AA-545 chạy (AA-527 build 05/09, AA-545 build 06/09) — AA-545
chỉ sửa DATA LAYER bên dưới 1 trang admin đã tồn tại, không phải xây UI mới trên nền trống.

## Phần 1 — Cấu trúc trang Admin thật (find thật, đối chiếu skill cũ)

`find frontend/app/admin -name page.tsx` — **17 trang** (không phải 10 như skill liệt kê):

```
a4-oversight, atom-curation, brand, dashboard, llm-usage, master-content,
pipeline/{s1,s2,s3,s4-blog,s4-social} (5 trang), review, run-health,
s1-rewrite, settings, tenants, upload
```

Đối chiếu với danh sách skill (`dashboard, upload, s1-rewrite, review, run-health, master-content,
brand, tenants, settings, a4-oversight`) — **skill thiếu 7 trang thật**: `atom-curation`,
`llm-usage`, và cả 5 trang `pipeline/s2,s3,s4-blog,s4-social,s1` (ACPv1 legacy — vẫn tồn tại file,
KHÔNG có trong nav `AdminSidebar.tsx`, chỉ truy cập trực tiếp bằng URL, theo đúng comment tại
`AdminSidebar.tsx:27-30`: "routes/pages... untouched and still reachable directly by URL" — không
phải bug, là quyết định có chủ đích khi bỏ nhóm ACPv1 khỏi nav). `marketplace` (skill không nhắc
nhưng CLAUDE.md cũ có) đã bị xoá hẳn (không còn file) — khớp AA-472 "portfolio+angle removed".

Nav thật đầy đủ (`AdminSidebar.tsx`, grep `href.*admin`):
`dashboard`, `tenants`, `run-health`, `a4-oversight`, `llm-usage`, `atom-curation`, `settings` +
nhóm `CONTENT_AUTHORING_NAV` (`upload`, `s1-rewrite`, `review`, `brand`, `master-content`).

**`admin/dashboard/page.tsx` không liên quan gì tới Segment/Score/Route/Hub** — đọc trực tiếp
(492 dòng): gọi `/api/admin/metrics/seo`, `/api/admin/metrics/library`, `/api/admin/metrics?days=7`
— đây là dashboard CHẤT LƯỢNG NỘI DUNG cũ (Model/Versions/Avg Score/Cost, SEO metrics, Score
Distribution của **`generated_content.quality_score`** — một khái niệm "Score" HOÀN TOÀN KHÁC,
trùng tên nhưng khác nghĩa với `atom_ranking`'s Score). Cần lưu ý tránh nhầm 2 khái niệm "Score"
khi thiết kế UI tiếp theo — nếu Claude Chat soạn spec dùng chữ "Score" mà không chỉ rõ, dễ gây
hiểu lầm là sửa `admin/dashboard/page.tsx`.

## Phần 2 — Dữ liệu Segment/Score/Route/Hub cần hiển thị: field thật + gap thật

### 2a. Field trả về thật của 4 endpoint (đọc trực tiếp `admin_dashboard.py`, đã build/sửa ở AA-545)

**`GET /admin/dashboard/segments?tour_id=`** (dòng 62-101, sửa ở AA-545 — bỏ tenant, thêm market):
`segment_id, canonical_place, canonical_action, member_count, market, total_rank, recurrence,
excluded_reason, route_id, route_hub_name` — 1 hàng/(Segment, market).

**`GET /admin/dashboard/score?tour_id=`** (dòng 118-145, sửa ở AA-545):
`market, segment_id, canonical_place, canonical_action, demand_rank, recurrence_rank,
questions_rank, said_rank, total_rank, demand_market, demand_volume, recurrence, questions, said,
excluded_reason, computed_at` — 1 hàng/(Segment, market).

**`GET /admin/dashboard/routes?tour_id=`** (dòng 161-198, sửa ở AA-545 — bỏ tenant/score cũ, thêm
market + AVG(total_rank) tính lúc đọc):
`route_id, hub_id, hub_name, ordered_segment_ids, first_day, last_day, created_at, version,
superseded_at, market, score` — 1 hàng/(Route version, market).

**`GET /admin/dashboard/slate?tour_id=`** (dòng 216-245, KHÔNG đổi ở AA-545 — Slate vẫn per-tenant
đúng thiết kế): `subject_id, tenant_id, tenant_name, channel, state, score, segment_id, route_id,
cleared_bar_reason, created_at` — vẫn có tenant thật (đúng, vì Slate không nằm trong phạm vi
platform-wide AA-545).

### 2b. GAP THẬT phát hiện được — regression hiển thị do AA-545, chưa có ai sửa (KHÔNG phải bug mới cần code ngay, chỉ ghi nhận theo đúng yêu cầu KHÔNG CODE của issue này)

Đọc `frontend/app/admin/atom-curation/page.tsx` dòng 507-595 (`SegmentRow`/`ScoreRow`/`RouteRow`
interface + cột hiển thị) — cả 3 type đều có field `tenant_name: string | null` và 1 cột "Tenant"
render `r.tenant_name ?? "—"` (dòng 508/522, 532/547, 558/586). **AA-545 đã bỏ hẳn `tenant_name`
khỏi cả 3 response** (xác nhận ở mục 2a trên — không còn trong SELECT list) → sau khi AA-545 deploy
(đã deploy, task def `:229`), cột "Tenant" trên cả 3 panel Segment/Score/Route/Hub của trang
`atom-curation` **LUÔN hiện "—"** cho MỌI hàng — không crash (không tham chiếu field không tồn tại
theo cách gây lỗi JS, chỉ optional-chaining ra `undefined ?? "—"`), nhưng cột này **không còn mang
thông tin gì** (khác biệt thật với dữ liệu, không phải giả thuyết — đã verify field `tenant_name`
không có trong response thật qua `GET /admin/dashboard/routes` live ngày 06/09, xem comment build
AA-545 trên Linear).

Đi kèm: **không có cột "Market" nào** trên cả 3 bảng — trong khi backend giờ trả **nhiều hàng/1
Segment hoặc Route** (1 hàng/market, tối đa 6 hàng) mà KHÔNG có field nào phân biệt trực quan ngoài
số liệu rank/score tự nó khác nhau. `rowKey={r => r.segment_id}` / `rowKey={r => r.route_id}`
(dòng 520, 578) dùng `segment_id`/`route_id` làm React key — với 6 hàng cùng `segment_id`/`route_id`
(khác `market`), key KHÔNG còn duy nhất → React sẽ cảnh báo duplicate key trong console, và hàng
hiển thị trông như bị lặp lại vô nghĩa với người xem không biết về khái niệm market.

**Đây là 2 gap thật, cụ thể, có file:line — ứng viên hợp lý nhất cho phạm vi UI Admin sắp thiết kế**
(sửa cột Tenant → Market, xử lý key/group theo market) — không phải suy đoán "cần dashboard đẹp".

### 2c. Endpoint platform-wide (không tour_id) cho Segment/Score — CHƯA có, xác nhận đúng issue nêu

`grep` toàn bộ `api/routers/*.py` tìm mọi endpoint chạm `acp_contract.atom_segment`/`atom_ranking`
— chỉ có 3 file: `admin_dashboard.py` (tour-scoped, bắt buộc `tour_id`), `admin_atoms.py` (cũng
tour-scoped), `v1_route_hub.py` (tenant-scoped qua tour đã pick). **Không có bất kỳ endpoint nào
cho "xem toàn bộ Segment platform-wide, tìm theo place/verb, xem recurrence xuyên suốt catalog"**
— đúng như issue tự nêu là ví dụ gap có thể có. Module docstring `admin_dashboard.py:10-15` tự ghi
rõ đây là quyết định có chủ đích ("expensive/meaningless để page qua mọi tenant/tour 1 lúc cho lần
xây đầu"), không phải thiếu sót vô tình.

## Phần 3 — Nhu cầu giám sát vận hành THẬT (không tự suy đoán thêm)

Đối chiếu với 3 gợi ý issue tự nêu:

1. **"2 tenant cùng 1 tour → 1 Route" đang hoạt động đúng, cần theo dõi liên tục"** — hiện **KHÔNG
   có cơ chế theo dõi liên tục nào** (guarantee này được verify 1 LẦN, thủ công, khi build AA-545 —
   xem Linear comment 06/09). Không có alert/cron/test tự động nào re-check định kỳ. Đây là nhu cầu
   THẬT nhưng CHƯA được xác nhận là ưu tiên — chỉ có 1 tour thật đang có dữ liệu Route trong toàn hệ
   thống (`4bf83a2c-...`), quy mô hiện tại không đủ lớn để biện minh 1 hệ thống giám sát tự động
   riêng; xem qua trang `atom-curation` (đã có, panel 04) là đủ cho quy mô hiện tại.
2. **"phát hiện Segment/Route bất thường (0 recurrence dù nhiều tour cùng place/verb)"** — panel
   `02 · Segment` đã hiện cột "Recurrence" trực tiếp từ `atom_ranking.recurrence` (xem 2a) — NHƯNG
   chỉ tour-scoped, không so sánh xuyên-tour để phát hiện "lẽ ra phải trùng mà không trùng" (đúng
   gap ở Phần 2b của comment build AA-545: 23 hàng cũ hiện có 0 case nào recurrence>1 thật để kiểm
   chứng cơ chế này bằng mắt). Cần dữ liệu thật nhiều tour hơn để nhu cầu này trở nên cụ thể, hiện
   chưa đủ cơ sở để thiết kế 1 tính năng riêng.
3. **"theo dõi hiệu năng Slate liên tục (điều kiện Q3 AA-545)"** — đã đo 1 lần thủ công trước/sau
   migration (comment Linear AA-545). Không có cơ chế theo dõi liên tục (không APM, không log
   query-time tự động cho riêng query này). Nhu cầu thật nhưng KHÔNG có tiền lệ theo dõi hiệu năng
   query cụ thể nào khác trong toàn bộ codebase (không tìm thấy 1 nơi nào khác làm việc này) — nếu
   xây riêng cho Slate sẽ là tính năng MỚI hoàn toàn, không phải mở rộng cái đã có.

**Kết luận Phần 3**: không xác định được nhu cầu vận hành nào đủ cụ thể/cấp bách để đề xuất tính
năng MỚI ngoài phạm vi đã có. Đề xuất scope tối thiểu thật sự cần, nếu có build tiếp: **sửa 2 gap
hiển thị đã tìm thấy ở Phần 2b** (cột Tenant → Market, key theo market) trên trang `atom-curation`
đã có sẵn — không cần trang mới, không cần endpoint mới.

## Tóm tắt cho spec tiếp theo

- UI Admin giám sát Segment/Score/Route/Hub **đã tồn tại** (`/admin/atom-curation`, AA-527, đã
  merge, đã deploy) — việc "thiết kế UI mới" theo ADR-0003 phần lớn đã hoàn thành từ trước, KHÔNG
  cần xây từ đầu.
- Việc thật còn lại (nếu Claude Chat soạn spec tiếp theo): sửa 2 gap hiển thị cụ thể ở trang đã có
  (cột Tenant→Market, xử lý duplicate key theo market) trên 3/4 panel (Segment/Score/Route — panel
  Slate không bị ảnh hưởng, vẫn đúng per-tenant).
- Không có nhu cầu vận hành mới nào đủ bằng chứng để đề xuất tính năng/endpoint mới ngoài phạm vi
  trên.
