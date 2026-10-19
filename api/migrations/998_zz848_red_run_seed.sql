-- THROWAWAY (never merge, never copy to AA-CIS-Infra): seeds one violation per rule of the X2 RLS catalogue
-- guard, so the CIS Integration Tests job goes red on test_rls_catalogue_guard. Evidence for the guard's
-- Done-when; the branch is deleted after the run.
BEGIN;
CREATE SCHEMA IF NOT EXISTS zz848_red;
-- a: tenant_id table without RLS
CREATE TABLE zz848_red.a_no_rls (id int PRIMARY KEY, tenant_id uuid NOT NULL);
-- b: policy TO PUBLIC
CREATE TABLE zz848_red.b_public (id int PRIMARY KEY);
ALTER TABLE zz848_red.b_public ENABLE ROW LEVEL SECURITY;
CREATE POLICY b_public_open ON zz848_red.b_public USING (id > 0);
-- c: app.current_tenant
CREATE TABLE zz848_red.c_old_guc (id int PRIMARY KEY, tenant_id uuid);
ALTER TABLE zz848_red.c_old_guc ENABLE ROW LEVEL SECURITY;
CREATE POLICY c_old_guc_tenant ON zz848_red.c_old_guc TO aa_app_user
    USING (tenant_id::text = current_setting('app.current_tenant', true));
-- d: current_setting('app.tenant_id') without NULLIF
CREATE TABLE zz848_red.d_no_nullif (id int PRIMARY KEY, tenant_id uuid);
ALTER TABLE zz848_red.d_no_nullif ENABLE ROW LEVEL SECURITY;
CREATE POLICY d_no_nullif_tenant ON zz848_red.d_no_nullif TO aa_app_user
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- e: foreign key across owners (booking -> CIS)
CREATE SCHEMA IF NOT EXISTS booking;
CREATE TABLE booking.zz848_red_e (id int PRIMARY KEY, tenant_id uuid REFERENCES shared.tenants (tenant_id));
-- p: grant to PUBLIC
CREATE TABLE shared.zz848_red_p (id int PRIMARY KEY);
GRANT SELECT ON shared.zz848_red_p TO PUBLIC;
-- g: grant to another owner's role
CREATE TABLE shared.zz848_red_g (id int PRIMARY KEY);
GRANT SELECT ON shared.zz848_red_g TO booking;
-- t: unconditional true policy outside the X1 parity set
CREATE TABLE zz848_red.t_true (id int PRIMARY KEY);
ALTER TABLE zz848_red.t_true ENABLE ROW LEVEL SECURITY;
CREATE POLICY t_true_all ON zz848_red.t_true TO aa_app_user USING (true);
COMMIT;
