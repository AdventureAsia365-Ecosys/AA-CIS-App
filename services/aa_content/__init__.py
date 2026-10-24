"""AA-authored content (AA-977, CRM-B5): prep packs, starter templates and campaign blocks that AA staff write and
publish in CIS, served to the partner CRM over catalog.http.v1 ``/catalog/v1/aa-content``.

  validate.py   field rules shared by the admin API, the store and the seed (contract shape, AA countries, the
                prep-pack visa rule)
  store.py      admin writes and reads on gold_aa_internal.aa_content_item / aa_content_version (CIS 275)
  reader.py     the served reads over gold_aa_internal.v_aa_content_published (published versions only) and the
                served body / content_hash
"""
