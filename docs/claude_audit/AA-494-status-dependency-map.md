# AA-494 — STEP0: angle_gate_request.status dependency map

Investigate only, không sửa code, không commit, không PR trong phiên này. Đọc trên `main`.
Mục tiêu: liệt kê ĐẦY ĐỦ mọi nơi đọc/ghi `acp_shared.angle_gate_request.status` (backend +
frontend) trước khi AA-494 quyết định cách phá vỡ one-way ratchet hiện tại
(`null → pending_goal → pending_choice → approved`) để hỗ trợ quay lại 1 request đã `approved`
và chọn 1 angle option chưa dùng khác.

**Status: STEP0 xong — không đề xuất thiết kế cụ thể, chỉ báo cáo bản đồ phụ thuộc + chi phí sửa
thực tế của 2 kịch bản Nghiệp nêu.**

---

## 1. DB constraint (bước 8)

`api/migrations/113_acp_shared_angle_gate.sql`:
```sql
status TEXT NOT NULL DEFAULT 'pending_goal'
       CHECK (status IN ('pending_goal', 'pending_choice', 'approved')),
```
+ `CREATE INDEX idx_angle_gate_request_status ON acp_shared.angle_gate_request(status)`.

→ **CHECK-constrained TEXT, không phải free-text** (khác hẳn cột `goal` cùng bảng, vốn cố ý để
free TEXT — comment migration 113 giải thích rõ lý do: goal đã có 8-value list trong
`goals.py`, tránh 2 nguồn sự thật; `status` thì KHÔNG có comment tương tự, tức là đội ngũ coi đây
là true enum cần constraint). Thêm 1 giá trị mới = 1 migration nhỏ (`DROP CONSTRAINT` +
`ADD CONSTRAINT ... CHECK (status IN (..., 'giá_trị_mới'))`) — **đúng pattern migration 118 đã
làm cho `content_piece.status`** (thêm `processing`/`failed`), không cần data backfill vì các
row cũ vẫn nằm trong tập giá trị hợp lệ mới.

## 2. Backend — mọi nơi đọc/ghi status (bước 1, 3)

| # | File:line | Loại | Chi tiết |
|---|---|---|---|
| B1 | `service.py:210-212` | GUARD (read) | `set_goal_and_generate()` — raise `WrongStatusError` nếu `status != "pending_goal"` |
| B2 | `service.py:241` | WRITE | `UPDATE ... SET status = 'pending_choice'` (trong cùng transaction ghi `goal` + 3 `angle_gate_option` rows) |
| B3 | `service.py:268-270` | GUARD (read) | `choose_angle()` — raise `WrongStatusError` nếu `status != "pending_choice"` |
| B4 | `service.py:285` | WRITE | `UPDATE ... SET status = 'approved'` (cùng transaction với `UPDATE angle_gate_option SET chosen=true WHERE idx=$2` — **chỉ set true cho idx được chọn, KHÔNG unset các row khác** — xem cảnh báo ở mục 5) |
| B5 | `service.py:315` | passthrough | `fetch_request()` trả `status` nguyên văn trong response dict, không branch logic |
| B6 | `v1_angle_gate.py:78` | passthrough | `create_request` response trả `req["status"]` |
| B7 | `v1_angle_gate.py:95-96,125-126` | error mapping | `WrongStatusError` → HTTP 409, ở CẢ 2 endpoint `/goal` và `/choose` |
| B8 | `services/acp_content_writing/service.py:97-101` | **GUARD cross-package** | `start_write()` (T9) raise `RequestNotReadyError` nếu `req["status"] != "approved"` — **T9 phụ thuộc trực tiếp vào giá trị terminal `"approved"` của T8**, không nằm trong package `acp_angle_gate` |
| B9 | `services/acp_content_writing/service.py:102-107` | **invariant ngầm, cross-package** | `chosen = next((a for a in req["angles"] if a["chosen"]), None)` — giả định "đúng 1 angle có `chosen=true`" luôn đúng vì `choose_angle()` (B4) chỉ chạy 1 lần trong đời request (do B3 chặn); comment tự nhận đây là "Defensive — unreachable via the real API" |
| B10 | `api/routers/v1_content_writing.py:62-63` | error mapping | `RequestNotReadyError` → HTTP 409 |
| B11 | `api/routers/v1_publish.py:105,151` | JOIN nhưng KHÔNG đọc status | JOIN `angle_gate_request agr` chỉ để lấy `agr.channel` — **xác nhận T11/publish KHÔNG phụ thuộc `angle_gate_request.status` dưới bất kỳ hình thức nào**, chỉ phụ thuộc `content_piece.status` (dòng 159: `piece["status"] != "approved"`, đây là cột của `content_piece`, khác bảng) |

