# Calibration — `a3_keyword_belongs` (AA-661, S203, 29/09/2026)

Question (stage `a3_research`, AA-693 A3-3), asked before DataForSEO volumes are bought:
> *Is this search keyword about this particular place, not just the kind of thing the place is?*
> state = `{place, keyword}`

## Sample
- **200 (place, keyword) pairs** from Dev: the 2,222 Segment places × the 559 `search_demand` keywords with volume, where the keyword shares a non-generic word with the place. This is the ambiguous kind that `compute_demand()` claims today (A3-6).
- Stratified with seed 661: 30 `same` (keyword = place words), 80 `longer` (place ⊂ keyword), 90 `overlap` (partial).
- **Shadow pass:** the deployed `decide()` (stage `adhoc_aa661`), 200 verdicts, 0 errors, **$0.003**.
- Raw data: `data/a3_keyword_belongs_2026-09-29.json` (place, keyword, p, label, code).

## Labels — agent-proposed, then reviewed by Nghiệp (69 rows)
Operational meaning: *should this keyword's search volume be bought for / credited to this place?*

| Label | Codes |
|---|---|
| **1** | `same`; `sub` (an attraction inside the place); `logi` (getting to / around it, incl. the trip to it); `var` (spelling variant) |
| **0** | `other` (a different place, incl. a same-type neighbour); `cont` (the keyword is the containing place); `kind` (the place is only a generic kind: resort, monastery, homestay…); `route` (an "X to Y" transfer given an endpoint's demand); `ctry` (a country-level place given one spot inside it) |

Balance after review: 102 × 1, 98 × 0.

## Result
| Zone | Threshold | n | Precision vs labels |
|---|---|---|---|
| reject | p ≤ 0.30 | 55 (55% of the 0s) | **1.000** |
| reject | p ≤ 0.45 | 67 | 0.985 |
| accept | p ≥ 0.95 | 59 | **1.000** |
| accept | p ≥ 0.85 | 92 | 0.957 |

**Proposed floors:** `reject_ceiling = 0.30`, `accept_floor = 0.95` (`threshold_version` 1). The A3-3 gate acts only on reject.

Caveat: 55 rejects with 0 errors is a small set; the true error rate could still be a few percent. Re-check after the first enforced research runs, from `decision_log` on /admin/decisions.

## Where Jev is systematically wrong (all in the grey/accept zones, so the reject floor is unaffected)
- **`route`:** "Khone Phapheng Waterfall to Pakse" ↔ "Khone Phapheng" p=0.94.
- **`kind`:** "monastery" ↔ "tiger s nest monastery" p=0.94.
- **`ctry`:** "Laos" ↔ "Kong Lor Cave Laos" p=0.93.

Proposal (no Jev): skip keyword research and demand claims for places that are a transfer ("X to Y") or only a generic kind (`names_somewhere()` false). A rule is cheaper and exact here.

## Review by Nghiệp (fill the last column: ✓ agree / ✗ + correct label)
Rule from the design doc (§5): review ≥ 20% at random plus every agent-vs-Jev disagreement. If more than 10% of the reviewed labels are overturned → re-pose the question and relabel.

### A. Random 40 (seed 203)
| # | Place | Keyword | p | Label | Code | Review |
|---|---|---|---|---|---|---|
| 4 | Twin Waterfall | twin waterfall | 0.68 | 0 | kind | |
| 5 | Wat Xieng Thong | wat xieng thong | 0.98 | 1 | same | |
| 7 | Khao Sok resort | khao sok resort | 0.73 | 1 | same | |
| 10 | Institute of Traditional Medical Services | institute of traditional medical services | 0.91 | 1 | same | |
| 18 | Taktsang monastery | taktsang monastery | 0.98 | 1 | same | |
| 23 | Mongar Dzong | mongar dzong | 0.98 | 1 | same | |
| 24 | Manas National Park | manas national park | 0.98 | 1 | same | |
| 26 | Khone Phapheng Waterfall | khone phapheng waterfall | 0.98 | 1 | same | |
| 30 | Angkor | angkor wat tour | 0.94 | 1 | sub | |
| 33 | Dochula Pass | dochula pass bhutan | 0.98 | 1 | sub | |
| 45 | Andong Hahoe Village | andong hahoe folk village | 0.95 | 1 | sub | |
| 51 | Kanchanaburi | Kanchanaburi River Kwai | 0.92 | 1 | sub | |
| 54 | Laos | don khone laos | 0.84 | 0 | ctry | |
| 60 | Royal Chitwan National Park | royal chitwan national park safari | 0.97 | 1 | sub | |
| 63 | Ulaanbaatar to Irkutsk | Ulaanbaatar to Irkutsk train | 0.96 | 1 | logi | |
| 66 | Taktsang | taktsang monastery hike | 0.91 | 1 | sub | |
| 76 | Mebar Tsho Lake | mebar tsho burning lake | 0.95 | 1 | sub | |
| 88 | Punakha | punakha river rafting | 0.93 | 1 | sub | |
| 89 | resort | hot springs resort | 0.12 | 0 | kind | |
| 96 | Beijing | Beijing Duck restaurant | 0.17 | 0 | other | |
| 97 | monastery | Tara Monastery | 0.83 | 0 | kind | |
| 113 | Thai-Cambodian border | Mongolia Russia border | 0.02 | 0 | other | |
| 116 | Mardi Himal Base Camp to Low Camp | ger camp Mongolia | 0.04 | 0 | other | |
| 125 | Wat Bo Phuttharam | Wat Mahathat | 0.11 | 0 | other | |
| 126 | Gasa hot springs | gasa bhutan | 0.67 | 0 | cont | |
| 142 | Pinnawala Elephant Orphanage | best elephant sanctuary chiang mai | 0.02 | 0 | other | |
| 146 | Dumtse Lhakhang | kyichu lhakhang | 0.21 | 0 | other | |
| 150 | Wat Phrathat Doi Suthep | wat xieng thong luang prabang | 0.04 | 0 | other | |
| 152 | terraced rice paddies and suspension bridge near Shiobara | longest suspension bridge bhutan | 0.07 | 0 | other | |
| 153 | Daxueshan Visitor Center | Daxueshan | 0.76 | 0 | cont | |
| 156 | Samdrup Jongkhar Border Gate | Mongolia Russia border | 0.02 | 0 | other | |
| 164 | Beijing Chongwenmen Hotel surrounding area | Beijing acrobatic show | 0.12 | 0 | other | |
| 168 | Primate Rescue Centre | wildlife rescue center | 0.07 | 0 | other | |
| 177 | N Seoul Tower | seoul airport luggage delivery | 0.05 | 0 | other | |
| 181 | River Camp | ger camp Mongolia | 0.11 | 0 | other | |
| 182 | Son La to Moc Chau | chele la pass | 0.74 | 0 | other | |
| 185 | Damodar Kunda to Pokhara | pokhara lake | 0.52 | 0 | route | |
| 191 | Longmen Train Station | Longmen Grottoes tour | 0.33 | 0 | other | |
| 192 | high-altitude valleys to forest descent | taichung high speed rail station | 0.26 | 0 | other | |
| 193 | Peradeniya Railway Station | Death Railway Museum | 0.13 | 0 | other | |

### B. Agent vs Jev disagreements (p ≥ 0.5 but label 0, or p < 0.5 but label 1)
| # | Place | Keyword | p | Label | Code | Review |
|---|---|---|---|---|---|---|
| 17 | hotel in Turpan | Turpan hotel | 0.35 | 1 | same | |
| 12 | Memorial Chorten | memorial chorten | 0.46 | 1 | same | |
| 78 | longest suspension bridge | longest suspension bridge bhutan | 0.49 | 1 | sub | |
| 185 | Damodar Kunda to Pokhara | pokhara lake | 0.52 | 0 | route | |
| 197 | Punakha River | punakha to paro | 0.54 | 0 | route | |
| 134 | Jeju to Takayama | Jeju island nightlife | 0.55 | 0 | other | |
| 131 | Yala to Tangalle | Yala National Park | 0.58 | 0 | route | |
| 190 | Chimi Lhakhang | kurjey lhakhang bhutan | 0.60 | 0 | other | |
| 77 | homestay | Paro homestay | 0.61 | 0 | kind | |
| 136 | Yeosu to Jeju Island | Jeju island nightlife | 0.61 | 0 | other | |
| 143 | Lijiang's old streets | Tina's Guesthouse | 0.61 | 0 | other | |
| 169 | Chiang Mai's adventure park | elephant nature park chiang mai | 0.61 | 0 | other | |
| 157 | Orchha to Varanasi | varanasi ghats | 0.65 | 0 | route | |
| 31 | Beijing | Great Wall near Beijing | 0.66 | 0 | other | |
| 114 | 4000 Islands (Si Phan Don) | don daeng | 0.67 | 0 | other | |
| 126 | Gasa hot springs | gasa bhutan | 0.67 | 0 | cont | |
| 4 | Twin Waterfall | twin waterfall | 0.68 | 0 | kind | |
| 189 | Qinghai-Tibet Railway | death railway | 0.68 | 0 | other | |
| 122 | Muang Fuang to Vang Vieng | Vang Vieng tubing | 0.71 | 0 | route | |
| 155 | Sukhothai to Chiang Rai | chiang rai airport | 0.71 | 0 | route | |
| 170 | City Wall | Mutianyu Great Wall | 0.71 | 0 | other | |
| 182 | Son La to Moc Chau | chele la pass | 0.74 | 0 | other | |
| 153 | Daxueshan Visitor Center | Daxueshan | 0.76 | 0 | cont | |
| 194 | Shangri-la | dochu la pass | 0.77 | 0 | other | |
| 99 | monastery | tango monastery bhutan | 0.78 | 0 | kind | |
| 119 | Thai Buddhist vihara, Sarnath | sarnath buddha first sermon | 0.78 | 0 | cont | |
| 84 | resort | Khao Sok resort | 0.81 | 0 | kind | |
| 86 | Laos | Don Daeng island laos | 0.83 | 0 | ctry | |
| 95 | resort | khao sok resort | 0.83 | 0 | kind | |
| 97 | monastery | Tara Monastery | 0.83 | 0 | kind | |
| 54 | Laos | don khone laos | 0.84 | 0 | ctry | |
| 159 | Bangkok to Kanchanaburi | death railway kanchanaburi | 0.84 | 0 | route | |
| 128 | Yala to Tangalle | Yala | 0.89 | 0 | route | |
| 70 | Laos | Kong Lor Cave Laos | 0.93 | 0 | ctry | |
| 50 | monastery | tiger s nest monastery | 0.94 | 0 | kind | |
| 163 | Khone Phapheng Waterfall to Pakse | Khone Phapheng | 0.94 | 0 | route | |

## Review result (Nghiệp, 29/09/2026)
- Reviewed **69 rows** (A: 40 random + B: 36 disagreements, 7 in both). File: `AA-Ecosys/docs/calib/jev_a3_keyword_belongs_review.xlsx`.
- **67 agree, 2 overturned (2.9%)**, below the 10% re-pose limit:
  - #53 Beijing ↔ "Great Wall near Beijing": 0 → **1** ("the Great Wall is an attraction belonging to Beijing");
  - #54 4000 Islands (Si Phan Don) ↔ "don daeng": 0 → **1** ("Don Daeng is an island of the 4000 Islands").
- Both have p = 0.66 / 0.67 (grey), so the floors are unchanged after the correction: reject ≤ 0.30 → 55/55 correct, accept ≥ 0.95 → 59/59 correct.
- Borderline notes kept with their labels: #1 "twin waterfall", #20 "Beijing Duck restaurant", #43 "longest suspension bridge bhutan". The ctry rule (#13, #63, #67) was confirmed.
- Labels in `data/…json` now carry `review` and the corrected label (`code` suffixed `->nghiep`).

## Compared with Ms. Thư's run of the same question (`aa-soscial-media`, ticket 03, 21/09/2026)
- She judged 1,070 bought keywords **unlabelled** and put the floor at **0.15**. False rejects appeared at 0.42–0.50 (`peace memorial museum` 0.45, `kumano kodo` 0.48). Generic keywords spread 0.13–0.46, and `hot springs` (0.17) escaped her floor.
- Our **labelled** sample has no false reject up to 0.30, which is consistent with her false rejects sitting above 0.40. Her 0.15 was chosen without labels, as a margin against the unknown.
- Both runs agree on the asymmetry: a false reject is invisible later (a keyword never bought), while a false accept costs about a cent. So the floor stays **below every observed false reject**, and every probability is kept so the floor can move.

## Decision
- [x] Nghiệp reviewed A + B; overturned **2 / 69** (< 10%).
- [x] Enforce `a3_keyword_belongs` with **reject ceiling 0.30**, accept floor 0.95, `calibration_ref docs/calibration/a3_keyword_belongs.md`, threshold v1.
- [ ] After the first enforced research run: read the rejected keywords on /admin/decisions (Verdicts → question, zone Reject) and confirm there is no false reject. If there is one, lower the ceiling toward Ms. Thư's 0.15.
