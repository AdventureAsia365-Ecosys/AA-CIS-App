// app/admin/ai-gateway/llmShared.vi.ts — AA-994: the Vietnamese twin of the stage labels and stage group labels in
// llmShared.ts (STAGE_LABELS, STAGE_GROUPS[].label), picked with useLocalized() through useStageText().
// Same keys as the English side: a stage key, a group key. A missing key falls back to English.

export const STAGE_GROUP_LABELS_VI: Record<string, string> = {
  s1: "Viết lại S1 — người viết, giám khảo và sửa của A1 (quản trị)",
  t2: "Viết lại T2 cho đại lý — người viết, giám khảo và sửa",
  t7: "T7 — Giám khảo độ hợp thương hiệu của Slate Debate",
  a3: "A3 — Atomize",
  a3_place: "A3 — Thẻ địa điểm",
  t8: "T8 — Tạo góc tiếp cận",
  t9: "T9 — Viết nội dung + T10 giám khảo chất lượng",
  a0: "A0 — Tự nhận diện cột Excel",
  embed: "Embedding (khớp câu hỏi/atom)",
  tp: "TripPlanner",
};

export const STAGE_LABELS_VI: Record<string, string> = {
  s1_generate: "Tạo nội dung",
  s1_judge: "Giám khảo độ hợp thương hiệu",
  s1_brand_audit: "Kiểm tra thương hiệu",
  s1_flag_fix: "Sửa theo cờ lỗi",
  s1_itinerary_nudge: "Chỉnh ngày trong lịch trình",
  s1_atom_writer: "Người viết dựa trên atom",
  t2_generate: "Tạo nội dung cho đại lý",
  t2_judge: "Giám khảo độ hợp thương hiệu của đại lý",
  t2_brand_audit: "Kiểm tra thương hiệu của đại lý",
  t2_flag_fix: "Sửa theo cờ lỗi cho đại lý",
  t2_itinerary_nudge: "Chỉnh ngày trong lịch trình cho đại lý",
  t7_debate_judge: "Giám khảo tranh luận độ hợp thương hiệu",
  a3_atomize: "A3 atomize",
  a3_place_card: "Bản nháp thẻ địa điểm",
  a3_place_card_check: "Kiểm chứng dữ kiện thẻ địa điểm",
  t5_atomize: "A3 atomize",
  t8_angle_gen: "Tạo góc tiếp cận",
  t9_write: "Viết nội dung",
  t9_repair: "Sửa câu / phù hợp SEO",
  t10_judge: "Giám khảo chất lượng (F8+F9)",
  t10_claims: "Trích xuất khẳng định (F9 v2)",
  a0_column_map: "Ghép cột",
  f10_embed: "Embedding nội dung",
  tp_compose: "Soạn kế hoạch chuyến đi",
  tp_search_embed: "Embedding câu tìm kiếm",
  tp_extract: "Trích xuất thành phần (ngoại tuyến)",
  tp_component_embed: "Embedding thành phần (ngoại tuyến)",
};
