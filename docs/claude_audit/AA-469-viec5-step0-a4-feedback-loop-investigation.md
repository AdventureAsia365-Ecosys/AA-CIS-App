# AA-469 Việc 5 — STEP0 Investigate: lỗi LLM ở từng T-step có được A4 nhìn thấy không

Investigate only, không sửa code, không commit gì trong phiên này. Làm trên `main`, không dùng
worktree riêng (đọc-only). Đọc trực tiếp `api/routers/admin_a4.py` toàn bộ, đối chiếu
`services/acp_produce/tenant_pipeline.py` (T2/T5, đã đọc đầy đủ ở Việc 1),
`services/acp_content_writing/{service,quality_gates}.py` (T9/T10, đã đọc đầy đủ ở Việc 4),
`services/acp_shared/{competitor_index,dfs_relevance}.py` (T6/T7 scoring) phiên này.

**Status: STEP0 xong — VIỆC CUỐI CÙNG trong 5 việc AA-469, không đề xuất thiết kế A4 mở rộng,
chưa viết code.**

---

## 1-2. A4 thật có 4 endpoint, KHÔNG PHẢI 2 như premise ban đầu của task

`api/routers/admin_a4.py` toàn văn (231 dòng):

| Endpoint | Bảng đọc | Ghi chú |
|---|---|---|
| `GET /admin/a4/review-log` | `silver_aa_internal.review_queue` (WHERE `tenant_tour_version_id IS NOT NULL`) | AA-437, T3-only |
| `GET /admin/a4/trust-ramp` | `acp_deliver.packets` (`publish_mode`) | AA-437 |
| `GET /admin/a4/publish-log` | `acp_shared.publish_log` | **AA-455 bước 1 (24/08), task's premise KHÔNG biết endpoint này** |
| `POST /admin/a4/publish-log/{id}/unpublish` | `acp_shared.publish_log` | AA-455 bước 1 — hành động mutate DUY NHẤT của A4 |

`get_review_log()` (`admin_a4.py:59-104`) SQL thật:
```sql
SELECT rq.id::text, rq.tour_id::text, rq.tenant_id::text, t.name AS tenant_name,
       t.slug AS tenant_slug, rq.tenant_tour_version_id::text, rq.failure_summary,
       rq.escalate_detail, rq.review_status, rq.created_at
FROM silver_aa_internal.review_queue rq
LEFT JOIN shared.tenants t ON t.tenant_id = rq.tenant_id
WHERE rq.tenant_tour_version_id IS NOT NULL [AND rq.tenant_id = $1::uuid]
ORDER BY rq.created_at DESC LIMIT $N
```
Xác nhận: query CHỈ đọc `silver_aa_internal.review_queue`, KHÔNG chạm `content_piece`/
`quarter_plan_version`/`tour_atoms` ở bất kỳ đâu trong toàn file.

**`GET /admin/a4/publish-log` (mới, task premise chưa biết) đọc `acp_shared.publish_log`** — đây
là log LỖI PUBLISH của T11 (VD lỗi gọi WordPress API), có cột `last_error` riêng — **HOÀN TOÀN
KHÁC** với lỗi quality-gate của T10 (`content_piece.held_reason`/`gate_ledger`). A4 đã CÓ tầm nhìn
vào 1 loại lỗi của T11 (publish-attempt fail), nhưng KHÔNG có tầm nhìn vào lỗi content-quality
của T9/T10 xảy ra TRƯỚC khi 1 piece tới được T11.

## 3. T2 (rewrite→T3 QA-gate) — CÓ structured error, VÀ ĐÃ A4-reachable (không phải gap)

`escalate_t3_failure()` (`services/acp_produce/tenant_pipeline.py:165-198`) — khi T3's QA gate
fail sau tối đa 2 vòng repair:
```python
INSERT INTO silver_aa_internal.review_queue
    (tour_id, tenant_id, tenant_tour_version_id, failure_summary, escalate_detail)
```
`escalate_detail` là JSONB array, mỗi entry `{check_id, field, description, source_span,
suggested_fix}` (docstring dòng 173, verify qua code build entry ở dòng 176-186).

**Đây CHÍNH XÁC là bảng + điều kiện `tenant_tour_version_id IS NOT NULL` mà `admin_a4.py`'s
`review-log` endpoint đã đọc.** Nghĩa là: T2's lỗi (thông qua T3's escalation) là DUY NHẤT trong
5 step LLM đang **ĐÃ được A4 nhìn thấy hôm nay, có structured detail đầy đủ** — không phải gap.
`qa_repair_count` (cột riêng trên `tenant_tour_versions`) chỉ là 1 số đếm, không phải chi tiết lỗi
— chi tiết thật nằm ở `escalate_detail`, chỉ ghi 1 lần lúc escalate cuối cùng (không phải mỗi
attempt).

