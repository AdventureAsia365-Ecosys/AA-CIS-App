# AA-418 — N7 khảo sát: song song hoá piece-level + hiện trạng cost tracking

**Scope: khảo sát only, không code (trừ nếu Phần A xác nhận an toàn — KHÔNG, xem Kết luận).**
Task: `docs/claude_tasks/AA-418-01-parallel-run-and-cost-investigation.md`. Branch: `main`
(không tạo nhánh — task này không code). Investigated 16/08/2026, cùng ngày N7 run #6
(`d0722ae3`, xem `docs/claude_audit/AA-404-n7-run6-results.md`) và AA-415 merge.

---

## Phần A — Song song hoá piece-level

### A.1 — Hiện trạng: hoàn toàn tuần tự, ở CẢ 2 tầng

Hai vòng lặp tuần tự lồng nhau, không có concurrency nào:

1. **Slot-level** (`api/routers/admin_produce.py::_produce_slots_background()`, dòng 142):
   ```python
   async with pool.acquire() as conn:
       for slot in due_slots:
           pieces = await run_slot_production(conn, pool, tenant_id, slot, run_id, market)
   ```
   Toàn bộ vòng lặp slot dùng **CHUNG MỘT** connection (`conn`) lấy 1 lần trước for-loop.

2. **Piece-level** (`services/acp_produce/slot_runner.py::run_slot_production()`, dòng 159):
   ```python
   for piece in [blog_piece] + channel_pieces:
       result = await run_piece_through_produce_gates(piece, ..., db=db, ...)
   ```
   Cũng dùng chung 1 `db: asyncpg.Connection` truyền xuống từ slot-level.

Không có `asyncio.gather`/`asyncio.create_task`/thread pool nào trong toàn bộ
`services/acp_produce/*` hay `api/routers/admin_produce.py`.

### A.2 — Ràng buộc thật (đã XÁC NHẬN, không giả định)

**a) Bedrock rate limit — số liệu thật từ `aws service-quotas`, không phải ước tính:**

| Model | Account thực dùng | Global cross-region RPM |
|---|---|---:|
| Claude Sonnet 4.6 / Haiku 4.5 | **acc1** ([acc1 id], satellite fallback, N7 dùng qua `bedrock_satellite.py`) | **10,000** |
| Claude Sonnet 4.6 / Haiku 4.5 | acc2 ([acc2 id], native — N7 KHÔNG gọi Claude qua account này) | 10 (không áp dụng cho N7) |
| **Amazon Nova Pro** (F8/F9 judge, `judge_client.py`, LUÔN đi qua acc2 native) | **acc2** | **25** |

- **acc1** (E2-E5 writer/repair fallback): RPM=10,000 — không phải rào cản thực tế ở quy mô N7
  hiện tại (1 run/9 piece đỉnh điểm vài chục request/phút).
- **acc3** ([acc3 id] — AA-397/398's satellite CHÍNH, `repair.py` gọi
  `account="acc3"` trực tiếp) — **KHÔNG verify được trong session này**: không có AWS CLI
  profile nào cấu hình sẵn trỏ tới account [acc3 id] (chỉ có `pqnghiep-admin`→acc1,
  `aa365-admin`→acc2). Kiến trúc giống hệt acc1 (cùng pattern "satellite account riêng cho
  Bedrock") nên NHIỀU KHẢ NĂNG cũng có quota cao tương tự — nhưng đây là suy luận, chưa xác
  nhận số thật. **Cần verify acc3 quota trước khi commit vào bất kỳ thiết kế song song nào**
  (Nghiệp có quyền acc3 hay cần request 1 profile CLI mới).
- **Nova Pro/acc2 (RPM=25) là ràng buộc THẬT, đáng lo hơn.** F8+F9 judge luôn chạy qua acc2,
  bất kể writer route qua acc1/acc3 nào — không né được bằng cách chọn satellite account. Run
  #6 (9 piece) dùng 90 lệnh judge thật trong ~37 phút (≈2.4/phút trung bình) — nhưng judge
  KHÔNG rải đều: P0-3 (`run_gates()`) re-run TOÀN BỘ gate stack sau MỖI vòng repair, nên F8+F9
  bắn thành CỤM ngay khi 1 round repair xong. Nếu song song hoá piece-level thô (9 piece cùng
  lúc, mỗi piece có thể đang ở 1 round repair khác nhau), khi nhiều piece "chốt round" gần
  nhau, cụm judge-call có thể chạm/gần chạm 25 RPM — và AA-384 đã cho phép `posts_per_week`
  tự do đến 14 (không còn cố định như trước), nghĩa là quy mô 1 run trong tương lai có thể LỚN
  HƠN 9 piece nhiều — rủi ro RPM tăng theo, không cố định ở mức "an toàn hôm nay".

**b) Dependency giữa piece — CÓ, nhưng chỉ ở giai đoạn SINH nội dung, không ở giai đoạn
GATE/REPAIR (phần đang muốn song song hoá):**

