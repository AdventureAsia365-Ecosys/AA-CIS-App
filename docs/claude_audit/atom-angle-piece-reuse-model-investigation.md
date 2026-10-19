# STEP0 — Atom/Angle/Piece Reuse Model Investigation

Investigate only, không sửa code, không commit code, không tạo PR trong phiên này. Đọc trên
`main`. Đây là 1 phát hiện phụ nảy sinh khi bàn AA-469 Việc 2 (T7 year-scope), KHÔNG phải Việc 6
của AA-469 — chưa có issue Linear riêng, tên file tạm dùng cho tới khi Nghiệp quyết định tạo
issue mới.

**Status: STEP0 xong — chỉ trả lời "hiện tại schema/code hỗ trợ hay chặn gì", không đề xuất
thiết kế, không viết code.**

---

## Câu hỏi 1 — Atom tái sử dụng N lần → N content_piece riêng biệt?

**Trả lời: Schema KHÔNG chặn việc này, nhưng cũng KHÔNG có ràng buộc nào enforce "mỗi lần dùng
lại atom là 1 piece mới" — nó chỉ đơn thuần xảy ra như một side-effect của việc thiếu unique
constraint, không phải một tính năng được thiết kế có chủ đích.**

Bằng chứng (`api/migrations/113_acp_shared_angle_gate.sql`):
- `angle_gate_request` không có UNIQUE constraint nào trên `(tenant_id, atom_id, channel)` —
  chỉ có PK `request_id` (UUID, gen_random_uuid()) và 2 index thường (tenant, status).
- `services/acp_angle_gate/service.py::create_request()` (dòng 159-185) — grep `UNIQUE\|ON
  CONFLICT\|already exists\|duplicate` trong toàn file: **0 kết quả**. Hàm chỉ `_fetch_atom_for_
  tenant()` (kiểm tra atom thuộc tenant) rồi INSERT thẳng, không hề query xem atom_id này đã có
  request nào trước đó chưa.

→ Về mặt cơ học: tenant có thể gọi `create_request()` N lần với cùng `atom_id` (cùng hoặc khác
`channel`/`goal` sau đó), mỗi lần tạo 1 row `angle_gate_request` độc lập, mỗi row có thể đi hết
lifecycle riêng (`pending_goal` → `pending_choice` → `approved`) và sinh ra `content_piece` riêng
(khoá bởi `angle_gate_request_id`, migration 115). **Không có gì bị overwrite.**

Nhưng: đây không phải "1 atom : N pieces" theo nghĩa một model đã được thiết kế — nó là hệ quả
của việc `angle_gate_request` chưa từng có khái niệm "atom này đã dùng chưa", nên không ai (kể cả
tenant) được cảnh báo hay dẫn hướng khi tái sử dụng. Không có API nào liệt kê "lịch sử các request
trước đó của atom X" để tenant biết mà tránh trùng lặp trước khi viết bài mới.

## Câu hỏi 2a — Anti-duplication within-tenant (HIGH priority): có tồn tại không?

**Trả lời: KHÔNG tồn tại ở bất kỳ layer nào đã đọc.**

- `set_goal_and_generate()` (`service.py` dòng 204-260) gọi `generate_angles()` với 6 input:
  `content_seed` (text atom thô), `goal`, `channel`, `brand_audience`, `destination`,
  `trip_name` — **không có tham số nào mang lịch sử pieces/angles cũ của cùng atom này**.