## 4. T5 (atomize) — CÓ structured error trong code, nhưng KHÔNG BAO GIỜ persist — chỉ log

`services/acp_produce/tenant_pipeline.py`, `run_t5_atomize()`:
```python
except Exception as e:                                              # line 260
    logger.error("t5_atomize_llm_failed", ..., error=str(e))
    return {"status": "failed", "error": f"{type(e).__name__}: {e}"}
except (json.JSONDecodeError, KeyError, TypeError) as e:             # line 267
    logger.error("t5_atomize_parse_failed", ..., error=str(e))
    return {"status": "failed", "error": f"invalid atom JSON from model: {e}"}
```
Cả 2 nhánh chỉ gọi `logger.error()` (structlog → CloudWatch) — KHÔNG INSERT/UPDATE bất kỳ bảng
nào. Caller (`_do_rewrite_and_save()`, `v1_tours.py`, xác nhận từ Việc 1) cũng chỉ log tiếp
(`t5_error=t5.get("error")`), không ghi DB. **Kết luận: lỗi T5 tồn tại DUY NHẤT trong CloudWatch
logs — A4 (đọc DB) không thể nhìn thấy dù về mặt LÝ THUYẾT.** Đây là gap TOÀN PHẦN — không chỉ
thiếu route đọc, mà dữ liệu còn chưa từng được persist.

(`is_empty_marker` — LLM trả về 0 atom hợp lệ — là 1 outcome THÀNH CÔNG được thiết kế, không phải
lỗi, khác hẳn `status="failed"`.)

## 5-6. T6/T7 "scoring" — Nghiệp's uncertainty đã giải: KHÔNG có LLM nào ở bước này cả

`score_distinctiveness()` (`services/acp_shared/competitor_index.py:57-73`) — tự nhận nhãn
**"DET"** (deterministic) trong docstring, thuật toán: token-overlap set-intersection ratio so
với ngưỡng cố định 0.6/0.3. Zero LLM call (grep `LLMClient|invoke_claude|Bedrock|llm` trên cả
file → 0 kết quả).

`score_dfs_relevance()` (`services/acp_shared/dfs_relevance.py:51-77`) — tự nhận nhãn **"Pure"**,
thuật toán: `max(search_volumes)` so với ngưỡng cố định 50/500. Zero LLM call (cùng grep → 0 kết
quả).

Cả 2 hàm được gọi tại T5 (atomize time, `score_distinctiveness()` trong
`run_t5_atomize()`) và T7 (`compute_quarter_plan()`, đã xác nhận pure ở Việc 2). **T6 (Atom
Curation UI) chỉ HIỂN THỊ giá trị đã tính sẵn từ T5 — không tự tính gì.** → "T6/T7 scoring" trong
đầu bài AA-469 KHÔNG PHẢI 1 bước gọi LLM — không nên nằm trong danh sách 5 step cần thiết kế
error-capture. Nghiệp's own uncertainty ("có thể nhầm T6/T7") được giải đúng theo hướng "không
liên quan LLM cả 2".

## 7. Xác nhận gap TOÀN PHẦN, không phải một phần, cho T5/T9/T10 (+T8)

```
grep -n "content_piece\|angle_gate\|tenant_tour_versions\|tour_atoms" api/routers/admin_a4.py
```
→ **0 kết quả.** `admin_a4.py` không có bất kỳ reference nào tới 4 bảng này, xác nhận A4 hoàn toàn
không chạm vào T5 (`tour_atoms`)/T8 (`angle_gate_*`)/T9-T10 (`content_piece`) — gap toàn phần,
không phải thiếu 1 route lẻ.

## 8. Không có pattern/table error-logging chung nào giữa các stage

```
grep -rln "class.*Violation|class.*FailureLog|error_log|failure_log" services/ shared/
```
→ 0 kết quả — không có class/bảng error-log tổng quát nào.

Có 1 SỰ TRÙNG LẶP SHAPE đáng chú ý (không phải 1 pattern dùng chung): `GateResultLite`
(TypedDict, `services/acp_content_writing/quality_gates.py:141`, T10) và `GateResult` (Pydantic
BaseModel, `services/acp_produce/models.py:44`, N7) — 2 class RIÊNG, ĐỘC LẬP, tự nhận là
"referenced, not imported" (ADR §0.5, xác nhận lại từ Việc 4) — cùng ý tưởng
gate/passed/violations, nhưng cố tình không hợp nhất.

