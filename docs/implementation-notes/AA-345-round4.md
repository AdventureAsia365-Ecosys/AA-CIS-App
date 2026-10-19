# AA-345 — Round 4 (2 vấn đề live-verify mới: Curation không hiện tour vừa atom hoá + ngày "Atomized on" vẫn sai)

Ngày: 2026-08-13. Branch `pqnghiep1354/aa-345-atomize-round4` từ `main`
(sau khi #131, #132, #133, #134 đã merge). Bổ sung các file round trước,
không thay thế.

**Kết luận ngắn gọn: cả 2 vấn đề đều KHÔNG tái hiện được trong code hiện tại
(post-#134, đang chạy live production) khi kiểm tra bằng dữ liệu thật.
Không có thay đổi code nào cho logic của 2 trang — chỉ thêm test để khoá
lại 2 case cụ thể round này yêu cầu.**

## Vấn đề A — Tour vừa atom hoá không hiện trong Atom Curation

### Điều tra (theo đúng thứ tự yêu cầu, dùng dữ liệu thật, không data giả)

Tour thật Nghiep atomize: "Jomolhari Trek", `tour_id =
8555c70f-26bd-4722-91ba-e6dc3b97ac50` (xác nhận qua
`silver_aa_internal.raw_tours`, tên khớp chính xác). Atom count tổng DB
thật tăng 2416→2422 (+6), khớp báo cáo.

1. **DB thật**: `acp_contract.tour_atoms` có đúng 6 dòng cho tour_id này,
   `is_empty_marker=false`, `deleted=false`, `created_at` =
   `2026-08-13T04:11:38 UTC` (query trực tiếp qua RDS, S3-mediated ECS
   exec pattern).
2. **Backend logic (`api/routers/admin_atoms.py::list_atoms`)**: câu SQL
   dùng `ta.tour_id = ANY($1::uuid[])` — mảng 1 phần tử hợp lệ trong
   Postgres, không có off-by-one hay điều kiện `if len > 1` nào trong code
   (đọc trực tiếp source, không đoán). Mô phỏng đúng câu SQL này với
   `tour_ids = ['8555c70f-...']` → trả về đúng 6 atom, đúng `tour_name =
   "Jomolhari Trek"`.
3. **Live HTTP thật, KHÔNG mô phỏng**: `curl` trực tiếp vào chính container
   ECS đang phục vụ traffic thật (`aa-cis-dev-api`, qua `aws ecs
   execute-command`, gọi `localhost:8000` bên trong container — bỏ qua
   API Gateway/frontend hoàn toàn) với
   `GET /admin/atoms?tour_ids=8555c70f-26bd-4722-91ba-e6dc3b97ac50` →
   **trả về đúng 6 atom, đúng tour_name, đúng activity_type/emotional_hook
   cho từng atom**. Endpoint backend đang chạy live là ĐÚNG.
4. **`GET /admin/atoms/summary` (nguồn dữ liệu THỨ HAI mà trang Curation
   cần — không filter theo tour_ids)**: cũng curl trực tiếp vào container
   thật → `by_tour` (138 tour) CÓ chứa đúng entry Jomolhari Trek:
   `{"tour_id": "8555c70f-...", "atom_count": 6, "atomized_at":
   "2026-08-13T04:11:38...+00:00"}`. Endpoint này cũng ĐÚNG.
5. **Frontend logic (`frontend/app/admin/curation/page.tsx`)**: danh sách
   hiển thị (`orderedSections`) là giao của 2 nguồn trên —
   `summary.by_tour.filter(t => atomsByTour.has(t.tour_id))`. Với dữ liệu
   THẬT ở bước 3+4 (không phải giả định), Jomolhari Trek có mặt ở CẢ HAI
   nguồn → phải xuất hiện trong danh sách. Đọc kỹ logic parse URL
   (`highlightTourIds` — lazy `useState` initializer đọc
   `window.location.search`, `split(",")`/`join(",")`) cho trường hợp
   N=1: không có khác biệt so với N=2 (không có dấu phẩy thì `split(",")`
   trả mảng 1 phần tử, không lỗi).
6. **Loại trừ khả năng deploy cũ (stale Vercel)**: `vercel inspect` xác
   nhận deployment production hiện tại (`aa-cis.lumiguides.it.com`) được
   tạo lúc `2026-08-13T04:02:24Z` — ĐÚNG lúc merge PR #134, và SỚM HƠN 9
   phút so với lúc atomize Jomolhari Trek (04:11:38Z). Code frontend chạy
   live tại thời điểm Nghiep test chắc chắn là code post-#134 (khớp: URL
   Nghiep thấy dùng `tour_ids=` số nhiều — param này chỉ tồn tại từ PR
   #133 trở đi, không phải code cũ hơn).

### Kết luận Vấn đề A

**KHÔNG tìm được lỗi trong code hiện tại.** Cả backend (2 endpoint) lẫn
logic frontend đều đúng khi kiểm tra bằng chính tour_id thật từ sự cố, qua
dữ liệu live thật (không phải test giả lập). Đây KHÔNG phải trường hợp
"CI xanh nên coi là đúng" — đã verify độc lập bằng curl trực tiếp vào
container đang chạy production traffic.

**Không kết luận được đây là bug đã fix hay chưa từng là bug** — không thể
loại trừ hoàn toàn 1 sự cố nhất thời phía trình duyệt (VD: tab đã mở từ
trước, xem trang khi fetch chưa xong, hoặc 1 browser session/tab cũ) vì
không tái hiện được qua UI thật:

- **Blocker phát hiện thêm (AA-253, đã biết từ trước, KHÔNG phải bug mới
  của round này)**: đăng nhập role `content` (dùng trong toàn bộ E2E suite
  hiện có vì `admin`/`admin2026` đã stale — xem
  `tests/e2e/aa300-curation.spec.ts`'s comment) nhận cookie
  `cis_api_token`/`cis_role`, KHÔNG có `cis_admin_token` — mà
  `frontend/lib/auth-server.ts::requireAdmin()` bắt buộc cookie này, nên
  MỌI lời gọi `/api/admin/*` với role `content` đều 401 (verify qua curl
  trực tiếp vào BFF proxy local). Test hiện có (`aa300-curation.spec.ts`)
  đã biết và chấp nhận 2 nhánh kết quả (data thật HOẶC error-state) —
  nghĩa là **suite E2E hiện tại không thực sự chứng minh được data thật
  render đúng, chỉ chứng minh trang không crash**. Không thử reset mật
  khẩu tài khoản `e2e-test-admin` có sẵn trong DB (hành động này bị chính
  safety classifier của công cụ chặn khi thử — dừng lại đúng lúc thay vì
  tìm cách lách qua, đúng theo hướng dẫn). Đề xuất Nghiep: tạo 1 issue
  riêng để có credential admin JWT hợp lệ cho môi trường dev (không thuộc
  phạm vi AA-345) — nếu không có, sẽ không có cách nào verify UI thật của
  BẤT KỲ trang `/admin/*` nào qua Playwright, không riêng AA-345.
- Nếu Nghiep gặp lại: xin chụp màn hình + Network tab (request/response
  thật của `/api/admin/atoms?tour_ids=...` lúc đó) — sẽ xác định được ngay
  đây là lỗi render hay lỗi fetch tại đúng thời điểm xảy ra.

### Test thêm (Vấn đề A)

`tests/unit/test_aa300_admin_atoms.py::TestListAtoms::
test_tour_ids_single_element_produces_single_element_param` — case N=1
tour_id cụ thể, assert params thật gửi xuống SQL là mảng 1 phần tử đúng
giá trị (gap thật: test cũ `test_tour_ids_plural_wins_over_singular_tour_id`
đã dùng 1 tour_id nhưng chỉ assert clause `ANY(...)` được chọn, chưa assert
nội dung params).

## Vấn đề B — Ngày "Atomized on" vẫn hiện "Aug 12"

### Điều tra — phân biệt đúng 2 khả năng theo yêu cầu

1. **Rà lại code hiện tại (post-#134, không giả định PR mô tả đúng)**: cả
   `frontend/app/admin/atomize/page.tsx` và
   `frontend/app/admin/curation/page.tsx` đều có `formatDate()` hardcode
   `timeZone: "Asia/Ho_Chi_Minh"` — grep toàn bộ "Atomized"/`atomized_at`
   render site trong cả 2 file: **đúng 1 chỗ gọi `formatDate()` mỗi trang,
   không có chỗ thứ 2 bị bỏ sót**. PR #134 ĐÃ áp dụng đúng cho cả 2 trang
   như mô tả — không phải trường hợp "chỉ sửa Atomize, quên Curation".
2. **Backend trả timestamp có timezone hay naive?** — kiểm tra response
   JSON thật (không giả định): `"created_at":
   "2026-08-13T04:11:38.461672+00:00"` — CÓ offset `+00:00` rõ ràng
   (asyncpg trả `datetime` timezone-aware cho cột `timestamptz`,
   `.isoformat()` giữ nguyên offset). Nếu thiếu offset, `new Date(iso)`
   phía JS sẽ parse theo LOCAL time thay vì UTC, âm thầm vô hiệu hoá
   `timeZone` option — đã loại trừ khả năng này bằng dữ liệu thật.
3. **1 tour cụ thể để kiểm chứng Khả năng 1 vs Khả năng 2**: "Peaks and
   Passes of the Nubra Valley" — `created_at` thật =
   `2026-08-12T13:27:51.974314Z`. Quy đổi: 13:27 UTC + 7h = 20:27 giờ VN —
   **VẪN là ngày 12/8 ở cả UTC lẫn giờ VN**. Đây KHÔNG phải 1 case biên
   qua nửa đêm giờ VN → hiển thị "Aug 12" cho tour này là **Khả năng 1:
   dữ liệu đúng, không phải bug**.

### Kết luận Vấn đề B

**PR #134 đã fix đúng và đủ cho CẢ 2 trang — không có code nào cần sửa
thêm ở round này.** "Atomized Aug 12, 2026" mà Nghiep thấy phản ánh đúng
thời điểm atomize thật (giờ VN), không phải lỗi timezone tái phát. Nếu có
tour cụ thể khác mà Nghiep tin là sai ngày, cần tour_id chính xác để
verify lại — công thức chung "UTC + 7h, so ngày" ở trên áp dụng được cho
bất kỳ case nào.

### Test thêm (Vấn đề B)

`tests/verify_scripts/aa345_round4_curation_timezone_verify.mjs` — theo
đúng yêu cầu "không dựa vào test chung đã viết cho Atomize page": copy độc
lập chính `formatDate()` của `curation/page.tsx` (không import/dùng lại
copy của round 3), assert lại đúng 3 case biên nửa đêm giờ VN + case
timestamp thật của "Peaks and Passes of the Nubra Valley" ở trên. Cùng hạn
chế hạ tầng test đã ghi ở round 3 (không có jest/vitest, không CI nào chạy
tự động file `.mjs` này) — chạy tay:
`node tests/verify_scripts/aa345_round4_curation_timezone_verify.mjs`.

## Phát hiện phụ — KHÔNG sửa trong PR này (ngoài phạm vi 2 vấn đề)

`frontend/app/admin/curation/page.tsx` dòng ~635 (nút xác nhận xoá atom
hàng loạt) có text tiếng Việt: `"Không thể hoàn tác. Các atom này sẽ không
bao giờ xuất hiện trong slot allocator nữa."` — vi phạm rule "TOÀN BỘ UI
text phải là tiếng Anh". Có từ trước round này (không phải regression mới
gây ra), cố tình KHÔNG sửa ở đây theo đúng chỉ đạo "KHÔNG sửa gì ngoài
phạm vi 2 vấn đề trên" — Nghiep xác nhận có muốn 1 PR riêng để sửa không.

## Should know

- Full suite: 1152 pass (tăng từ 1151 ở round 3 — +1 test mới), flake8
  sạch trên các file đã sửa.
- KHÔNG có thay đổi nào ở `api/` production logic hay `frontend/app/`
  production logic — chỉ thêm test (1 pytest case + 1 Node verify script)
  và tài liệu này. Không có migration.
- Việc verify live dùng đúng pattern S3-mediated ECS exec đã quy ước
  (global CLAUDE.md), chạy `curl` trực tiếp vào container thật thay vì chỉ
  query DB — để loại trừ khả năng lỗi nằm ở tầng FastAPI routing/serialize
  mà 1 query DB đơn thuần không phát hiện được.
- Đã dọn sạch các script tạm trên `s3://aa-cis-bronze-{acc2-id}/scripts/`
  sau khi verify xong (không để lại rác trên S3 dùng chung).
