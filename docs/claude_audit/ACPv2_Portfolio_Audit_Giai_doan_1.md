# Rà soát ACPv2 — Giai đoạn 1/4: PRD ↔ Linear Issue Mapping (bản hợp nhất)

*Snapshot 14/08/2026. KHÔNG đọc code, KHÔNG verify trạng thái sống, KHÔNG kết luận trùng lặp/lỗi thời — chỉ đọc + lập bảng ánh xạ theo đúng phạm vi Giai đoạn 1. Bản này HỢP NHẤT 146 issue trong 3 project được giao + 25 issue phát hiện thêm (không gán project nào) vào 1 bức tranh duy nhất — không có bảng riêng cho nhóm 25 issue.*

---

## Xác nhận lại nhóm 25 issue ngoài phạm vi 3 project

Phương pháp xác nhận lại (không suy đoán): `list_projects` (query "ACP") → workspace chỉ có đúng 3 project ACP-liên-quan: `ACP v2`, `ACPv2 Phase 2`, và `ACP — Agency Content Pipeline` (ACP v1 gốc, status Completed — **loại khỏi phạm vi ACPv2 theo đúng yêu cầu**). Dò lại qua 3 đường: (a) `parentId=AA-386` và `parentId=AA-368` (2 epic mới nhất) → lộ ra toàn bộ chuỗi con không có `project`; (b) `list_issues query="ACPv2"` (51 kết quả, lọc theo prefix `[ACPv2]`/`[ACP2]` tường minh) → xác nhận thêm; (c) dò tay ID liên tiếp AA-393→AA-405 (AA-405 không tồn tại → AA-404 là ID mới nhất tại thời điểm đọc, không sót khoảng trống nào giữa AA-368 và AA-404).

**Danh sách 25 issue xác nhận lại, đầy đủ status/priority (không đổi so với lần 1, chỉ bổ sung AA-308 lúc trước chưa có status rõ — nay xác nhận Done):**

| Issue | Status | Priority | Stage suy luận | Tóm tắt |
|---|---|---|---|---|
| AA-308 | Done | Urgent | N2/N2c | Writer model bake-off gốc — Phần A (writer N7, Palmyra vs Claude, 20 tour) + Phần B mở rộng (N2 decompose, sau khi Activate Credits bị reject 22/07) |
| AA-369 | Done | High | N7-A | C1/C2 DataForSEO keyword+SERP theo SLOT + C3 brief/demand-law |
| AA-370 | Done | High | N7-B | E1 Outline + E2 Draft — writer chính |
| AA-371 | Done | Medium | N7-C | E3 Adapt channel + E4 FAQ/JSON-LD |
| AA-372 | Done | Medium | N7-D | 5 gate F2/F3/F4/F6/F7 còn thiếu, nối vào orchestrator đã có |
| AA-373 | Backlog | Low | N7 | Bug: atom dư dồn hết vào section cuối (FAQ) |
| AA-385 | Canceled | High | Ngoài N0-N8 (ACPv1 tàn dư) | `admin_acp_proxy.py` sống thật — data-risk investigation |
| AA-386 | Done | Urgent | N0-N8 (UI-EPIC, cross-cutting) | Kế hoạch build UI tổng thể — parent của 387/388/389/390/391/394 |
| AA-387 | Done | High | Marketplace | UI 1/7 — verify middleware + 2 UX fix |
| AA-388 | Done | Urgent | N5/Gate B | UI 2/7 — Gate B approval UI |
| AA-389 | Done | High | N1 | UI 3/7 — 4 endpoint N1 còn thiếu UI |
| AA-390 | Done | Low | Ngoài N0-N8 (UI nav) | UI 7/7 — Sidebar label fix |
| AA-391 | Done | Urgent | N0-N8 (E2E) | E2E 0/7 — verify chuỗi S0→N8 chạy thông bằng script |
| AA-392 | Done | Urgent | N2b | Model change: Palmyra X5 → Bedrock Sonnet/Haiku cho S1-from-atom |
| AA-393 | Backlog | Low | N2b | Entailment false-positive: nhãn thứ tự (Site 1/2) bị nhận nhầm số liệu |
| AA-394 | Done | Urgent | N7/N8 | `tenant_tour_pages` rỗng → backfill tạm 87 tour |
| AA-395 | Backlog | High | N8 | Landing page engine thật (ADR-2026-030) — chưa từng lên issue tới 12/08 |
| AA-397 | Done | High | Hạ tầng (L5, cross-cutting) | acc3 Bedrock satellite mới — role + Terraform |
| AA-398 | Done | High | Hạ tầng | acc2 ecs-task-policy AssumeRole vào acc3 |
| AA-399 | Done | No priority | Hạ tầng | acc3 routing chain thật — client.py/bedrock_satellite.py/models.py, 6/7 call site |
| AA-400 | Backlog | Medium | Marketplace | UX default view toàn tour atom=0 |
| AA-401 | Backlog | Medium | N1/Marketplace (i18n) | Sót tiếng Việt — Mirror tab + master-content + curation |
| AA-402 | Done | Medium | N8 (hạ tầng) | EventBridge module orphaned — dời sang acc2 |
| AA-403 | Backlog | Medium | N7/N8 | Nối N7 vào Mistake-to-Rule (H-3) — HITL rejection note chưa chạy trên N7 |
| AA-404 | Backlog | Medium | N7 | F3/F4 threshold calibration chưa xác định root cause |

