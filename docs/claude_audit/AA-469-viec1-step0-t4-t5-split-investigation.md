# AA-469 Việc 1 — STEP0 Investigate: tách T4 (Tenant Tour Pool) / T5 (Atomize) thành 2 trigger độc lập

Investigate only, không sửa code, không commit gì trong phiên này. Làm trên `main`, không dùng
worktree riêng (đọc-only). Đọc trực tiếp `api/routers/v1_tours.py`,
`services/acp_produce/tenant_pipeline.py` toàn bộ phiên này (không tin lại mô tả AA-425/AA-436 dù
đã Done) + query DB thật qua S3-mediated ECS exec pattern.

**Status: STEP0 xong — chưa build gì.** Đây là 1 trong 5 Việc AA-469 yêu cầu điều tra riêng
trước khi bất kỳ Việc nào được thiết kế/build — xem cross-Việc conflict note trong
`docs/claude_tasks/AA-469-redesign-t-series.md`.

---

## 1. Entry point thật của "bấm Viết" (T1)

`POST /pool/{published_tour_id}/rewrite` (`api/routers/v1_tours.py:198-199`, hàm `trigger_rewrite`).
Endpoint tạo 1 row `pending` trong `gold_aa_internal.tenant_tour_versions` (INSERT đồng bộ), sau đó
launch `_do_rewrite_and_save()` (nested closure, `v1_tours.py:320`) bằng
`asyncio.create_task(...)` — fire-and-forget, response trả về ngay lập tức
(`"message": "Rewrite started — check My Catalog in ~30 seconds for results"`). Toàn bộ T2→T3→T4→T5
chạy bên trong closure này, KHÔNG có trigger riêng nào khác gọi `run_t5_atomize()` ở bất kỳ router
hay service nào khác trong repo (`grep -rn "run_t5_atomize" api/routers/ services/acp_produce/` →
đúng 1 call site duy nhất, `v1_tours.py:462`).

## 2. Chuỗi gọi thật T2→T3→T4→T5, có file:line xác nhận qua grep -n

| Bước | Gì xảy ra | File:line |
|---|---|---|
| T2 | `_rewrite_tour()` (import từ `v1_pipeline.py`) — LLM rewrite | trong `_do_rewrite_and_save()`, `v1_tours.py:320+` |
| T3 | `run_t3_qa_gate()` — QA gate + self-repair tối đa 2 vòng | import tại `v1_tours.py:376` |
| **T4** | `UPDATE gold_aa_internal.tenant_tour_versions SET rewritten_content=…, status=…, quality_score=…, qa_status=…, qa_repair_count=…, qa_checked_at=…, qa_auto_passed=…` — đây CHÍNH XÁC là điểm "commit vào My Catalog" | `v1_tours.py:425` |
| (nhánh QA fail) | Nếu `qa["passed"]` false: `escalate_t3_failure()` — ghi `review_queue`, KHÔNG chặn chuỗi tiếp tục | check tại `v1_tours.py:443`, import tại `v1_tours.py:448` |
| **T5** | `run_t5_atomize()` — atomize, chạy **VÔ ĐIỀU KIỆN** ngay sau, không nằm trong `if`/`else` nào phân biệt qua/fail | import tại `v1_tours.py:461`, gọi tại `v1_tours.py:462` |
| (except toàn cục) | Nếu bất kỳ bước nào raise: `UPDATE ... SET status = 'needs_review' WHERE status = 'pending'` (best-effort, không phân biệt lỗi ở đâu) | `v1_tours.py:477` |

