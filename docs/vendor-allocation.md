# Vendor, client, quantity and CPI operations

This feature is additive and is now part of the main application. Vendor allocation is enforced in project listing, copied-link validation, respondent initiation, callback finalization and legacy callback reconciliation.

## Account rules

- `EmployeeProfile.account_type` remains the source of truth for employee, internal-vendor and external-vendor identity.
- Only an account with `vendors.manage` may create vendor accounts.
- Every internal vendor is assigned the system `Admin` role automatically. With `respondents.create` it may create employee/respondent children only.
- Every external vendor is assigned the safe system `External Vendor` role automatically. It can receive individual allow/deny overrides for permitted business functions.
- An external vendor is always terminal and cannot create users or roles, even if an Admin role or management allow-override is assigned accidentally. Identity, role, client, allocation and synchronization management functions are removed at permission evaluation time as a second line of defense.
- External vendors are independent terminal accounts. They never belong to the internal organization hierarchy.

## Internal organization hierarchy

Internal operations use real relational units rather than profile text fields:

1. a super-admin office or internal vendor owns one or more `OrganizationUnit` Branch rows;
2. every Sub-branch belongs to exactly one Branch;
3. every Shift belongs to exactly one Sub-branch; and
4. Team Leads and Employees are assigned to a Shift through `EmployeeProfile.organization_unit`.

Multiple Team Leads may share a Shift. Each Team Lead can see lower-rank employee Studies and User Hits in that Shift. A Manager or Admin assigned higher in the tree sees lower-rank activity in descendant units. Internal-vendor roots can manage and audit their full organization tree. Existing unassigned profiles remain valid for backward compatibility.

Organization structure totals roll upward without copying access rows: a Shift's members and unique client grants are included in its Sub-branch and Branch totals. This keeps management summaries accurate while preventing a client assigned to one Shift from leaking into sibling Shifts.

`OrganizationClientAccess` assigns clients to any unit using nearest-level precedence. Direct Shift grants form that Shift's complete client set and override broader Sub-branch or Branch grants. A Shift without direct grants inherits the closest configured Sub-branch, then Branch; a configured Sub-branch likewise overrides its Branch. Main-office grants may use any active client. Internal-vendor grants must also exist in that vendor's active `VendorClientAllocation`; the unit rule can narrow vendor visibility but can never expand beyond owner-controlled allocations. Project listing, detail access and manually punched respondent links enforce the same client scope.

## Data hierarchy

1. `Client` identifies a buyer/source account.
2. `ClientIntegration` stores non-secret upstream connection metadata. It stores the environment-variable name for a credential, never the token.
3. `VendorCommercialProfile` stores a vendor's default CPI cut, currency and delivery mode (`panel`, `api` or `both`). Internal vendors are always panel-only with zero cut.
4. `VendorClientAllocation` makes a client eligible for project assignment and limits total completes across that client's allocated projects. It does not expose every client survey.
5. `VendorSurveyAllocation` is a mandatory project whitelist entry inside the parent client allocation. It controls project visibility, the per-project complete cap and an optional CPI-cut override. Without an active allocation, that project is absent from both panel and API responses and its respondent link is rejected.
6. `AllocationReservation` records the reserved, consumed, released or expired quantity associated with one survey attempt.

## CPI precedence and snapshot

For external vendors, cut precedence is survey override, client override, then vendor default. Internal vendors always receive a zero-percent cut. External-vendor project and tracking APIs do not expose source CPI; they return payable CPI and the applied cut. On reservation, `SurveyAttempt` freezes:

- vendor and client;
- client and survey allocation IDs;
- source CPI;
- applied cut percentage;
- payable CPI; and
- currency.

Changing the live survey CPI later cannot change an existing attempt snapshot.

Each new attempt resolves the current source CPI and current survey/client/vendor cut again. Consequently, a completed attempt at CPI 3 remains CPI 3 after an update, while later attempts use the newly published CPI and current configured cut.

## External delivery channels

An external vendor can be configured as Panel only, API only, or Panel + API. API-only vendors cannot establish or retain a browser session. Panel-only vendors cannot receive or use API keys.

Owner workspace users issue revocable keys from Vendor Management. A plaintext key is displayed exactly once; the database stores only an HMAC-SHA256 digest plus a masked prefix/suffix. Send the key as either:

```http
X-API-Key: exh_...
```

or:

```http
Authorization: Api-Key exh_...
```

The key authenticates as its external vendor. It does not carry a copied client list or copied CPI: every request applies that vendor's current function permissions, active client grants, explicit project allocations, quantities and per-client/project CPI cut. The same external vendor can therefore receive selected Client ABC projects at 30% cut and selected Client BCZ projects at 50% cut in both panel and API responses. `/api/v1/surveys/?client_name=ABC` filters the allocated client label.

## Quantity lifecycle

The reservation service locks the client row and mandatory project-allocation row in one database transaction. Capacity is available only when client remaining, project remaining and upstream survey remaining are all positive.

- Initiation: reserve one client unit and one project-allocation unit.
- Status `1`: move the reserved unit to consumed.
- Status `2`, `3` or `4`: release the reserved unit.
- Abandoned attempt: `vendors.expire_allocation_reservations` runs every `VENDOR_RESERVATION_CLEANUP_INTERVAL_SECONDS` and releases reservations older than `VENDOR_RESERVATION_TTL_MINUTES`.

Finalization is idempotent. Database check constraints prevent consumed or reserved counters from exceeding their limits.

## UAT API

All endpoints require function permissions and are documented in Swagger:

- `/api/v1/vendors/clients/`
- `/api/v1/vendors/integrations/`
- `/api/v1/vendors/commercial-profiles/`
- `/api/v1/vendors/api-keys/` (issue, list masked metadata, update label/expiry and revoke)
- `/api/v1/vendors/client-allocations/`
- `/api/v1/vendors/survey-allocations/`
- `/api/v1/vendors/reservations/` (read-only audit)
- `/api/v1/vendors/directory/` (vendor policy directory)
- `/api/v1/vendors/management-options/` (non-secret vendor/client selector data)
- `/api/v1/vendors/organization-units/` (Branch, Sub-branch and Shift CRUD)
- `/api/v1/vendors/organization-client-access/` (unit client visibility CRUD)
- `/api/v1/vendors/organization-options/` (scoped owner/client selector data)

The responsive `/vendors/` workspace uses separate modals for commercial policy, client allocation, project allocation and API-key operations. `/organization/` manages Branch, Sub-branch, Shift and inherited client routing without horizontal page overflow. User creation stays in the Access Control modal and selects a real organization unit, so account type, role, hierarchy and function-level allow/deny overrides have one source of truth.

Super admins and non-vendor management accounts see the full authorized dataset. Vendor accounts and respondents below an internal vendor are restricted to that vendor's allocations. Commercial policies, quantities and API keys remain owner-controlled and read-only for vendor-scoped accounts, even if a manage permission is assigned accidentally.

The first migrations map existing `company_name=InnovateMR` surveys to a seeded InnovateMR client without changing survey IDs, source CPI or respondent flow. Every later InnovateMR inventory sync applies the same client mapping, and its closed-survey pass cannot close inventory belonging to a future provider.
