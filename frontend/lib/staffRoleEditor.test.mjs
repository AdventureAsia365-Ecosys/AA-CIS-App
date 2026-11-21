// AA-1155 (spec tasks T13 / T14): the pure helpers of the Staff page's CIS role editor and preset editor. The backend
// re-checks every write (tests/unit/test_aa1155_role_editor.py, test_aa1155_staff_presets.py). Invented codes only.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  CIS_ROLE_EDITOR_PATH, CUSTOM_ROLE_CODE_RE, PRESET_CODE_RE, PRESET_FIELDS, assignableByArea, blankPreset,
  cloneCapabilities, presetCreateBody, presetEditBody, presetFormProblem, roleEditBody, roleFormOk,
} from "./staffRoleEditor.ts";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const backend = (p) => readFileSync(join(FRONTEND, "..", p), "utf8");

const VIEW = {
  builtin: [
    { code: "admin", label: "Administrator", capabilities: ["content.read", "review.approve", "staff.read"],
      assignable: true, clonable: false },
    { code: "content", label: "Content Team", capabilities: ["content.read", "content.write", "review.approve"],
      assignable: true, clonable: true },
  ],
  custom: [],
  custom_available: true,
  capabilities: [
    { name: "content.write", assignable: true }, { name: "content.read", assignable: true },
    { name: "review.approve", assignable: false }, { name: "staff.read", assignable: true },
  ],
  can_edit: true,
};

test("capabilities offered: only the assignable ones, grouped by area", () => {
  assert.deepEqual(assignableByArea(VIEW), [["content", ["content.read", "content.write"]], ["staff", ["staff.read"]]]);
  assert.deepEqual(assignableByArea(undefined), []);
});

test("a new role starts from the built-in one without its approvals", () => {
  assert.deepEqual(cloneCapabilities(VIEW, "content"), ["content.read", "content.write"]);
  assert.deepEqual(cloneCapabilities(VIEW, "nope"), []);
});

test("a role edit sends only what changed", () => {
  const before = { code: "custom_zz_desk", label: "Zz desk", based_on: "content", archived: false,
    capabilities: ["content.read"], holders: 0 };
  assert.deepEqual(roleEditBody(before, " Zz desk ", ["content.read"]), {});
  assert.deepEqual(roleEditBody(before, "Zz front desk", ["content.write", "content.read", "content.read"]),
    { label: "Zz front desk", capabilities: ["content.read", "content.write"] });
});

test("a role form needs a custom code, a name and one capability", () => {
  assert.ok(roleFormOk({ code: "custom_zz_desk", label: "Zz", capabilities: ["content.read"] }));
  assert.ok(!roleFormOk({ code: "zz_desk", label: "Zz", capabilities: ["content.read"] }));
  assert.ok(!roleFormOk({ code: "custom_zz_desk", label: "  ", capabilities: ["content.read"] }));
  assert.ok(!roleFormOk({ label: "Zz", capabilities: [] }));
  assert.ok(roleFormOk({ label: "Zz", capabilities: ["staff.read"] }));    // an edit has no code
});

test("a preset form is normalized and checked like the backend", () => {
  const f = { ...blankPreset(), code: "zz_desk", label: " Zz desk ", cis_role: "support", booking_mode: "keep",
    booking_role: "sales", crm_mode: "keep", crm_roles: ["ops"], cis_keep_roles: ["admin"] };
  assert.equal(presetFormProblem(f, true), null);
  const body = presetCreateBody(f);
  assert.equal(body.label, "Zz desk");
  assert.equal(body.booking_role, null);       // only with booking_mode set
  assert.deepEqual(body.crm_roles, []);       // only when the CRM roles are not kept
  assert.deepEqual(body.cis_keep_roles, []);  // only on an additive-only preset
  assert.equal(presetFormProblem({ ...f, code: "Zz" }, true), "code");
  assert.equal(presetFormProblem({ ...f, code: "Zz" }, false), null);
  assert.equal(presetFormProblem({ ...f, booking_mode: "set", booking_role: null }, true), "bookingRole");
  assert.equal(presetFormProblem({ ...f, crm_mode: "add", crm_roles: [] }, true), "crmRoles");
  assert.equal(presetFormProblem({ ...f, additive_only: true, booking_mode: "clear" }, true), "additive");
  assert.equal(presetFormProblem({ ...f, cis_role: null }, true), "empty");
});

test("a preset edit sends only the fields that changed, never an owner flag", () => {
  const before = { ...blankPreset(30), code: "zz_desk", label: "Zz desk", cis_role: "support" };
  assert.deepEqual(presetEditBody(before, { ...before }), {});
  assert.deepEqual(presetEditBody(before, { ...before, label: "Zz help desk", crm_mode: "add", crm_roles: ["ops"] }),
    { label: "Zz help desk", crm_mode: "add", crm_roles: ["ops"] });
  assert.ok(!PRESET_FIELDS.some((f) => f.includes("owner")));
});

test("the editor shapes match the backend routes and patterns", () => {
  assert.equal(CIS_ROLE_EDITOR_PATH, "/api/admin/platform/roles/cis");
  const roles = backend("api/core/staff_roles.py");
  const presets = backend("api/services/staff_presets.py");
  assert.ok(roles.includes(CUSTOM_ROLE_CODE_RE.source), "custom role pattern");
  assert.ok(presets.includes(`PRESET_CODE_RE = re.compile(r"${PRESET_CODE_RE.source}")`), "preset pattern");
  const editable = presets.match(/EDITABLE: tuple\[str, \.\.\.\] = \(([^)]*)\)/)[1].match(/"(\w+)"/g)
    .map((s) => s.replaceAll('"', ""));
  assert.deepEqual(editable, [...PRESET_FIELDS]);
});
