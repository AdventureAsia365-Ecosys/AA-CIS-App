# STEP0 — Audit i18n (VN→EN UI) + Tenant/Admin layout split

Audit thuần đọc code, KHÔNG sửa gì. Chạy trên `AA-CIS-App` branch `main`
(`4477877`), có tham chiếu thêm PR #184 (AA-427, **chưa merge** — xem lưu ý ở Phần 2).
Ngày audit: 22/08/2026.

**Ghi chú phương pháp quan trọng:** skill `aa-ecosys-repos` (last verified 31/07/2026,
S131) mô tả cấu trúc FE là `frontend/app/(internal)/admin/` — **đã lỗi thời**. Cấu trúc
thật hiện tại có **4 route group riêng biệt**, không phải 1:
- `frontend/app/admin/*` — Admin UI chính (dashboard, upload, pipeline/s1-s4, review,
  master-content, brand, tenants, settings, curation, atomize, marketplace,
  quarter-plan, produce) — dùng `AdminSidebar` + `adminUi.tsx`
- `frontend/app/(internal)/*` — 1 route group khác, path thật KHÔNG có prefix
  `/internal` (Next.js route group `(name)` không vào URL): `/upload`, `/brand`,
  `/catalog`, `/review` — dùng `InternalSidebar` + `internalUi.tsx`, **song song và
  KHÁC** với `/admin/upload`, `/admin/brand`, `/admin/review` (cùng tên tính năng,
  2 bộ code riêng — xem thêm AA-424 note bên dưới)
- `frontend/app/(tenant)/portal/*` — Tenant Portal, path thật là `/portal` (1 route
  duy nhất, không phải nhiều path con — xem Phần 2)
- `frontend/app/{login,tenant-login}/page.tsx` — 2 trang login riêng biệt (staff vs
  tenant)

Toàn bộ số liệu bên dưới lấy từ audit trực tiếp (`find`, `grep`, đọc file), không suy
diễn từ skill cũ.

---

## Phần 1 — Audit i18n (Vietnamese → English UI)

### 1.1 Cơ chế i18n hiện có: KHÔNG có gì cả

`package.json` (18 dependencies) — 0 package tên chứa `intl`/`i18n`/`locale`/`translat`.
Không có `next-intl`, không `react-i18next`, không thư mục `locales/`, không file
`*.json` locale nào (`find` toàn `frontend/` cho `*i18n*`/`*locale*`/`*translation*` ra
0 kết quả). **100% string hardcode trực tiếp trong JSX/TSX** — không có lớp trừu tượng
nào để thay ngôn ngữ qua config.

### 1.2 Text tiếng Việt hardcode trong UI thật (không tính comment)

Phương pháp: quét toàn bộ `frontend/{app,lib,components}` + `middleware.ts` theo regex
ký tự có dấu tiếng Việt (à-ỹ, đ/Đ, kể cả dạng hoa), sau đó đọc từng dòng để phân loại
**comment code** (`//`, `{/* */}`) vs **string thật sự render ra UI**.

**Kết quả: 52 dòng khớp regex trên 13 file — nhưng 47/52 là comment code** (giải thích
quyết định kỹ thuật, tham chiếu issue "Việc 1/2/3", "Phần A/B/C" — không hiển thị cho
user). Chỉ **5 dòng là text UI thật**, cả 5 đều nằm trong **Admin UI** (`/admin/*`),
**0 dòng** trong Tenant Portal (`(tenant)/portal/*`):