- `generate.py::generate_angles()` và `prompts.py::build_user_prompt()` (đã xác nhận lại trong
  AA-469 Việc 4's STEP0, không đọc lại raw trong phiên này) — theo trích dẫn trong
  `docs/claude_audit/AA-469-viec4-step0-t8-t11-chain-investigation.md` mục 1-2: prompt chỉ có 5
  khối (CONTENT SEED / TRIP / GOAL / CHANNEL / BRAND AUDIENCE), không có khối "avoid repeating
  these prior angles/pieces".
- Không có query nào trong `service.py` hay `content_writing` package (tên suy từ router đã biết,
  không đọc lại full trong phiên này) fetch `content_piece` cũ theo `atom_id` để đưa vào prompt
  hay để so sánh sau khi sinh.

→ Nếu tenant chọn lại đúng atom X, đặt đúng goal Y đã dùng trước đó, LLM sinh 3 angle mới **hoàn
toàn độc lập với lần trước** — không có cơ chế nào ngăn 2 angle giống hệt nhau về ý tưởng/cấu trúc
xuất hiện ở 2 request khác thời điểm. Đây là gap thật, đúng như premise của Nghiệp trong đầu bài.

## Câu hỏi 2b — Cross-tenant anti-duplication (LOW priority, chỉ xác nhận, không điều tra sâu)

**Trả lời: Có tồn tại schema cho embedding-based similarity (pgvector), nhưng nó CHẾT hoàn
toàn — không code nào ghi hay đọc, và một trong 2 bảng có cột này đã bị DROP.**

- `api/migrations/041_pgvector_embeddings.sql` (21/05/2026, AA-62 — rất cũ, trước cả T-series):
  thêm cột `content_embedding vector(1536)` (Bedrock Titan Embed Text v2) + ivfflat cosine index
  vào 2 bảng: `gold_aa_internal.published_tours` và `acp_silver_s4.blog_drafts`.
- `grep -rn "content_embedding" services/ api/ --include="*.py"` → **0 kết quả**. Không có writer,
  không có reader, ở bất kỳ đâu trong codebase Python hiện tại.
- `acp_silver_s4.blog_drafts` — bảng thứ 2 mang cột này — đã bị `DROP TABLE` trong
  `api/migrations/121_drop_acpv1_stage_chain.sql` dòng 57 (migration này **đã apply thật**, theo
  memory phiên trước: "21 dead ACPv1 tables dropped 27/08 16:55 UTC"). Nghĩa là nửa hạ tầng
  pgvector cho mục đích này không còn tồn tại vật lý trong DB nữa.
- `content_piece` (migration 115) — bảng THẬT sự chứa nội dung T9/T10 hiện tại — **không có cột
  embedding nào cả**.

→ Xác nhận đúng premise của Nghiệp: (b) là rủi ro lý thuyết, chưa từng được implement cho
model atom/angle/piece hiện tại. Cái duy nhất từng tồn tại (AA-62, pre-T-series) đã một nửa chết
theo code, một nửa chết theo schema (table bị xoá). Không có gì đang hoạt động để "giữ nguyên"
hay "phải tương thích ngược" nếu sau này có ai muốn xây dedup thật.

## Câu hỏi 3 — T7 planning: year hay chỉ quarter?

**Trả lời: Có bảng `year_plan` (migration 112, theo comment trong quarter.py) nhưng nó CHỈ là
một parent/grouping row (get-or-create theo `(tenant_id, year)`), KHÔNG có logic tính toán ở
year-scope. Toàn bộ compute thật (`compute_quarter_plan`, `compute_slot_grid`) vẫn hoạt động ở
quarter/month/week scope như cũ.**

Bằng chứng — `services/acp_planning/quarter.py::save_quarter_plan_version()` (dòng 359-404):
```python
await conn.execute(
    "INSERT INTO acp_shared.year_plan (tenant_id, year) VALUES ($1, $2) "
    "ON CONFLICT (tenant_id, year) DO NOTHING", plan.tenant_id, plan.year,
)
year_plan_id = await conn.fetchval(
    "SELECT year_plan_id FROM acp_shared.year_plan WHERE tenant_id = $1 AND year = $2", ...
)
await conn.execute(
    "INSERT INTO acp_shared.quarter_plan (tenant_id, year, quarter, year_plan_id) "
    "VALUES (...) ON CONFLICT (...) DO UPDATE SET year_plan_id = COALESCE(...)", ...
)
```
Comment của chính hàm (AA-448 round 6, Shape 1) nói rõ: "ensures a parent acp_shared.year_plan
row exists for (tenant_id, year) and links this quarter_plan row to it... Purely additive — every
other column/row this function already writes keeps its exact prior behavior."

→ `year_plan` hiện tại chỉ là 1 FK-grouping row rỗng nội dung (không thấy cột nào khác ngoài
`year_plan_id`/`tenant_id`/`year` được tham chiếu) — một cái "thư mục" để sau này gom 4
quarter_plan lại dưới 1 năm, **không phải một cơ chế lập kế hoạch year-scope thật**. Không tìm
thấy hàm nào tên `compute_year_plan` hay tương đương (`grep "def.*year"` trong toàn bộ
`services/acp_planning/*.py` chỉ ra các hàm nhận tham số `year: int` như một input thông thường
của quarter/month/week computation, không phải hàm tính TOFU/MOFU/BOFU theo năm).

Về phần "substitute an atom từ chỗ khác trong cùng THÁNG nếu atom của tuần đó đã bị dùng ở nơi
khác trong tháng" — không đọc lại `compute_slot_grid()`/atom floor logic chi tiết trong phiên này
(đã có STEP0 riêng ở AA-469 Việc khác xác nhận đây là pure function ở month/week scope), nhưng
theo cấu trúc `year_plan` vừa xác nhận: **hiện tại không có bất kỳ khái niệm "tháng liền kề trong
cùng year_plan" nào được liên kết** — mỗi `quarter_plan` link tới đúng 1 `year_plan_id`, nhưng
việc "xem atom nào đã dùng ở tuần khác trong THÁNG" là việc `compute_slot_grid()` (quarter.py)
vốn đã làm nội bộ trong phạm vi 1 tháng — không liên quan gì đến `year_plan` mới thêm.

## Câu hỏi 4 — Published/approved pieces immutable, "write new" tạo row riêng?

**Trả lời: Đúng, đã được đảm bảo bởi chính thiết kế `content_piece`/`publish_log` — không phải
suy luận, mà là comment tường minh trong migration.**

- `content_piece` (migration 115): "One row per WRITE ATTEMPT... No `previous_piece_id` chain: an
  attempt is never a branch, only ever 'replaces what's shown for this request'... the row with
  the highest `attempt_number` for a given request is the one to display." UNIQUE
  `(angle_gate_request_id, attempt_number)` — mỗi attempt là 1 row mới, không UPDATE row cũ.
- `publish_log` (migration 116, Option B): tách riêng khỏi `content_piece.status` — "keeps T10's
  approval state... fully separate from T11's delivery state — a piece can be approved and never
  published, or published and later force-unpublished, without overloading content_piece.status
  with a third concern." `piece_id` FK trỏ CHẶT vào 1 `content_piece` cụ thể (không CASCADE
  update ngược).