`slot_runner.py::run_slot_production()`: `adapt_channels(blog_piece, ...)` (E3, facebook/
tiktok) đọc TRỰC TIẾP `blog_piece.body_tagged` đã sinh xong — channel piece phụ thuộc thật vào
nội dung blog, không thể song song hoá E1-E4 (sinh nội dung). NHƯNG dependency này xảy ra
**TRƯỚC** vòng lặp `for piece in [blog_piece] + channel_pieces: await run_piece_through_
produce_gates(...)` — tới lúc vòng lặp gate+repair bắt đầu, cả 3 piece đã có `body_tagged`
độc lập, không piece nào đọc state của piece khác trong `run_piece_through_produce_gates()`.
**Kết luận: vòng lặp CỤ THỂ đang muốn song song hoá (gate+repair, phần chậm/tốn LLM nhất)
không có cross-piece dependency thật — an toàn về mặt dữ liệu.**

**c) Cost per call khi chạy song song — XÁC NHẬN không tăng, đúng như dự đoán:** Bedrock tính
tiền theo token (input/output), không theo thời gian chạy hay số request đồng thời — không có
dòng "concurrency surcharge" nào trong bảng Service Quotas hay pricing. Số liệu cost thật tính
được ở Phần B (bên dưới) khớp chính xác công thức tokens × giá/1M, không có hệ số thời gian.

**d) State chia sẻ trong repair loop:**
- `PieceInvariants` (vừa mở rộng ở AA-415) **AN TOÀN** — build MỚI HOÀN TOÀN mỗi lần gọi
  `run_piece_through_produce_gates()` (biến local trong closure của hàm đó), không có state
  module-level/global nào chia sẻ giữa các piece. Song song hoá không phá invariant này.
- **NHƯNG connection `db`/`conn` dùng CHUNG là rào cản THẬT, cấu trúc, không phải giả định:**
  `asyncpg.Connection` không an toàn để dùng đồng thời từ nhiều coroutine (2 `await conn.
  execute(...)` chồng lên nhau trên CÙNG object connection sẽ lỗi `InterfaceError` hoặc
  tương tự). Code hiện tại truyền đúng 1 connection object xuyên suốt cả vòng lặp slot VÀ
  vòng lặp piece — **đây là điều BẮT BUỘC phải sửa trước khi song song hoá bất cứ gì**, không
  chỉ đơn giản bọc `asyncio.gather()` quanh code hiện tại.
- **Phát hiện thêm, không nằm trong danh sách task nhưng trực tiếp liên quan — mức độ nghiêm
  trọng ngang với (d):** `invoke_claude()` (`bedrock_satellite.py`) và `invoke_judge()`
  (`judge_client.py`) là hàm **boto3 ĐỒNG BỘ (blocking), KHÔNG async** — được gọi trực tiếp
  (không `await`, không `asyncio.to_thread`) từ bên trong các hàm async
  (`repair_piece()`/`generate_draft()`/...). Bọc vòng lặp hiện tại trong `asyncio.gather()`
  **SẼ KHÔNG tạo concurrency thật** — mỗi lệnh gọi Bedrock vẫn chặn (block) toàn bộ event
  loop cho tới khi trả lời xong (số liệu thật: 1 lệnh `e5_repair_success` thật trong run #6
  mất `latency_ms=13790.6` — gần 14 giây CHẶN event loop). Đây chính là cơ chế nghi vấn thật
  (chưa chứng minh 100%, nhưng có bằng chứng cụ thể) đứng sau sự cố ALB health-check timeout 2
  lần trong chính run #6 (`docs/claude_audit/AA-404-n7-run6-results.md`, Step 2) — **và ECS
  service hiện chỉ chạy 1 task duy nhất** (`aws ecs describe-services`: desired=1, running=1,
  task-def `aa-cis-dev-api:101`, xác nhận sống 16/08/2026), nghĩa là API server phục vụ traffic
  admin bình thường VÀ N7 BackgroundTask chia sẻ chung 1 process/1 event loop trên 1 container.
  → Song song hoá nếu làm ẩu (chỉ bọc `gather()` không giải quyết blocking) **có nguy cơ làm
  NẶNG THÊM chính sự cố ALB timeout đã xảy ra thật**, không chỉ vô dụng về tốc độ.