| # | File | Dòng | Text | Vị trí UI |
|---|------|------|------|-----------|
| 1 | `app/admin/master-content/page.tsx` | 629 | `tooltip="Điểm gate = min(validate, judge). Đây là điểm quyết định publish."` | Tooltip trên `ScoreBar` |
| 2 | `app/admin/master-content/page.tsx` | 633 | `tooltip="Validate sub-score (rule-based, chưa qua judge cap)."` | Tooltip trên `ScoreBar` |
| 3 | `app/admin/master-content/page.tsx` | 636 | `Overall = min(validate, judge) là điểm gate. Quality = validate sub-score.` | Text chú thích dưới score bar (mixed VN/EN cùng câu) |
| 4 | `app/admin/curation/page.tsx` | 562 | `sub={count === 0 ? "chưa có (AA-317)" : "distinctiveness"}` | Sub-label trên `StatCard` (mixed — nhánh else là tiếng Anh) |
| 5 | `app/admin/curation/page.tsx` | 791 | `Không thể hoàn tác. Các atom này sẽ không bao giờ xuất hiện trong slot allocator nữa.` | Câu cảnh báo trong dialog xác nhận xoá (title dialog "Delete N atoms?" đã là tiếng Anh — chỉ phần thân bị bỏ sót) |

**Nhận xét quan trọng:** cả 5 chỗ đều là kiểu "quên dịch 1 câu" trong 1 file phần lớn
đã tiếng Anh (title/label xung quanh đều English) — không phải cả trang bị bỏ sót.
Không tìm thấy Vietnamese trong: label chính, button, page title, empty state, error
toast, `confirm()`/`alert()`/`window.prompt()` (đã kiểm riêng — cả 3 chỗ dùng
`confirm()`/`prompt()` trong toàn bộ `app/` đều tiếng Anh).

**Giới hạn phương pháp:** regex chỉ bắt được text CÓ dấu. Text tiếng Việt không dấu
(vd. ai đó gõ "khong the hoan tac" không dấu) sẽ không bị bắt — nhưng quan sát qua các
file đã đọc, codebase này luôn gõ tiếng Việt có dấu đầy đủ kể cả trong comment, nên rủi
ro bỏ sót dạng không dấu là thấp, không phải 0.

### 1.3 Backend trả text tiếng Việt cho FE hiển thị trực tiếp — CÓ, xác nhận thật

`api/routers/admin_pipeline.py`, endpoint `POST /admin/pipeline/ingest-s3` (nhánh
`dry_run=True` — preview trước khi upload thật), 3 chỗ:

```python
# line 1127 — dry-run bị chặn vì file hash trùng
"message": "File đã upload trước đó — nội dung và tên file trùng hoàn toàn.",

# line 1153 — 1 tour cụ thể bị chặn vì trùng tên
"reason": "duplicate_tour", "message": "Tour này đã tồn tại trong hệ thống.",

# line 1159 — 1 tour cụ thể bị chặn vì thiếu field bắt buộc
"message": f"Thiếu fields: {', '.join(missing)}",
```

Xác nhận FE **render trực tiếp** giá trị này, không phải chỉ log:
`frontend/app/admin/upload/page.tsx:1340`:
```tsx
{fs.parseError || fs.parseResult?.message || "Upload failed"}
```
`parseResult.message` chính là field JSON ở trên — tenant/staff sẽ nhìn thấy câu tiếng
Việt này ngay trên UI khi upload Excel bị dry-run chặn (duplicate file/tour, thiếu
field). Đây là trang `/admin/upload` (S0), KHÔNG phải tenant portal.

Đã quét có mục tiêu (`detail`/`error`/`message`/`msg`/`reason` gần ký tự có dấu) toàn
bộ `api/`, `services/`, `shared/` — chỉ 3 chỗ trên là message thật trả ra response,
phần còn lại khớp cùng pattern chỉ là comment/docstring. Đây là quét có mục tiêu, không
phải exhaustive tuyệt đối — khuyến nghị chạy lại full `grep detail=` khi bắt tay sửa
thật (xem câu hỏi mở bên dưới) để chắc chắn không sót message dựng qua f-string nhiều
dòng hoặc constant riêng.

### 1.4 Quy mô — nhỏ, không cần chia nhiều PR

- 5 dòng UI text + 3 message backend = **8 chỗ cần sửa tổng cộng**, trải trên **3 file**
  (2 FE `.tsx` + 1 BE `.py`).