`silver_aa_internal.review_queue.escalate_detail`'s shape (`{check_id, field, description,
source_span, suggested_fix}`, mục 3) là ứng viên GẦN NHẤT với 1 "format chung" đã tồn tại và ĐÃ
được A4 đọc — nhưng hiện tại chỉ có 1 người viết (T3's `escalate_t3_failure()`), không có stage
nào khác ghi vào bảng này.

---

## Bảng tổng hợp

| Step | Gọi LLM? | (a) Structured error captured? | (b) A4-reachable hôm nay? |
|---|---|---|---|
| **T2** (rewrite → T3 QA-gate) | Có | **CÓ** — `escalate_detail` JSONB, `{check_id, field, description, source_span, suggested_fix}` mỗi issue, ghi vào `silver_aa_internal.review_queue` | **CÓ** — `GET /admin/a4/review-log` đọc đúng bảng này, `tenant_tour_version_id IS NOT NULL` |
| **T5** (atomize) | Có | Có TRONG CODE (`error: f"{type}: {msg}"`) nhưng **KHÔNG BAO GIỜ persist** — chỉ `logger.error()` → CloudWatch | **KHÔNG** — không có bảng nào để đọc, gap toàn phần kể cả về lý thuyết |
| **T6/T7** (scoring) | **KHÔNG** — cả `score_distinctiveness()` và `score_dfs_relevance()` đều pure/deterministic, 0 LLM call | N/A — không phải step LLM | N/A |
| **T9** (write) | Có | Có nhưng chỉ ở `status="failed"` (lỗi hệ thống, exception message thô) — không phải judge-gate detail | **KHÔNG** — `content_piece` không được A4 đọc |
| **T10** (quality gate, inline trong T9) | Có (2 gate LLM: `F8_framework`, `F9_brand_voice`) | **CÓ, chi tiết nhất trong 5 step** — `gate_ledger` (mọi gate, pass/fail + violations), `repair_log` (feedback đã dùng để retry), `held_reason` — tất cả trên `acp_shared.content_piece` | **KHÔNG** — `content_piece` không được A4 đọc dù dữ liệu đã có sẵn, cấu trúc tốt |

**Bức tranh thật, ngắn gọn**: T2 đã xong (error capture + A4 path đều có). T10 có error capture
TỐT NHẤT trong 5 step (structured, per-gate, có cả feedback loop nội bộ) nhưng ZERO đường ra A4 —
đây là gap "dễ vá nhất" vì dữ liệu đã sẵn, chỉ thiếu 1 route đọc `content_piece`. T5 là gap SÂU
NHẤT — dữ liệu lỗi chưa từng được persist, cần thêm cả bước ghi DB trước khi A4 có gì để đọc. T9's
lỗi hệ thống (`status="failed"`) là loại lỗi khác (crash/timeout, không phải chất lượng nội dung)
— cũng chưa A4-reachable nhưng ít giá trị pattern-detection hơn T10's gate-level data.

## Giới hạn phiên này

- Không đọc `services/acp_content_writing/generate.py`/`framework_rubrics.py` toàn văn (đã đủ
  context từ Việc 4 + `quality_gates.py`'s docstring để trả lời câu hỏi Việc 5).
- Không kiểm tra CloudWatch logs thật để xác nhận T5's `t5_atomize_llm_failed`/
  `t5_atomize_parse_failed` có thực sự từng bắn ra hay chưa — chỉ xác nhận code PATH tồn tại,
  không xác nhận đã có occurrence thật (Việc 1 đã tìm 1 case thật `qa_auto_passed=true,
  atom_count=0` gợi ý T5 CÓ fail thật ít nhất 1 lần, nhưng không truy được log chi tiết trong
  phạm vi STEP0 này).
- Không query DB trong phiên này — thuần đọc code, không cần số liệu mới cho task này.

---

## STEP0 toàn bộ 5 Việc AA-469 đã hoàn tất

Việc 1-5 đều đã có audit file riêng trong `docs/claude_audit/`, task log tổng hợp tại
`docs/claude_tasks/AA-469-redesign-t-series.md`. Sẵn sàng cho bước tổng hợp thiết kế cross-Việc
(chưa làm trong phiên này, theo đúng yêu cầu "không đề xuất thiết kế").