- Rào cản thứ 3 mới phát hiện: `asyncpg.create_pool(..., min_size=2, max_size=10)`
  (`api/main.py`) — pool DB toàn app chỉ có tối đa 10 connection, DÙNG CHUNG với mọi traffic
  admin khác trên container. Song song hoá 9 piece (mỗi piece cần 1 connection riêng nếu sửa
  đúng theo mục (d) ở trên) sẽ chiếm gần hết pool, để lại rất ít chỗ cho traffic admin thật
  đang chạy song song trên cùng container.

### A.3 — Có làm PoC nhánh riêng không? **KHÔNG — chưa đủ an toàn theo đúng tiêu chí bước 4 của task.**

3 rào cản cấu trúc thật (không phải giả thuyết) phải sửa TRƯỚC khi bất kỳ số đo timing nào có
ý nghĩa:
1. Tách connection riêng cho mỗi task song song (không dùng chung `db`/`conn`).
2. Bọc lệnh gọi Bedrock đồng bộ vào `asyncio.to_thread()` (hoặc đổi sang client Bedrock async)
   — nếu không, `gather()` không tạo concurrency thật, chỉ đo sai.
3. Verify quota RPM thật của acc3 (chưa xác nhận được trong session này).

Chạy PoC ngay bây giờ (chỉ bọc `gather()` quanh code hiện tại, không sửa 3 điều trên) sẽ cho
số liệu **sai** (không giảm thời gian thật vì vẫn block tuần tự trên event loop) — hoặc tệ hơn,
có rủi ro thật (không giả định) làm nặng thêm sự cố ALB timeout đã xảy ra trên chính container
đang phục vụ traffic admin thật. Không tạo nhánh `feature/aa-418-parallel-cost-investigation`
vì không có gì an toàn để đo — dừng ở báo cáo ràng buộc, đúng bước 4 của task.

### A.4 — Ảnh hưởng lên Run History UI nếu sau này song song hoá

**Rủi ro thấp, không cần đổi gì ở tầng backend response shape.** `GET /admin/produce/
run/{run_id}` (`admin_produce.py::get_produce_run()`) chỉ SELECT trực tiếp từ
`acp_v2_slots`/`acp_deliver.pieces` theo `run_id` — không có logic tính "% hoàn thành" giả
định thứ tự tuần tự nào. Frontend (`frontend/app/admin/produce/page.tsx`) cũng chỉ đếm
`passedCount`/`heldCount` từ mảng `run.pieces` thật (poll lại mỗi lần), không dựng progress
bar theo index. Nếu song song hoá, giao diện chỉ đơn giản thấy nhiều piece/slot "xuất hiện
cùng lúc" thay vì tuần tự — không vỡ.

**Một điều CẦN sửa nếu song song hoá** (không phải UI, mà là error-isolation semantics):
`_produce_slots_background()` hiện có try/except RIÊNG cho từng slot trong vòng lặp tuần tự —
1 slot lỗi không kéo sập các slot khác, slot lỗi bị để lại `status='due'` để retry lần sau
(comment dòng 128-135, xác nhận đây là quyết định có chủ đích 15/08). Song song hoá cần giữ
NGUYÊN đặc tính này — ví dụ `asyncio.gather(..., return_exceptions=True)` rồi xử lý từng kết
quả riêng — không được để 1 exception ở 1 task làm crash cả `gather()` và kéo theo các slot
khác đang chạy dở.

---

## Phần B — Hiện trạng cost tracking

### B.1 — Có log usage mỗi lần gọi Bedrock không? CÓ — nhưng chỉ ra CloudWatch, KHÔNG ra DB

