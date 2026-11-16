# AA-963 / AA-970 lane l11 — one agency's value of a CRM setting from Platform › Settings (S282)

Enabler for two CRM cards: global search (AA-963, `crm.search.enabled`) and the release preview (AA-970,
`crm.release.preview`). Both switches are CRM `platform+tenant` keys, off by default, meant to be switched on for ONE
agency at a time (the pilot first). Before this lane the CIS admin page could change a CRM key at platform scope only
(`scopeOptions` returned `["platform"]` for `app = "crm"`), so saving the dialog would have switched the feature on for
every agency. The CRM API already took an agency value (crm.ops.v1 `POST /settings/{key}/changes` with `tenant_id`) and
the CIS relay (`api/services/crm_settings_relay.propose`) already forwarded `tenant_id`: this lane is frontend only.

## Decisions

- **`scopeOptions` offers Platform / One tenant for every `platform+tenant` key, CRM included.** `tenantScoped` (the
  CIS tenant picker) stays CIS / TripPlanner only (`app !== "crm"`); `agencyScoped` selects the agency branch of the
  form. A new `hasScopeChoice` replaces `tenantScoped` in the row's Change button: an open platform proposal no longer
  disables Change on a CRM agency key, because a change for another agency is still allowed (one open proposal per key
  and scope, as before).
- **The agency picker reads the CRM agency list** through the existing relay the Platform › Tenants › Agencies console
  uses (`GET /api/admin/platform/crm/agencies`, keyset pages, 200 per page = the relay's largest page), every page
  until the CRM answers no next cursor (a repeated cursor stops the loop). Offboarded agencies are left out (their data
  is gone); a locked or offboarding agency is listed with its state. CIS tenants are never offered: an agency is not a
  CIS tenant row.
- **"Feature switch off by default" is read from the definition**, not a key list: `agencyFeatureSwitch(d)` = CRM
  agency key, `value_type = bool`, `default_value = false`. Today that is exactly `crm.search.enabled` and
  `crm.release.preview`; a later switch of the same shape gets the same guard without a code change.
- **Such a switch opens on One agency with no agency picked** (`initialScope`), so nothing can be sent until an agency
  is chosen (`scopeReady`). Picking Platform shows a warning (`ScopeWarning.tsx`): a platform value reaches every
  agency without its own value, the pilot included. The warning shows whenever Platform is picked (not only when the
  new value is on), because the platform value of such a switch is what every other agency inherits.
- **The value the form starts from, "In force now" and `base_version` follow the agency** (`scopeTarget`): the agency's
  own value and version (the relay's `tenant_overrides`), else the platform value it inherits and version 0. That is
  the CRM's optimistic lock: `_propose` compares `base_version` with the agency scope's current version (0 = none).
- **Maker-checker and self-approval are unchanged**: the same `ApplyChoice`, the same `proposalBody` (adds
  `tenant_id`), and `decisionBody` already sent the agency id for Approve / Reject of an agency row.

## Changed

- `frontend/lib/settingsRegistry.ts`: `scopeOptions` (CRM included), `tenantScoped` (CIS/TP only), new
  `agencyFeatureSwitch`, `hasScopeChoice`, `initialScope`, `platformSwitchWarning`, `agencyOptions`, `AGENCIES_API`,
  `agenciesPath`, `allAgencies`.
- `frontend/app/admin/platform/settings/SettingScope.tsx`: `AgencyScopeFields` (Platform / One agency, agency Select,
  loading / error / empty lines, the warning) and `ScopeCurrent` split into tenant and agency lines (the agency named
  from the CRM list, else the names the relay gave, else its id).
- `SettingPropose.tsx`: the form opens on `initialScope(item)`.
- `SettingsRegistry.tsx`: the Change button uses `hasScopeChoice`.
- New `useCrmAgencies.ts` (one cached read) and `ScopeWarning.tsx` (imports the kit core directly so a render test can
  load it).
- `messages.ts`: EN/VI strings for the agency scope and the warning.
- `CONTEXT.md`: the CRM tab now authors agency values.
- Tests: `lib/settingsRegistry.test.mjs` (scope options, initial scope, warning rule, agency scope target, request
  body with `tenant_id` and the agency's `base_version`, decision body, agency options, paging, BFF read rule);
  new `lib/settingsScopeWarning.test.mjs` (renders the warning with react-dom/server, EN and VI, and no warning for
  One agency or other keys).

## Tradeoffs

- **The agency list is read in full, not searched.** Dev has a handful of synthetic agencies; a plain Select is the
  kit pattern the CRM tab already uses for the history scope. A searchable picker can come when the CRM has many.
- **No backend change.** The relay forwards `tenant_id` already and the CRM checks the group (`crm-platform-admin`
  writes), the key's scope (`422 not_tenant_scoped`) and an env override (`409 env_override_active`); those refusals
  reach the dialog as the CRM's message.
- **No new setting.** The page size 200 is the relay's own `le=200` page limit, not a business threshold.

## Should know

- Live check after deploy (orchestrator): Platform › Settings › CRM tab, `crm.search.enabled` → Change opens with
  "Applies to: One agency" and no agency picked (Submit disabled); pick the pilot agency → "In force now for <name>:
  off (no value of its own yet…)"; switch to Platform → the amber warning. Screenshots desktop + phone, light + dark.
- The agency list request needs the same CRM read access as the CRM tab itself (the staff member's own Cognito
  token); without it the picker shows "The CRM agencies could not be read: …" and Platform stays available.