**Xác nhận không trùng:** 25 ID này (AA-308, 369-373, 385-395, 397-404) hoàn toàn tách biệt khỏi 146 ID đã quét trong 3 project (không có ID nào xuất hiện 2 lần). **Tổng chính thức: 171 issue.**

---

## Phần A — Tóm tắt PRD theo khung N0→N8

*(Không đổi so với lần đọc 1 — PRD Notion không có bản cập nhật mới giữa 2 lần chạy. Giữ nguyên toàn bộ nội dung để tài liệu này tự đủ, không cần đọc file lần 1.)*

Nguồn: Notion "📘 ACP v2 PRD — Atom-based Rebuild (N0→N8)" (id `3a2b8a41...`). 4 lớp cập nhật, đọc theo thứ tự ưu tiên **v1.2b > v1.2 > v1.1 > thân PRD gốc (v1.0, §1-13)**.

### 8 quyết định khoá kiến trúc (D1-D8, chốt 13/07/2026)
| # | Quyết định | Ý nghĩa |
|---|---|---|
| D1 | Atom decompose từ `src_itineraries` (sự thật supplier), KHÔNG từ `aa_itineraries` | Tránh model tự chứng nhận chính nó |
| D2 | Atom đứng TRƯỚC S1 — 1 tầng sự thật, 2 output | S0/S1 KHÔNG bị thay thế, chạy song song |
| D3 | Atom thuộc TOUR (`owner_scope='platform'`), KHÔNG thuộc tenant | AA curate 1 lần → mọi tenant thừa hưởng |
| D4 | Mode A — tenant license catalog CỦA AA | Đúng SSP thesis |
| D5 | AA HOST trang tour white-label (`trip_url`) | Không có → gate F6 reject mọi slot |
| D6 | HITL 2 tầng: Gate A (Mirror, onboard) · Gate B (Slot Grid, 1×/quý, NEVER auto) · Gate C (Weekly Packet, veto 48h, trust ramp) | Thay 3 gate per-run cũ |
| D7 | T1 Weekly Packet = $799/mo MANAGED SERVICE | Bỏ chữ "free" |
| D8 | Facts pack cấp platform | Giải nghịch lý TOFU (§7) |

### 6 luật bất biến (L1-L6)
L1 Closed world · L2 Determinism boundary · L3 Cross-weight judge · L4 Deterministic flywheel (`acp_output_rules`) · L5 Bedrock only · L6 Silence is prohibited.

### Từng Nhịp

| Nhịp | Mục đích | Tần suất/Compute | Gate | Issue chính (PRD §12, snapshot 19/07) |
|---|---|---|---|---|
| **S0 Ingest** | Excel → `raw_tours` | khi có tour mới, Lambda | — | CIS production, không track riêng |
| **S1 Rewrite** | Viết trang tour | ECS LangGraph | validate→judge→brand_audit→flag_fix→revalidate | CIS production. ⚠️ vẫn 100% GPT-4.1 |
| **N0 Enrich** | LLM trích xuất từ `src_itineraries` | Step Functions→Bedrock Batch, Llama 4 Scout | — | AA-299 |
| **N1 Onboard** | Tạo tenant → seed → gán góc → Mirror → Gate A | 1×/tenant, ECS API | Gate A | AA-309 (Q1=A: Marketplace tách riêng, TRƯỚC N1) |
| **N2 Corpus** | Atom decompose | Bedrock Batch −50%, Claude Haiku 4.5 | atom-density ≥1 cite/300 từ | AA-299+AA-305 |
| **N3 Curate** | Người duyệt atom, 50/màn hình | UI human 10 phút/tour | atom floor | AA-300 |
| **N4 Strategy** | Runway map | 1×/năm, Lambda, $0 LLM | L2 | AA-301 |
| **N5 Portfolio** | Chấm điểm tour → trọng tâm quý | 1×/quý, Lambda | **Gate B** | AA-301 + AA-320 (persist) |
| **N5b Override** | Đổi trọng tâm đột xuất | mở rộng, KHÔNG MVP | qua Gate B lại | AA-307 |
| **N6 Schedule** | Slot Allocator | 1×/tháng, Lambda, $0 | atom floor | AA-301 |
| **N7 Produce** | C1/C2 keyword→C3 brief→E1-E4 write/adapt→F1-F9 gate→E5 repair | hàng tuần, ECS Fargate Spot | F1-F9 | AA-298→AA-368→(AA-369/370/371/372) |
| **N8 Deliver+Learn** | Weekly Packet + Gate C + flywheel + unknown ledger | hàng tuần, Lambda+SES | **Gate C** | AA-298→AA-328 |
| **Marketplace** | Duyệt catalog→chọn portfolio→hợp đồng→gán góc | trước N1 | 4 tầng chống cannibalization | AA-302 Phần B→AA-330 |
| **Contract** | `v_trip_registry` | — | — | AA-302 Phần A — ✅ Done 17/07 |