- Toàn bộ nằm trong Admin UI / S0 upload — **0 chỗ trong Tenant Portal**.
- Không có phụ thuộc dây chuyền (không phải sửa 1 chỗ kéo theo sửa chỗ khác) — mỗi dòng
  độc lập.

### 1.5 Đề xuất phương pháp

**Khuyến nghị: sửa trực tiếp từng string, KHÔNG cần dựng i18n library.**

Lý do:
- Quy mô 8 chỗ, 3 file — chi phí dựng `next-intl`/`react-i18next` (thêm dependency,
  locale JSON, provider wrapper, đổi cách gọi text ở MỌI component dù chỉ 8 chỗ cần
  đổi) lớn hơn nhiều giá trị thu được ở quy mô này.
- Yêu cầu hiện tại là "UI dùng tiếng Anh" (1 ngôn ngữ cố định), không phải "hỗ trợ đa
  ngôn ngữ chọn được" — nếu tương lai thật sự cần đa ngôn ngữ (vd. tenant quốc tế chọn
  VN/EN/khác), đó là quyết định sản phẩm khác hẳn, lúc đó mới đáng dựng i18n library,
  và dựng lúc đó sẽ dễ hơn (ít chỗ tiếng Việt hơn nữa vì đã dọn theo audit này).

**Điểm cần Nghiệp/Claude Chat chốt trước khi giao task sửa thật:**
1. Dòng #3 và #4 ở bảng 1.2 là câu MIXED VN/EN trong cùng 1 câu — sửa cả câu sang tiếng
   Anh hoàn toàn, hay chỉ thay phần tiếng Việt (rủi ro câu đọc gượng)?
2. 3 message backend (`admin_pipeline.py`) — dịch nguyên xi sang tiếng Anh, hay viết lại
   theo văn phong lỗi hiện có của các endpoint khác trong cùng file (chưa audit văn
   phong lỗi tiếng Anh hiện tại của toàn bộ `admin_pipeline.py` để đề xuất câu chữ cụ
   thể — nằm ngoài scope STEP0 này)?
3. Có cần audit thêm **log message** (không phải response, chỉ để debug nội bộ qua
   CloudWatch) không? Task gốc chỉ nói "UI" + "message lỗi cho FE hiển thị" — log nội
   bộ tiếng Việt không ảnh hưởng user, audit này **không tính log** vào scope, xác nhận
   lại nếu hiểu sai.

---

## Phần 2 — Audit tenant/admin layout split

### 2.0 Tình trạng PR #184 (AA-427) — quan trọng cho audit này

**PR #184 CHƯA merge** (`gh pr view 184` → `state: OPEN, mergedAt: null`, CI xanh).
`main` hiện tại (`4477877`) **chưa có** 3 route BFF mới của AA-427
(`/api/auth/tenant-login`, `/api/auth/tenant-logout`, `/api/tenant/me`) — code AA-427
chỉ tồn tại trên branch `pqnghiep1354/aa-427-security-tenant-jwt-cookie-khong-httponly-lo-qua`.
Middleware.ts hiện tại trên `main` **vẫn dùng flow login cũ** (cookie set qua
`document.cookie` phía client — lỗ hổng AA-427 đang vá). Điều này KHÔNG ảnh hưởng kết
luận audit layout bên dưới (route/shell/middleware structure giống nhau ở cả 2
branch — AA-427 không đổi route path hay layout, chỉ đổi cách mint cookie) — chỉ ghi
chú để không nhầm "đã merge" khi đọc báo cáo này.

### 2.1 T0 (BrandTab) và T1 (Rewrite/PoolTab) — PHÁT HIỆN NGƯỢC với giả định ban đầu

**Cả 2 ĐÃ nằm đúng trong shell Tenant Portal, KHÔNG phải Admin UI:**

