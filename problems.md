# Known Problems & Issues

## 1. Standard `ServiceRequest` object has no migration mapping

**Salesforce Object:** `ServiceRequest` (Standard Object)

Salesforce has a standard object called `ServiceRequest` (API name: `ServiceRequest`), but there is no mapping file for it in the migration tool. Currently, ServiceNow `sc_request` data is mapped to `Case` via `InsertCase.json`, and `sc_req_item` data is mapped to `SvcCatalogRequest` via `ServiceRequest.json`. The `ServiceRequest` Salesforce object is completely unused. If the ITSM setup requires this object to be populated, a new mapping file needs to be created.

---

## 2. `SvcCatalogReqExtdAttrVal` object not available in Salesforce org

**Salesforce Object:** `SvcCatalogReqExtdAttrVal` (Service Catalog Request Extended Attribute Value)

The migration UI shows `SvcCatalogReqExtdAttrVal` as a selectable object, but this object does not exist in the current Salesforce org. It is part of the Salesforce ITSM managed package / Service Catalog feature, which may not be installed or enabled. Selecting this object for migration will fail. Either skip it during migration or install the required managed package in Salesforce.

---

## 3. Standard Salesforce / ITSM objects with no migration mapping

The migration tool has mapping files for 22 unique Salesforce destination objects. Several common standard or ITSM objects that may exist in a Salesforce org are **not** targeted by any mapping file.

| Salesforce Object | API Name | Status in migration tool | Notes |
|---|---|---|---|
| Service Request | `ServiceRequest` | No mapping | See issue #1. `sc_request` is mapped to `Case` instead via `InsertCase.json`. |
| Knowledge Article | `Knowledge__kav` | No mapping | ServiceNow knowledge base (`kb_knowledge`) is not covered. |
| Asset | `Asset` | No mapping | ServiceNow CMDB configuration items are not migrated to Salesforce Assets. |
| CMDB / Configuration Item | Various CMDB objects | No mapping | Only `Service__c` and `Service_Offering__c` (custom) are mapped from `cmdb_ci_service` / `service_offering`. Broader CMDB data is not covered. |

**Objects that ARE mapped (for reference):**

