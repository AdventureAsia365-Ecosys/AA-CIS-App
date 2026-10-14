"""AA-808 — the place-card drafter must not append traveller clauses to background facts.

Fixtures are REAL facts from the S235 card-kinds audit (AA-Ecosys docs/audits/2026-10-14-S235-
ecosystem-team-pilot/card-kinds-audit, copied in — never read at test time):
- LEAKS: 31 writer-kind facts both reviewers moved to visiting/practical (group agreed_leak),
  as (current kind, proposed kind, card, text);
- UNLEAKS: 22 visiting/practical facts both reviewers judged pure background (agreed_unleak),
  as (proposed writer kind, card, text);
- BACKGROUND: every writer-kind fact of the 110 audited cards that is not in the apply plan
  (437; untouched background), as (kind, card, text).
"""
import pytest

from services.acp_shared import atom_seed
from services.acp_shared import place_cards as pc

LEAKS = [
    ('setting', 'visiting', 'Dochula Pass', "On clear days the pass offers panoramic views of the eastern Himalayan peaks, including Masagang, Tsendagang, and Gangkar Puensum — Bhutan's highest unclimbed mountain."),  # noqa: E501
    ('setting', 'visiting', 'Jampa Lhakhang', 'The temple is located close to Kurje Lhakhang, another major sacred site in Bumthang, and both are often visited together.'),  # noqa: E501
    ('setting', 'visiting', 'Kuensel Phodrang Nature Park', 'The park sits on the Kuensel Phodrang hill and offers panoramic views over the Thimphu valley.'),  # noqa: E501
    ('setting', 'visiting', 'Kuensel Phodrang Nature Park', 'The surrounding nature park contains walking trails through pine and mixed forest, and is used by local residents for recreation.'),  # noqa: E501
    ('setting', 'practical', 'Kyichu Lhakhang', "The temple is located a short distance from Paro town, making it a standard inclusion on visits to the Paro valley alongside Rinpung Dzong and the Tiger's Nest monastery."),  # noqa: E501
    ('setting', 'practical', 'Mongar', 'The Ura-Mongar highway passes through dramatic gorges and over high passes, making the drive itself a notable part of the eastern Bhutan experience.'),  # noqa: E501
    ('setting', 'visiting', 'Mongar Dzong', 'It is situated on a ridge above Mongar town, offering views over the surrounding forested valleys of eastern Bhutan.'),  # noqa: E501
    ('setting', 'practical', 'Mongar Dzong', 'Mongar town itself is a staging point for travel into the remote eastern districts of Bhutan, including Lhuentse and Trashigang.'),  # noqa: E501
    ('setting', 'practical', 'Mongar Dzong', 'The road to Mongar crosses high mountain passes, and the town sits at an elevation that gives the area a cooler climate than lower eastern valleys.'),  # noqa: E501
    ('setting', 'practical', 'National Handloom Development Corporation, Khaling', 'Khaling sits at a mid-altitude in the hills of eastern Bhutan, and the drive to reach it typically passes through forested mountain terrain along the eastern highway.'),  # noqa: E501
    ('setting', 'practical', 'National Institute for Zorig Chusum', "The school is located in Thimphu, Bhutan's capital city, making it easily combined with visits to other cultural sites such as the National Library and Tashichho Dzong."),  # noqa: E501
    ('setting', 'practical', "Paro Taktsang (Tiger's Nest Monastery)", 'The surrounding forest is composed largely of blue pine and rhododendron, and prayer flags line much of the approach trail.'),  # noqa: E501
    ('what_it_is', 'practical', 'Paro Town', "The town is home to Bhutan's only international airport, making it the country's main entry and exit point for international travellers."),  # noqa: E501
    ('what_it_is', 'practical', 'Samdrup Jongkhar Border Crossing', 'It is one of only a small number of official land entry and exit points into Bhutan open to tourists, making it a significant gateway for overland travel.'),  # noqa: E501
    ('setting', 'practical', 'Samdrup Jongkhar Border Crossing', 'The nearest major Indian city on the Assam side is Guwahati, roughly 170 km from the border, making it a common arrival or departure hub for travelers using this crossing.'),  # noqa: E501
    ('setting', 'practical', 'Samdrup Jongkhar Border Crossing', "The road from Samdrup Jongkhar westward into Bhutan's interior traverses mountainous terrain and is the starting point for overland journeys heading toward Thimphu and Paro."),  # noqa: E501
    ('setting', 'practical', 'Taktsang Monastery', 'The round-trip trek from the base car park passes through blue-pine and rhododendron forest; a midpoint viewpoint café offers a classic face-on view of the monastery across a ravine.'),  # noqa: E501
    ('setting', 'visiting', 'Gyatso La', 'The surrounding landscape is a treeless, high-altitude Himalayan plateau with broad mountain and snow-peak views.'),  # noqa: E501
    ('setting', 'visiting', 'Old Tingri', 'On clear days, the surrounding area can provide views toward major Himalayan peaks including Everest, Cho Oyu, Makalu and Lhotse.'),  # noqa: E501
    ('setting', 'visiting', 'Peiku Tso', 'Snow-covered Himalayan peaks, including Shishapangma, are visible from the lake in clear conditions.'),  # noqa: E501
    ('what_it_is', 'practical', 'Lokpriya Gopinath Bordoloi International Airport', 'It functions as the primary air gateway for travellers entering Northeast India and onward to Bhutan, with road connections to the Bhutanese border at Phuentsholing.'),  # noqa: E501
    ('setting', 'visiting', 'Luang Prabang', 'Mount Phousi, a hill rising roughly 100 metres above the town centre, offers panoramic views of the Mekong, the Nam Khan, and the surrounding mountains and is a popular sunset viewpoint.'),  # noqa: E501
    ('what_it_is', 'practical', 'Muang Khua', 'It serves as a key transit hub for travellers moving along the Nam Ou River between Nong Khiaw to the south and Phongsali town to the north.'),  # noqa: E501
    ('history', 'visiting', 'Nong Khiaw', 'During the Second Indochina War, the region was heavily bombed and locals sheltered in caves in the limestone cliffs; one such cave, Pha Tok Cave, is accessible to visitors.'),  # noqa: E501
    ('what_it_is', 'practical', 'Nong Khouay', 'Like many villages in this part of Phongsali, Nong Khouay has historically been without connection to the national electricity grid, relying instead on small-scale or no power generation.'),  # noqa: E501
    ('setting', 'practical', 'Mandalgovi', 'It sits on the northern edge of the Gobi Desert, roughly midway between Ulaanbaatar and the deeper southern Gobi, making it a natural stopping point on the main north–south road.'),  # noqa: E501
    ('setting', 'practical', 'Mandalgovi', 'The town lies approximately 260 km south of Ulaanbaatar along the paved highway that continues toward Dalanzadgad.'),  # noqa: E501
    ('what_it_is', 'practical', 'Mörön', 'It serves as the main hub and gateway town for travellers heading to Khövsgöl Lake, located roughly 100 km to the north.'),  # noqa: E501
    ('setting', 'visiting', 'Sod Camp', 'It faces the Khoridol Saridag mountain range, which forms a dramatic backdrop on the opposite shore and offers trekking routes.'),  # noqa: E501
    ('setting', 'practical', 'Tsaatan Reindeer Herder Settlements', 'Their territory lies deep in the taiga, typically reached from the town of Tsagaannuur, and access requires a multi-hour horse or reindeer ride, often crossing rivers and boggy forest terrain.'),  # noqa: E501
    ('setting', 'practical', 'Nậm Cắn / Nam Khan Border Crossing', 'On the Lao side the border post sits at around 1,300 metres above sea level in mountainous terrain, making the approach a significant climb on bicycle.'),  # noqa: E501
]

UNLEAKS = [
    ('what_it_is', 'Jampa Lhakhang', 'The complex is an active place of worship and contains religious statues, butter lamps, and murals that have been renovated over the centuries.'),  # noqa: E501
    ('what_it_is', 'Kuensel Phodrang Nature Park', 'The statue is hollow and houses over one hundred thousand smaller Buddha statues made of bronze and gilded in gold, installed inside the throne structure.'),  # noqa: E501
    ('history', 'Kurje Lhakhang', "A large juniper tree within the complex is traditionally believed to have grown from Guru Rinpoche's walking staff, and is considered sacred."),  # noqa: E501
    ('what_it_is', 'Kyichu Lhakhang', 'The original lhakhang enshrines a revered statue of Jowo Shakyamuni, and the newer temple houses a statue of Guru Rinpoche.'),  # noqa: E501
    ('what_it_is', 'Mongar Dzong', 'The dzong features traditional Bhutanese whitewashed stone and rammed-earth construction, with characteristic tapering walls, a central tower (utse), and decorated woodwork.'),  # noqa: E501
    ('what_it_is', 'National Handloom Development Corporation, Khaling', 'The centre produces a range of traditional Bhutanese fabrics, including kishuthara (raw silk patterned cloth), which is characteristic of eastern Bhutan.'),  # noqa: E501
    ('what_it_is', 'National Institute for Zorig Chusum', 'Students typically enroll for a multi-year programme, spending years mastering a single craft discipline under experienced master craftspeople.'),  # noqa: E501
    ('what_it_is', 'Paro', 'The National Museum of Bhutan is housed in a former watchtower called Ta Dzong, overlooking Rinpung Dzong, and holds collections of thangkas, textiles, weaponry, and natural history.'),  # noqa: E501
    ('setting', 'Paro Town', 'The main street is lined with traditional Bhutanese wooden-fronted shop-houses painted with characteristic decorative motifs, giving the town a well-preserved vernacular architectural character.'),  # noqa: E501
    ('what_it_is', 'Paro Town', 'The National Museum of Bhutan, housed in a former watchtower (Ta Dzong) above Rinpung Dzong, holds collections of thangkas, armour, textiles, and natural history specimens.'),  # noqa: E501
    ('what_it_is', 'Punakha Dzong', 'The interior courtyards contain a utse (central tower) and several temples adorned with elaborate woodcarving, paintings, and gilded roofs typical of Bhutanese architecture.'),  # noqa: E501
    ('what_it_is', 'Trashigang', 'Eastern Bhutan, and Trashigang in particular, is known for distinctive hand-woven textiles, including the raw-silk Kishuthara fabric produced by communities in the region.'),  # noqa: E501
    ('setting', 'Gyirong', 'The town is near Gyirong Port on the China–Nepal border; the Nepal-side crossing is associated with Rasuwagadhi.'),  # noqa: E501
    ('what_it_is', 'Mount Kailash', 'Mount Kailash is regarded as a sacred summit and is not climbed as a conventional mountaineering objective.'),  # noqa: E501
    ('setting', 'Paryang', 'Paryang is a road settlement on the China National Highway 219 route through western Tibet.'),  # noqa: E501
    ('what_it_is', 'Potala Palace', 'The complex includes the White Palace, the Red Palace, chapels, assembly halls, murals, statues, and tomb stupas of several Dalai Lamas.'),  # noqa: E501
    ('what_it_is', 'Luang Prabang', 'The city contains more than 30 Buddhist temples (wats), the most prominent of which is Wat Xieng Thong, dating from the 16th century.'),  # noqa: E501
    ('what_it_is', 'Baga Gazriin Chuluu', "The site is protected as a nature reserve within Mongolia's network of specially protected areas."),  # noqa: E501
    ('what_it_is', 'Erdene Zuu Monastery', 'The three surviving temples—dedicated to the three ages of the Buddha (childhood, adulthood, and old age)—now function as museums displaying thangkas, Buddhist sculptures, and ceremonial objects.'),  # noqa: E501
    ('what_it_is', 'Kharkhorin', "The monastery's enclosing wall, approximately 400 metres on each side, is punctuated by 108 white stupas, a number of great significance in Tibetan Buddhism."),  # noqa: E501
    ('what_it_is', 'Kharkhorin', 'A large stone tortoise sculpture, one of several originally marking the boundaries of Karakorum, still stands near Erdene Zuu and is a well-known landmark of the site.'),  # noqa: E501
    ('setting', 'Ulaanbaatar', 'Ulaanbaatar is the coldest national capital in the world by average annual temperature, with winters regularly dropping below −20 °C.'),  # noqa: E501
]