Xác nhận qua đọc code trực tiếp (không phải suy đoán):
- `invoke_claude()` (`shared/llm_client/bedrock_satellite.py`) trả về `BedrockInvokeResult.
  usage` — dict thật từ response Bedrock (`input_tokens`/`output_tokens`/cache fields).
- `invoke_judge()` (`services/acp_produce/judge_client.py`) trả `{input_tokens, output_tokens,
  ...}` + tự log `logger.info("judge_llm_success", ..., in_tokens=..., out_tokens=...)`.
- Callers (`repair.py::repair_piece()`, `generation.py`, `adapt.py`, `faq.py`) mỗi hàm tự log
  1 dòng structlog riêng (`e5_repair_success`/`e2_draft_batch_success`/...) kèm `usage=...`
  đầy đủ — **NHƯNG chỉ đi qua `structlog` → stdout → CloudWatch `/ecs/aa-cis-dev`** (retention
  14 ngày, đã xác nhận trước đây), KHÔNG có bất kỳ `INSERT`/`UPDATE` nào ghi token/cost vào DB
  từ 5 call site này.
- Xác nhận trực tiếp bằng cách đọc `_persist_piece()` (`pipeline.py`, hàm DUY NHẤT ghi
  `acp_deliver.pieces`) — câu `INSERT` liệt kê đủ 14 cột, KHÔNG có cột cost/token nào.
  `acp_shared.acp_v2_runs`/`acp_v2_slots` (migration 096) cũng không có cột cost.
- Log KHÔNG có `run_id`/`piece_id` gắn trong dòng log — verify bằng thực nghiệm: filter theo
  `"e5_repair_success" "d0722ae3"` trong CloudWatch trả về 0 kết quả dù đúng run_id đó thật.
  Correlation với 1 run cụ thể **chỉ làm được bằng khung thời gian** (như audit trước đây và
  phần B.4 dưới đây đã làm) — xấp xỉ, không tuyệt đối chính xác nếu 2 run trùng giờ.

### B.2 — Có cơ chế DB nào ĐÃ CÓ SẴN cho việc này không? CÓ — nhưng gắn với bảng SAI (S1-S4, không phải N7)

Phát hiện quan trọng, chưa có trong bất kỳ audit trước: `services/acp_shared/cost_utils.py`
(migration 055, ticket **AA-118**) đã xây ĐẦY ĐỦ cơ chế cost tracking dạng bảng —
`calc_bedrock_cost()`, `record_stage_cost()` (ghi `acp_shared.acp_stage_runs`, upsert theo
`(run_id, stage)`, cộng dồn mỗi lần gọi), `finalize_run_cost()` (tổng hợp vào
`acp_shared.acp_runs.total_llm_cost_usd`). Callers thật: `services/acp/s2/tools/
synthesize.py`, `services/acp_s4/graph.py`, `api/routers/v1_s4_social.py`,
`api/routers/v1_acp.py` — **toàn bộ đều thuộc pipeline S1-S4 CŨ, KHÔNG PHẢI N7**
(`services/acp_produce/*`). `acp_stage_runs.run_id` có FK trỏ tới `acp_shared.acp_runs` — một
bảng KHÁC HẲN `acp_shared.acp_v2_runs` mà N7 dùng (migration 096). Có nghĩa: N7 không thể chỉ
"gọi `record_stage_cost()`" — FK sẽ fail vì `acp_v2_runs.run_id` không tồn tại trong
`acp_runs`. Cơ chế đã có SHAPE tái dùng được (bảng + hàm) nhưng KHÔNG thể cắm thẳng vào N7 mà
không sửa (bảng mới trỏ đúng FK, hoặc dùng lại pattern chứ không dùng lại bảng).

### B.3 — Effort ước tính để thêm structured logging cho N7