→ **Nhưng có 1 giới hạn quan trọng chưa được đầu bài nhắc tới**: "attempt" trong `content_piece`
hiện tại bị scope bởi **`angle_gate_request_id`**, không phải bởi atom hay angle độc lập. Nghĩa
là "viết mới từ CÙNG atom" theo đúng nghĩa của câu hỏi 1 ở trên **luôn luôn đòi hỏi tạo 1
`angle_gate_request` MỚI trước** (vì 1 request chỉ sinh tối đa 2 attempt theo kiến trúc đã confirm
ở migration 115's comment, và không có API nào thêm attempt thứ 3+ vào 1 request đã `approved`/
`held` xong xuôi) — rồi request mới đó mới sinh `content_piece` mới với `angle_gate_request_id`
khác. Vậy "piece mới, tách biệt, không đè piece cũ" là đúng — nhưng nó luôn đi kèm việc phải tạo
lại toàn bộ `angle_gate_request` (và 3 angle mới, xem câu 5), không có đường tắt "viết piece mới
từ 1 angle đã có sẵn của request cũ".

## Câu hỏi 5 — 2 tầng reusable lồng nhau: (atom,goal)→3 angle, (angle,channel)→piece lặp lại nhiều lần

**Trả lời: Tầng 1 (3 angle được persist thật, không phải chỉ angle được chọn) — ĐÚNG, đã có sẵn.
Tầng 2 (angle,channel) như một đơn vị độc lập có thể tái dùng nhiều lần — KHÔNG được model, vì
`channel` bị khoá cứng vào `angle_gate_request` NGAY TỪ TRƯỚC KHI có goal/angle, và `content_piece`
chỉ trỏ tới `angle_gate_request_id` chứ không trỏ tới `angle_gate_option_id` cụ thể nào.**

Bằng chứng tầng 1 — `angle_gate_option` (migration 113): UNIQUE `(request_id, idx)`, đúng 3 row
mỗi request (idx 0/1/2), 2 cột boolean tách biệt `recommended` (LLM's pick) và `chosen` (tenant's
pick) — cả 3 row đều tồn tại vĩnh viễn trong DB (ON DELETE CASCADE chỉ xảy ra nếu xoá cả
request cha, không có logic xoá/expire riêng lẻ từng option). `fetch_request()` (`service.py`
dòng 295-319) trả về **cả 3 angle** trong response (`"angles": [dict(o) for o in option_rows]`),
không chỉ angle đã chọn — về mặt dữ liệu, 2 angle chưa dùng vẫn đọc lại được nguyên vẹn.

Nhưng — không có LIFECYCLE nào cho phép "quay lại chọn 1 trong 2 angle còn lại" sau khi đã
`approved`:
- `choose_angle()` (`service.py` dòng 264-292) require `req["status"] == "pending_choice"` — gọi
  raise `WrongStatusError` nếu không đúng. Sau khi 1 angle được chọn, hàm tự UPDATE
  `angle_gate_request.status = 'approved'` (dòng 285-287) — **status này là one-way ratchet**
  (`pending_goal` → `pending_choice` → `approved`, không có trạng thái nào đi ngược).