BACKGROUND = [
    ('what_it_is', 'Babesa Village', 'Babesa is a traditional village located on the southern edge of Thimphu valley, along the highway leading into the capital.'),  # noqa: E501
    ('setting', 'Babesa Village', 'Babesa sits at roughly 2,300 metres above sea level, consistent with the broader Thimphu valley elevation.'),  # noqa: E501
    ('what_it_is', 'Dewathang', 'Dewathang is a small town in Samdrup Jongkhar District in southeastern Bhutan, close to the border with the Indian state of Assam.'),  # noqa: E501
    ('history', 'Dewathang', 'The area is historically significant as the site of a major 19th-century battle between Bhutanese forces and British India; the Battle of Dewangiri (1865) took place in this vicinity during the Duar War.'),  # noqa: E501
    ('history', 'Dewathang', "The British briefly occupied a fort at Dewangiri after the Duar War of 1864–65, and the site of that fortification is associated with the 'historical battlefield' referenced in itineraries."),  # noqa: E501
    ('setting', 'Dewathang', 'The town sits at a relatively low elevation compared with much of Bhutan, in a subtropical valley near the Assam foothills, giving it a warm and humid climate.'),  # noqa: E501
    ('what_it_is', 'Dewathang', "Dewathang is home to one of Bhutan's Royal Bhutan Army camps, reflecting its continued strategic importance near the southern border."),  # noqa: E501
    ('setting', 'Dewathang', "The surrounding landscape is characterised by dense subtropical forest and river valleys typical of Bhutan's southern lowland belt."),  # noqa: E501
    ('what_it_is', 'Dochula Pass', 'Dochula Pass is a high-altitude mountain pass on the road between Thimphu and Punakha, sitting at approximately 3,100 metres (10,170 feet) above sea level.'),  # noqa: E501
    ('history', 'Dochula Pass', 'The pass is crowned by 108 chortens (memorial stupas) called the Druk Wangyal Chortens, built by Queen Mother Ashi Dorji Wangmo Wangchuck in honour of Bhutanese soldiers killed in a 2003 military operation against Indian separatist militants sheltering in southern Bhutan.'),  # noqa: E501
    ('what_it_is', 'Dochula Pass', 'The Druk Wangyal Lhakhang temple, dedicated to the fourth king of Bhutan, stands at the pass and features murals and sculptures depicting Bhutanese royal and military history.'),  # noqa: E501
    ('setting', 'Dochula Pass', 'The pass lies within a temperate conifer and rhododendron forest; rhododendrons bloom in vivid colours typically in spring, making the area particularly scenic between March and May.'),  # noqa: E501
    ('what_it_is', 'Jampa Lhakhang', 'Jampa Lhakhang is one of the oldest temples in Bhutan, traditionally dated to the 7th century CE.'),  # noqa: E501
    ('history', 'Jampa Lhakhang', 'It is attributed to the Tibetan king Songtsen Gampo, who is said to have built 108 temples across the Himalayas in a single day to pin down a demoness; Jampa Lhakhang was one of them.'),  # noqa: E501
    ('what_it_is', 'Jampa Lhakhang', "The temple is dedicated to Maitreya, the future Buddha, which is reflected in its name — 'Jampa' (or 'Jambay') is the Dzongkha/Tibetan name for Maitreya."),  # noqa: E501
    ('setting', 'Jampa Lhakhang', "The temple sits in the Chokhor Valley of Bumthang, a fertile highland valley considered one of Bhutan's most sacred landscapes."),  # noqa: E501
    ('history', 'Jampa Lhakhang', "Jampa Lhakhang is the site of an annual tsechu (religious festival) known for its 'naked dance' (Jampa Lhakhang Drup), performed at night as part of fertility rites."),  # noqa: E501
    ('what_it_is', 'Kuensel Phodrang Nature Park', 'Kuensel Phodrang Nature Park is a forested hillside park situated above Thimphu, the capital of Bhutan.'),  # noqa: E501
    ('what_it_is', 'Kuensel Phodrang Nature Park', 'The park is dominated by the Buddha Dordenma, a monumental gilded bronze statue of Shakyamuni Buddha seated on a throne that also functions as a meditation hall.'),  # noqa: E501
    ('what_it_is', 'Kuensel Phodrang Nature Park', 'The Buddha Dordenma statue stands approximately 51.5 metres (169 feet) tall, making it one of the largest Buddha statues in the world.'),  # noqa: E501
    ('history', 'Kuensel Phodrang Nature Park', 'The statue was built to commemorate the centenary of the Wangchuck dynasty and the 60th birthday of the Fourth Druk Gyalpo, Jigme Singye Wangchuck.'),  # noqa: E501
    ('history', 'Kuensel Phodrang Nature Park', 'Construction of the Buddha Dordenma was completed in 2015.'),
    ('what_it_is', 'Kurje Lhakhang', "Kurje Lhakhang is a large temple complex located in the Chokhor Valley of Bumthang, one of Bhutan's most sacred religious sites."),  # noqa: E501
    ('naming', 'Kurje Lhakhang', "The name 'Kurje' means 'body print' in Dzongkha, referring to the impression of Guru Rinpoche's (Padmasambhava's) body left in a rock inside the oldest of the three temples."),  # noqa: E501
    ('history', 'Kurje Lhakhang', 'Guru Rinpoche is said to have meditated in a cave at this site in the 8th century CE, making it one of the holiest places associated with the introduction of Buddhism to Bhutan.'),  # noqa: E501
    ('what_it_is', 'Kyichu Lhakhang', 'Kyichu Lhakhang is one of the oldest and most sacred Buddhist temples in Bhutan.'),  # noqa: E501
    ('history', 'Kyichu Lhakhang', 'It is traditionally believed to have been built in the 7th century CE by Tibetan Emperor Songtsen Gampo, who is said to have constructed 108 temples across the Himalayan region in a single day to pin down a demoness.'),  # noqa: E501
    ('what_it_is', 'Kyichu Lhakhang', 'The temple complex consists of two main shrines: the original 7th-century structure and a second temple built in 1968 by the Royal Grandmother Ashi Kesang.'),  # noqa: E501
    ('setting', 'Kyichu Lhakhang', 'The complex sits in the Paro valley surrounded by ancient trees, including an orange tree that is said to bear fruit year-round.'),  # noqa: E501
    ('what_it_is', 'Mebar Tsho (Burning Lake)', 'Mebar Tsho is a sacred lake set within a gorge of the Tang River in the Tang Valley of Bumthang district.'),  # noqa: E501
    ('history', 'Mebar Tsho (Burning Lake)', "The name 'Burning Lake' derives from a legend in which Terton Pema Lingpa, the 15th-century treasure-revealer, dove into the lake holding a burning butter lamp and emerged with the lamp still lit and religious treasures in his hands."),  # noqa: E501
    ('history', 'Mebar Tsho (Burning Lake)', "Pema Lingpa (1450–1521) is one of Bhutan's most revered Buddhist saints, and Mebar Tsho is considered one of the most important pilgrimage sites associated with him."),  # noqa: E501
    ('setting', 'Mebar Tsho (Burning Lake)', 'The lake occupies a narrow, rocky gorge where the Tang River pools into a dark, deep basin, giving it a dramatic and atmospheric setting.'),  # noqa: E501
    ('what_it_is', 'Mongar', 'Mongar is a small hill town and the administrative headquarters of Mongar District in eastern Bhutan.'),  # noqa: E501
    ('setting', 'Mongar', 'It sits at an elevation of roughly 1,800 metres (about 5,900 feet) above sea level, giving it a relatively mild climate compared with lower eastern valleys.'),  # noqa: E501
    ('history', 'Mongar', 'The town developed as a regional centre after the construction of roads connecting eastern Bhutan to the rest of the country in the latter half of the 20th century.'),  # noqa: E501
    ('what_it_is', 'Mongar', "Mongar Dzong, a whitewashed fortress-monastery built in the 1930s, dominates the town and serves as the seat of the district's civil and religious administration."),  # noqa: E501
    ('setting', 'Mongar', 'The surrounding landscape is characterised by terraced rice and maize fields, subtropical forest at lower elevations, and conifer forest higher up.'),  # noqa: E501
    ('what_it_is', 'Mongar Dzong', 'Mongar Dzong is a fortress-monastery (dzong) that serves as the administrative and religious headquarters of Mongar District in eastern Bhutan.'),  # noqa: E501
    ('history', 'Mongar Dzong', 'The current structure was built in the 20th century, making it one of the more recently constructed dzongs in Bhutan compared to the medieval fortresses found in the west.'),  # noqa: E501
    ('what_it_is', 'Mongar Dzong', 'Like most dzongs in Bhutan, Mongar Dzong houses both monk bodies and government offices for the district administration.'),  # noqa: E501
    ('what_it_is', 'National Handloom Development Corporation, Khaling', 'The National Handloom Development Corporation (NHDC) centre in Khaling is a government-run weaving facility established to preserve and promote traditional Bhutanese textile production.'),  # noqa: E501
    ('what_it_is', 'National Handloom Development Corporation, Khaling', 'The centre employs local weavers, predominantly women, who produce textiles using both traditional backstop looms and frame looms.'),  # noqa: E501
    ('history', 'National Handloom Development Corporation, Khaling', 'Khaling is known as one of the important weaving towns of eastern Bhutan, a region historically associated with distinctive textile traditions including raw silk and cotton weaving.'),  # noqa: E501
    ('history', 'National Handloom Development Corporation, Khaling', 'The NHDC was created as part of a national effort to sustain cottage industries and provide rural livelihoods through the commercial production of traditional Bhutanese fabrics.'),  # noqa: E501
    ('what_it_is', 'National Institute for Zorig Chusum', "The National Institute for Zorig Chusum is a government-run school in Thimphu dedicated to preserving and teaching Bhutan's thirteen traditional arts and crafts, collectively known as Zorig Chusum."),  # noqa: E501
    ('what_it_is', 'National Institute for Zorig Chusum', 'The thirteen arts taught include thangka painting, wood carving, sculpture, weaving, embroidery, lacquerwork, and other crafts that are central to Bhutanese cultural and religious life.'),  # noqa: E501
    ('history', 'National Institute for Zorig Chusum', 'The institute was established to ensure that traditional Bhutanese craft skills are formally transmitted to new generations and not lost to modernisation.'),  # noqa: E501
    ('history', 'National Institute for Zorig Chusum', "The institute plays a direct role in supplying skilled artisans for the restoration and decoration of Bhutan's dzongs, monasteries, and other religious structures."),  # noqa: E501
    ('what_it_is', 'National Library of Bhutan', "The National Library of Bhutan is the country's principal repository for Bhutanese written heritage, holding thousands of Buddhist manuscripts, xylographs (woodblock prints), and modern publications."),  # noqa: E501
    ('history', 'National Library of Bhutan', 'It was established in 1967 under the patronage of the Royal Government of Bhutan to preserve classical Dzongkha and Tibetan-script religious and literary texts.'),  # noqa: E501
    ('setting', 'National Library of Bhutan', 'The library building is designed in traditional Bhutanese architectural style, featuring whitewashed walls, red window bands, and a sloping timber roof typical of Bhutanese public buildings.'),  # noqa: E501
    ('what_it_is', 'National Library of Bhutan', 'The collection includes rare hand-written pecha (loose-leaf manuscripts) on Buddhist philosophy, medicine, astrology, and history, some of which are centuries old.'),  # noqa: E501
    ('history', 'National Library of Bhutan', "The institution also undertakes digitisation and conservation work to safeguard Bhutan's manuscript heritage for future generations."),  # noqa: E501
    ('what_it_is', 'National Textile Museum of Bhutan', "The National Textile Museum of Bhutan is dedicated to preserving and showcasing Bhutan's rich tradition of weaving and textile arts, which hold deep cultural and spiritual significance in Bhutanese society."),  # noqa: E501
    ('setting', 'National Textile Museum of Bhutan', 'The museum is housed in a traditional Bhutanese building in central Thimphu, near the clock tower area.'),  # noqa: E501
    ('history', 'National Textile Museum of Bhutan', 'Bhutanese textiles are traditionally woven by women, and weaving proficiency has historically been considered a mark of status and skill for Bhutanese women.'),  # noqa: E501
    ('what_it_is', 'Paro', 'Paro is a town and river valley in western Bhutan, situated along the Paro Chhu (river) at an elevation of roughly 2,200 metres above sea level.'),  # noqa: E501
    ('what_it_is', 'Paro', "Paro International Airport, the country's only international airport, is located in the valley; its short runway and surrounding mountains make it one of the most technically demanding commercial approaches in the world."),  # noqa: E501
    ('setting', 'Paro', 'The valley contains one of the highest concentrations of Buddhist monasteries, temples, and dzongs (fortress-monasteries) in Bhutan.'),  # noqa: E501
    ('history', 'Paro', "Rinpung Dzong, a large 17th-century fortress-monastery beside the Paro Chhu, is the district's administrative and religious centre and is a dominant landmark of the town."),  # noqa: E501
    ('setting', 'Paro', "The valley's relatively flat, fertile floor supports rice and buckwheat farming, giving it a distinctly agricultural character compared to many other Bhutanese valleys."),  # noqa: E501
    ('what_it_is', "Paro Taktsang (Tiger's Nest Monastery)", 'Paro Taktsang is a sacred Buddhist monastery complex built into a sheer granite cliff face roughly 900 metres above the floor of the Paro Valley.'),  # noqa: E501
    ('history', "Paro Taktsang (Tiger's Nest Monastery)", 'According to Bhutanese tradition, Guru Rinpoche (Padmasambhava) flew to this site on the back of a tigress in the 8th century CE and meditated in a cave there, giving the site its name.'),  # noqa: E501
    ('history', "Paro Taktsang (Tiger's Nest Monastery)", 'The main temple structures were built around the sacred cave in 1692 under the direction of Gyalse Tenzin Rabgye, the fourth temporal ruler of Bhutan.'),  # noqa: E501
    ('history', "Paro Taktsang (Tiger's Nest Monastery)", 'A fire in 1998 severely damaged much of the complex; it was subsequently restored and reopened in 2004.'),  # noqa: E501
    ('what_it_is', "Paro Taktsang (Tiger's Nest Monastery)", 'The complex comprises four main temples, several sacred caves, and a series of staircases and bridges linking the structures along the cliff.'),  # noqa: E501
    ('history', "Paro Taktsang (Tiger's Nest Monastery)", "Paro Taktsang is one of Bhutan's most revered pilgrimage sites; Bhutanese Buddhists traditionally aim to visit it at least once in their lifetime."),  # noqa: E501
    ('setting', 'Paro Town', "Paro is one of Bhutan's most historically significant towns, situated in the broad, fertile Paro Valley at roughly 2,200 metres above sea level."),  # noqa: E501
    ('history', 'Paro Town', "Paro Dzong (Rinpung Dzong), a large fortress-monastery dating to the 17th century, dominates the valley above the town and is one of Bhutan's most iconic structures."),  # noqa: E501
    ('setting', 'Paro Town', 'The Paro River (Paro Chhu) flows through the valley alongside the town and is crossed by traditional covered cantilever bridges.'),  # noqa: E501
    ('setting', 'Paro Town', "The valley's relatively flat terrain and fertile land support rice cultivation, and rice paddies are a visible feature of the landscape around the town."),  # noqa: E501
    ('history', 'Punakha', 'Punakha served as the capital of Bhutan from the 17th century until 1955, when the capital was moved to Thimphu.'),  # noqa: E501
    ('setting', 'Punakha', 'The town sits at the confluence of the Pho Chhu (Father River) and Mo Chhu (Mother River), giving it a fertile, relatively warm climate compared with most of Bhutan.'),  # noqa: E501
    ('what_it_is', 'Punakha', 'Punakha Dzong, officially known as Pungtang Dechen Photrang Dzong, is the dominant landmark — a massive fortress-monastery built in 1637–1638 by Zhabdrung Ngawang Namgyal.'),  # noqa: E501
    ('setting', 'Punakha', 'The dzong sits on a narrow tongue of land at the river confluence and is connected to the riverbank by a traditional cantilever bridge.'),  # noqa: E501
    ('history', 'Punakha', 'Punakha Dzong houses the embalmed body of Zhabdrung Ngawang Namgyal and remains an active monastic and administrative center.'),  # noqa: E501
    ('history', 'Punakha', 'The dzong suffered several major floods and fires over the centuries and has been substantially restored; restoration work is ongoing in parts of the complex.'),  # noqa: E501
    ('setting', 'Punakha', 'The valley is known for rice cultivation and jacaranda trees that bloom along the river in spring, making it a visually distinctive destination within Bhutan.'),  # noqa: E501
    ('what_it_is', 'Punakha Dzong', 'Punakha Dzong is a large Buddhist monastery-fortress (dzong) located at the confluence of the Pho Chhu (Male River) and Mo Chhu (Female River) in Punakha.'),  # noqa: E501
    ('history', 'Punakha Dzong', 'It was built in 1637–1638 by Zhabdrung Ngawang Namgyal, the unifier of Bhutan, who founded it to serve as the administrative and religious centre of the country.'),  # noqa: E501
    ('history', 'Punakha Dzong', 'Punakha Dzong served as the capital and seat of government of Bhutan until 1955, when the capital was moved to Thimphu.'),  # noqa: E501
    ('what_it_is', 'Punakha Dzong', 'It remains the winter residence of the Je Khenpo (Chief Abbot of Bhutan) and the Central Monk Body, who relocate here from Thimphu each year.'),  # noqa: E501
    ('history', 'Punakha Dzong', 'The dzong houses the sacred relic of Zhabdrung Ngawang Namgyal and important religious artefacts, including the Rangjung Kharsapani, a naturally formed image.'),  # noqa: E501
    ('setting', 'Punakha Dzong', 'The building spans a long peninsula between the two rivers and is connected to the land by a wooden cantilever bridge, giving it a striking riverside setting.'),  # noqa: E501
    ('history', 'Punakha Dzong', 'The dzong has been damaged and rebuilt multiple times due to fire and floods, most recently undergoing significant restoration work in the late 20th and early 21st centuries.'),  # noqa: E501
    ('what_it_is', 'Samdrup Jongkhar Border Crossing', "Samdrup Jongkhar is Bhutan's main southeastern land border crossing, linking the country to the Indian state of Assam."),  # noqa: E501
    ('what_it_is', 'Samdrup Jongkhar Border Crossing', 'The crossing connects the Bhutanese town of Samdrup Jongkhar with the Indian town of Darranga across the border.'),  # noqa: E501
    ('setting', 'Samdrup Jongkhar Border Crossing', 'The town of Samdrup Jongkhar sits in the lowland subtropical foothills of southeastern Bhutan, at a notably lower elevation than most of the country.'),  # noqa: E501
    ('what_it_is', 'Samdrup Jongkhar Border Gate', "Samdrup Jongkhar is Bhutan's main southeastern land border crossing, connecting the country to the Indian state of Assam."),  # noqa: E501
    ('what_it_is', 'Samdrup Jongkhar Border Gate', 'On the Indian side of the gate lies the town of Darranga in Assam, making this the primary overland gateway between eastern Bhutan and northeastern India.'),  # noqa: E501
    ('setting', 'Samdrup Jongkhar Border Gate', 'The town of Samdrup Jongkhar sits at a low elevation in the subtropical foothills of the eastern Himalayas, making it significantly warmer and more humid than most other parts of Bhutan.'),  # noqa: E501
    ('setting', 'Samdrup Jongkhar Border Gate', "The surrounding Samdrup Jongkhar District borders India's Assam and Arunachal Pradesh states, giving the area a distinct cultural and ecological character influenced by the lowland plains."),  # noqa: E501
    ('history', 'Samdrup Jongkhar Border Gate', 'The border crossing is a busy commercial point; the town has developed substantially as a trade hub between Bhutan and India.'),  # noqa: E501
    ('what_it_is', 'Ta Dzong National Museum', 'The National Museum of Bhutan is housed in Ta Dzong, a circular watchtower that was built in 1649 to defend Rinpung Dzong below it.'),  # noqa: E501
    ('history', 'Ta Dzong National Museum', 'Ta Dzong was converted into a museum in 1968 under the reign of the third King of Bhutan, Jigme Dorji Wangchuck.'),  # noqa: E501
    ('setting', 'Ta Dzong National Museum', 'The circular tower sits on a hillside directly above Rinpung Dzong and overlooks the Paro valley and the Paro Chhu river.'),  # noqa: E501
    ('history', 'Ta Dzong National Museum', 'The original Ta Dzong building was damaged by an earthquake in 2011, and a new museum annex building was subsequently constructed nearby to house and display much of the collection.'),  # noqa: E501
    ('what_it_is', 'Takin Preserve (Motithang Takin Preserve)', "The Motithang Takin Preserve is a wildlife enclosure in the Motithang suburb of Thimphu dedicated to Bhutan's national animal, the takin (Budorcas taxicolor whitei)."),  # noqa: E501
    ('history', 'Takin Preserve (Motithang Takin Preserve)', "The takin is Bhutan's national animal and occupies a central place in Bhutanese mythology, said to have been created by the Divine Madman Drukpa Kunley in the 15th century from the bones of a cow and a goat."),  # noqa: E501
    ('history', 'Takin Preserve (Motithang Takin Preserve)', 'The preserve was originally established as a conventional zoo, but King Jigme Singye Wangchuck ordered the animals to be released into the wild; the takin kept returning to Thimphu, leading authorities to create the dedicated preserve.'),  # noqa: E501
    ('setting', 'Takin Preserve (Motithang Takin Preserve)', 'The preserve is set on a forested hillside on the northern edge of Thimphu, giving the takin a semi-natural, wooded habitat rather than bare enclosures.'),  # noqa: E501
    ('what_it_is', 'Takin Preserve (Motithang Takin Preserve)', 'The takin is a large bovid found in the eastern Himalayas; it has a distinctive bulky body, a concave face, and golden-to-dark-brown shaggy coat.'),  # noqa: E501
    ('what_it_is', 'Taktsang Monastery', 'Taktsang Monastery is a sacred Vajrayana Buddhist monastery complex built into a sheer granite cliff face roughly 900 metres above the floor of the Paro Valley.'),  # noqa: E501
    ('history', 'Taktsang Monastery', "The site is venerated as one of the holiest places in Bhutan; according to tradition, Guru Rinpoche (Padmasambhava) flew to the cave on the back of a tigress and meditated there in the 8th century, giving rise to the 'Tiger's Nest' name."),  # noqa: E501
    ('what_it_is', 'Taktsang Monastery', 'The complex comprises four main temples, several smaller shrines, and a series of interconnected staircases and bridges carved into or clinging to the cliff.'),  # noqa: E501
    ('setting', 'Trashigang', 'The town sits at roughly 1,150 metres above sea level, perched on a steep ridge above the Drangme Chhu river gorge.'),  # noqa: E501
    ('history', 'Trashigang', 'Trashigang Dzong, a fortress-monastery built in 1659, stands dramatically on a promontory overlooking the confluence of the Drangme Chhu and Gamri rivers.'),  # noqa: E501
    ('what_it_is', 'Trashigang', 'The dzong is still an active administrative and religious centre, housing both government offices and monks.'),  # noqa: E501
    ('setting', 'Trashigang', 'The surrounding landscape is characterised by deep river valleys, subtropical forests at lower elevations, and terraced fields of maize and rice.'),  # noqa: E501
    ('what_it_is', 'Ura Village', 'Ura is one of the four valleys of Bumthang district in central-eastern Bhutan, and at roughly 3,100 metres above sea level it is one of the highest permanently inhabited settlements in the country.'),  # noqa: E501
    ('setting', 'Ura Village', 'The village is known for its compact, traditional Bhutanese architecture, with houses clustered closely together and connected by narrow stone-paved lanes — a layout relatively rare in Bhutan.'),  # noqa: E501
    ('history', 'Ura Village', "The valley's high altitude and short growing season make buckwheat and barley the dominant crops rather than rice, which is why buckwheat-based dishes such as puta (buckwheat noodles) are staples of local cuisine."),  # noqa: E501
    ('setting', 'Ura Village', 'The surrounding valley is used for yak and sheep herding, and the open alpine meadows give the landscape a distinctly pastoral character compared to lower Bhutanese valleys.'),  # noqa: E501
    ('what_it_is', 'Barkhor Street', 'The Barkhor is a historic urban quarter and pilgrimage circuit surrounding Jokhang Temple in central Lhasa.'),  # noqa: E501
    ('history', 'Barkhor Street', 'The area developed around Jokhang Temple, which was founded in the 7th century during the period of the Tibetan Empire.'),  # noqa: E501
    ('setting', 'Barkhor Street', 'Its streets combine traditional Tibetan buildings, religious sites, shops, stalls and spaces used by pilgrims.'),  # noqa: E501
    ('what_it_is', 'China–Nepal Friendship Highway', 'The Friendship Highway is a major trans-Himalayan road connection between Kathmandu in Nepal and Lhasa in Tibet, China.'),  # noqa: E501
    ('setting', 'China–Nepal Friendship Highway', 'The route crosses the Nepal–China border and continues across the high Tibetan Plateau.'),  # noqa: E501
    ('setting', 'China–Nepal Friendship Highway', 'In Tibet, the highway passes through or near major settlements including Nyalam, Tingri, Shigatse and Lhatse.'),  # noqa: E501
    ('naming', 'China–Nepal Friendship Highway', 'Its Tibetan section largely follows China National Highway 318, which continues far beyond the Nepal border toward eastern China.'),  # noqa: E501
    ('what_it_is', 'Chiu Monastery', 'Chiu Monastery is a Tibetan Buddhist monastery built on a rocky hill beside Lake Manasarovar.'),  # noqa: E501
    ('history', 'Chiu Monastery', 'The monastery is traditionally associated with Padmasambhava, also known as Guru Rinpoche, who is said to have meditated there.'),  # noqa: E501
    ('setting', 'Chiu Monastery', 'It stands on the northwestern shore of Lake Manasarovar in Purang County, within the high-altitude landscape of Ngari.'),  # noqa: E501
    ('setting', 'Darchen', 'Darchen is a high-altitude settlement in Purang County, Ngari Prefecture, at the southern foot of Mount Kailash.'),  # noqa: E501
    ('what_it_is', 'Darchen', 'It is the traditional starting point for the pilgrimage circuit, or kora, around Mount Kailash.'),  # noqa: E501
    ('what_it_is', 'Everest North Base Camp', 'It is the northern base-camp area for mountaineering expeditions on Mount Everest.'),  # noqa: E501
    ('setting', 'Everest North Base Camp', 'The site lies near Rongbuk Monastery on the northern side of Everest, in Tingri County.'),  # noqa: E501
    ('setting', 'Everest North Base Camp', 'At about 5,150 metres above sea level, it is a sparse, barren high-altitude site on the Tibetan Plateau.'),  # noqa: E501
    ('history', 'Everest North Base Camp', 'Rongbuk Monastery, one of the world’s highest Buddhist monasteries, is located nearby.'),  # noqa: E501
    ('what_it_is', 'Gyantse', 'Gyantse is a historic town in southern Tibet, on the route between Shigatse and Lhasa.'),
    ('history', 'Gyantse', 'Pelkor Chode Monastery, founded in the fifteenth century, is known for its distinctive Kumbum stupa and murals.'),  # noqa: E501
    ('history', 'Gyantse', 'Gyantse Dzong is a hilltop fortress overlooking the town and is associated with the 1904 British expedition to Tibet.'),  # noqa: E501
    ('setting', 'Gyantse', 'The town lies at roughly 4,000 metres above sea level in a high-altitude Himalayan setting.'),  # noqa: E501
    ('what_it_is', 'Gyatso La', 'Gyatso La is a high mountain pass on China National Highway 318, commonly known as the Friendship Highway in this region.'),  # noqa: E501
    ('setting', 'Gyatso La', 'The pass lies in Tingri County in the Shigatse area of the Tibet Autonomous Region, China.'),  # noqa: E501
    ('setting', 'Gyatso La', 'Its elevation is commonly given as approximately 5,200 metres, with published figures varying slightly.'),  # noqa: E501
    ('what_it_is', 'Gyirong', 'Gyirong is a town and county seat in southern Tibet, near the Nepal border.'),
    ('setting', 'Gyirong', 'It lies in the Himalayan Gyirong Valley, a deep mountain valley connected with Nepal by a historic trade route.'),  # noqa: E501
    ('history', 'Gyirong', 'Gyirong has long been associated with trade and travel between the Tibetan plateau and Nepal.'),  # noqa: E501
    ('what_it_is', 'Gyirong County', 'Gyirong County is an administrative county in Shigatse Prefecture in southern Tibet.'),  # noqa: E501
    ('setting', 'Gyirong County', 'The county lies on the China–Nepal border and includes the Gyirong border-trade area.'),  # noqa: E501
    ('history', 'Gyirong County', 'Gyirong has long been associated with trans-Himalayan trade and travel between Tibet and Nepal.'),  # noqa: E501
    ('setting', 'Gyirong County', 'The county contains deep Himalayan valleys as well as high, open plateau terrain.'),
    ('what_it_is', 'Jokhang Temple', 'Jokhang Temple is a major Tibetan Buddhist temple and pilgrimage site in central Lhasa.'),  # noqa: E501
    ('history', 'Jokhang Temple', 'The temple was founded in the 7th century during the reign of Tibetan king Songtsen Gampo.'),  # noqa: E501
    ('setting', 'Jokhang Temple', 'It stands within the Barkhor area, a traditional pilgrimage route and market district surrounding the temple.'),  # noqa: E501
    ('history', 'Jokhang Temple', 'Jokhang Temple forms part of the UNESCO-listed Historic Ensemble of the Potala Palace, together with the Potala Palace and Norbulingka.'),  # noqa: E501
    ('what_it_is', 'Kamba La Pass', 'Kamba La is a high mountain pass on the road between Lhasa and Gyantse in southern Tibet.'),  # noqa: E501
    ('setting', 'Kamba La Pass', 'The pass is about 4,794 metres above sea level.'),
    ('setting', 'Kamba La Pass', 'It overlooks Yamdrok Lake, one of Tibet’s major sacred lakes, and the surrounding high plateau.'),  # noqa: E501
    ('what_it_is', 'Karo La', 'Karo La is a high mountain pass on the road between Gyantse and Nagarzê in southern Tibet.'),  # noqa: E501
    ('setting', 'Karo La', 'The pass is at about 5,050 metres above sea level.'),
    ('setting', 'Karo La', 'The pass lies below Nyenchen Khangsar, also spelled Nyichen Kangsang, a major glaciated mountain in the region.'),  # noqa: E501
    ('what_it_is', 'Kongtang Lhamo Pass', 'Kongtang Lhamo Pass is a high mountain pass on the Tibetan Plateau near the Nepal border.'),  # noqa: E501
    ('setting', 'Kongtang Lhamo Pass', 'The pass lies in Gyirong County of Tibet Autonomous Region, China.'),
    ('setting', 'Kongtang Lhamo Pass', 'Its elevation is commonly given as about 5,236 metres above sea level.'),
    ('what_it_is', 'Lake Manasarovar', 'Lake Manasarovar is a high-altitude freshwater lake in western Tibet, near Mount Kailash and Lake Rakshastal.'),  # noqa: E501
    ('history', 'Lake Manasarovar', 'The lake is sacred in Hinduism, Buddhism, Jainism and the Bon tradition, and is visited by pilgrims who traditionally circumambulate its shores.'),  # noqa: E501
    ('setting', 'Lake Manasarovar', 'It lies in a remote, high-altitude landscape of the Ngari region, with mountain and plateau scenery around the lake.'),  # noqa: E501
    ('what_it_is', 'Lake Manasarovar eastern shore', 'The eastern shore is part of Lake Manasarovar in Burang County, in Tibet’s Ngari region.'),  # noqa: E501
    ('setting', 'Lake Manasarovar eastern shore', 'Lake Manasarovar is a high-altitude freshwater lake on the Tibetan Plateau, near Mount Kailash and Lake Rakshastal.'),  # noqa: E501
    ('history', 'Lake Manasarovar eastern shore', 'The lake and its shores are sacred in Hinduism, Buddhism, Jainism and the Bon tradition.'),  # noqa: E501
    ('what_it_is', 'Lhasa', 'Lhasa is a city on the Lhasa River in the Tibetan Plateau.'),
    ('history', 'Lhasa', 'It is the capital of the Tibet Autonomous Region of China and a historic centre of Tibetan Buddhism.'),  # noqa: E501
    ('setting', 'Lhasa', 'At roughly 3,650 metres above sea level, Lhasa is one of the world’s highest cities.'),
    ('what_it_is', 'Mayum La Pass', 'Mayum La Pass is a high-altitude mountain pass on the Tibetan Plateau.'),
    ('setting', 'Mayum La Pass', 'The pass lies on the overland route between western Tibet and the region of Mount Kailash and Lake Manasarovar.'),  # noqa: E501
    ('setting', 'Mayum La Pass', 'It is commonly described as lying at roughly 4,900 metres or higher, depending on the source and surveying convention.'),  # noqa: E501
    ('setting', 'Mayum La Pass', 'The surrounding landscape is a sparsely settled, treeless high-altitude environment with broad valleys and mountain ranges.'),  # noqa: E501
    ('what_it_is', 'Milarepa Cave', 'Milarepa Cave is a Tibetan Buddhist pilgrimage site associated with the yogi and poet Milarepa.'),  # noqa: E501
    ('history', 'Milarepa Cave', 'The cave is traditionally regarded as a place where Milarepa meditated and attained spiritual realization.'),  # noqa: E501
    ('setting', 'Milarepa Cave', 'It lies in a wooded Himalayan valley near Kyirong, at roughly 3,700 metres above sea level.'),  # noqa: E501
    ('what_it_is', 'Mount Kailash', 'Mount Kailash is a distinctive, pyramid-shaped mountain in the western Tibetan Plateau.'),  # noqa: E501
    ('setting', 'Mount Kailash', 'It rises to about 6,638 metres and is part of the Transhimalaya near the headwaters of several major Asian rivers.'),  # noqa: E501
    ('history', 'Mount Kailash', 'The mountain is sacred in Hinduism, Buddhism, Jainism and the Bon tradition.'),
    ('what_it_is', 'Namling', 'Namling is a town and the administrative seat of Namling County in Shigatse Prefecture, Tibet.'),  # noqa: E501
    ('setting', 'Namling', 'The town lies in the high-altitude Tibetan Plateau of the Tibet Autonomous Region, China.'),
    ('setting', 'Namling', 'Namling County is associated with the upper Yarlung Tsangpo valley and has both farming and pastoral landscapes.'),  # noqa: E501
    ('what_it_is', 'Namling', 'Namling is a predominantly Tibetan settlement with Tibetan Buddhist cultural traditions.'),  # noqa: E501
    ('what_it_is', 'Old Tingri', 'Old Tingri is a high-altitude settlement in Tingri County on the Tibetan Plateau.'),
    ('setting', 'Old Tingri', 'It lies north of the main Himalayan range in the Tibet Autonomous Region of China.'),
    ('naming', 'Old Tingri', 'Old Tingri is distinct from Shegar, the larger county seat commonly known in English as New Tingri.'),  # noqa: E501
    ('what_it_is', 'Paryang', 'Paryang is a remote Tibetan settlement in Zhongba County, within Shigatse Prefecture-level city.'),  # noqa: E501
    ('setting', 'Paryang', 'It lies in the high-altitude landscape of western Tibet, surrounded by broad plains and mountain ranges.'),  # noqa: E501
    ('what_it_is', 'Peiku Tso', 'Peiku Tso is a high-altitude lake on the Tibetan Plateau.'),
    ('setting', 'Peiku Tso', 'The lake lies in Tibet Autonomous Region, near the Nepal border, in the wider Nyalam area.'),  # noqa: E501
    ('setting', 'Peiku Tso', 'The lake is surrounded by broad, sparsely settled plateau landscapes and mountain terrain.'),  # noqa: E501
    ('what_it_is', 'Potala Palace', 'Potala Palace is a historic Buddhist palace and monument on Marpo Ri hill above Lhasa.'),  # noqa: E501
    ('history', 'Potala Palace', 'The site originated in the 7th century, while the present palace complex was largely built from the 17th century under the Fifth Dalai Lama.'),  # noqa: E501
    ('history', 'Potala Palace', 'The palace traditionally served as the winter residence of the Dalai Lamas and as a major centre of Tibetan religious and political life.'),  # noqa: E501
    ('what_it_is', 'Potala Palace', 'Potala Palace forms part of the UNESCO-listed Historic Ensemble of the Potala Palace, Lhasa.'),  # noqa: E501
    ('setting', 'Rongbuk', 'Rongbuk is a high-altitude valley and settlement in Tingri County, Tibet.'),
    ('what_it_is', 'Rongbuk', 'Rongbuk Monastery is a Tibetan Buddhist monastery associated with the Nyingma tradition.'),  # noqa: E501
    ('setting', 'Rongbuk', 'The monastery stands near the northern base of Mount Everest in the Himalaya.'),
    ('what_it_is', 'Rongbuk Monastery', 'Rongbuk Monastery is a Tibetan Buddhist monastery in the Rongbuk Valley on the northern approach to Mount Everest.'),  # noqa: E501
    ('setting', 'Rongbuk Monastery', 'It stands at roughly 5,000 meters above sea level in a high Himalayan valley.'),
    ('history', 'Rongbuk Monastery', 'The monastery and surrounding valley were damaged during the Cultural Revolution and later restored.'),  # noqa: E501
    ('setting', 'Rongbuk Valley', 'Rongbuk Valley lies on the northern side of Mount Everest in the Tibet Autonomous Region.'),  # noqa: E501
    ('what_it_is', 'Rongbuk Valley', 'Rongbuk Monastery is a Tibetan Buddhist monastery in the valley and is commonly associated with the Rongbuk area.'),  # noqa: E501
    ('what_it_is', 'Saga', 'Saga is a high-altitude town and the administrative seat of Saga County in western Tibet.'),
    ('setting', 'Saga', 'Saga stands on the broad, arid Tibetan Plateau amid river valleys and widely scattered settlements.'),  # noqa: E501
    ('history', 'Saga', 'Saga County has long served as a regional passage area between central Tibet and routes toward Nepal and western Tibet.'),  # noqa: E501
    ('what_it_is', 'Sera Monastery', 'Sera Monastery is a major Tibetan Buddhist monastery of the Gelug school.'),
    ('history', 'Sera Monastery', 'It was founded in 1419 by Sakya Yeshe, a disciple of the Gelug founder Tsongkhapa.'),
    ('setting', 'Sera Monastery', 'The monastery stands north of central Lhasa at the foot of the hills.'),
    ('what_it_is', 'Shigatse', 'Shigatse is a city in southwestern Tibet and the administrative center of Shigatse Prefecture-level city.'),  # noqa: E501
    ('history', 'Shigatse', 'It is the historic center of Tsang, one of Tibet’s traditional regions.'),
    ('history', 'Shigatse', 'Tashilhunpo Monastery, founded in 1447, is the traditional seat of the Panchen Lamas and the city’s principal historical landmark.'),  # noqa: E501
    ('what_it_is', 'Tashilhunpo Monastery', 'Tashilhunpo is a major Tibetan Buddhist monastery in Shigatse, traditionally associated with the Panchen Lamas.'),  # noqa: E501
    ('setting', 'Tashilhunpo Monastery', 'It stands at the foot of Drolmari Hill on the western side of Shigatse.'),
    ('what_it_is', 'Tso La Pass', 'Tso La Pass is a high mountain pass on the Tibetan Plateau.'),
    ('setting', 'Tso La Pass', 'The pass is generally described as lying at approximately 4,500 metres above sea level.'),  # noqa: E501
    ('setting', 'Tso La Pass', 'It is associated with overland routes between Saga and the Kailash–Manasarovar region.'),  # noqa: E501
    ('setting', 'Tso La Pass', 'The surrounding landscape is a sparsely settled, high-altitude environment of broad valleys and barren mountain terrain.'),  # noqa: E501
    ('what_it_is', 'Yamdrok Lake', 'Yamdrok Lake is a high-altitude freshwater lake on the Tibetan Plateau.'),
    ('setting', 'Yamdrok Lake', 'It lies in southern Tibet, southwest of Lhasa, near Nagarzê.'),
    ('setting', 'Yamdrok Lake', 'The lake sits at roughly 4,400 meters above sea level and is surrounded by rugged mountain terrain.'),  # noqa: E501
    ('history', 'Yamdrok Lake', "Yamdrok Lake is regarded as one of Tibet's sacred lakes and is important in Tibetan Buddhism."),  # noqa: E501
    ('what_it_is', 'Zhongba', 'Zhongba is the county seat of Zhongba County in western Tibet.'),
    ('naming', 'Zhongba', 'Zhongba County is administered by Shigatse, a prefecture-level city in the Tibet Autonomous Region.'),  # noqa: E501
    ('setting', 'Zhongba', 'The town lies in the high, sparsely settled Tibetan Plateau, amid broad plateau landscapes.'),  # noqa: E501
    ('setting', 'Zhongba', 'The wider county is in southwestern Tibet near the China–Nepal border.'),
    ('what_it_is', 'Zhongba', 'The area is associated with Tibetan communities and culture.'),
    ('what_it_is', 'Zhongba County', 'Zhongba County is an administrative county in western Tibet, under Shigatse City.'),  # noqa: E501
    ('setting', 'Zhongba County', 'The county lies on the high Tibetan Plateau along routes linking western Tibet with Nepal and Mount Kailash.'),  # noqa: E501
    ('setting', 'Zhongba County', 'Zhongba is a remote, sparsely populated high-altitude area with broad plateau landscapes and mountain views.'),  # noqa: E501
    ('what_it_is', 'Lokpriya Gopinath Bordoloi International Airport', 'Lokpriya Gopinath Bordoloi International Airport is the main commercial airport serving Guwahati, the largest city in Assam and the gateway to Northeast India.'),  # noqa: E501
    ('setting', 'Lokpriya Gopinath Bordoloi International Airport', 'The airport is located at Borjhar, on the southwestern outskirts of Guwahati, roughly 20 kilometres from the city centre.'),  # noqa: E501
    ('what_it_is', 'Lokpriya Gopinath Bordoloi International Airport', 'The airport holds international airport status and handles both domestic and a limited number of international flights.'),  # noqa: E501
    ('history', 'Lokpriya Gopinath Bordoloi International Airport', 'The airport was formally renamed in honour of Gopinath Bordoloi, the first Chief Minister of Assam and a prominent figure in the Indian independence movement.'),  # noqa: E501
    ('setting', 'Lokpriya Gopinath Bordoloi International Airport', 'The airport sits in the Brahmaputra river valley and is surrounded by the low-lying plains of Assam, with hills visible in the distance.'),  # noqa: E501
    ('what_it_is', 'Ban Long Lao Mai', 'Ban Long Lao Mai is a rural village in the forested hills of Luang Prabang Province.'),  # noqa: E501
    ('what_it_is', 'Ban Long Lao Mai', 'The village is associated with both Hmong and Khmu residents.'),
    ('history', 'Ban Long Lao Mai', 'Many residents were relocated or resettled in the post-civil-war period as part of government village-consolidation policies.'),  # noqa: E501
    ('what_it_is', 'Kuang Si Falls', 'Kuang Si Falls is a multi-tiered limestone waterfall system located approximately 29–30 kilometres south of Luang Prabang city.'),  # noqa: E501
    ('what_it_is', 'Kuang Si Falls', 'The falls cascade over a series of travertine limestone terraces, creating a sequence of turquoise-blue pools before reaching the main drop.'),  # noqa: E501
    ('setting', 'Kuang Si Falls', 'The main upper fall drops roughly 50 metres into a large pool at the base.'),
    ('setting', 'Kuang Si Falls', 'The vivid turquoise colour of the water is caused by high calcium carbonate content leaching from the surrounding limestone.'),  # noqa: E501
    ('setting', 'Kuang Si Falls', 'The site is set within forested hills and is part of a protected area; the surrounding jungle contributes to its scenic setting.'),  # noqa: E501
    ('setting', 'Luang Prabang', 'Luang Prabang is a small city situated at the confluence of the Mekong River and the Nam Khan River in northern Laos.'),  # noqa: E501
    ('history', 'Luang Prabang', 'It served as the royal capital of the Kingdom of Lan Xang and later of the Kingdom of Luang Prabang until the communist takeover in 1975.'),  # noqa: E501
    ('what_it_is', 'Luang Prabang', 'The entire old town was inscribed as a UNESCO World Heritage Site in 1995, recognised for its well-preserved blend of traditional Lao wooden architecture and French colonial buildings.'),  # noqa: E501
    ('what_it_is', 'Muang Khua', 'Muang Khua is a small riverside town and the capital of Muang Khua District in Phongsali Province, northern Laos.'),  # noqa: E501
    ('setting', 'Muang Khua', 'The town sits at the confluence of the Nam Ou River and the Nam Phak River, giving it a distinctive riverbank setting.'),  # noqa: E501
    ('setting', 'Muang Khua', 'The surrounding hills are home to several ethnic minority groups, including Akha, Tai Dam, and Khmu communities.'),  # noqa: E501
    ('history', 'Muang Khua', 'Muang Khua remained relatively isolated until road improvements in the 2000s; before that, the Nam Ou was the primary route in and out of the district.'),  # noqa: E501
    ('what_it_is', 'Nam Ou River Gorge', 'The Nam Ou is a major tributary of the Mekong River, flowing southward through northern Laos before joining the Mekong near Pak Ou.'),  # noqa: E501
    ('setting', 'Nam Ou River Gorge', 'The gorge section lies in the remote northern provinces of Phongsali and Luang Prabang, passing through steep limestone karst mountains and dense jungle.'),  # noqa: E501
    ('history', 'Nam Ou River Gorge', 'The river corridor is home to several ethnic minority communities, including Khmu, Akha, and Tai Lue villages perched along its banks.'),  # noqa: E501
    ('history', 'Nam Ou River Gorge', 'The construction of several hydroelectric dams on the Nam Ou, completed in stages from the 2010s onward, has altered water levels and partially submerged some riverside villages and landscapes.'),  # noqa: E501
    ('setting', 'Nam Ou River Gorge', "The gorge scenery is characterised by towering forested cliffs, narrow passages, and dramatic changes in the river's width and current."),  # noqa: E501
    ('what_it_is', 'Nam Ou River Valley', 'The Nam Ou is one of the largest tributaries of the Mekong River in Laos, flowing roughly southward through the mountainous north of the country before joining the Mekong near Pak Ou.'),  # noqa: E501
    ('setting', 'Nam Ou River Valley', 'The valley runs through Phongsali, Luang Prabang, and intervening provinces, passing through some of the most remote and least-developed terrain in northern Laos.'),  # noqa: E501
    ('setting', 'Nam Ou River Valley', "The surrounding landscape is characterised by dramatic limestone karst formations, forested ridges, and steep valley walls typical of the Greater Mekong Subregion's karst geology."),  # noqa: E501
    ('what_it_is', 'Nam Ou River Valley', 'The valley is home to numerous ethnic minority communities, including Khmu, Hmong, and various Tai-speaking groups, many living in small riverside or hillside villages.'),  # noqa: E501
    ('history', 'Nam Ou River Valley', 'The Nam Ou River was historically a key transport corridor linking the far north of Laos to Luang Prabang, with boat travel long being the primary means of moving people and goods through the valley.'),  # noqa: E501
    ('history', 'Nam Ou River Valley', 'Construction of a cascade of hydropower dams on the Nam Ou, largely completed in the 2010s, significantly altered water levels and affected traditional boat routes along parts of the river.'),  # noqa: E501
    ('what_it_is', 'Nong Khiaw', 'Nong Khiaw is a small riverside town on the Nam Ou River in northern Laos.'),
    ('setting', 'Nong Khiaw', 'The town sits beneath dramatic limestone karst mountains that rise steeply from the river valley.'),  # noqa: E501
    ('what_it_is', 'Nong Khiaw', 'A single-lane bridge spans the Nam Ou River at Nong Khiaw, connecting the two sides of the village and serving as a local landmark.'),  # noqa: E501
    ('history', 'Nong Khiaw', 'The surrounding area is home to ethnic minority communities, including Khmu and Hmong villages.'),  # noqa: E501
    ('what_it_is', 'Nong Khouay', 'Nong Khouay is a small, remote upland village in Phongsali Province, one of the most northerly and least-developed provinces of Laos.'),  # noqa: E501
    ('history', 'Nong Khouay', 'Phongsali Province borders both China and Vietnam, giving the broader region a distinct cultural mix of Lao, Akha, Phunoy and other ethnic minority communities.'),  # noqa: E501
    ('what_it_is', 'Pak Mong', 'Pak Mong is a rural village and road settlement in northern Laos.'),
    ('setting', 'Pak Mong', 'It lies in Luang Prabang Province, in the Nam Ou River valley.'),
    ('setting', 'Pak Mong', 'The surrounding landscape includes steep, forested hills and prominent limestone karst formations.'),  # noqa: E501
    ('what_it_is', 'Amarbayasgalant Monastery', 'Amarbayasgalant Monastery is a large Buddhist monastic complex located in the Iven River valley in Selenge Province, northern Mongolia.'),  # noqa: E501
    ('history', 'Amarbayasgalant Monastery', 'It was built between 1727 and 1736 during the reign of the Qing Emperor Yongzheng, commissioned to enshrine the remains of the revered Mongolian Buddhist master Zanabazar.'),  # noqa: E501
    ('what_it_is', 'Amarbayasgalant Monastery', "The monastery's architecture follows a Chinese imperial style with Mongolian and Tibetan Buddhist elements, and its construction is notably free of metal nails or metal structural elements, relying instead on traditional woodworking joinery."),  # noqa: E501
    ('history', 'Amarbayasgalant Monastery', "Unlike the vast majority of Mongolia's monasteries, which were largely destroyed or severely damaged during the Stalinist purges of the late 1930s, Amarbayasgalant survived in a comparatively intact state."),  # noqa: E501
    ('what_it_is', 'Amarbayasgalant Monastery', 'The complex originally comprised around 40 temples and buildings; a significant number of these structures still stand today.'),  # noqa: E501
    ('naming', 'Amarbayasgalant Monastery', 'It is considered one of the most important and best-preserved monastic complexes in Mongolia, alongside Gandantegchinlen Monastery in Ulaanbaatar and Erdene Zuu Monastery in Kharkhorin.'),  # noqa: E501
    ('setting', 'Amarbayasgalant Monastery', 'The complex sits in a quiet valley surrounded by forested hills, giving it a secluded and scenic setting typical of northern Mongolian landscapes.'),  # noqa: E501
    ('history', 'Amarbayasgalant Monastery', 'A major restoration project supported by UNESCO and other organizations took place from the 1980s onward to repair and preserve the deteriorating structures.'),  # noqa: E501
    ('what_it_is', 'Baga Gazriin Chuluu', 'Baga Gazriin Chuluu is a granite rock massif rising from the semi-desert steppe of the Middle Gobi, not a plateau in the strict sense, but a cluster of rocky outcrops and hills.'),  # noqa: E501
    ('history', 'Baga Gazriin Chuluu', 'The area contains ruins of an ancient monastery destroyed during the Stalinist purges of the late 1930s, when many Mongolian Buddhist monasteries were demolished.'),  # noqa: E501
    ('setting', 'Baga Gazriin Chuluu', 'The site is home to diverse wildlife including ibex, argali wild sheep, eagles, and various other bird species.'),  # noqa: E501
    ('what_it_is', 'Chinggis Khaan International Airport', 'Chinggis Khaan International Airport is the primary international gateway to Mongolia, serving the capital city Ulaanbaatar.'),  # noqa: E501
    ('what_it_is', 'Chinggis Khaan International Airport', 'The airport carries the IATA code ULN and the ICAO code ZMUB.'),  # noqa: E501
    ('naming', 'Chinggis Khaan International Airport', "The airport was renamed in honour of Chinggis Khaan (Genghis Khan), the founder of the Mongol Empire, reflecting national pride in Mongolia's imperial heritage."),  # noqa: E501
    ('what_it_is', 'Darkhan', "Darkhan is Mongolia's second-largest city, after the capital Ulaanbaatar, and serves as a major industrial and commercial centre."),  # noqa: E501
    ('history', 'Darkhan', "The city was founded in 1961 during the Soviet era as a planned industrial city, making it one of Mongolia's youngest major urban centres."),  # noqa: E501
    ('setting', 'Darkhan', 'It lies in the Darkhan valley in northern Mongolia, roughly 220 kilometres north of Ulaanbaatar by road, close to the Russian and Chinese rail corridor.'),  # noqa: E501
    ('history', 'Darkhan', 'Darkhan grew around heavy industry, including a steel plant and building-materials factories, reflecting its Soviet-era purpose as an industrial hub.'),  # noqa: E501
    ('what_it_is', 'Darkhan', 'The city is divided into an older, traditional district and a newer district of Soviet-style apartment blocks, giving it a distinctly planned urban character unusual in Mongolia.'),  # noqa: E501
    ('setting', 'Darkhan', "The surrounding Darkhan-Uul province contains some of Mongolia's most productive agricultural land, in contrast to the steppe and desert landscapes found elsewhere in the country."),  # noqa: E501
    ('what_it_is', 'Erdene Zuu Monastery', 'Erdene Zuu is widely regarded as the oldest surviving Buddhist monastery in Mongolia.'),  # noqa: E501
    ('history', 'Erdene Zuu Monastery', 'It was founded in 1586 by Abtai Sain Khan, using stones quarried from the ruins of the nearby ancient Mongol capital Karakorum.'),  # noqa: E501
    ('what_it_is', 'Erdene Zuu Monastery', 'The monastery complex is enclosed by a distinctive white wall approximately 400 metres on each side, punctuated by 108 stupas—108 being a sacred number in Buddhism.'),  # noqa: E501
    ('history', 'Erdene Zuu Monastery', 'At its height the monastery housed several hundred monks and contained dozens of temples; by some accounts up to 1,000 monks resided there at times.'),  # noqa: E501
    ('history', 'Erdene Zuu Monastery', "During the Stalinist purges of 1937–1938, Soviet-backed authorities destroyed most of the monastery's temples and executed or imprisoned many of its monks; only three temples survived."),  # noqa: E501
    ('history', 'Erdene Zuu Monastery', "The monastery was reopened for active religious practice after Mongolia's democratic transition in 1990, and monks once again conduct ceremonies on site."),  # noqa: E501
    ('setting', 'Erdene Zuu Monastery', 'Erdene Zuu is part of the Orkhon Valley Cultural Landscape, inscribed as a UNESCO World Heritage Site in 2004.'),  # noqa: E501
    ('setting', 'Erdene Zuu Monastery', "The site sits directly adjacent to the archaeological zone of ancient Karakorum, and a stone tortoise sculpture—one of four that once marked the city's corners—remains visible near the monastery walls."),  # noqa: E501
    ('what_it_is', 'Flaming Cliffs', 'The Flaming Cliffs (Bayanzag) are a series of red, orange, and rust-coloured sandstone cliffs and badlands in the Gobi Desert.'),  # noqa: E501
    ('history', 'Flaming Cliffs', 'The site became world-famous after American explorer Roy Chapman Andrews and his American Museum of Natural History expeditions (1922–1925) discovered dinosaur fossils there, including the first scientifically documented dinosaur eggs.'),  # noqa: E501
    ('setting', 'Flaming Cliffs', "The cliffs glow most vividly in the light of sunrise and sunset, which is the origin of Andrews's evocative English name."),  # noqa: E501
    ('history', 'Flaming Cliffs', 'The area is an important palaeontological site; fossils of Protoceratops, Oviraptor, and Velociraptor have all been recovered from these sediments.'),  # noqa: E501
    ('setting', 'Flaming Cliffs', 'Scattered saxaul trees (Haloxylon ammodendron) grow across the site, giving it its Mongolian name Bayanzag.'),  # noqa: E501
    ('what_it_is', 'Gobi Gurvan Saikhan National Park', "Gobi Gurvan Saikhan is Mongolia's largest national park, covering roughly 27,000 square kilometers in the southern Gobi Desert."),  # noqa: E501
    ('history', 'Gobi Gurvan Saikhan National Park', 'The park was established in 1993 and protects a representative section of the Gobi Desert ecosystem.'),  # noqa: E501
    ('what_it_is', 'Gobi Gurvan Saikhan National Park', 'The name refers to three mountain ridges — Zuun (East), Dund (Middle), and Baruun (West) Saikhan — that rise dramatically from the desert floor.'),  # noqa: E501
    ('setting', 'Gobi Gurvan Saikhan National Park', "The Khongoryn Els sand dunes, among the largest in Mongolia, lie within the park's boundaries and rise up to around 300 meters in height."),  # noqa: E501
    ('setting', 'Gobi Gurvan Saikhan National Park', 'The park provides habitat for rare and endangered species including snow leopards, Gobi bears (Mazaalai), ibex, and argali wild sheep.'),  # noqa: E501
    ('what_it_is', 'Kharkhorin', 'Kharkhorin is the modern town built adjacent to the ruins of Karakorum, the 13th-century capital of the Mongol Empire founded by Ögedei Khan, son of Genghis Khan, around 1235.'),  # noqa: E501
    ('history', 'Kharkhorin', 'The site of ancient Karakorum was the political and administrative hub of the largest contiguous land empire in history during the 13th century.'),  # noqa: E501
    ('setting', 'Kharkhorin', 'Kharkhorin lies within the UNESCO-listed Orkhon Valley Cultural Landscape, a World Heritage Site recognised for its pastoral and nomadic traditions spanning thousands of years.'),  # noqa: E501
    ('setting', 'Kharkhorin', 'The Orkhon River flows through the valley near Kharkhorin, shaping a fertile corridor that has attracted nomadic cultures, including the Xiongnu, Turkic peoples, Uyghurs, and Mongols, for millennia.'),  # noqa: E501
    ('what_it_is', 'Kharkhorin Museum', 'The Kharkhorin Museum is an archaeological and historical museum dedicated to the ancient Mongol imperial capital of Karakorum and the wider Orkhon Valley.'),  # noqa: E501
    ('history', 'Kharkhorin Museum', 'It was established to preserve and display finds from the Karakorum archaeological site, which served as the capital of the Mongol Empire founded by Ögedei Khan in the 13th century.'),  # noqa: E501
    ('history', 'Kharkhorin Museum', "The museum's collections include artifacts predating the Mongol Empire, reflecting earlier Turkic and Uyghur civilizations that also flourished in the Orkhon Valley."),  # noqa: E501
    ('setting', 'Kharkhorin Museum', 'The museum is situated in the modern town of Kharkhorin, close to the Erdene Zuu Monastery, which was itself built in the 16th century using stones from the ruins of Karakorum.'),  # noqa: E501
    ('setting', 'Kharkhorin Museum', 'The Orkhon Valley, in which Kharkhorin sits, is a UNESCO World Heritage Site recognized for its outstanding cultural landscape spanning nomadic and settled civilizations.'),  # noqa: E501
    ('history', 'Kharkhorin Museum', "The museum building itself was constructed with support from Japanese funding and cooperation, reflecting international scholarly interest in Karakorum's archaeology."),  # noqa: E501
    ('what_it_is', 'Khongor Sand Dunes', 'Khongoryn Els is one of the largest and most spectacular sand dune systems in Mongolia, stretching roughly 180 kilometres in length.'),  # noqa: E501
    ('what_it_is', 'Khongor Sand Dunes', 'The dunes rise to heights of up to approximately 300 metres in places, making them among the tallest dunes in the Gobi Desert.'),  # noqa: E501
    ('history', 'Khongor Sand Dunes', "They lie within Gobi Gurvan Saikhan National Park, Mongolia's largest national park, established in 1993."),  # noqa: E501
    ('setting', 'Khongor Sand Dunes', "The dunes are bordered to the north by the Khongor River, which creates a strip of greenery and supports Bactrian camels and other wildlife at the desert's edge."),  # noqa: E501
    ('what_it_is', 'Khongor Sand Dunes', "The nickname 'Singing Sands' comes from the rumbling or humming sound produced when wind moves the dry sand grains, a phenomenon audible especially during strong gusts."),  # noqa: E501
    ('what_it_is', 'Khongoryn Els Sand Dunes', 'Khongoryn Els is one of the largest sand dune systems in Mongolia, stretching roughly 180 km in length and reaching widths of up to 27 km.'),  # noqa: E501
    ('setting', 'Khongoryn Els Sand Dunes', "The dunes lie within Gobi Gurvansaikhan National Park, Mongolia's largest national park, in the South Gobi region."),  # noqa: E501
    ('what_it_is', 'Khongoryn Els Sand Dunes', 'The highest dunes rise to approximately 300 metres, making them among the most dramatic in the Gobi Desert.'),  # noqa: E501
    ('naming', 'Khongoryn Els Sand Dunes', "The dunes earn their nickname 'Singing Sands' (Mongolian: Duut Mankhan) because wind and sliding sand create a low, rumbling or humming sound audible from a distance."),  # noqa: E501
    ('setting', 'Khongoryn Els Sand Dunes', 'The Khongoryn River runs along the northern edge of the dunes, creating a narrow green belt of vegetation and a reliable water source that supports nomadic families and their Bactrian camels.'),  # noqa: E501
    ('setting', 'Khongoryn Els Sand Dunes', 'Wildlife in the surrounding Gobi Gurvansaikhan National Park includes the endangered Bactrian camel, snow leopard, Gobi bear, and various raptor species.'),  # noqa: E501
    ('what_it_is', 'Khorgo-Terkhiin Tsagaan Nuur National Park', 'The park is a protected area in central Mongolia encompassing the extinct Khorgo volcano and the large freshwater Terkhiin Tsagaan Nuur (Great White Lake).'),  # noqa: E501
    ('history', 'Khorgo-Terkhiin Tsagaan Nuur National Park', 'Terkhiin Tsagaan Nuur was formed thousands of years ago when lava flows from the Khorgo volcano dammed a river valley, creating the lake.'),  # noqa: E501
    ('setting', 'Khorgo-Terkhiin Tsagaan Nuur National Park', "The Khorgo volcano is a well-preserved cinder cone rising above the surrounding steppe, and its lava field extends across a wide area around the lake's eastern shore."),  # noqa: E501
    ('setting', 'Khorgo-Terkhiin Tsagaan Nuur National Park', 'The lake sits at roughly 2,060 metres above sea level on the Mongolian plateau and is fed by several rivers and springs.'),  # noqa: E501
    ('what_it_is', 'Khorgo Volcano', 'The volcano is the centrepiece of Khorgo-Terkhiin Tsagaan Nuur National Park, established to protect both the volcanic landscape and the adjacent freshwater lake.'),  # noqa: E501
    ('history', 'Khorgo Volcano', "Lava flows from Khorgo formed the basin that created Terkhiin Tsagaan Nuur (White Lake), which sits at the volcano's base."),  # noqa: E501
    ('history', 'Khorgo Volcano', 'The volcano last erupted roughly 8,000 years ago, making its cone and lava fields unusually intact.'),  # noqa: E501
    ('setting', 'Khorgo Volcano', 'The surrounding landscape is characterised by hardened lava fields, scattered cave formations, and sub-alpine steppe, providing a stark contrast to typical Mongolian grassland scenery.'),  # noqa: E501
    ('setting', 'Khorgo Volcano', 'The national park supports wildlife including waterfowl on the lake, raptors, and occasional wolves and foxes on the steppe.'),  # noqa: E501
    ('what_it_is', 'Lake Khövsgöl', 'Lake Khövsgöl is a large freshwater lake in northern Mongolia, sitting at roughly 1,645 metres above sea level near the border with Russia.'),  # noqa: E501
    ('what_it_is', 'Lake Khövsgöl', "It holds an estimated 70 percent of Mongolia's fresh water and about two percent of the world's unfrozen surface fresh water."),  # noqa: E501
    ('what_it_is', 'Lake Khövsgöl', 'The lake is approximately 136 kilometres long and up to 36 kilometres wide, making it one of the largest lakes by volume in Asia.'),  # noqa: E501
    ('setting', 'Lake Khövsgöl', "The surrounding landscape is dominated by Siberian taiga forest — largely Siberian larch and pine — giving the area an appearance markedly different from Mongolia's steppes."),  # noqa: E501
    ('history', 'Lake Khövsgöl', 'The region is home to the Tsaatan (Dukha) people, a small indigenous community who herd reindeer and live in the forests north of the lake.'),  # noqa: E501
    ('history', 'Lake Khövsgöl', "Khövsgöl National Park, established in 1992, protects the lake and its surrounding forests and is one of Mongolia's most biodiverse protected areas."),  # noqa: E501
    ('what_it_is', 'Mandalgovi', 'Mandalgovi is the capital city of Dundgovi Province, one of the central Gobi provinces of Mongolia.'),  # noqa: E501
    ('what_it_is', 'Mandalgovi', 'Mandalgovi is a small provincial centre with a population of roughly 15,000–18,000 people, serving as the administrative and commercial hub for the surrounding semi-desert region.'),  # noqa: E501
    ('setting', 'Mandalgovi', 'The surrounding landscape is typical of the transitional zone between the Mongolian steppe and the Gobi Desert — flat, sparse grassland gradually giving way to gravel plains and low rocky hills.'),  # noqa: E501
    ('setting', 'Mandalgovi', 'The region around Mandalgovi is known for its semi-nomadic herding communities, with camels, goats, and sheep common sights in the surrounding countryside.'),  # noqa: E501
    ('what_it_is', 'Mörön', 'Mörön is the capital city of Khövsgöl Province, the northernmost aimag (province) of Mongolia.'),  # noqa: E501
    ('setting', 'Mörön', 'The city sits in a broad valley at an elevation of approximately 1,283 metres above sea level.'),  # noqa: E501
    ('setting', 'Mörön', 'The Delgermörön River flows near the town, and the surrounding landscape is characterised by steppe grasslands and distant forested hills.'),  # noqa: E501
    ('history', 'Mörön', 'Mörön developed as an administrative and trade centre during the Soviet era and retains several Soviet-style public buildings from that period.'),  # noqa: E501
    ('what_it_is', 'Ongi Monastery', 'Ongi Monastery (Ongiin Khiid) is a ruined Buddhist monastic complex situated on both banks of the Ongi River in the semi-desert of Dundgovi Province.'),  # noqa: E501
    ('history', 'Ongi Monastery', 'It was founded in 1660 and grew to become one of the largest monastery complexes in Mongolia, eventually comprising two main temple complexes — Barlim Khiid on the north bank and Khutagt Khiid on the south bank.'),  # noqa: E501
    ('history', 'Ongi Monastery', 'At its peak the complex housed more than 30 temples and around 1,000 monks.'),
    ('history', 'Ongi Monastery', 'The monastery was almost entirely destroyed by the Soviet-backed Mongolian communist government during the religious purges of 1937–1939; many monks were executed or sent to labor camps.'),  # noqa: E501
    ('history', 'Ongi Monastery', 'Following the end of communist rule, restoration work began in the 1990s; a small functioning temple has been rebuilt and a resident community of monks has returned, though the majority of the site remains in ruins.'),  # noqa: E501
    ('setting', 'Ongi Monastery', 'The site sits along the Ongi River, one of the few rivers in the Mongolian steppe that does not reach the sea; the river valley provides a green corridor through the surrounding arid landscape.'),  # noqa: E501
    ('setting', 'Ongi Monastery', 'The surrounding terrain is typical of the northern Gobi fringe — sparse steppe grassland transitioning to semi-desert — making the green river valley a visually striking contrast.'),  # noqa: E501
    ('what_it_is', 'Sod Camp', 'It consists of traditional Mongolian felt tents (gers) arranged as accommodation for visitors to the Khövsgöl region.'),  # noqa: E501
    ('setting', 'Sod Camp', "The camp sits at the edge of the Khövsgöl National Park, one of Mongolia's largest and most visited protected areas."),  # noqa: E501
    ('setting', 'Sod Camp', "Lake Khövsgöl, which the camp overlooks, holds approximately two percent of the world's fresh surface water and is one of the deepest lakes in Asia."),  # noqa: E501
    ('what_it_is', 'Terkhiin Tsagaan Lake', 'Terkhiin Tsagaan Lake is a large freshwater lake in central Mongolia, sitting at roughly 2,060 metres above sea level on the Khangai plateau.'),  # noqa: E501
    ('history', 'Terkhiin Tsagaan Lake', 'The lake was formed when lava flows from volcanic eruptions of the Khorgo volcano dammed the Terkh River valley, making it of relatively recent geological origin.'),  # noqa: E501
    ('what_it_is', 'Terkhiin Tsagaan Lake', "Khorgo volcano, a dormant cinder cone located at the lake's eastern end, is the centrepiece of Khorgo-Terkhiin Tsagaan Nuur National Park, which protects both the lake and the surrounding volcanic landscape."),  # noqa: E501
    ('history', 'Terkhiin Tsagaan Lake', 'The national park was established in 1965 and covers an area of volcanic terrain characterised by lava fields, craters, and cave formations alongside the lake.'),  # noqa: E501
    ('setting', 'Terkhiin Tsagaan Lake', 'The lake and its shores support diverse birdlife, including migratory waterfowl, and the surrounding steppe is habitat for wildlife including marmots and various raptors.'),  # noqa: E501
    ('what_it_is', 'Tsaatan Reindeer Herder Settlements', 'The Tsaatan (Dukha) are a small semi-nomadic indigenous community, numbering only a few hundred people, who herd reindeer in the remote taiga forest of northern Khövsgöl Province.'),  # noqa: E501
    ('what_it_is', 'Tsaatan Reindeer Herder Settlements', "Their settlements are not permanent villages but seasonal camps of teepee-style dwellings called 'urts' (similar to Siberian chums), which are moved several times a year as the reindeer graze."),  # noqa: E501
    ('history', 'Tsaatan Reindeer Herder Settlements', 'The Tsaatan are ethnically and linguistically related to Tuvan peoples of southern Siberia, and their reindeer-herding culture is one of the last of its kind in the world.'),  # noqa: E501
    ('what_it_is', 'Tsaatan Reindeer Herder Settlements', 'Reindeer in Tsaatan culture are used for riding and as pack animals as well as for milk; herds are not primarily raised for meat, making this practice distinct from many other reindeer-herding traditions.'),  # noqa: E501
    ('history', 'Tsaatan Reindeer Herder Settlements', 'The Tsaatan community has faced pressures including population decline, restrictions on cross-border movement with Russia, and the social and economic challenges of maintaining a traditional herding lifestyle.'),  # noqa: E501
    ('what_it_is', 'Tsagaan Suvarga', 'Tsagaan Suvarga is a large natural cliff formation in the Gobi region of Dundgovi Province, southern Mongolia.'),  # noqa: E501
    ('naming', 'Tsagaan Suvarga', 'The site takes its name from the way the white and cream-coloured cliffs resemble a Buddhist stupa when viewed from a distance.'),  # noqa: E501
    ('history', 'Tsagaan Suvarga', 'The cliffs are composed of sedimentary rock layers that were deposited during ancient shallow-sea and lake environments, making them geologically significant.'),  # noqa: E501
    ('setting', 'Tsagaan Suvarga', 'Mineral variation in the rock strata produces a range of colours across the cliff face, including red, orange, yellow, and white bands.'),  # noqa: E501
    ('setting', 'Tsagaan Suvarga', 'The formation rises steeply from the flat surrounding steppe and desert plain, creating a dramatic visual contrast with the landscape.'),  # noqa: E501
    ('history', 'Tsagaan Suvarga', 'Fossil remains, including marine organisms, have been found in the area, reflecting its prehistoric origins as an ancient seabed.'),  # noqa: E501
    ('what_it_is', 'Ulaanbaatar', "Ulaanbaatar is the capital and largest city of Mongolia, serving as the country's political, economic, and cultural centre."),  # noqa: E501
    ('history', 'Ulaanbaatar', 'The city was founded in 1639 as a nomadic Buddhist monastic centre and became a fixed settlement in its current location in 1778.'),  # noqa: E501
    ('setting', 'Ulaanbaatar', 'It sits in a valley on the Tuul River, surrounded by four sacred mountains, at an elevation of roughly 1,350 metres above sea level.'),  # noqa: E501
    ('what_it_is', 'Ulziit Khishig Ger Camp', 'Ulziit Khishig is a tourist ger camp, meaning accommodation is provided in traditional Mongolian felt yurts (gers), with some camps also offering cabin-style rooms.'),  # noqa: E501
    ('setting', 'Ulziit Khishig Ger Camp', 'The camp is situated in the Ulziit soum area of Övörkhangai Province, in central Mongolia, roughly on the corridor between Khövsgöl and the Gobi Desert.'),  # noqa: E501
    ('setting', 'Ulziit Khishig Ger Camp', "The surrounding landscape is characteristic of Mongolia's forest-steppe transition zone, with grassy valleys flanked by larch- and pine-covered hills."),  # noqa: E501
    ('history', 'Ulziit Khishig Ger Camp', 'Övörkhangai Province borders the Orkhon Valley, a UNESCO World Heritage Site, making the region historically significant as the heartland of several Mongolian empires.'),  # noqa: E501
    ('what_it_is', 'Uran Togoo Volcano', 'It is the centerpiece of the Uran Togoo-Tulga Uul Natural Monument, a protected natural area established to preserve its volcanic landscape.'),  # noqa: E501
    ('history', 'Uran Togoo Volcano', 'The volcano last erupted approximately 20,000–24,000 years ago, making it geologically recent in volcanic terms.'),  # noqa: E501
    ('what_it_is', 'Uran Togoo Volcano', 'Uran Togoo is considered the largest of several dormant volcanoes in the surrounding valleys of this part of Bulgan Province.'),  # noqa: E501
    ('setting', 'Uran Togoo Volcano', 'The surrounding landscape combines open green steppe grasslands with patches of forest, creating a contrast with the dark volcanic rock of the cone.'),  # noqa: E501
    ('what_it_is', 'Yol Valley', 'Yol Valley (Yolyn Am) is a narrow, steep-walled gorge carved into the Zuun Saikhan Mountains within Gurvan Saikhan National Park.'),  # noqa: E501
    ('naming', 'Yol Valley', 'The valley is named after the Yol (lammergeier or bearded vulture), a large raptor species that nests in the rocky cliffs of the gorge.'),  # noqa: E501
    ('setting', 'Yol Valley', 'It lies in the Gobi Desert region of southern Mongolia, yet its high elevation and shaded canyon walls historically caused a thick sheet of ice to persist on the valley floor well into summer.'),  # noqa: E501
    ('what_it_is', 'Yol Valley', "The valley is part of Gurvan Saikhan National Park, one of Mongolia's largest protected areas, established to conserve Gobi Desert ecosystems."),  # noqa: E501
    ('setting', 'Yol Valley', 'The gorge narrows dramatically in places, with towering rock walls creating a dramatic and photogenic landscape very different from the open desert surroundings.'),  # noqa: E501
    ('what_it_is', 'Kathmandu', 'Kathmandu is the capital and largest urban center of Nepal.'),
    ('setting', 'Kathmandu', 'The city lies in the Kathmandu Valley in central Nepal, at roughly 1,400 metres above sea level.'),  # noqa: E501
    ('history', 'Kathmandu', 'Kathmandu’s historic center includes Kathmandu Durbar Square and the former Hanuman Dhoka royal palace complex.'),  # noqa: E501
    ('what_it_is', 'Rasuwagadhi Border Crossing', "Rasuwagadhi is an international land border crossing between Nepal and China's Tibet Autonomous Region."),  # noqa: E501
    ('setting', 'Rasuwagadhi Border Crossing', "The crossing connects Nepal's Rasuwa District with Gyirong, also commonly called Kerung, on the Tibetan side."),  # noqa: E501
    ('setting', 'Rasuwagadhi Border Crossing', 'Rasuwagadhi lies in the Himalayan valley north of Timure, on the road linking Kathmandu with Gyirong and the Tibetan plateau.'),  # noqa: E501
    ('what_it_is', 'Cuc Phuong National Park', 'Cuc Phuong National Park is a protected karst-forest area in northern Vietnam.'),  # noqa: E501
    ('history', 'Cuc Phuong National Park', 'Established in 1962, it was Vietnam’s first national park.'),
    ('setting', 'Cuc Phuong National Park', 'The park contains tropical rainforest, limestone mountains, caves and archaeological sites.'),  # noqa: E501
    ('setting', 'Cuc Phuong National Park', 'The park extends across Ninh Binh, Hoa Binh and Thanh Hoa provinces.'),
    ('what_it_is', 'Cuc Phuong National Park', "Cúc Phương is Vietnam's first national park, established in 1962."),
    ('what_it_is', 'Dien Bien Phu', 'Dien Bien Phu is a city and the capital of Dien Bien Province in the remote northwestern highlands of Vietnam, close to the borders with Laos and China.'),  # noqa: E501
    ('setting', 'Dien Bien Phu', 'The city sits in the Muong Thanh Valley (also called the Dien Bien Basin), a broad, flat, rice-growing plain surrounded by steep jungle-covered mountains.'),  # noqa: E501
    ('history', 'Dien Bien Phu', 'The valley is historically the homeland of the Thai (Tai) ethnic minority peoples, who remain a significant part of the local population today.'),  # noqa: E501
    ('history', 'Dien Bien Phu', 'In spring 1954, the Battle of Dien Bien Phu was fought here between French Union forces and the Viet Minh; the Viet Minh victory on 7 May 1954 ended French colonial rule in Indochina.'),  # noqa: E501
    ('what_it_is', 'Endangered Primate Rescue Center', 'The Endangered Primate Rescue Center is a conservation facility for rescued and confiscated primates.'),  # noqa: E501
    ('setting', 'Endangered Primate Rescue Center', 'It is located within Cuc Phuong National Park in northern Vietnam.'),  # noqa: E501
    ('history', 'Endangered Primate Rescue Center', 'The center was established in the 1990s and supports research, captive breeding and conservation of threatened Vietnamese primates.'),  # noqa: E501
    ('what_it_is', 'Endangered Primate Rescue Center', 'Its work includes species recovery efforts involving the critically endangered Cat Ba langur.'),  # noqa: E501
    ('what_it_is', 'Hanoi', "Hanoi is the capital city of Vietnam and the country's second-largest urban centre."),
    ('setting', 'Hanoi', 'It sits on the western bank of the Red River in the northern Red River Delta region.'),
    ('history', 'Hanoi', 'The city has been a political centre for over a thousand years, serving as the capital of successive Vietnamese dynasties from the 11th century onward.'),  # noqa: E501
    ('what_it_is', 'Hanoi', 'The Old Quarter (Phố Cổ) is a dense grid of narrow streets historically organised by craft and trade guilds, each street traditionally associated with a specific merchandise.'),  # noqa: E501
    ('history', 'Hanoi', "The Temple of Literature (Văn Miếu), founded in 1070, is a well-preserved example of traditional Vietnamese architecture and was Vietnam's first national university."),  # noqa: E501
    ('what_it_is', 'Mai Chau', 'Mai Chau is a rural valley and district in northwestern Vietnam, known for rice fields and villages.'),  # noqa: E501
    ('history', 'Mai Chau', 'The area is home to Thai communities, with White Thai (Tai Khao) cultural traditions especially associated with its villages.'),  # noqa: E501
    ('setting', 'Mai Chau', 'Mai Chau lies among forested limestone mountains and karst hills west of Hanoi.'),
    ('what_it_is', 'Ma River', 'The Ma River is a transboundary river flowing through Laos and Vietnam.'),
    ('setting', 'Ma River', 'Its valley includes villages, cultivated fields and other working agricultural land.'),
    ('naming', 'Ma River', 'The river is known locally as Sông Mã in Vietnam and Nam Ma in Laos.'),
    ('what_it_is', 'Moc Chau', 'Moc Chau is a highland town and agricultural area in northwestern Vietnam.'),
    ('setting', 'Moc Chau', 'It lies on the Moc Chau Plateau in Son La Province, near Vietnam’s border with Laos.'),
    ('what_it_is', 'Moc Chau', 'The area is known for tea plantations, dairy farming, orchards and flower cultivation.'),  # noqa: E501
    ('setting', 'Moc Chau', 'Moc Chau is home to communities including Thai and Hmong people, alongside the Kinh majority.'),  # noqa: E501
    ('what_it_is', 'Pha Din Pass', 'Pha Din Pass is a high mountain pass on National Route 6 in northern Vietnam.'),
    ('setting', 'Pha Din Pass', 'The pass lies across the boundary between Dien Bien and Son La provinces, near Tuan Giao and Thuan Chau.'),  # noqa: E501
    ('naming', 'Pha Din Pass', 'The name Pha Đin is commonly explained from Tai-language words meaning “heaven” and “earth.”'),  # noqa: E501
    ('history', 'Pha Din Pass', 'The pass was an important route for transporting supplies during the 1954 Battle of Dien Bien Phu.'),  # noqa: E501
    ('what_it_is', 'Son La', 'Son La is a city in northwestern Vietnam and the capital of Son La Province.'),
    ('setting', 'Son La', 'The city lies in a mountainous inland valley in Vietnam’s Northwest region.'),
    ('setting', 'Son La', 'Son La is a regional centre in a province with substantial Thai and Hmong communities.'),
    ('what_it_is', 'Tuan Giao', 'Tuan Giao is a district town and the administrative centre of Tuan Giao District.'),
    ('setting', 'Tuan Giao', 'It is in northwestern Vietnam, in Điện Biên Province.'),
    ('setting', 'Tuan Giao', 'The town lies in a mountainous valley in an area associated with Thai and Hmong communities.'),  # noqa: E501
    ('history', 'Nậm Cắn / Nam Khan Border Crossing', 'The crossing follows Route 7 on the Lao side and National Highway 7 on the Vietnamese side, a historic trans-Indochina road corridor through the Annamite Mountains.'),  # noqa: E501
    ('setting', 'Nậm Cắn / Nam Khan Border Crossing', 'The Annamite Mountains (Truong Son range) form the natural boundary here; the terrain on both sides is heavily forested highland with steep gradients.'),  # noqa: E501
]