- **Option rẻ nhất (chỉ thêm run_id/piece_id vào dòng log hiện có):** thêm 2 kwarg
  (`run_id=`, `piece_id=`) vào 5 lệnh `logger.info(...)` đã có sẵn `usage=...`
  (`e2_draft_batch_success`/`e3_adapt_channel_success`/`e4_faq_batch_success`/
  `e5_repair_success`/`judge_llm_success`) — nhưng 3/5 hàm (`repair_piece()`,
  `invoke_judge()`'s caller trong `gates.py`) **hiện KHÔNG nhận `piece_id` làm tham số ở tầng
  gọi LLM** (chỉ có `body_tagged`/`violations`) — phải thread `piece_id` xuyên qua thêm 1-2
  tầng hàm, không phải chỉ thêm 1 dòng. Effort: nhỏ (~vài giờ, tương đương độ lớn 1 phần của
  fix AA-415), rủi ro thấp (chỉ thêm field log, không đổi logic).
- **Option ghi DB thật (per-run hoặc per-piece):** cần 1 migration mới (bảng riêng cho N7,
  hoặc thêm cột `cost_usd`/`tokens_input`/`tokens_output` — có thể gắn thẳng vào
  `RepairRoundLog` (`models.py`, đã có sẵn, N7 dùng để lưu từng vòng repair) thay vì bảng mới,
  vì N7 đã có đúng đơn vị "piece" + "round" rồi, tái dùng structure có sẵn hợp lý hơn tạo bảng
  song song kiểu `acp_stage_runs`) + code ghi tại đúng 5 call site + threading `piece_id` như
  trên. Effort: trung bình (~1 ngày làm việc thật, có test), rủi ro trung bình (đụng vào đúng
  vòng repair AA-415 vừa sửa — cần cẩn thận không phá `PieceInvariants`/prompt logic).
- **Option dashboard đầy đủ** — xem B.5 option 3, phụ thuộc option ghi DB thật ở trên có trước.

### B.4 — Cost thật 1 run gần nhất, TRACE THẬT qua CloudWatch (không phải ước tính)

Dùng run #6 (`d0722ae3`, tenant `aa_internal`, 2026-09 W2 — đúng run vừa dùng làm baseline
AA-415) vì đây là run gần nhất có timeline xác nhận đầy đủ. Query thật:
`aws logs filter-log-events --log-group-name /ecs/aa-cis-dev` trong cửa sổ
`2026-08-16T02:08:00Z` → `02:46:00Z` (bao trùm cả thời điểm bắt đầu tới `status=completed`
lúc 02:45:02Z, theo `docs/claude_audit/AA-404-n7-run6-results.md`), parse `usage=` thật từ
từng dòng log, nhân giá/1M-token (Sonnet 4.6: $3 in / $15 out; Nova Pro: $0.8 in / $3.2 out —
2 mức giá này TÁI DÙNG nguyên từ investigation cost trước đó trong `AA-404.md`, back-derived
khớp khớp số $ đã công bố ở đó, không phải số tôi tự đặt ra):

| Stage | Lệnh gọi thật | Input tok | Output tok | Cost |
|---|---:|---:|---:|---:|
| E2 draft | 8 | 18,052 | 6,222 | $0.1475 |
| E3 adapt FB/TikTok | 8 | 29,068 | 2,029 | $0.1176 |
| E4 FAQ | 4 | 7,124 | 521 | $0.0292 |
| **E5 repair** | **35** | **100,018** | **31,619** | **$0.7743 (60%)** |
| F8/F9 judge (Nova Pro) | 90 | 180,909 | 21,085 | $0.2122 |
| **TỔNG run #6** | **145** | **335,171** | **61,476** | **$1.2809** |

**Lưu ý quan trọng — số này BAO GỒM 2 sự cố ECS/ALB timeout thật xảy ra trong chính run #6**
(container bị kill+replace 2 lần giữa chừng, 1 slot phải regenerate lại từ đầu) — nghĩa là đây
là "tiền THẬT đã tốn cho lần chạy này" đúng như task yêu cầu, KHÔNG PHẢI "tiền của 1 lần chạy
sạch không sự cố". Không tách được chính xác phần nào thuộc slot bị orphan vs slot chạy 1 lần
ăn ngay trong phạm vi khảo sát này (cần match thêm timestamp ECS task start/stop — ngoài scope
task này). Để so sánh, run tuần trước đó không sự cố (post-#153, week 4, 12 piece,
`docs/implementation-notes/AA-404.md`) có cost đo được ~$1.23 — cùng độ lớn, không lệch nhiều
dù run #6 có 2 lần retry, gợi ý phần cost "lãng phí" do sự cố hạ tầng không quá lớn so với
tổng — nhưng đây là so sánh gián tiếp (2 run khác code state/số piece), không phải phép trừ
trực tiếp.

