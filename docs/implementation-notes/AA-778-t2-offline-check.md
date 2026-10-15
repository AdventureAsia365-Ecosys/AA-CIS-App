# AA-778 S238 T2 extension: offline check

Input: the two latest T2 versions of the Mongolia 15-day tour, exported by the main session on 15/10/2026
(`.tmp-session/s238/t2_samples.json`, `rewritten_content`):

- Atlas & Hearth v3 (`5b39474a-e06e-445d-a20a-87fbcf55cd9b`, score 7.00, 1,972 words)
- Trail Pulse v2 (`81b5167f-b37d-450b-9cc0-9a160f8f756b`, 1,769 words)

Run: `services.content_generation.t2_dashes.fix_t2_dashes(rc, source)` with the source name
`Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days` (the Master `aa_name`, from the S235 T2 rerun pairs). The
other source fields were not exported, so no quoted span was protected from the source; none of the dashes in
these versions sat inside a quote. No LLM call, no DB.

## Em-dash counts

| Field | Atlas & Hearth before | after | Trail Pulse before | after |
|---|---|---|---|---|
| name | 1 | 0 | 1 | 0 |
| subtitle | 1 | 0 | 1 | 0 |
| summary | 1 | 0 | 3 | 0 |
| highlights | 6 | 0 | 5 | 0 |
| itineraries: `Day N —` marker | 15 | 0 | 15 | 0 |
| itineraries: day bodies | 18 | 0 | 19 | 0 |
| seo_title | 1 | 0 | 1 | 0 |
| seo_meta, trip_type | 0 | 0 | 0 | 0 |
| **Total** | **43** | **0** | **45** | **0** |

Both names are the tenant writer's own ("…Volcanic Steppe & the Gobi — 15 Days", "…to the Gobi Desert — 15 Days"),
not the Master name, so they are tenant copy and were converted. A version whose name is the Master name word
for word keeps it (unit test `test_the_master_name_is_kept_verbatim_everywhere`); none did here, so the protected
count is 0.

## Before / after sample (read for grammar)

1. name. B: `Mongolia: Khövsgöl Lake to the Gobi Desert — 15 Days`
   A: `Mongolia: Khövsgöl Lake to the Gobi Desert, 15 Days`
2. seo_title. B: `Mongolia: Khövsgöl Lake & Gobi Desert — 15 Days`
   A: `Mongolia: Khövsgöl Lake & Gobi Desert, 15 Days`
3. subtitle. B: `North to south through Mongolia — reindeer herders, volcanic craters, ancient monasteries, and the Gobi's singing dunes over 15 active days`
   A: `North to south through Mongolia: reindeer herders, volcanic craters, ancient monasteries, and the Gobi's singing dunes over 15 active days`
4. day title. B: `Day 4 — Northward to Lake Khövsgöl: Taiga Shore and Steppe Boundary`
   A: `Day 4: Northward to Lake Khövsgöl: Taiga Shore and Steppe Boundary`
5. summary. B: `Along the way, it passes through landscapes shaped by eruption, empire, and centuries of nomadic life — dormant volcanic craters, the ruins of once-vast monastic complexes, …`
   A: `Along the way, it passes through landscapes shaped by eruption, empire, and centuries of nomadic life: dormant volcanic craters, the ruins of once-vast monastic complexes, …`
6. summary. B: `This is Mongolia as a physical journey — wide, fast-changing, and worth every kilometer.`
   A: `This is Mongolia as a physical journey: wide, fast-changing, and worth every kilometer.`
7. summary. B: `Start in the north at Lake Khövsgöl — 2,760 square kilometers of freshwater backed by Siberian taiga — where you visit the Tsaatan reindeer herders, …`
   A: `Start in the north at Lake Khövsgöl (2,760 square kilometers of freshwater backed by Siberian taiga) where you visit the Tsaatan reindeer herders, …`
8. highlight. B: `Kharkhorin — the former capital of the Mongol Empire — and Erdene Zuu Monastery, Mongolia's oldest Buddhist monastery, …`
   A: `Kharkhorin (the former capital of the Mongol Empire) and Erdene Zuu Monastery, Mongolia's oldest Buddhist monastery, …`
9. highlight. B: `Bayanzag — the Flaming Cliffs — site of the world's first fossilized dinosaur egg discovery and a landmark of the Roy Chapman Andrews expeditions`
   A: `Bayanzag, the Flaming Cliffs, site of the world's first fossilized dinosaur egg discovery and a landmark of the Roy Chapman Andrews expeditions`
