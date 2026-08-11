# Synchronization runbook

Client integrations are isolated by `(integration, source_key)`. Beat checks due integrations every 30 seconds. Active InnovateMR integrations synchronize every 150 seconds; verified active Research For Good integrations synchronize every 60 seconds. Hidden BioBrain integrations are skipped until their deployment has `BIOBRAIN_API_KEY`; after the first non-empty successful sync they become active and visible. Other providers continue to use their configured scheduled interval. The Projects page silently reloads normalized database inventory every 30 seconds, so users do not need to press Sync to see background updates. See [client integrations](client-integrations.md).

## Upstream endpoints

- `GET /supply/getAllocatedSurveys`
- `GET /supply/getAllocatedSurveysPaged?limit=100&next={cursor}`
- `GET /supply/getQuotaForSurvey/{surveyId}`
- `GET /supply/getSurveyTargeting/{surveyId}`

All calls use `x-access-token` and a configurable timeout. Pagination stops when no next cursor is returned, a cursor repeats, or `INNOVATEMR_MAX_PAGES` is exceeded.

## Inventory lifecycle

1. Create a running `SyncRun`.
2. Fetch the full endpoint.
3. Walk every paged endpoint cursor.
4. Merge by `surveyId` and newest `modifiedDate`.
5. Upsert normalized rows in one database transaction.
6. Mark previously-live rows absent from a completely successful merged response as closed.
7. Finish the audit run.

If either inventory source fails, the sync fails before closure logic, so a partial upstream response cannot incorrectly close surveys.

## Detail lifecycle

The minute detail job selects live surveys where quota or targeting has never synchronized or is older than `source_modified_at`. It processes at most `INNOVATEMR_DETAIL_REFRESH_BATCH` rows. Quota and targeting are replaced independently. A 404 means that detail type has no data and is stored as an empty successful result, so a missing quota cannot block targeting questions.

The UI's Quota and Pre-screening actions apply the same stale test. When refresh fails but a prior cache exists, the cache is served. When no cache has ever existed, the API returns an error instead of presenting an empty result as authoritative.

## Operational checks

- `/api/v1/sync-runs/?status=failed` shows inventory failures.
- `vendors.expire_allocation_reservations` releases abandoned vendor starts. Its interval and reservation TTL are independently controlled by `VENDOR_RESERVATION_CLEANUP_INTERVAL_SECONDS` and `VENDOR_RESERVATION_TTL_MINUTES`.

## Legacy redirect reconciliation

`surveys.reconcile_pending_attempts` runs every `INNOVATEMR_ATTEMPT_RECONCILE_INTERVAL_SECONDS` (60 seconds by default). It checks a bounded round-robin batch of recent attempts that reached InnovateMR but have no callback. Configure batch size with `INNOVATEMR_ATTEMPT_RECONCILE_BATCH` and the retry window with `INNOVATEMR_ATTEMPT_RECONCILE_LOOKBACK_HOURS`.

This is a compatibility fallback for old client redirect URLs. Once account-level or survey-specific redirect URLs and postbacks point to this application, browser callbacks/server notifications should remain the primary source and polling may be disabled by removing its Celery Beat entry.
- `python manage.py sync_surveys` isolates Celery/Redis from upstream and database troubleshooting.
- Swagger provides a direct authenticated-network test surface at `/api/docs/`.
- Validate credentials, DNS, supplier account allocation, and upstream rate limits before increasing detail batch size.

## Recovery

No destructive reset is required. Restore connectivity and rerun the sync. Upsert and replacement operations are idempotent. Local internal IDs remain stable across repeated imports and survey updates.
