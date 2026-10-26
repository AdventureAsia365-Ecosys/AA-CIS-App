"""AA-919 (WM-17) — the eval golden set as DB rows (CIS 284): derivation (derive), storage (store), the importer CLI
(importer), the LLM-call guard of the importer process (llm_guard) and the no-tenant-text CI guard (text_guard).

Kept import-light on purpose: text_guard runs in the CI lint job, which installs no app dependency.
"""
