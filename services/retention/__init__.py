"""AA-825 — retention of CIS log tables (S237 decision E3, data standards DM-7 / C5.4).

  policy.py   which table, which window, which rows, archive or not (the DM-7 retention table)
  rollups.py  shared.llm_call_daily / shared.tenant_api_usage_monthly, written before raw rows go
  archive.py  one gzipped JSONL object per batch in s3://aa-cis-bronze-005097885195/archive/<table>/
  runner.py   count candidates, then batched keyset DELETE (archive first where the policy says so)

The job kinds that drive these live in services/jobs/retention_job.py.
"""