`services/acp_content_writing/prompts.py` (tìm thấy ở bước 6) — chỉ có 1 dòng comment nhắc tên
bảng, không có logic phụ thuộc status thật.

## 3. Frontend — `AngleGateTab.tsx` (bước 4, 5)

**Quan trọng: phải tách rõ 3 domain khác nhau đều dùng `.status`, kẻo nhầm khi sửa:**
- `req.status` = `angle_gate_request.status` (3 giá trị, đối tượng của AA-494)
- `piece.status`/`fresh.status` = `content_piece.status` (4 giá trị, migration 118, KHÔNG liên quan)
- `r.status` = mã HTTP response, không liên quan

### req.status (đối tượng thật của AA-494) — 7 điểm:

| Line | Điều kiện | Hệ quả |
|---|---|---|
| 356 | `req.status === "pending_goal"` | Render card "2 · Choose a Goal" |
| 381 | `req.status === "pending_choice" \|\| req.status === "approved"` | Render card "3 · Choose an Angle" — **1 card DÙNG CHUNG cho cả 2 status** |
| 383 | `req.status === "approved" ? "Approved" : "3 · Choose an Angle"` | Đổi title của card trên |
| 386 | `req.status === "approved"` | Hiện banner xanh "Angle chosen — writing..." |
| 412 | `req.status === "pending_choice"` | **CHỈ hiện nút "Choose this angle" khi pending_choice** — ẩn hẳn khi đã approved |
| 424 | `req.status === "approved"` | Render card "4 · Content" (khu vực viết bài + hiển thị piece) |
| 279 (trong `choose()` callback) | `d.status === "approved"` (d = response của POST .../choose) | **Auto-trigger `writeContent()` một lần duy nhất** ngay khi status chuyển approved — không phải watcher liên tục, chỉ bắn 1 lần trên response của chính lệnh choose |

### piece.status / content_piece — KHÔNG phải target của AA-494, liệt kê để loại trừ nhầm lẫn:
Dòng 219 (`fresh.status !== "processing"`, trong `pollPiece()`), 475, 492, 495, 514, 518 — toàn
bộ đọc `content_piece.status` (processing/approved/held/failed), bảng khác, cột khác, không đụng
tới `angle_gate_request.status`.

### Polling (bước 5) — phát hiện quan trọng nhất của mục này:

**`angle_gate_request.status` KHÔNG hề được poll.** `req` state chỉ được set trực tiếp từ response
của 3 lệnh fetch (`create_request`, `set_goal`/submitGoal, `choose`) — không có `setInterval` hay
`useEffect` nào re-fetch `GET /requests/{id}` theo chu kỳ. Poll duy nhất trong file
(`pollPiece()`, dòng 205-226) nhắm vào `content_piece.status` qua `GET .../pieces/{piece_id}`,
điều kiện dừng là `fresh.status !== "processing"` — **1 phép so sánh "khác giá trị non-terminal
duy nhất"**, tự động chịu được khi thêm giá trị terminal mới (đã chứng minh bằng migration 118
thêm `'failed'` mà không cần sửa `pollPiece()`).