- **T0** = `frontend/app/(tenant)/portal/_components/BrandTab.tsx`, mount qua
  `(tenant)/portal/page.tsx` khi `tab === "brand"`. Dùng style token riêng của portal
  (`./ui.tsx` — `T`, `serif`, `sans`, KHÔNG phải `adminUi.tsx`). Gọi
  `/api/tenant/admin/brand-identity` (tenant proxy, `Authorization: Bearer
  <cis_tenant_token>` — đã sửa đúng tenant JWT từ AA-424). Sidebar là
  `(tenant)/portal/_components/Sidebar.tsx` (nav: Dashboard/Pool/Catalog/**Brand**/API),
  KHÔNG phải `AdminSidebar`.
- **T1** = `frontend/app/(tenant)/portal/_components/PoolTab.tsx`, mount khi
  `tab === "pool"` — đầy đủ luồng browse pool + config rewrite (language/SEO
  mode/brand rules) + trigger `POST /api/tenant/v1/tours/pool/{id}/rewrite` (tenant
  proxy), kết quả đổ vào `CatalogTab.tsx` (tab "catalog", có approve/reject/edit).
  Toàn bộ 826 dòng đọc trực tiếp, không thấy chỗ nào import `AdminSidebar`/`adminUi`.

**Có 2 trang admin TRÙNG TÊN nhưng KHÁC tính năng/data model — dễ gây nhầm là "cùng 1
thứ bị đặt sai chỗ", nhưng thật ra không phải:**

| Trang admin | Feature thật | Data model | Khác gì T0/T1 |
|---|---|---|---|
| `app/admin/brand/page.tsx` (AA-129) | Quản lý **catalog nhiều brand** của AA (vd. "Terra Family", "Discreet Executive Adventure") cho content pipeline nội bộ | `GET/POST /api/admin/brands` — nhiều brand, có tên riêng | Không liên quan tenant, không phải "T0 bị đặt sai chỗ" |
| `app/(internal)/brand/page.tsx` | Sửa brand rules của **chính AA-internal** (tenant_id hardcode), dùng bởi content team | Cùng bảng `shared.tenant_brand_rules` với T0 thật, nhưng hardcode 1 tenant_id cố định (AA-424 note) | Là 1 use case hợp lệ riêng (staff quản lý brand của AA), không phải chỗ tenant thật đang bị nhốt |
| `app/admin/pipeline/s1/page.tsx`, `app/admin/s1-rewrite/page.tsx` | S1 Rewrite **core-CIS** (viết lại cho catalog AA, ghi `generated_content`) | Khác bảng với T1 (T1 ghi `tenant_tour_versions`, theo commit AA-425 tuần này) | Song song về khái niệm ("rewrite") nhưng khác luồng dữ liệu — không phải cùng 1 tính năng bị nhân đôi sai chỗ |

**Kết luận Phần 2.1: giả định "T0/T1 đang render trong shell Admin UI" — audit thật
KHÔNG xác nhận được.** T0/T1 đã có UI riêng, đúng shell tenant, đúng auth (tenant JWT
qua proxy), từ AA-424/AA-425 (tuần này). Nếu Nghiệp thấy cụ thể 1 màn hình brand/rewrite
nào đó "trông giống admin" thì rất có thể đang nhìn nhầm 1 trong 3 trang admin trùng tên
ở bảng trên — cần Nghiệp chỉ đích danh URL/screenshot để audit lại chính xác thay vì
audit tiếp trên giả định.

### 2.2 T6 (Atom Curation) — XÁC NHẬN ĐÚNG: 100% admin-only, và vấn đề SÂU hơn routing

`app/admin/curation/page.tsx` (826 dòng) + `app/admin/curation/preview/page.tsx` (448
dòng) — import `AdminSidebar`, gate ở `middleware.ts` prefix `/admin/curation` với
`roles: ["admin", "reviewer", "content"]` (KHÔNG có `tenant`). **0 component nào trong
`(tenant)/portal/_components/` liên quan atom/curation** — xác nhận T6 hoàn toàn không
tồn tại dưới dạng tenant-facing.

**Nhưng vấn đề không chỉ là route path/layout — backend `api/routers/admin_atoms.py`
(564 dòng) chưa sẵn sàng cho tenant, cụ thể:**