| Category | Salesforce Object | API Name | Mapping file(s) |
|---|---|---|---|
| Core ITSM | Incident | `Incident` | ParentIncident.json, ChildIncident.json |
| Core ITSM | Problem | `Problem` | Problem.json, UpdateProblemFinal.json |
| Core ITSM | Change Request | `ChangeRequest` | ChangeRequest.json, UpdateChangeRequestFinal.json |
| Core ITSM | Case | `Case` | InsertCase.json, UpdateCaseForServiceRequestId.json |
| Core ITSM | Service Catalog Request | `SvcCatalogRequest` | ServiceRequest.json, UpdateServiceRequestFinal.json |
| Core ITSM | Problem–Incident link | `ProblemIncident` | RelatedProblemIncident.json |
| Core ITSM | Change–Incident link | `ChangeRequestRelatedIssue` | RelatedCRIncidentByCause.json, RelatedCRIncidentByRFC.json |
| Service Catalog | Extended attribute values | `SvcCatalogReqExtdAttrVal` | SvcCatalogReqExtdAttrVal.json (see issue #2 if object missing in org) |
| Communication | Email Message | `EmailMessage` | EmailMessage.json, EmailMessageSR.json, ArchivedEmailMessage.json, ArchivedEmailMessageSR.json |
| Communication | Chatter / Feed | `FeedItem` | ChatterNotes.json, ChatterNotesSR.json |
| Attachments | Files | `ContentVersion` | AttachmentIncidents.json, AttachmentProblem.json, AttachmentChangeRequest.json, AttachmentServiceRequest.json |
| Users & org | User | `User` | AdminUser.json, CommunityUser.json, UpdateUserForManager.json, UpdateUserForDept.json, UpdateAdminUserFinal.json |
| Users & org | Account | `Account` | Account.json |
| Users & org | Group / Group Member | `Group`, `GroupMember` | Group.json, GroupMember.json |
| Users & org | Queue membership | `QueueSobject` | QueueCase.json, QueueIncident.json, QueueProblem.json, QueueChangeRequest.json, QueueSvcCatalogRequest.json, QueueCaseServiceProcess.json |
| Location | Location | `Location` | ParentLocation.json, ChildLocation.json, ParentLocationUpdateForVisitorAddressId.json |
| Location | Address | `Address` | Address.json |
| Location | Internal org unit | `InternalOrganizationUnit` | Company.json |
| Custom (must exist in org) | Department | `Department__c` | Department.json |
| Custom (must exist in org) | Service | `Service__c` | Service.json |
| Custom (must exist in org) | Service Offering | `Service_Offering__c` | ServiceOffering.json |
| Custom (must exist in org) | Approval history | `ServiceNow_Approval_History__c` | ApprovalHistoryCR.json, ApprovalHistorySR.json |

**Action needed:** Confirm with your Salesforce ITSM design whether unmapped objects (`ServiceRequest`, Knowledge, Asset, CMDB) are required. If yes, new mapping JSON files must be added. If not, they can be ignored during migration selection.

---

## 4. [FIXED] Problem, InsertCase, and ServiceRequest loaded 0 records — wrong operation type

**Root cause:** `Problem.json`, `InsertCase.json`, and `ServiceRequest.json` all had `"operation": "update"` instead of `"upsert"`. The transformation service's `transform_batch()` function skips every record when `operation == "update"` and no Salesforce ID mapping exists yet (which is the case on a first migration).

**How it manifested:**
- Transformation phase ran but produced 0 output records (no CSV files created)
- Loading phase found no transformation directory, so it loaded 0 records silently
- `SvcCatalogReqExtdAttrVal` (132 records) failed with `REQUIRED_FIELD_MISSING` because it depends on `SvcCatalogRequestId` lookups from ServiceRequest records that were never inserted

**Comparison with working objects:**
| Object | Operation (before fix) | Result |
|---|---|---|
| Group | `insert` | Loaded successfully |
| Account | `insert` | Loaded successfully |
| ChangeRequest | `upsert` | Loaded successfully (75 records) |
| Problem | `update` | **0 records** — all skipped |
| InsertCase | `update` | **0 records** — all skipped |
| ServiceRequest | `update` | **0 records** — all skipped |

**Fix applied:** Changed `"operation"` from `"update"` to `"upsert"` in all three mapping files. With `"upsert"`:
- First run (no existing Salesforce data): records get **inserted**
- Re-runs (Salesforce IDs exist in id_mappings): records get **updated**

**Files changed:**
- `data/mappings/field_mappings/Problem.json`
- `data/mappings/field_mappings/InsertCase.json`
- `data/mappings/field_mappings/ServiceRequest.json`

---

## 5. [FIXED] InsertCase produced 0 records — missing Salesforce Account extraction

**Root cause:** `InsertCase.json` maps `ContactId` using `look_up_sys_id_for_contact`, which reads Salesforce Account data (`PersonContactId`) from `data/downloads/salesforce/Account`. But the `salesforce_objects` array in `InsertCase.json` only listed `Case`, not `Account`. The Account CSV was never downloaded from Salesforce, so every row threw `FileNotFoundError` and was skipped.

**Fix applied:** Added `Account` with fields `["Id", "PersonContactId"]` to `InsertCase.json`'s `salesforce_objects` array.

**File changed:** `data/mappings/field_mappings/InsertCase.json`

---

## 6. [FIXED] Stale execution cache between migration runs

**Root cause:** The `execution_cache` singleton persists for the entire server lifetime. Lookup functions like `look_up_service_request_sys_id` cache their ID mapping queries on first call. If a previous migration run cached stale (empty) data, subsequent runs reuse it even though new records have been loaded to Salesforce.

**How it manifested:** After fixing issue #4, ServiceRequest loaded 821 records on the second run. But `SvcCatalogReqExtdAttrVal` still failed because the cache had an empty `"ID_MAPPING::SERVICEREQUEST"` dict from the first run.

**Fix applied:** Added `execution_cache.clear()` at the start of each migration run in `migration_manager.py`'s `_do_run()`.

**File changed:** `api/migration_manager.py`

---

## 7. [FIXED] Problem: 9 records failed — missing `ResolvedById` on Resolved/Closed status

**Salesforce validation:** Salesforce requires `ResolvedById` to be populated when a Problem has status "Resolved" or "Closed".

**Root cause:** 9 ServiceNow Problem records have "Resolved" or "Closed" status but their `resolved_by` and `closed_by` fields are both empty (or the referenced user was not found in Salesforce). The `look_up_resolved_by_for_problem` function returned empty, and Salesforce rejected the record.

**Fix applied:**
- Changed `look_up_resolved_by_for_problem` to accept a variable number of fallback fields using `*candidate_sys_ids`. It tries each candidate until one resolves to a valid Salesforce user.
- Updated `Problem.json` to pass 4 fields in fallback order: `resolved_by` → `closed_by` → `assigned_to` → `opened_by`
- This is generic — works for any org where resolver data may be incomplete.

**Files changed:**
- `helpers/transformation_helper.py` — `look_up_resolved_by_for_problem` function
- `data/mappings/field_mappings/Problem.json` — `ResolvedById` source fields

---

## 8. [FIXED] SvcCatalogReqExtdAttrVal: 132 records failed — incomplete `look_up_variable` hardcoded dictionary

**Root cause:** The `look_up_variable` function in `transformation_helper.py` used a **hardcoded dictionary** mapping ServiceNow `item_option_new` sys_ids to Salesforce `SvcCatalogItemAttribute` IDs. Records whose `item_option_new` sys_id was not in the dictionary got an empty `SvcCatalogItemAttributeId`, which Salesforce rejected.

**Fix applied:**
- Added `SvcCatalogItemAttribute` (with fields `Id`, `Label`, `DeveloperName`) to `SvcCatalogReqExtdAttrVal.json`'s `salesforce_objects` so it gets extracted from Salesforce before transformation.
- Created `_build_variable_lookup()` function that dynamically matches ServiceNow `item_option_new.name` to Salesforce `SvcCatalogItemAttribute.Label` (or `DeveloperName` as fallback).
- The dynamic mapping is merged with the hardcoded dictionary (dynamic values take priority), so existing manual entries are preserved as a safety net.
- Result is cached via `execution_cache` for performance.
- This is generic — works for any org without needing to maintain a hardcoded dictionary.

**Files changed:**
- `helpers/transformation_helper.py` — `_build_variable_lookup()` + refactored `look_up_variable`
- `data/mappings/field_mappings/SvcCatalogReqExtdAttrVal.json` — added `SvcCatalogItemAttribute` to `salesforce_objects`

---

## 9. [FIXED] Problem: 9 records still failed — fallback queried empty User ID mapping tables

**Root cause:** The fix in issue #7 added a fallback that queried `execution_cache` for `ID_MAPPING::USER`, which fetches from `sys_user_User_AdminUser` and `sys_user_User_CommunityUser` ID mapping tables. However, when AdminUser/CommunityUser are **not selected** in the migration, these tables are empty — so the fallback returned 0 mappings and `ResolvedById` remained empty. Salesforce rejected the records.

**Debug evidence (runtime logs):**
- All 16 function calls (9 unique records, some retried) showed `"mappings_count": 0, "has_mappings": false` — confirming the User ID mapping tables were completely empty.
- Every `look_up_user_sys_id` call returned `""` for all candidate sys_ids (`resolved_by`, `closed_by`, `assigned_to`, `opened_by`) — the users exist in ServiceNow but were never migrated to Salesforce.

**Fix applied:**
- Replaced the empty-table lookup with a direct Salesforce query: `SELECT Id FROM User`.
- This fetches an existing Salesforce User ID directly from the org, guaranteeing a valid fallback regardless of which migration objects are selected.
- The result is cached via `execution_cache` under key `FALLBACK_SF_USER_ID`, so only one Salesforce API call is made per migration run.

**Data quality note:** The fallback assigns an arbitrary Salesforce User as the resolver. This is a trade-off: the record migrates successfully (no data loss) but `ResolvedById` may not match the original ServiceNow resolver. For production migrations, include AdminUser/CommunityUser in the selected objects so user lookups resolve correctly and the fallback is rarely needed.

**Files changed:**
- `helpers/transformation_helper.py` — `look_up_resolved_by_for_problem` fallback logic