Ngược lại, **cả 7 điểm ở trên đọc `req.status` đều dùng so sánh `===` cứng với 1 trong 3 chuỗi cụ
thể** — không có điểm nào dùng pattern "khác pending_goal" hay "trong tập terminal". Đây là khác
biệt cấu trúc quan trọng: nếu thêm giá trị mới vào `angle_gate_request.status`, **không có single
choke point nào tự động xử lý đúng** — phải rà từng dòng trong 7 điểm trên.

## 4. Các file khác ngoài AngleGateTab.tsx (bước 6, 7)

`grep -rln "angle_gate_request\|pending_goal\|pending_choice"` toàn repo (frontend + services +
api) trả về đúng 8 file — 4 file đã phủ ở mục 2/3
(`AngleGateTab.tsx`, `service.py`, `v1_angle_gate.py`, `__init__.py` chỉ có docstring), 4 file
còn lại đã đọc trong bước 7:

- `services/acp_content_writing/prompts.py` — chỉ docstring, không logic.
- `api/routers/v1_content_writing.py` — đã cover ở B10 (error mapping RequestNotReadyError).
- `api/routers/v1_publish.py` — đã cover ở B11 (JOIN nhưng không đọc status).
- `services/acp_content_writing/service.py` — đã cover ở B8/B9 (guard + invariant ngầm).

**Không tìm thấy view admin/A4/dashboard nào đọc `angle_gate_request.status`** — đúng như kỳ
vọng, vì T8 là tenant self-service thuần (ADR-2026-038 §0.2), A4 (feedback loop, AA-437) chỉ đọc
`content_piece`/`publish_log`, không có route nào trong `frontend/app/admin/*` match grep này.

---

## 5. Cảnh báo phát hiện phụ — bug tiềm ẩn nằm sẵn trong code hiện tại (không phải do AA-494 gây ra, nhưng AA-494 CHẮC CHẮN sẽ chạm vào)

`choose_angle()` (`service.py:277-281`):
```python
updated = await conn.execute(
    "UPDATE acp_shared.angle_gate_option SET chosen = true "
    "WHERE request_id = $1 AND idx = $2", request_id, idx,
)
```
Câu UPDATE này **chỉ SET `chosen=true` cho idx được chọn — không có câu nào SET `chosen=false`
cho 2 idx còn lại.** Hiện tại vô hại 100% vì B3 (guard `status != "pending_choice"`) đảm bảo hàm
này chỉ chạy được đúng 1 lần trong đời mỗi request — không có gì để "unset" vì trước đó cả 3 đều
`chosen=false` mặc định.

**Nhưng bất kỳ hướng nào của AA-494 cho phép gọi lại logic "chọn angle" trên 1 request đã
`approved`** (để chọn 1 trong 2 angle chưa dùng) **đều sẽ để lại 2 row cùng `chosen=true`** nếu
không sửa câu UPDATE này thành có `SET chosen = (idx = $2)` (unset toàn bộ trước, hoặc unset
tường minh các idx khác). Hậu quả cụ thể: B9 (`acp_content_writing/service.py:102`,
`next((a for a in req["angles"] if a["chosen"]), None)`) sẽ **lấy nhầm angle đầu tiên theo thứ tự
idx có `chosen=true`**, không đảm bảo là angle được chọn GẦN NHẤT — T9 có thể viết bài dựa trên
angle SAI (angle cũ, không phải angle mới vừa chọn lại). Đây là chi phí BẮT BUỘC phải sửa ở CẢ
HAI kịch bản bên dưới, không phải optional.

---

## 6. Kịch bản A — Thêm giá trị mới (VD `"reusable"`) sau `"approved"`

**Phải sửa:**
1. Migration mới: `DROP CONSTRAINT` + `ADD CONSTRAINT CHECK (status IN (..., 'reusable'))` — 1
   file nhỏ, giống hệt migration 118. **Rẻ.**
