# AA-1059 (S274): small islands refused as "outside_country_polygon"

## Decisions
- The S274 completeness audit found geocoder-confirmed island places stored as `unknown`
  (`last_error = outside_country_polygon`). Phi Phi Islands was one of them. The country polygons come from
  Natural Earth admin-0 1:10m, and that layer does not draw Koh Tao, Phi Phi or the Similan Islands at all.
  Building the file again cannot add them.
- So the guard changes: `shape.offshore_of(lat, lng, iso2)` accepts a point that lies inside no country's
  polygon and whose nearest coast, within `OFFSHORE_KM = 100` km, belongs to the claimed country.
  `guards.point_ok` uses it only after the 10 km polygon check fails.
- A point inside another country's polygon is still refused, so the AA-990 cases stay refused: Kilimanjaro as
  Kenya, Siem Reap as Thailand and Skardu as India.
- The polygon build (`scripts/aa847_country_polygons.py`) used to drop every ring that simplified to fewer than
  4 vertices. It now keeps such a ring unsimplified, with 4-decimal rounding only. With this change the
  Lakshadweep atolls (India) are inside India again.

## Changed
- `services/places/shape.py`:
  - `_border_km` is the distance version of `_near_border`.
  - `_bbox_index` is built once per process. Each country's rings are parsed only to compute its box.
  - New function `offshore_of`.
- `services/places/guards.py`: the offshore exception in `point_ok`.
- `scripts/aa847_country_polygons.py`: new helper `_keep_small`.
- Data files regenerated from Natural Earth v5.1.2 (same source and version):
  - `services/places/geodata/country_polygons.json` is now 3.64 MB.
  - `shared/countries/countries.json`: 24 boxes grow slightly. These are countries with small remote islands
    (GB/Rockall, CK, KI, UM, MV, ...). No box of a country we sell tours in changed.

## Tradeoffs
- 100 km covers the Similans (about 60 km offshore) and Koh Tao (about 40 km from Koh Phangan). A model or
  geocoder point at sea near the right country is accepted. That point was already confirmed by Geoapify,
  whose country must equal the model's, and it is still checked against the tour route (AA-997). The polygon
  guard exists to stop wrong-country answers on land, and that check is unchanged.
- `offshore_of` runs only for points that fail the polygon check (rare), so its cost does not matter: about
  0.05 s on the first call (box index) and milliseconds after that.

## Should know
- `geo.place_name` rows already marked `unknown` with `outside_country_polygon` / `unconfirmed_by_geocoder` are
  not retried automatically, because those reasons are final (AA-997). After the deploy, the orchestrator
  re-queues those few rows (S274 session work, not part of this PR).