### 8 quyết định Q1-Q8 (chốt 25/07/2026)
| Q | Chủ đề | Quyết định |
|---|---|---|
| Q1 🔶 | Phạm vi N1 | **A** — Marketplace tách riêng, TRƯỚC N1 |
| Q2 🔶 | `THIN_TRIP_MAX_SHARE` | **A** — giữ 0.15, review sau 1 quý |
| Q3 | Gate B persist | **B** — đầy đủ, có versioning ngay lần đầu |
| Q4 🔶 | N5b — SLA + cấp override | **A+A** — SLA chung, 1 cấp override |
| Q5 | Ô "reactive" 10% N6 | **C** — buffer chạy lại slot HELD |
| Q6 🔶 | 4 tầng cannibalization | **B** — Tầng 4 (dedup) trước |
| Q7 | S1 cũ 100% GPT-4.1 | **B** — không đụng, đo cost trước |
| Q8 | Writer model N7 | **C rồi A** — bake-off thêm, mặc định giữ Palmyra nếu không chênh rõ |

### Việc bắt buộc trước production (thứ tự chặn)
1. AA-328 (N8) 2. AA-320 (Gate B persist) 3. AA-309 (N1) 4. Tầng 4 dedup (Q6) 5. AA-326 (superlative claims)

---

## Phần B — Bảng issue theo Nhịp (HỢP NHẤT — 171 issue, không giới hạn số lượng)

Ký hiệu: **[P]** tiền tố `[ACPv2][N…]` tường minh · **[S]** suy luận · `†` = issue thuộc nhóm 25 phát hiện thêm (không gán project).

### N0 — Enrich
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-299 | Done | Urgent | [P] N0+N2 gộp — tầng sự thật + facts pack + hallucination detector |
| AA-316 | Done | High | [S] Sửa `v_trip_registry` — bỏ `review_status='approved'` chết |
| AA-315 | Canceled | High | [S] Gap UI duyệt `review_status` — superseded bởi AA-316 |
| AA-314 | Done | Medium | [S] Bug render HIGHLIGHTS — ảnh hưởng cả N0+N2 |
| AA-343 | Done | Urgent | [S] Ingest 29/07 hỏng `raw_tours` (nguồn N0) |

### N1 — Onboard
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-309 | Done | High | [P] Spec chốt Q1=A: tạo tenant·seed·gán góc·Mirror·Gate A |
| AA-383 | Backlog | Medium | [S] Lambda brand-brief-parser thiếu `brand_name` |
| AA-384 | Done | High | [P] Đổi hướng Mirror/`posts_per_week` |
| AA-389 `†` | Done | High | [P] UI 3/7 — 4 endpoint N1 còn thiếu UI |
| AA-401 `†` | Backlog | Medium | [S] i18n Mirror tab + master-content + curation |

### N2 — Corpus (Atom Decompose) + S1-from-atom (N2b)
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-299 | Done | Urgent | (gộp với N0) |
| AA-305 | Done | High | [P] `decompose_atoms()` luôn gọi Batch — fail khi <100 |
| AA-317 | Done | Medium | [S] `distinctiveness` phụ thuộc CompetitorIndex chưa có issue |
| AA-336 | Backlog | Medium | [P] Bake-off decompose mở rộng — toàn bộ ứng viên acc2 |
| AA-308 `†` | Done | Urgent | [P] N2c Writer bake-off gốc — Phần A (N7 writer) + Phần B (N2 decompose, mở rộng scope S122) |
| AA-306 | Done | Urgent | [P] N2b S1-from-atom — lắp ráp từ atom curated |
| AA-350 | Backlog | High | [S] Atom không mang trường ngày — fail 100% gate day-count |
| AA-352 | Done | High | [P] Schema atom: thêm trường ngày/thứ tự |
| AA-353 | Done | High | [S] Sửa kiến trúc nén itinerary — bám nguồn |
| AA-355 | Done | High | [P] S1-from-atom chưa đọc `itinerary_day` |
| AA-356 | Done | Urgent | [P] Entailment false-positive số ngày — blocker AA-353 |
| AA-357 | Backlog | High | [P] S1-from-atom viết quá ngắn (~41-46% nguồn) |
| AA-358 | Backlog | Medium | [P] Thiếu cost_usd logging |
| AA-359 | Backlog | Medium | [P] Exception floor/ceiling ngày nguồn cực ngắn |
| AA-392 `†` | Done | Urgent | [P] Model change: Palmyra X5 → Bedrock Sonnet/Haiku |
| AA-393 `†` | Backlog | Low | [S] Entailment false-positive: nhãn thứ tự (Site 1/2) |
| AA-339 | Done | Medium | [S] Bake-off thứ ba — S1 cũ vs S1-from-atom |
| AA-340 | Done | High | [S] Chạy S1 cũ trên Lao-biking để đối chiếu |
| AA-346 | Done | High | [S] Nén itinerary do prompt hay model |