1. Auth: mọi endpoint dùng `x_admin_secret: str = Header(None)` (raw admin secret) —
   **không có nhánh nào chấp nhận tenant JWT**, khác hẳn pattern
   `_resolve_brand_tenant_id()` mà AA-424 đã làm cho brand-identity (thử Bearer JWT
   trước, fallback X-Admin-Secret). Curation chưa có tương đương.
2. Data scoping: các query list/patch atoms (`_LIST_SELECT_COLS`, `patch_atoms_bulk`,
   `patch_atom`) lọc theo `tour_id`/`atom_id`, **không lọc theo `owner_scope`**. Tìm
   thấy `tour_atoms.owner_scope` có 2 giá trị thật: `'platform'` (atom do AA sở hữu —
   đây là cái `/admin/curation` hiện đang thao tác) VÀ `owner_scope = tenant_id` (atom
   do **T5** — `services/acp_produce/tenant_pipeline.py`, code từ AA-425 tuần này —
   tạo ra từ nội dung tenant tự rewrite). Nếu build UI tenant curation bằng cách tái sử
   dụng thẳng các endpoint `admin_atoms.py` hiện tại mà không thêm filter
   `owner_scope = $tenant_id`, tenant A có thể thấy/sửa atom của tenant B hoặc atom
   platform — đây là **lỗ hổng auth y hệt lớp** mà AA-424 vừa vá cho brand-identity,
   nếu không cẩn thận sẽ tái tạo lại.

**Kết luận Phần 2.2: T6 cần cả 3 — route path mới, layout/shell tenant, VÀ backend
thêm auth+scoping — không phải chỉ "dời path/đổi layout wrapper".** Đây là task nặng
hơn hẳn T0/T1 (T0/T1 chỉ cần đổi vài dòng route/anh đã xong từ trước).

### 2.3 T3 (QA escalation) — KHÔNG xác định được đây là tính năng nào, cần Nghiệp chỉ rõ

Đã tìm nhưng không thấy tên/khái niệm "QA escalation" khớp trực tiếp với bất kỳ route,
router, hay bảng DB nào. 2 ứng viên gần nhất, KHÁC NHAU rõ rệt, cả 2 đều không khớp
hoàn toàn mô tả "escalation":

- **`/admin/review`** (`app/admin/review/page.tsx`, AA-234) — HITL Review Queue: staff
  sửa/duyệt `generated_content` sau khi tự động F1-F9/validate-judge-brand_audit fail.
  100% staff-only, không có khái niệm tenant "escalate" gì cả — staff chủ động vào xem
  hàng đợi, không phải tenant bấm nút escalate.
- **`CatalogTab.tsx`** (đã tenant-shelled, xem 2.1) — tenant tự approve/reject bản
  rewrite của CHÍNH MÌNH (`PATCH /api/tenant/v1/tours/versions/{id}` action
  approve/reject/edit). Đây là tenant tự QA nội dung của mình, không phải "escalate lên
  người thật" — reject chỉ đổi status, không có trường "lý do", không gọi endpoint nào
  báo cho staff.

**Không tìm thấy: nút/flow nào cho tenant chủ động yêu cầu con người xem xét
(escalate), không tìm thấy bảng/cột nào track "escalated_at"/"escalated_by", không
tìm thấy notification nào bắn sang staff khi tenant reject.**

**Câu hỏi mở bắt buộc trước khi giao task sửa T3:** "QA escalation" nghĩa cụ thể là
gì trong ADR-2026-038? 2 khả năng rất khác nhau về khối lượng việc:
(a) chỉ là cho tenant XEM trạng thái/lý do khi bản rewrite của họ bị hệ thống tự động
đánh rớt (mở rộng CatalogTab hiện có, nhỏ) — hay
(b) một luồng MỚI hoàn toàn: tenant chủ động bấm "escalate" để 1 nhân viên AA thật sự
xem xét case cụ thể, có thông báo/hàng đợi riêng cho staff (backend chưa có gì, việc
lớn hơn nhiều — gần như xây use case mới, không phải "tách layout").
Nếu là (b), đây không phải task tách layout nữa mà là 1 feature mới, nên tách khỏi cặp
"i18n + layout split" đang bàn, giao task riêng.