E5 repair chiếm 60% cost — khớp pattern đã thấy nhất quán qua mọi run trước đó (63% trong tổng
4-run trước, `AA-404.md`).

### B.5 — 3 phương án cho cost/run visibility (không code, chỉ đề xuất)

**Option 1 — Rẻ nhất, không đụng pipeline (khuyến nghị làm TRƯỚC):** script/endpoint admin
chạy CloudWatch query giống hệt B.4 (đã viết + verify thật trong session này, có thể tái dùng
làm base), cho `run_id` bất kỳ → trả `estimated_cost_usd`, hiện lên Run History UI như 1 field
bổ sung trong response `GET /admin/produce/run/{run_id}` hiện có (không đổi schema DB, không
đổi hot path). Hạn chế: chỉ chạy được cho run trong 14 ngày gần nhất (CloudWatch retention),
đúng/sai phụ thuộc cửa sổ thời gian không trùng run khác (rủi ro thấp — N7 hiện chạy thưa,
Nghiệp tự trigger).

**Option 2 — Ghi thật vào DB, gắn `piece_id`+`run_id` chính xác (medium effort, cần threading
piece_id qua 5 call site — xem B.3):** tái dùng SHAPE của `acp_stage_runs`/`record_stage_cost()`
đã có (không tái dùng được bảng đó trực tiếp vì FK sai bảng — B.2) nhưng viết bảng mới đúng
cho N7 hoặc gắn cột vào `RepairRoundLog`/`acp_deliver.pieces`. Cho số chính xác vĩnh viễn
(không phụ thuộc CloudWatch retention), query được trực tiếp trong Run History UI không cần
gọi CloudWatch mỗi lần.

**Option 3 — Dashboard đầy đủ (breakdown theo gate/repair round/model/account, xu hướng theo
thời gian):** PHỤ THUỘC Option 2 có dữ liệu structured trước — không nên spec chi tiết trước
khi Option 2 tồn tại, vì dashboard chỉ hiển thị lại dữ liệu, không tự tạo ra dữ liệu.

---

## Kết luận & khuyến nghị

1. **Song song hoá piece-level: KHÔNG làm ngay.** Không phải vì có dependency dữ liệu thật
   (đã xác nhận: không có, ở đúng vòng lặp gate+repair) — mà vì 3 rào cản HẠ TẦNG/CODE thật
   (connection dùng chung, lệnh Bedrock đồng bộ chặn event loop, quota acc3 chưa verify) phải
   sửa trước, và container hiện tại (1 task duy nhất, chia sẻ event loop với API serving thật)
   đã có TIỀN SỬ sự cố ALB timeout thật liên quan trực tiếp tới đúng cơ chế blocking-call này.
   Làm ẩu rủi ro thật (không phải giả thuyết): tốn thêm engineering time xây 1 giải pháp không
   giảm thời gian thật (nếu chỉ bọc `gather()`), hoặc tệ hơn làm nặng thêm sự cố hạ tầng đang
   ảnh hưởng traffic admin thật. Nếu muốn theo đuổi: việc đầu tiên là verify quota RPM acc3
   thật (cần 1 CLI profile mới hoặc Nghiệp tự check Console), sau đó thiết kế lại quanh
   connection-per-task + `asyncio.to_thread()`, và cân nhắc nghiêm túc chạy N7 production trên
   1 task/service RIÊNG khỏi API-serving container thay vì cùng chung — không phải chỉ "thêm
   gather() vào for-loop".
2. **Cost tracking: đáng làm TRƯỚC, rủi ro thấp hơn nhiều.** Option 1 (B.5) không đụng pipeline
   AA-415 vừa sửa, không cần migration, cho số thật trong vài giờ việc — trực tiếp trả lời câu
   "chạy verify nhiều lần tốn bao nhiêu" mà không cần chờ song song hoá xong trước.
3. **Nếu chỉ chọn 1 việc làm tiếp theo: cost tracking Option 1.** Thấp rủi ro, trả lời đúng câu
   hỏi thực dụng nhất ("mỗi lần verify tốn bao nhiêu"), và độc lập hoàn toàn với rủi ro hạ tầng
   của song song hoá.