### N3 — Curate
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-300 | Done | High | [P] Curation UI — batch 50 atom/màn hình + atom floor |
| AA-322 | Backlog | Medium | [S] content-role không có JWT thật — chặn e2e N3 |
| AA-321 | Backlog | High | [S] fastapi/starlette chưa pin — phát hiện qua CI AA-300 |

### N4/N5/N6/N5b — Cụm Plan
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-301 | Done | Urgent | [P] N4+N5+N6 gộp — 100% Python |
| AA-307 | Backlog | Medium | [P] N5b Override — spec chốt Q4=A+A |
| AA-319 | Done | Medium | [P] `THIN_TRIP_MAX_SHARE` — chốt Q2=A |
| AA-320 | Done | Urgent | [P] Persist QuarterPlan + Gate B — CHẶN PRODUCTION, chốt Q3=B |
| AA-331 | Backlog | Medium | [P] Ô reactive 10% → buffer HELD — chốt Q5=C |
| AA-377 | Done | High | [P] Slot Schedule không persist |
| AA-379 | Done | High | [P] `keyword_seed` cắt chữ, không phải từ khoá thật |
| AA-323 | Done | Medium | [S] UI Phase 2 — Runway Map bảng năm, N6 config |
| AA-388 `†` | Done | Urgent | [P] UI 2/7 — Gate B approval UI |

### N7 — Produce
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-298 | Done | Urgent | [P] Phần A gốc — 9 gate, 3 bug P0, cross-weight judge |
| AA-368 | Done | Urgent | [P] C1-E5 pipeline thật + F2-F7 (parent 4 chunk dưới) |
| AA-369 `†` | Done | High | [P] N7-A — C1/C2 DataForSEO + C3 brief/demand-law |
| AA-370 `†` | Done | High | [P] N7-B — E1 Outline + E2 Draft |
| AA-371 `†` | Done | Medium | [P] N7-C — E3 Adapt + E4 FAQ/JSON-LD |
| AA-372 `†` | Done | Medium | [P] N7-D — 5 gate F2/F3/F4/F6/F7 |
| AA-373 `†` | Backlog | Low | [P] Bug: atom dư dồn vào FAQ |
| AA-375 | Done | High | [P] Slot-runner — glue chạy C1→gates trên 1 Slot |
| AA-376 | Done | High | [P] `repair_fn` không wire — hardcode `max_repairs=0` |
| AA-378 | Done | Medium | [P] `acp_runs` — chưa có cơ chế tạo run |
| AA-380 | Done | High | [Infra] ECS role thiếu `cloudwatch:PutMetricData` |
| AA-381 | Backlog | Medium | [P] `output_rules` gate không nằm trong `run_gates()` |
| AA-382 | Backlog | Medium | [P] `repair_fn` thiếu rubric context cho F8/F9 |
| AA-396 | Done | High | [P] 9 piece thật đầu tiên bị HELD sau `max_repairs=3` |
| AA-403 `†` | Backlog | Medium | [P] Nối N7 vào Mistake-to-Rule (H-3) |
| AA-404 `†` | Backlog | Medium | [P] F3/F4 threshold calibration |
| AA-327 | Backlog | Medium | [P] 4 bug P1 (B10/B12/B13/B14) |
| AA-334 | Canceled | Medium | [P] Bake-off writer riêng N7 — Q8=C rồi A |
| AA-337 | Canceled | High | [P][RISK] Palmyra X5 quota 1 req/phút |
| AA-349 | Backlog | Medium | [S] Grounding checker chỉ so atom-được-cite |
| AA-325 | Done | Urgent→Done | [S] Audit `check_grounding()` — 1.9% bịa fact, đã fix |
| AA-326 | Backlog | Medium | [S] ~9% claim superlative không nguồn |
| AA-347 | Done | No priority | [S] `brand_audit_node` — 39/58 tour bịa, đã unpublish |
| AA-341 | Backlog | Medium | [S] Nợ kỹ thuật S1 cũ gom 1 lượt |
| AA-360 | Backlog | Medium | [S] Model selector thống nhất + cost tracking |
| AA-348 | Backlog | Low | [S] Lệch version hằng số BEDROCK_SONNET |
| AA-351 | Backlog | Medium | [S] Kiểm kê GPT trên acc2 làm judge thay Nova Pro |
| AA-291 | Done | Urgent | [Infra] Bedrock acc2 bị chặn (channel program) |
| AA-296 | Done | Urgent | [Infra] Cross-account Bedrock satellite acc1 (L5) |
| AA-397 `†` | Done | High | [Infra] acc3 Bedrock satellite mới — role + Terraform |
| AA-398 `†` | Done | High | [Infra] acc2 ecs-task-policy AssumeRole vào acc3 |
| AA-399 `†` | Done | No priority | [Infra] acc3 routing chain — client.py/bedrock_satellite.py, 6/7 call site |
| AA-335 | Done | High | [S] Kiểm kê catalog Bedrock acc2 — GATE chặn quyết định model |
| AA-336 | Backlog | Medium | (đã liệt kê N2) |
| AA-338 | Backlog | Medium | [S] 3 dữ kiện chưa giải quyết từ AA-335 |
| AA-281 | Done | High | [Infra] Integration test không chạy CI — BLOCKED-BY của N7 |
| AA-295 | Done | Medium | [Infra] Background job chết câm khi rolling-deploy — BLOCKED-BY N7 |
| AA-288 | Done | Low | [Infra] Prompt caching chưa dùng |
| AA-324 | Backlog | Medium | [Infra] Prompt caching vô hiệu trên acc1 satellite |