### 2.4 Middleware — ĐÃ SẴN SÀNG cho mọi phương án routing tương lai, không cần sửa

`middleware.ts` matcher hiện tại: `"/portal/:path*"` (wildcard, **không** liệt kê từng
sub-path cụ thể) → `PROTECTED_ROUTES` có `{ prefix: "/portal", roles: ["admin",
"tenant"] }`. Do dùng prefix-match + wildcard matcher, **bất kỳ route mới nào tạo dưới
`/portal/...`** (dù là route thật `/portal/t6-atoms/page.tsx` hay vẫn giữ kiểu tab hiện
tại) **tự động được gate đúng, không cần sửa `middleware.ts` hay
`PROTECTED_ROUTES` gì thêm.** Đây là tin tốt — không phải việc trong 2 task sắp giao.

`verifyTenantToken()` trong middleware xác thực JWT thật (`POST /auth/verify-tenant`),
không chỉ đọc cookie `cis_role` — đã đúng chuẩn từ trước AA-427 (AA-252 fix), AA-427 chỉ
đổi CÁCH cookie được set (httpOnly), không đổi cơ chế verify này.

**1 điểm cần lưu ý, không phải block:** middleware hiện cho `role: "admin"` đi qua
`/portal` y hệt `"tenant"` (dòng `{ prefix: "/portal", roles: ["admin", "tenant"] }`).
Nếu T6 (curation) build vào trong `/portal`, một admin session cũng sẽ vào được — có
thể là ý đồ (staff cần xem tenant portal để hỗ trợ) hoặc không — chưa rõ, hỏi Nghiệp.

### 2.5 Convention đặt route cho T0-T11 tương lai (kể cả T8 chưa build)

**Phát hiện quan trọng nhất của Phần 2: Tenant Portal hiện tại KHÔNG dùng route path
theo từng stage — toàn bộ là 1 route `/portal` duy nhất (`page.tsx`), chuyển tab bằng
`useState` (`tab === "brand" | "pool" | "catalog" | "api" | ...`), sidebar
(`Sidebar.tsx`) chỉ gọi `setTab()`, không có `<Link>`/router.push giữa các tab.** Đây
khác hẳn Admin UI (mỗi trang admin là 1 route Next.js thật: `/admin/curation`,
`/admin/brand`, v.v., có thể bookmark/share link thẳng).

Có nghĩa là **quy ước đề xuất trong task ("`portal/{t0-brand, t1-rewrite,
t3-review,...}/`") KHÔNG khớp pattern thật đang có** — nếu áp dụng máy móc sẽ tạo ra 2
kiểu điều hướng khác nhau trong cùng 1 app (T0/T1/T2 dùng tab-state, T6/T8 mới dùng
route thật) — không nhất quán, và cũng có nghĩa **T6/T8 xây theo route riêng sẽ không
bookmark/deep-link được theo cách người dùng portal hiện tại đã quen** (họ luôn ở
`/portal`, F5 lại về tab Dashboard).

**Đây là quyết định cần Nghiệp chốt, không tự chọn thay — 2 hướng, đánh đổi rõ:**

| Hướng | Ưu điểm | Nhược điểm |
|---|---|---|
| **(A) Tiếp tục tab-state trong `/portal/page.tsx`** — T6/T8 chỉ là thêm 1 component `+ tab mới vào `ExtTab` union + `Sidebar` nav, giữ nguyên convention hiện có | Nhất quán 100% với T0/T1/T2 đã có, 0 việc middleware, code đổi tối thiểu (đúng tinh thần AA-424/427 vừa làm) | `/portal/page.tsx` đã 189 dòng + import 8 tab component, sẽ phình to hơn theo thời gian; không deep-link được thẳng tab cụ thể (vd. share link "coi atom curation của tôi" — phải nói "vào Portal rồi bấm tab Atoms") |
| **(B) Route thật `/portal/t6-atoms`, `/portal/t8-produce`, ...** — mỗi T-stage 1 page.tsx riêng, `Sidebar` đổi sang `<Link>` | Deep-link/bookmark được, code-split tự nhiên (Next.js), khớp pattern Admin UI đã quen | Phải viết lại `Sidebar.tsx` (từ `setTab` sang `<Link>`+`usePathname`), T0/T1/T2 cũ (đang tab-state) có nên dời theo route thật luôn không (đổi để đồng bộ, hay để lại tab-state, sống chung 2 kiểu)? — nếu dời cũ theo, đây không còn là task nhỏ nữa |

