---
status: accepted
---

# Jev Verdicts may act, but only when enforced, confident and calibrated

Ms. Thư's reference repo (`aa-soscial-media`, ADR 0023 there) lets Jev only flag, never block. We decided the opposite for the single clean rerun (Nghiệp, S203, 29–30/09/2026): a Jev Question in **enforce** Mode may Act — exclude a keyword from a DataForSEO purchase, a question or landing from Score, a row from ingest, or send unpublished content back for repair — because a flag nobody reads leaves the same bad data in the rerun we are paying for once. Three limits keep it from being the veto her ADR guards against: it acts only in the accept or reject Zone (grey and error keep the Stage's existing rule, and Jev failing never blocks), only after a Calibration Record reviewed by a person shows ≥ 95% precision at the Floor (a DB check refuses enforce without one), and never deletes data or unpublishes published content; every Verdict is kept in `shared.decision_log`, so a Floor can move without asking Jev again.

## Consequences

- Each Floor sits on the side of the costlier mistake: low where a false reject is invisible afterwards (a keyword never bought, a landing never counted), higher where a false accept is a vote in Score.
- Tenant content is sent to TypeSafe only for tenants on `shared.jev_tenant_allowlist` (AA test tenants) until a DPA/ZDR with TypeSafe is in place.
- Implements root ADR 0001 decision 2 (Model Gateway) for the `decide()` seam.