### N8 — Deliver + Learn
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-328 | Backlog | High | [P] N8 gộp — Weekly Packet, flywheel, B8, unknown ledger |
| AA-361 | Done | High | [P] N8-1 — usage_log write-back + atom.weight |
| AA-362 | Done | Medium | [P] N8-2 — 5 alarm CloudWatch còn thiếu |
| AA-363 | Done | High | [P] N8-3 — L4 wiring `apply_output_rules()` |
| AA-364 | Done | High | [P] N8-4 — Weekly Packet assembly, sửa B8 |
| AA-365 | Done | Medium | [P] N8-5 — Gate C / trust ramp / veto window |
| AA-366 | Backlog | Low | [P] N8-6 — Unknown ledger + supplier scorecard |
| AA-367 | Done | Medium | [P] N8-4b — gán Piece thật vào packet |
| AA-386 `†` | Done | Urgent | [P] UI-EPIC N0→N8 (parent 387/388/389/390/391/394) |
| AA-387 `†` | Done | High | [P] UI 1/7 — Marketplace middleware + 2 UX fix |
| AA-391 `†` | Done | Urgent | [P] E2E 0/7 — verify chuỗi S0→N8 |
| AA-394 `†` | Done | Urgent | [P] `tenant_tour_pages` rỗng — backfill tạm 87 tour |
| AA-395 `†` | Backlog | High | [P][ADR-2026-030] Landing page engine thật |
| AA-402 `†` | Done | Medium | [Infra] EventBridge module orphaned — dời sang acc2 |
| AA-390 `†` | Done | Low | [P] UI 7/7 — Sidebar label fix |

### Marketplace (Tầng 3, trước N1)
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-302 | Done | Urgent | [P] Contract + Marketplace Phần A/B |
| AA-330 | Done | High | [P] Duyệt catalog + chọn portfolio — tách khỏi N1 |
| AA-384 | Done | High | (đã liệt kê N1) |
| AA-400 `†` | Backlog | Medium | [P][UX] Default view toàn tour atom=0 |
| AA-332 | Backlog | High | [P] Tầng 4 dedup pgvector >0.92 (Q6=B) |

### Contract (input)
AA-302 Phần A — Done 17/07.

### Model/Bake-off/Infra (cross-cutting)
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-335 | Done | High | (đã liệt kê N7) |
| AA-338 | Backlog | Medium | (đã liệt kê N7) |
| AA-333 | Backlog | Medium | [S] Đo chi phí GPT-4.1 thật S1/tháng — điều kiện kèm Q7=B |

### Ngoài phạm vi N0-N8 (ACPv1 tàn dư / UI-nav / infra không gắn 1 nhịp)
| Issue | Trạng thái | Priority | Tóm tắt |
|---|---|---|---|
| AA-385 `†` | Canceled | High | [ACPv1 tàn dư] `admin_acp_proxy.py` sống thật — data-risk investigation, kết luận Canceled (không phải bug thật, xem PRD note) |