- Do đó, dù 2 row `angle_gate_option` chưa dùng vẫn nằm trong DB và đọc được qua
  `fetch_request()`, **không có endpoint/hàm nào gọi lại `choose_angle()` trên cùng
  `request_id` đã approved** để "kích hoạt" 1 trong 2 angle còn lại cho 1 `content_piece` mới.
  Muốn dùng angle khác của cùng atom+goal, cách duy nhất hiện tại là tạo `angle_gate_request` MỚI
  → gọi `set_goal_and_generate()` lại → tốn 1 lần gọi LLM sinh **3 angle mới hoàn toàn** (không
  tái dùng 2 angle cũ đang có sẵn) — đúng như lo ngại premise của Nghiệp ("có thể chọn 1 trong 2
  angle chưa dùng mà không tốn LLM call mới" là **KHÔNG khả thi với code hiện tại**).

Bằng chứng tầng 2 (angle,channel) — `content_piece` (migration 115): FK duy nhất là
`angle_gate_request_id` (dòng 37), **không có `angle_gate_option_id`/`angle_id` nào**. Và
`channel` không phải là input độc lập ở bước viết piece — nó đã bị cố định từ lúc
`create_request(tenant_id, atom_id, channel, ...)` (dòng 159-185), TRƯỚC CẢ khi goal được chọn
(`set_goal_and_generate`) và TRƯỚC CẢ khi angle được sinh. Nghĩa là:
- 1 `angle_gate_request` = 1 `atom` × 1 `channel` × 1 `goal` (khoá cứng theo thứ tự này) → 3
  angle → 1 angle được chọn → tối đa 2 `content_piece` attempt, TẤT CẢ cùng channel đó.
- Muốn "channel khác, cùng angle" (theo đúng nghĩa câu hỏi 5: (angle, channel) là 1 đơn vị tái
  dùng) — không thể, vì angle không tồn tại độc lập ngoài 1 request đã gắn chết với 1 channel.
  Phải tạo lại toàn bộ chuỗi atom→goal→angle mới cho channel mới, dù ý tưởng ("angle") có thể
  giống hệt ý muốn ban đầu.
- Muốn "viết lại cùng channel, sau này" (không phải retry trong cùng session) — khả thi về mặt
  tạo row `content_piece` mới (câu hỏi 4 đã xác nhận), nhưng chỉ SAU KHI tạo `angle_gate_request`
  mới → 3 angle mới → chọn lại — không có đường nào tái dùng angle cũ.

---

## Tổng kết bằng chứng — cái gì tồn tại vs. cái gì bị chặn

| Câu hỏi | Model Nghiệp mô tả | Thực tế code/schema hiện tại |
|---|---|---|
| 1. Atom dùng N lần → N pieces | Mong muốn | **Không bị chặn** (không UNIQUE constraint), nhưng cũng không phải tính năng thiết kế — là side-effect của việc thiếu ràng buộc |
| 2a. Anti-dup within-tenant (HIGH) | Cần có | **Không tồn tại** — prompt không mang lịch sử angle/piece cũ của atom |
| 2b. Anti-dup cross-tenant (LOW, chỉ xác nhận) | Rủi ro lý thuyết | **Hạ tầng cũ (pgvector, AA-62) đã chết hoàn toàn** — 0 code ref, 1/2 bảng đã bị DROP (migration 121) |
| 3. T7 year-scope | Mong muốn (Ms. Thư's research) | **`year_plan` chỉ là parent-grouping row** (get-or-create), không có compute year-scope thật; mọi tính toán vẫn ở quarter/month/week |
| 4. Published/approved immutable, write-new = row riêng | Yêu cầu cứng | **Đã đúng theo thiết kế** (`attempt_number`, không `previous_piece_id` chain; `publish_log` tách khỏi `content_piece.status`) |
| 5a. 3 angle đều persist | Yêu cầu | **Đã đúng** — UNIQUE(request_id, idx), cả 3 đọc lại được qua `fetch_request()` |
| 5b. Angle chưa dùng chọn lại được sau đó (không tốn LLM) | Mong muốn | **Bị chặn** — `angle_gate_request.status` là one-way ratchet, `choose_angle()` từ chối khi đã `approved` |
| 5c. (angle, channel) là đơn vị tái dùng độc lập | Mong muốn | **Không được model** — `channel` khoá cứng vào request TRƯỚC cả khi angle tồn tại; `content_piece` không có FK tới `angle_gate_option` |

**Không có đề xuất thiết kế nào trong tài liệu này** — đây là STEP0 xác nhận hiện trạng, việc
thiết kế (nếu Nghiệp quyết định tạo issue Linear) sẽ là một bước riêng.