**Không có rủi ro "breaking bookmark cũ"** cho A vì `/portal` vẫn giữ nguyên URL dù chọn
hướng nào (chỉ khác cách vào tab con) — nhưng hướng B nếu ÁP DỤNG NGƯỢC cho T0/T1/T2
hiện tại (đổi chúng từ tab sang route) thì CÓ — bất kỳ ai đã bookmark `/portal` với kỳ
vọng thấy Dashboard mặc định vẫn ổn, nhưng nếu có chỗ nào từng link sâu kiểu
`/portal?tab=brand` (chưa thấy pattern này trong code, nhưng chưa audit toàn bộ nơi tạo
link vào portal từ ngoài, vd. email template/Linear/Notion) thì cần kiểm tra riêng.

**Khuyến nghị (không tự quyết định thay Nghiệp):** hướng (A) — chỉ thêm tab — là lựa
chọn ít rủi ro/ít việc hơn nhiều cho 2 task sắp giao, và đúng tinh thần "T8 theo đúng
convention từ đầu, không dọn lại lần 2" nếu convention CHỌN là tab-state. Hướng (B) chỉ
nên chọn nếu deep-link là yêu cầu sản phẩm thật sự cần (chưa thấy trong context task).

---

## Tổng hợp câu hỏi mở cần Nghiệp/Claude Chat quyết trước khi giao task sửa

1. (i18n) Câu mixed VN/EN (#3, #4 bảng 1.2) — dịch cả câu hay chỉ thay từ tiếng Việt?
2. (i18n) Văn phong tiếng Anh cho 3 message backend — dịch sát nghĩa hay viết lại theo
   giọng lỗi hiện có của các endpoint khác (cần audit riêng nếu chọn cách 2)?
3. (i18n) Log nội bộ (CloudWatch) có tính vào scope "English UI" không? — audit này giả
   định KHÔNG.
4. (layout) T0/T1 audit KHÔNG xác nhận được giả định "đang ở shell admin" — Nghiệp có
   thể chỉ đích danh URL/screenshot cụ thể đang thấy sai để audit lại đúng chỗ không?
   (rất có thể đang nhìn 1 trong 3 trang admin trùng tên ở bảng 2.1, không phải T0/T1
   thật)
5. (layout) T3 "QA escalation" là (a) mở rộng CatalogTab cho tenant xem lý do rớt QA
   (nhỏ), hay (b) luồng escalate-tới-người-thật hoàn toàn mới (feature mới, không phải
   layout task)?
6. (layout) T6 cần thêm việc backend (tenant-JWT auth + `owner_scope` filter trong
   `admin_atoms.py`) trước khi có UI tenant an toàn — xác nhận việc này nằm trong task
   T6 sắp giao, hay tách thành task backend riêng trước?
7. (layout) Convention route cho T6/T8: chọn hướng (A) tab-state (khuyến nghị) hay (B)
   route thật? Nếu (B), có dời luôn T0/T1/T2 hiện tại theo không?
8. (layout) `/portal` hiện cho cả role `admin` vào (không chỉ `tenant`) — giữ nguyên
   hay giới hạn lại khi thêm T6 (dữ liệu nhạy hơn brand/pool)?