WRITER = atom_seed.WRITER_FACT_KINDS


def _kind_after_clean(text, kind):
    return pc._clean_facts([{"text": text, "kind": kind}], "card")[0]["kind"]


def test_fixture_sizes():
    assert (len(LEAKS), len(UNLEAKS), len(BACKGROUND)) == (31, 22, 437)


def test_writer_kinds_match_seed():
    assert pc._WRITER_KINDS == atom_seed.WRITER_FACT_KINDS


@pytest.mark.parametrize("current,proposed,card,text", LEAKS, ids=[f"{c}-{i}" for i, (_, _, c, _) in enumerate(LEAKS)])
def test_every_audited_leak_leaves_writer_kinds(current, proposed, card, text):
    assert current in WRITER
    kind = _kind_after_clean(text, current)
    assert kind not in WRITER and kind in ("visiting", "practical")


def test_leak_target_kind_mostly_matches_reviewers():
    same = sum(_kind_after_clean(t, cur) == prop for cur, prop, _, t in LEAKS)
    assert same >= 29  # Kyichu / Zorig Chusum "combined with visits": visiting by rule, reviewers said practical


def test_unleaks_false_positives_at_most_3():
    hits = [t for k, _, t in UNLEAKS if _kind_after_clean(t, k) not in WRITER]
    assert len(hits) <= 3, hits


