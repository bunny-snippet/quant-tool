# Architecture

The vendor/client allocation and internal organization model is documented in `vendor-allocation.md`. It adds client ownership, commercial policy, quantity allocation, Branch → Sub-branch → Shift routing and immutable attempt CPI snapshots without replacing the existing survey synchronization or respondent tracking tables.

## Boundary and responsibilities

The browser talks only to same-origin Django endpoints. Django owns the InnovateMR token, outbound calls, normalization, merge decisions, IDs, persistence, filtering, and OpenAPI schema. Celery workers reuse the same service layer as the manual management command and internal sync endpoint.

```mermaid
flowchart LR
    B[Responsive Projects UI] -->|same-origin JSON| A[Django REST API]
    A --> DB[(Survey database)]
    C[Celery Beat every 60s] --> W[Celery worker]
    W --> S[Survey sync service]
    S --> F[Allocated surveys full]
    S --> P[Allocated surveys paged]
    S --> DB
    W --> D[Bounded detail refresh]
    D --> Q[Quota endpoint]
    D --> T[Targeting endpoint]
    D --> DB
```

## Data model

### `Survey`

One row per client/provider survey. `source_key` is the canonical string provider identifier and is unique together with its integration; `source_id` remains a nullable numeric compatibility field for InnovateMR and other numeric providers. `local_id` is an immutable, indexed 14-digit public identifier. `buyer_id` normalizes the provider's buyer/sub-client identifier. `survey_type` normalizes known `groupType` values (`Consumer`/`B2C` to `B2C`, `Business`/`B2B` to `B2B`) while preserving the original value in `group_type`. Core inventory fields are normalized for filters and reporting while `raw_data` preserves the complete upstream payload for future fields and debugging.

`source_created_at` and `source_modified_at` retain upstream timestamps. `created_at` and `updated_at` are database audit timestamps. `last_seen_at` records inventory presence. A missing survey is marked `closed`, not deleted.

### `SurveyQuota`

Normalized quota metrics plus the original per-quota targeting JSON. Replacement is transactional: readers see either the previous complete set or the new complete set.

### `TargetingQuestion`

Question metadata and the upstream `Options` list. The options list intentionally remains JSON because age ranges, text answers, ZIP codes, and lookup-backed options are structurally different.

### `SyncRun`

Immutable operational history with endpoint counts, merged total, create/update/unchanged/closed counters, detail failures, status, timing and sanitized error text.

### `SurveyAttempt`

One record per respondent journey. RID is a random 10-character identifier and is supplied as both InnovateMR PID and `trackId`. The row connects survey and user ID, captures pre-screening answers, supplier code derived from the allocated entry link, initiation/submission/redirect/callback timestamps, entry and exit IPs, entry/exit browser-device-OS-user-agent snapshots, safe client hints, callback count, terminal status and measured LOI. Browser callbacks are unverified until a trusted notification or hash confirms them. `source_cpi_snapshot` remains immutable after entry and `cpi_snapshot_source` records whether it was captured live or recovered during the legacy backfill. Revenue sums completed snapshots, so later inventory CPI changes cannot rewrite historical revenue.

Employee roles have a configurable `cpi_visibility_percent` (default 100). It changes only what that role can see in Projects, Studies and exports; it does not mutate source CPI, historical revenue storage, or vendor commercial allocations.

The responsive Studies UI deliberately renders a compact operational subset. Its filtered CSV stream joins attempt, survey, platform-user, employee-profile and role context and includes the full audit payload. Streaming iteration keeps large exports memory-bounded, while CSV formula escaping prevents spreadsheet formula injection.

The User Hits report derives its metrics from these immutable attempt rows. Each result represents one platform user and one IST calendar date. All attempts initiated on that date count as hits; status `1` attempts count as completes. Desktop, mobile and tablet use the captured entry-device classification, while missing legacy device audit data remains explicitly unclassified. Branch, Sub-branch and Shift resolve from `EmployeeProfile.organization_unit`; stable text-field fallbacks remain for legacy unassigned profiles.

### `LocalIdSequence`

One locked counter per `YYYYMM` prefix. New survey creation increments the counter in a database transaction and formats it as eight digits. InnovateMR updates never change this project ID.

## Merge rule

Both inventory sources are flattened into a map keyed by integer `surveyId`. For duplicate IDs, parsed `modifiedDate` is compared; if equal, the later source in the merge call wins. The service passes full inventory first and paged inventory second, making paged data the tie-breaker. An unparseable modified date falls back to `createdDate`, then the oldest UTC value.

The local row is updated when the incoming timestamp is newer, raw payload differs, or a closed survey becomes live again. Otherwise only `last_seen_at` changes.

## Scale decisions

Inventory ingestion never waits for two detail calls per survey. This matters for large initial imports. Detail refresh is a separately bounded minute job and is also demand-driven from the two row actions. Change detection uses `detail_synced_at < source_modified_at`.

For production volume, use PostgreSQL and Redis, run at least one dedicated worker for inventory and another for detail tasks, and monitor failed/retried Celery tasks plus `SyncRun.status`.

## Security

- Generic provider tokens are encrypted at rest or resolved from process environment and sent only to their configured HTTPS endpoint. RFG APID/secret values remain in environment variables and requests use timestamped HMAC-SHA1 authentication.
- It is never serialized, rendered, logged intentionally, or stored in survey payloads.
- Raw upstream errors are kept server-side.
- Organization UI, component controls and REST actions use function-level permissions; querysets are additionally restricted to the current super-admin or internal-vendor workspace.
