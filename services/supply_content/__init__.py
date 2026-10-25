"""AA-835 (root ADR 0004a) — CIS supply content: the customer-facing description, highlights and photos of the hotels
and services Booking owns.

Flow (storage: CIS migration 278):
  pull.py     the consumer of the Booking supply feed (booking.v_supply_outbox + the v_supply_hotels / v_supply_services
              facts views, contracts supply.hotel.v1 / supply.service.v1). Runs in the api's ops sampler tick.
  writer.py   one LLM draft (stage supply_content_writer) from the facts, quoted as untrusted data.
  photos.py   copies the rights-cleared Booking photos into the CIS photo store + CDN.
  review.py   the human review: edit (a new human version), publish, reject, unpublish, regenerate.
  services/jobs/supply_content_draft_job.py runs writer + photos for one facts version.
Booking reads the published result only through catalog.v_supply_content (CIS 278).
"""