### S0/S1 + Hạ tầng CIS chung + Security/Ops — "Không rõ Nhịp" (project `AA-CIS`)
~55 issue, liệt kê đầy đủ:
AA-12, AA-13, AA-51, AA-52, AA-129, AA-130, AA-131, AA-133, AA-134, AA-136, AA-153, AA-160, AA-182, AA-199, AA-211, AA-212, AA-215, AA-223, AA-229, AA-230, AA-234, AA-235, AA-237, AA-238, AA-239, AA-240, AA-241, AA-242, AA-243, AA-247, AA-248, AA-249, AA-250, AA-251, AA-252, AA-253, AA-254, AA-255, AA-256, AA-257, AA-258, AA-259, AA-262, AA-263, AA-271, AA-274, AA-275, AA-276, AA-278, AA-279, AA-281, AA-282, AA-283, AA-284, AA-285, AA-286, AA-287, AA-289, AA-290, AA-292, AA-293, AA-294, AA-295, AA-310.

Lớp S0/S1 CIS production (không bị ACPv2 thay thế, D2) + infra/security/cost nền tảng dùng chung.

---

## Phần C — Ánh xạ PRD-requirement ↔ Issue (cập nhật sau merge)

| PRD yêu cầu/quyết định | Issue tương ứng | Trạng thái ánh xạ |
|---|---|---|
| D1-D3 (Atom = tầng sự thật) | AA-299, AA-305, AA-306 | ✅ |
| D4 (Mode A licensing) | AA-302, AA-330 | ✅ |
| D5 (`trip_url` AA-host) | AA-302 Phần A (Done — VIEW/cột) · AA-394`†` (Done — backfill TẠM 87 tour) · AA-395`†` (Backlog — engine THẬT, ADR-2026-030 chưa từng lên issue tới 12/08) | ⚠️ 3 tầng hoàn thiện khác nhau — engine thật vẫn thiếu |
| D6 (HITL Gate A/B/C) | Gate A=AA-309; Gate B=AA-320(persist)+AA-388`†`(UI); Gate C=AA-365(logic)+AA-402`†`(schedule infra) | ✅ đầy đủ cả logic lẫn UI sau merge — **trước merge lần 1, UI Gate B/C (AA-388) chưa được biết tới, nay đã có** |
| D7 ($799/mo) | Không có issue kỹ thuật riêng (quyết định thương mại) | Không áp dụng, không phải gap |
| D8 (Facts pack platform) | Chỉ lồng trong AA-299, không có issue riêng cho facts pack visa/mùa/an toàn/tiền | 🔴 vẫn là gap sau merge — không issue nào trong 25 issue mới bổ sung phần này |
| L1 Closed world/entailment | AA-298 P0-1, AA-325, AA-326, AA-349, AA-393`†` | ✅ nhiều issue, lặp lại pattern false positive/negative |
| L2 Determinism boundary | AA-301 | ✅ |
| L3 Cross-weight judge | AA-298, ADR-2026-014 | ✅ |
| L4 `acp_output_rules` | AA-363 (Done) + AA-403`†` (Backlog — Mistake-to-Rule CHƯA nối N7) | ⚠️ vẫn chưa đóng vòng hoàn chỉnh sau merge |
| L5 Bedrock only | AA-296 (acc1) + AA-397`†`/AA-398`†`/AA-399`†` (acc3 mới, Done) | ✅ **vượt kỳ vọng PRD — PRD chỉ biết acc1, acc3 là phát sinh sau (12/08), merge lần này mới lộ ra toàn bộ** |
| L6 Silence prohibited | AA-396 | ✅ |
| Q1 Marketplace tách N1 | AA-330, AA-309 | ✅ |
| Q2 `THIN_TRIP_MAX_SHARE` | AA-319 | ✅ Done |
| Q3 Gate B persist | AA-320 (Done) + AA-388`†` (UI, Done) | ✅ **merge xác nhận Gate B ĐÃ có UI thật (Done 09/08), không chỉ persist DB — thông tin này KHÔNG có ở lần đọc 1** |
| Q4 N5b override | AA-307 | ⚠️ vẫn Backlog |
| Q5 Ô reactive → buffer HELD | AA-331 | ⚠️ vẫn Backlog |
| Q6 Tầng 4 dedup trước | AA-332 | ⚠️ vẫn Backlog — 1/5 điều kiện production |
| Q7 Không đụng S1 cũ + đo cost | AA-333 | ⚠️ vẫn Backlog |
| Q8 Bake-off N7 rồi giữ Palmyra | AA-308`†` (Done — bake-off GỐC đã chạy 22/07, kết quả 14/20 KHÔNG đạt ý nghĩa thống kê) → AA-334 (Canceled — bake-off BỔ SUNG theo Q8 chưa từng chạy) → AA-337 (Canceled — nhưng dữ kiện throttle 1 req/phút của nó được AA-392 trích dẫn) → **AA-392`†` (Done 09/08 — đảo ngược: chuyển hẳn sang Bedrock Sonnet/Haiku)** | 🔴 **Xác nhận rõ hơn sau merge: Q8 KHÔNG chỉ "có dấu hiệu đảo ngược" — đã đảo ngược THẬT qua AA-392, và AA-392 trích dẫn trực tiếp dữ kiện của AA-337 (issue đã bị Canceled) làm căn cứ. PRD (v1.2b, 26/07) không phản ánh việc này vì AA-392 xảy ra sau (09/08)** |
| §4.3 model routing bake-off N2 | AA-308`†` (Done — đã chạy Phần B N2 decompose bake-off) | ✅ **merge làm rõ: AA-308 không "trạng thái không rõ" như lần 1 — đã Done, bao gồm cả phần N2** |
| §5.0.1 wire `invoke_claude()` vào S1 CIS thật (gap PRD tự ghi) | AA-399`†` liệt kê 6/7 call site được sửa: research.py/generation.py/repair.py/faq.py/adapt.py/**s1_from_atom.py** — đây là các file thuộc pipeline ACPv2 N7/N2b, KHÔNG phải node S1 CIS rewrite gốc | 🔴 **Vẫn là gap sau merge — AA-397/398/399 mở rộng satellite cho ACPv2, không xác nhận đã wire cho S1 CIS cũ. Cần Giai đoạn 2 verify trực tiếp code S1 CIS.** |
| §7 nguồn atom (7 nguồn, PRD chỉ ghi rõ 3/7) | Không tìm thấy issue bổ sung 4 nguồn còn lại trong cả 171 issue | 🔴 vẫn là gap sau merge |
| §9.2 bug P1 (B4-B14) | B4/B5/B6/B7/B9/B11→AA-301; B8→AA-364; B10/B12/B13/B14→AA-327 | ✅ |
| Marketplace 4 tầng cannibalization | Tầng 4: AA-332 (Backlog). Tầng 1-3: không tìm thấy issue riêng (kể cả trong 25 issue mới) | 🔴 vẫn là gap — PRD tự ghi "làm khi có tenant #2 thật" nên có thể cố ý trì hoãn |
| UI-EPIC (phát sinh sau PRD, 09/08) | AA-386`†`→391`†`/394`†`/390`†` | ✅ 7/7 phần UI đã Done — **đây là phát hiện độc lập, KHÔNG nằm trong PRD gốc vì phát sinh sau, nhưng lấp đúng nhiều gap PRD tự ghi ("Gate B 0% UI", "N1 4/6 endpoint thiếu UI") mà lần đọc 1 không biết** |

---

## Phần D — Danh sách nghi vấn chồng lấn (cập nhật)

1. **AA-297 (EPIC gốc 13/07) vs AA-386`†` (UI-EPIC mới 09/08)** — AA-297 track kiến trúc/backend, AA-386 track UI; cả hai đều nhắc issue con trùng nhau (AA-309, AA-320, AA-328...). Cần Giai đoạn 3.
2. **AA-334 (Canceled) vs AA-337 (Canceled) vs AA-308`†` (Done) vs AA-392`†` (Done)** — 4 issue cùng chủ đề "writer/model cho N7/N2", nhưng chuỗi nhân-quả giờ rõ hơn: AA-308 chạy trước (bake-off gốc, không đạt ý nghĩa thống kê) → AA-334 định làm bake-off bổ sung nhưng bị Canceled (không rõ lý do — Giai đoạn 3 nên hỏi) → AA-337 (rủi ro throttle) cũng Canceled nhưng dữ kiện của nó vẫn được dùng làm căn cứ cho AA-392. **Việc Canceled 1 issue nhưng vẫn dùng dữ kiện của nó để quyết định là mẫu hình đáng chú ý — không phải lỗi, nhưng dễ gây hiểu lầm "Canceled = bỏ qua" khi đọc Linear đơn thuần.**
3. **AA-315 (Canceled) vs AA-316 (Done)** — không phải trùng lặp thật, PRD tự ghi rõ quan hệ investigate→fix.
4. **AA-325 (Done) vs AA-326 (Backlog) vs AA-349 (Backlog) vs AA-393`†` (Backlog)** — 4 issue cùng xoay quanh grounding/entailment checker, cơ chế khác nhau theo mô tả nhưng đáng xem có consolidate được không.
5. **AA-321 vs AA-322 vs AA-341** — 3 issue "phát hiện phụ trong lúc làm việc khác", phạm vi hẹp, có thể trùng nợ kỹ thuật khác trong AA-CIS.
6. **AA-395`†` vs AA-394`†` vs AA-302 (D5/trip_url)** — 3 tầng hoàn thiện cùng 1 khái niệm, dễ nhầm "đã xong" khi chỉ VIEW/backfill tạm tồn tại.
7. **AA-353 vs AA-346 vs AA-340 vs AA-339** — cụm 4 issue "nén itinerary S1 cũ/S1-from-atom" xoay quanh 1 câu hỏi gốc, có thể trùng phạm vi test.
8. **AA-402`†` (EventBridge orphaned) vs AA-365 (Gate C veto)** — quan hệ nhân-quả gần (phát hiện lúc chuẩn bị Terraform cho AA-365) nhưng là 2 vấn đề độc lập.
9. **`†` MỚI — AA-397`†`/AA-398`†`/AA-399`†` (acc3 satellite) vs AA-296 (acc1 satellite, L5 gốc)** — 2 hệ thống satellite cross-account song song (acc1 fallback, acc3 chính) cho cùng mục đích (Bedrock invoke ngoài acc2). Không phải chồng lấn/lỗi, nhưng là thay đổi kiến trúc L5 đáng kể mà PRD gốc (viết khi chỉ có acc1) không phản ánh — Giai đoạn 3 nên xác nhận L5 có cần viết lại không.
10. **`†` MỚI — AA-385`†` (`admin_acp_proxy.py` data-risk) Canceled** — issue điều tra rủi ro dữ liệu ACPv1 tàn dò sống trong ACPv2 UI, kết luận Canceled (không phải data risk thật theo note cuối AA-386). Ghi nhận để không mất track, không phải cảnh báo mới.

---

## Phần E — Issue nhắc tới `aa-marketing-v2`/`aamc` tường minh (không đổi sau merge)

⚠️ Giới hạn công cụ giữ nguyên: Linear `query` search là fuzzy-match, không dùng được để quét toàn văn 171 description một cách đáng tin cậy ở phạm vi Giai đoạn 1.

**Xác nhận qua title/snippet đã đọc trực tiếp:** AA-317 — mô tả nói rõ "đối chiếu trực tiếp bản nghiên cứu gốc `aa-marketing-v2`". Không có issue nào trong 25 issue mới (nhóm `†`) nhắc `aa-marketing-v2` tường minh — nhóm này toàn bộ là phát hiện/build về sau (N7 wiring, UI, acc3 infra), không quay lại đối chiếu prototype gốc.

PRD bản thân nhắc `aa-marketing-v2` nhiều lần làm khung tham chiếu, rõ nhất ở §10 "Cái gì NÉM, cái gì GIỮ".

---

## Tổng kết số liệu (cập nhật sau merge)

- **171 issue đã xem xét** = 146 (3 project được giao) + 25 (phát hiện thêm, không gán project) — xác nhận không trùng ID.
- PRD: đọc 100% (2510 dòng nguồn, 4 lớp v1.0→v1.2b).
- **3 phát hiện nổi bật nhất sau khi merge 25 issue (so với lần đọc 1):**
  1. **Q8 (writer model) đã đảo ngược THẬT, không chỉ "có dấu hiệu"** — AA-392 (09/08, Done) chuyển N2b từ Palmyra X5 sang Bedrock Sonnet/Haiku, trích dẫn trực tiếp dữ kiện throttle của AA-337 (dù AA-337 bản thân bị Canceled). PRD v1.2b (26/07) không biết việc này vì xảy ra sau.
  2. **Gate B ĐÃ có UI thật (AA-388, Done 09/08)** — lần đọc 1 chỉ biết AA-320 (persist DB), không biết UI đã build xong. Đây là 1 trong 5 điều kiện chặn production (mục D, PRD) — nay đã gỡ được phần UI.
  3. **L5 (Bedrock only) đã mở rộng thành 2 tài khoản satellite song song (acc1 + acc3 mới)** — thay đổi kiến trúc PRD gốc không biết tới, phát sinh hoàn toàn sau khi PRD viết (12/08).
- **Gap còn lại KHÔNG được lấp bởi 25 issue mới** (vẫn cần theo dõi ở Giai đoạn 2/3): D8 facts pack platform riêng, §5.0.1 wire `invoke_claude()` vào S1 CIS cũ (không phải S1-from-atom), §7 4/7 nguồn atom còn thiếu, Marketplace tầng 1-3 (chỉ tầng 4 có issue).

*Dừng lại tại đây theo đúng phạm vi Giai đoạn 1/4 — chờ Nghiep review trước khi sang Giai đoạn 2 (verify trạng thái thật qua code/route/DB).*