10. highlight. B: `Ride camels to the Khongor Sand Dunes — 120 kilometers long, 6–12 kilometers wide, and famous for the sound they make in the wind`
    A: `Ride camels to the Khongor Sand Dunes: 120 kilometers long, 6–12 kilometers wide, and famous for the sound they make in the wind`
11. day 2. B: `Founded in 1727 and built entirely with traditional woodworking methods — no metal beams, no nails — it stands as one of Mongolia's three major monastic centers.`
    A: `Founded in 1727 and built entirely with traditional woodworking methods (no metal beams, no nails), it stands as one of Mongolia's three major monastic centers.`
12. day 2. B: `Built in 1727 using traditional woodworking methods, the structure contains no metal beams or nails — and unlike most Mongolian monasteries, it survived the political purges of the 1930s largely intact.`
    A: `Built in 1727 using traditional woodworking methods, the structure contains no metal beams or nails, and unlike most Mongolian monasteries, it survived the political purges of the 1930s largely intact.`
13. day 4. B: `It is also the traditional territory of the Tsaatan people — the Dukha — whose lives remain shaped by reindeer herding.`
    A: `It is also the traditional territory of the Tsaatan people, the Dukha, whose lives remain shaped by reindeer herding.`
14. day 4. B: `A big drive today — around 350 kilometers north to Lake Khövsgöl, the tour's headline destination in the north.`
    A: `A big drive today: around 350 kilometers north to Lake Khövsgöl, the tour's headline destination in the north.`
15. day 7. B: `Nearby, the dormant Khorgo volcano last erupted over 8,000 years ago — that eruption directly shaped the surrounding landscape and played a role in the lake's formation.`
    A: `Nearby, the dormant Khorgo volcano last erupted over 8,000 years ago: that eruption directly shaped the surrounding landscape and played a role in the lake's formation.`
16. day 10. B: `After settling into camp, a visit to the local information center introduces the Roy Chapman Andrews expeditions that brought the site to international attention — expeditions widely credited as an inspiration behind the fictional Indiana Jones.`
    A: `…that brought the site to international attention: expeditions widely credited as an inspiration behind the fictional Indiana Jones.`
17. day 12. B: `After settling into camp and lunch, the afternoon moves into Yol Valley — Vulture Valley — where cliff walls rise above 2,000 meters and bearded vultures nest in the high rock faces.`
    A: `After settling into camp and lunch, the afternoon moves into Yol Valley, Vulture Valley, where cliff walls rise above 2,000 meters and bearded vultures nest in the high rock faces.`
18. day 14. B: `…destroyed during the political purges of 1937 — and then walking further into the rock formations themselves, where the terrain shifts in character at every turn.`
    A: `…destroyed during the political purges of 1937, and then walking further into the rock formations themselves, …`

## Grammar read

All 88 converted dashes were read (both versions, every field). No comma splice, no fragment, no fact changed
(numbers and ranges such as `50–60 meters` and `6–12 kilometers` untouched).

Found and fixed in this lane: sentence 11 first came out as `… methods (no metal beams, no nails) it stands …`
(the comma the dash pair stood for was lost). `fix_dashes` now closes a bracketed pair with `), ` when the text
before it has no finite verb and the text after it opens on a subject (`it`, `they`, `we`, `this`, `there`, …).
On the T9 rounds (r1–r6, 99 pieces with dashes) the same change alters exactly one sentence, also for the better:
`When visitors enter the courtyards and the assembly hall (dressed, as required, …), they are moving …`.

Acceptable but not ideal (left as is):

- Sentence 7: the bracket rule fires because `2,760` has a comma; `(2,760 … taiga) where you visit` reads fine
  but a comma before `where` would be smoother. The original had none either.
- Double colon on a day line whose title has its own colon (sentence 4; 17 of the 30 day lines here). The rule 5
  title example (`Mae Taeng Valley Cycling: Waterfalls, Farmland & Temple`) invites a colon in the title. A
  different marker (`Day 4 · Title`, a line break) is a one-line change in `t2_dashes._CANONICAL_MARKER_RE`
  if chị Thư prefers it.
- Colon density: besides the 15 marker colons each, Atlas & Hearth gains 11 colons and Trail Pulse 19. The T2 prompt now forbids
  the em-dash at the source, so the live versions should need far fewer conversions.