def test_untouched_background_false_positive_rate_at_most_3_percent():
    hits = [t for k, _, t in BACKGROUND if _kind_after_clean(t, k) not in WRITER]
    assert len(hits) / len(BACKGROUND) <= 0.03, hits


def test_withheld_kinds_and_text_are_kept():
    out = pc._clean_facts([{"text": "Access requires a permit.", "kind": "practical"},
                           {"text": "Visitors can climb  the tower.", "kind": "visiting"}])
    assert out == [{"text": "Access requires a permit.", "kind": "practical"},
                   {"text": "Visitors can climb the tower.", "kind": "visiting"}]


def test_bogus_kind_defaults_then_is_checked():
    out = pc._clean_facts([{"text": "The road to Mongar crosses high passes.", "kind": "bogus"}])
    assert out == [{"text": "The road to Mongar crosses high passes.", "kind": "practical"}]


def test_rekind_is_logged_with_card_and_pattern(monkeypatch):
    calls = []
    monkeypatch.setattr(pc.logger, "info", lambda event, **kw: calls.append((event, kw)))
    pc._clean_facts([{"text": "It serves as a key transit hub on the Nam Ou.", "kind": "what_it_is"},
                     {"text": "Built in the 17th century.", "kind": "history"}], "Muang Khua")
    assert calls == [("place_card_fact_rekinded", {"card": "Muang Khua", "pattern": "hub", "from_kind": "what_it_is",
                                                   "to_kind": "practical",
                                                   "text": "It serves as a key transit hub on the Nam Ou."})]


def test_traveller_clause_returns_none_for_background():
    assert pc.traveller_clause("The dzong was built in 1637 at the confluence of two rivers.") is None


def test_drafter_prompt_forbids_traveller_clauses_and_sha_changed():
    prompt = pc._CARD_SYSTEM
    assert "Never end a background fact with a clause about travellers" in prompt
    assert "making it a stop / base / standard inclusion" in prompt
    assert "kind is one of: what_it_is, history, setting, naming." in prompt
    assert pc.PROMPT_SHA != "681e1e20ff92d7f3"  # pre-AA-808 value: stored misses are retried
