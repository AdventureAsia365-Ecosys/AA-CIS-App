"""AA-712 — the place pipeline (ADR 0007): day skeleton, places, geocoding and the tour graph of every
live tour, computed by CIS job kinds (place_extract, place_geocode, tour_graph_build) into schema
`geo` (migration 226). Ported from AA-TripPlanner-Web backend/extraction/* @ 6523816.

Pure modules: names, days, components, guards, graph, locate. I/O: geocoder (external geocoder
adapters), store (SQL), llm (gateway calls + cost log), enqueue (job triggers).
"""