Comment ngay tại chỗ gọi T5 (`v1_tours.py:454-460`) tự mô tả rõ ràng đây là quyết định có chủ đích
từ AA-436 ("AA-436 removed the old `else` here — T5 now always runs, real pass or auto-pass
alike"), không phải lỗi copy-paste.

## 3. Bug đã xác nhận: T5 chạy vô điều kiện sau T4, không có gate theo `qa_status`/`qa_auto_passed`

Không có branch/if nào giữa dòng UPDATE T4 (`v1_tours.py:425`) và lời gọi T5 (`v1_tours.py:462`)
kiểm tra `qa_auto_passed`. Chuỗi thật:

```
T4 UPDATE (line 425)
  → if not qa["passed"]: escalate_t3_failure() (line 443-451, chỉ GHI review_queue, không return/break)
  → run_t5_atomize() (line 461-465) — LUÔN chạy, kể cả khi qa["passed"]=False
```

Nghĩa là 1 tour rewrite fail QA thật (2 vòng repair đều không qua), được auto-pass theo AA-436
(`new_status = "needs_review"`, `qa_status = "escalated"`, `qa_auto_passed = True`), vẫn bị atomize
ngay sau đó — không có gate chặn. Đây là premise đúng của yêu cầu tách T4/T5 trong AA-469 Việc 1.

## 4. `run_t5_atomize()` — "parameter-pure nhưng không DB-self-sufficient"

Signature (`services/acp_produce/tenant_pipeline.py:203`):
```python
async def run_t5_atomize(tenant_id: str, tour_id: str, rewritten: dict, pool, country: str = "") -> dict:
```

- `rewritten: dict` được truyền TRỰC TIẾP từ closure `_do_rewrite_and_save()` — hàm KHÔNG tự query
  `tenant_tour_versions` để lấy nội dung.
- `tour_id` phải là `silver_aa_internal.raw_tours.tour_id` (docstring tự ghi rõ: caller truyền
  `published_tours.tour_id`, KHÔNG phải `tenant_tour_versions.id`).
- `country` cũng được caller truyền sẵn (từ `tour_dict["country"]`, vốn lấy qua JOIN `raw_tours`
  ở `trigger_rewrite()`), hàm không tự JOIN lại.
- Bên trong hàm hoàn toàn side-effect-free ngoài 4 input này (idempotency check qua `source_hash`
  scoped `(tour_id, owner_scope)`, gọi LLM, build `CompetitorIndex`, INSERT `tour_atoms`).

**Ý nghĩa cho việc tách trigger**: hàm này KHÔNG cần refactor sâu để chạy độc lập — nhưng một
endpoint atomize mới (VD `POST /pool/{id}/atomize`) sẽ cần 1 adapter nhỏ để tái tạo `rewritten` +
`country` từ DB (đọc lại `tenant_tour_versions.rewritten_content` JSONB, parse, JOIN `raw_tours`
lấy `country`) — vì hiện tại không có nơi nào query lại 2 giá trị này từ chỉ `tour_id`+`tenant_id`.
Không phải zero-code "chỉ thêm route gọi hàm có sẵn", nhưng cũng không phải deep refactor.

## 5. Không có cột "đã atomize hay chưa" trên `tenant_tour_versions`

`grep -rn "atomized_at\|is_atomized" services/acp_produce/tenant_pipeline.py api/routers/v1_tours.py`
→ 0 kết quả. UPDATE T4 thật (`v1_tours.py:425`) chỉ ghi
`rewritten_content, status, quality_score, qa_status, qa_repair_count, qa_checked_at,
qa_auto_passed` — không có field atomize nào. `run_t5_atomize()` tự nó cũng không đọc cột này để
quyết định chạy hay không — nó tự idempotency-check qua `source_hash` trên chính bảng
`acp_contract.tour_atoms` (`WHERE tour_id=$1 AND owner_scope=$2 ORDER BY created_at DESC LIMIT 1`).

Có 1 tiền lệ đáng chú ý: `api/routers/admin_atoms.py` (platform-scope, `owner_scope='platform'`)
đã giải quyết đúng vấn đề này bằng cách DERIVE `atomized_at` on-the-fly qua
`MAX(ta.created_at)` join từ `acp_contract.tour_atoms`, KHÔNG lưu cột riêng. Hai lựa chọn thật cho
tenant-scope (không tự quyết trong STEP0 này):
- (a) Derive giống `admin_atoms.py` — join `LEFT JOIN (SELECT tour_id, owner_scope,
  MAX(created_at) atomized_at FROM acp_contract.tour_atoms GROUP BY tour_id, owner_scope)`, scope
  theo `owner_scope = tenant_id::text` — không cần migration.
- (b) Thêm cột thật trên `tenant_tour_versions` (cần 1 migration nhỏ) nếu tenant portal cần field
  nhanh/indexed thay vì join tính mỗi lần.

## 6. Bằng chứng thiết kế gốc: AA-425/AA-436 KHÔNG dự tính tách trigger, chỉ dự tính "relabel"

Docstring đầu file `tenant_pipeline.py` (đọc nguyên văn):

> Extends the tenant-rewrite flow that already exists at api/routers/v1_tours.py's
> trigger_rewrite() / _do_rewrite_and_save() closure ... This module is what T2's caller runs AFTER
> a successful rewrite: T3 QA gate, then (on pass) T5 atomize. **T4 ("Tenant Tour Pool") is not a
> separate step here — it's the existing gold_aa_internal.tenant_tour_versions UPDATE the caller
> already does; this module only adds the qa_status verdict that write now carries** (see migration
> 107).
>
> Per AA-425's updated plan (Linear comment, 21/08, after AA-426): no temp trigger endpoint.
> **PoolTab.tsx's existing "Rewrite" button (-> POST /pool/{id}/rewrite -> _rewrite_tour()) IS the
> entry point T1 will eventually relabel, not replace.**

Kết luận: người viết code (AA-425 build session) đã BIẾT rõ T4 không phải bước riêng — đây là
quyết định có chủ đích, không phải thiếu sót. Đoạn thứ 2 cho thấy có dự tính TƯƠNG LAI (relabel nút
T1), nhưng vẫn là 1 nút cho toàn bộ chuỗi T2→T5, không phải tách thành 2 trigger độc lập. **AA-469
Việc 1 là một hướng sản phẩm THẬT SỰ MỚI, không phải tiếp nối 1 kế hoạch tách đã có sẵn.**

## 7. Xác nhận qua DB thật (S3-mediated ECS exec, RDS aa-cis-dev-db)

Tenant WanderLux Travel (`a1b2c3d4-0001-4000-8000-000000000001`, tạo 2026-04-20):
- `distinct_tours_atomized = 1` — toàn bộ lịch sử tenant này chỉ atomize đúng 1 tour, 1 lần duy nhất.
- `total_atoms = 7`, tất cả 7 atom được insert trong 1 batch duy nhất (`first_atom_at` →
  `last_atom_at` cách nhau 7.3ms: `2026-08-24T10:43:08.910864` → `2026-08-24T10:43:08.918194`).
- Tenant record tạo 2026-04-20, sự kiện atomize duy nhất diễn ra 2026-08-24T10:43:08 — 4 tháng
  sau — trùng khớp thời điểm build/live-verify session AA-425/AA-436 (không phải traffic tổ chức
  thật của tenant).
- Trong 10 rows `tenant_tour_versions` hiện có của tenant này, chỉ 1 row có `qa_auto_passed=true`
  (case QA thật sự fail nhưng vẫn cho qua theo AA-436) — row đó có `atom_count=0`. Vì
  `run_t5_atomize()` luôn insert ít nhất 1 marker row khi LLM trả về 0 atom hợp lệ (`is_empty_marker`),
  `atom_count=0` ở đây có nghĩa là lần gọi T5 đó đã LỖI trước khi tới bước INSERT (LLM error/parse
  error) — không phải bằng chứng "T5 đúng thiết kế không chạy trên tour QA-fail". Tổ hợp
  "smoking gun" thật (`qa_auto_passed=true` VÀ `atom_count>0`) **không xuất hiện** trong 10 rows
  hiện có — bug là có thật theo code, nhưng chưa có bằng chứng thực tế production để xác nhận nó
  từng thực sự tạo ra atom trên nội dung chưa qua QA.

**Kết luận chung: 0 traffic tổ chức thật qua toàn bộ chuỗi T2→T5 tính đến thời điểm audit
(2026-08-29).** Toàn bộ dữ liệu quan sát được là từ các session build/live-verify.

## Net conclusion

Việc 1 đã **sẵn sàng để thiết kế** (đủ hiểu code path, đủ hiểu schema, đủ hiểu 2 lựa chọn cho
"trạng thái đã atomize") nhưng **chưa sẵn sàng để build** — còn 1 quyết định sản phẩm treo: khi
tách T4/T5 thành 2 trigger, nút "Atomize" độc lập có nên bị chặn (disable/warning) khi
`qa_auto_passed=true` hay không, hay vẫn cho tenant tự quyết. Đây là câu hỏi cần Nghiệp/Claude Chat
quyết trước khi thiết kế response shape của endpoint atomize mới. Ngoài ra, theo yêu cầu của
Nghiệp, Việc 1 KHÔNG nên được build trước khi cả 5 Việc AA-469 đã qua STEP0 — xem
`docs/claude_tasks/AA-469-redesign-t-series.md` cho cross-Việc conflict note.