2. Cần 1 hàm/transition MỚI trong `service.py` (chưa tồn tại) để đưa request từ `approved` →
   trạng thái cho phép chọn lại — hiện KHÔNG có đường nào đi ngược từ `approved`.
3. B3 (`choose_angle()`'s guard, `service.py:268`) — hiện chỉ chấp nhận đúng `"pending_choice"`.
   Nếu tái dùng luôn hàm `choose_angle()` cho cả lần chọn đầu và chọn lại, guard này phải nới ra
   chấp nhận CẢ `"pending_choice"` VÀ giá trị mới (`in (...)` thay vì `==`).
4. **BẮT BUỘC sửa bug ở mục 5** — câu UPDATE `angle_gate_option` phải unset `chosen` cũ trước khi
   set cái mới, nếu không B9 sẽ đọc sai.
5. Frontend dòng 381 — điều kiện render card phải thêm giá trị mới:
   `status === "pending_choice" || status === "approved" || status === "reusable"`.
6. Frontend dòng 412 — điều kiện hiện nút "Choose this angle" phải thêm giá trị mới (đang chỉ
   `pending_choice`), nếu không tenant sẽ THẤY card nhưng KHÔNG click chọn lại được gì.
7. Frontend dòng 383/386 — cần quyết định UI: giá trị mới hiện title/banner gì (khác `"Approved"`
   tĩnh hiện tại) — ít nhất phải thêm 1 nhánh, không thể để mặc định rơi vào nhánh `else`.
8. Frontend dòng 424 — cần quyết định: card "4 · Content" (hiển thị piece cũ) có tiếp tục hiện
   song song với card chọn-lại-angle không, hay ẩn đi cho tới khi chọn lại xong.
9. B8 (`acp_content_writing/service.py:97`) — **KHÔNG cần sửa nếu** thiết kế đảm bảo mọi lần chọn
   lại luôn đưa status VỀ LẠI `"approved"` trước khi tenant có thể bấm "Write" — chỉ cần sửa NẾU
   thiết kế giữ status ở giá trị `"reusable"` trong lúc vẫn cho viết bài.

→ **Chi phí: 1 migration nhỏ + 1 hàm service mới + 1 guard nới lỏng + 1 bug-fix bắt buộc + 4-5
điểm sửa rời rạc trong frontend (không có choke point chung, phải rà từng dòng vì mọi so sánh
đều `===` cứng — xem mục 3).** Rủi ro chính: dễ sót 1 trong 5 điểm frontend vì không compile-time
check nào bắt được string literal thiếu.

## 7. Kịch bản B — Bỏ hẳn `status`, chuyển trạng thái xuống `content_piece`

**Phải sửa (nhiều hơn và sâu hơn kịch bản A):**
1. B1 (`service.py:210`, guard "chưa set goal") — mất `status` thì phải suy ra từ cột khác:
   `goal IS NULL` (cột `goal` đã có sẵn, khả thi) thay cho `status == "pending_goal"`.
2. B3 (`service.py:268`, guard "đã set goal, chưa chọn angle") — phải suy ra bằng
   `EXISTS(SELECT 1 FROM angle_gate_option WHERE request_id=... AND chosen=true)` — 1 query
   thêm, thay vì đọc 1 cột có sẵn.
3. **B8 là chi phí lớn nhất** — `start_write()` (T9, package KHÁC — `acp_content_writing`) hiện
   check `req["status"] != "approved"` để biết "T8 đã xong chưa". Bỏ status ở
   `angle_gate_request` nghĩa là **T9 phải tự suy luận lại điều kiện này qua
   `angle_gate_option.chosen`** — sửa code ở 1 package hoàn toàn khác với package đang được
   redesign, đúng dạng lỗi mà memory `feedback_check_all_callers_before_deleting_shared_function`
   đã cảnh báo (đã kiểm chứng: đây thực sự là caller ngoài package, không phải suy đoán).
4. `fetch_request()`/router response (B5, B6) hiện trả `"status": req["status"]` thẳng ra — nếu
   xoá cột, phải tự tính 1 giá trị tương đương để KHÔNG đổi contract response (nếu muốn giữ
   backward-compat cho FE), hoặc đổi luôn contract → kéo theo sửa TẤT CẢ 7 điểm frontend, không
   phải chỉ thêm nhánh như kịch bản A mà là THAY THẾ toàn bộ logic (`req.status === "approved"`
   → `req.angles.some(a => a.chosen)`, v.v., ở cả 7 chỗ).
5. **Vấn đề khái niệm, không chỉ chi phí sửa code**: đề bài nói "chuyển trạng thái xuống
   `content_piece`" — nhưng `content_piece.status` (migration 118: `processing/approved/held/
   failed`) đang mô tả **kết quả VIẾT BÀI** (T9/T10), một khái niệm khác hẳn "đã chọn angle hay
   chưa" (T8). Ép 2 khái niệm vào chung 1 cột đi ngược đúng nguyên tắc migration 116 (Option B,
   `publish_log`) đã tự nêu rõ: "keeps T10's approval state fully separate from T11's delivery
   state... without overloading content_piece.status with a third concern." Hơn nữa
   `content_piece` row **chỉ được tạo ở bước T9 `start_write()`** — TRƯỚC lúc angle được chọn thì
   chưa hề có row `content_piece` nào tồn tại, nên "trạng thái chọn-angle" không có chỗ vật lý
   nào để đứng nếu thật sự dồn xuống bảng này mà không tạo thêm cột/giá trị MỚI đúng nghĩa T8 (
   tức là vẫn phải thêm giá trị vào 1 status column nào đó, chỉ là đổi chủ nhà — không tránh được
   chi phí kiểu kịch bản A, mà còn cộng thêm toàn bộ chi phí đảo ngược thứ tự tạo row).

→ **Chi phí: sửa 2 guard nội bộ + 1 guard CROSS-PACKAGE (T9 phải tự suy luận lại điều kiện T8) +
khả năng phải đổi response contract kéo theo sửa lại (không phải thêm nhánh) cả 7 điểm frontend +
1 vấn đề kiến trúc thật (content_piece chưa tồn tại tại thời điểm cần biết trạng thái chọn-angle)
— tốn nhiều hơn kịch bản A ở mọi mặt đã kiểm chứng, và vẫn phải tự hỏi "vậy trạng thái mới nằm ở
đâu" chứ không giải quyết được gốc vấn đề.**

---

## Tổng kết so sánh (dựa trên bằng chứng, không lý thuyết)

| | Kịch bản A (thêm giá trị) | Kịch bản B (bỏ status, dồn xuống content_piece) |
|---|---|---|
| Migration | 1 file nhỏ (DROP+ADD CHECK) | 1 file (DROP COLUMN hoặc để chết) + có thể cần thêm giá trị ở NƠI KHÁC (xem điểm 7.5) |
| Backend nội bộ package | 1 guard nới lỏng | 2 guard viết lại bằng query suy luận |
| Backend cross-package | Không cần đụng (nếu thiết kế quay về `approved` trước khi write) | **BẮT BUỘC sửa `acp_content_writing/service.py`** — package khác |
| Frontend | Thêm nhánh ở ~4-5 điểm có sẵn | Thay thế logic ở TẤT CẢ 7 điểm (nếu đổi contract) |
| Bug bắt buộc phải fix trước (mục 5) | Có | Có |
| Vấn đề kiến trúc mới phát sinh | Không | Có — content_piece row chưa tồn tại tại thời điểm cần biết trạng thái |
| Rủi ro sót chỗ sửa | Trung bình (string literal rải rác) | Cao hơn (phạm vi rộng hơn, đụng package khác) |

Không đề xuất kịch bản nào trong tài liệu này — quyết định thuộc về Nghiệp/Claude Chat dựa trên
bảng so sánh chi phí thật ở trên.
