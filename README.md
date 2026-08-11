# Survey Workspace

Django-based multi-client survey workspace that synchronizes upstream provider inventory, stores it locally, and exposes responsive Projects UI plus a documented REST API. InnovateMR remains supported and Research For Good Live Alert is available through the Client Catalog integration flow.

Hostinger Ubuntu VPS deployment with MySQL, Nginx, Gunicorn, Redis and Celery is covered in [`deploy/README.md`](deploy/README.md).

## What is included

- Dashboard placeholder and responsive Projects workspace matching the supplied table reference.
- Full and cursor-paged InnovateMR inventory ingestion.
- Secure UI-driven Research For Good onboarding, HMAC-SHA1 API adapter, preview, per-client scheduled sync, targeting, deduplication and callback tracking.
- Deterministic merge by `surveyId`; the payload with the newest `modifiedDate` wins.
- Immutable 14-digit project IDs in `YYYYMM########` format, for example `20260800000001`.
- Quota and survey-targeting/pre-screening persistence with stale-data refresh.
- Environment-configurable Celery Beat jobs (60-second defaults) for inventory and bounded detail refresh.
- Search, multi-select company/market/status filters, date/CPI filters, ordering, pagination, and mobile survey cards.
- Direct survey detail drawer with equal-width Pre-screening and Quota tabs.
- Dynamic respondent pre-screener with 10-character RID, answer capture, supplier redirect, four callback outcomes, IP tracking and measured LOI.
- Session login plus dynamic role/function access control with per-user allow and deny overrides.
- UAT vendor operations workspace with internal/external policy, client visibility, quantity limits, CPI cuts and optional survey overrides.
- Internal organization workspace with strict Branch → Sub-branch → Shift hierarchy, multiple Team Leads per Shift and inherited client visibility.
- Transactional allocation reservation at respondent start, terminal consume/release and scheduled abandoned-reservation expiry.
- Swagger UI, ReDoc, downloadable OpenAPI schema, Django Admin, sync audit records, and automated tests.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py runserver
```

Set `INNOVATEMR_API_TOKEN` in `.env` before synchronizing. The credential is only read by Django and is never returned to browser code.

Open:

- Projects UI: `http://127.0.0.1:8000/projects/`
- Dashboard: `http://127.0.0.1:8000/dashboard/`
- Swagger: `http://127.0.0.1:8000/api/docs/`
- ReDoc: `http://127.0.0.1:8000/api/redoc/`
- Admin: `http://127.0.0.1:8000/admin/`
- Organization: `http://127.0.0.1:8000/organization/`

Run one sync without Celery:

```powershell
python manage.py sync_surveys
```

## Scheduled jobs

Start Redis, then run these in separate terminals. On Windows, Celery's solo pool is the most reliable local option.

```powershell
celery -A config worker --loglevel=info --pool=solo
celery -A config beat --loglevel=info
```

Beat schedules three independent dispatch/check jobs every minute by default:

1. `surveys.dispatch_due_integrations` queues each active scheduled client integration only when its own interval is due. The worker synchronizes inventory and a bounded detail batch; RFG has a hard 600-second minimum.
2. `surveys.reconcile_pending_attempts` checks redirected attempts whose legacy client return URL has not called this application.
3. `vendors.expire_allocation_reservations` releases capacity held by abandoned vendor attempts after the configured TTL.

Opening Quota or Pre-screening in the UI also refreshes that survey immediately when its cached details are older than its source `modifiedDate`. Cached details remain available during a temporary upstream outage.

## REST API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/surveys/` | Paginated, searchable survey inventory |
| `GET` | `/api/v1/surveys/{local_id}/` | One survey with stored quotas and targeting |
| `GET` | `/api/v1/surveys/{local_id}/quotas/` | Current quota definitions |
| `GET` | `/api/v1/surveys/{local_id}/targeting/` | Pre-screening questions and accepted options |
| `POST` | `/api/v1/sync/` | Queue an inventory sync |
| `POST` | `/api/v1/sync/?wait=true` | Run a synchronous operational sync |
| `GET` | `/api/v1/sync-runs/` | Sync audit history |
| `GET` | `/api/v1/survey-attempts/` | Staff-only RID, answers, status, IP and LOI audit |
| CRUD | `/api/v1/vendors/commercial-profiles/` | Internal/external vendor CPI policy |
| CRUD | `/api/v1/vendors/client-allocations/` | Vendor client visibility and total quantity |
| CRUD | `/api/v1/vendors/survey-allocations/` | Optional per-survey limit or CPI override |
| CRUD | `/api/v1/vendors/api-keys/` | Issue/revoke hashed external-vendor API credentials (plaintext returned once) |
| `GET` | `/api/v1/vendors/reservations/` | Reservation lifecycle audit |
| CRUD | `/api/v1/vendors/organization-units/` | Branch, Sub-branch and Shift hierarchy |
| CRUD | `/api/v1/vendors/organization-client-access/` | Inherited unit-level client visibility |
| CRUD | `/api/v1/vendors/integrations/` | Non-secret client integration metadata |
| `GET/POST` | `/api/v1/vendors/integrations/{id}/preview/`, `test-connection/`, `sync-now/` | Provider onboarding and operations |
| CRUD | `/api/v1/access/roles/` | Roles and their explicit function assignments |
| CRUD | `/api/v1/access/functions/` | Function permission catalog |
| CRUD | `/api/v1/access/users/` | Employee accounts, role and individual allow/deny overrides |
| `GET` | `/api/schema/` | OpenAPI 3 schema |

The interactive docs describe every query parameter, response object, enum, pagination envelope, and nested detail shape.

## Respondent pre-screener flow

Create an attempt by opening a validated survey link:

```text
/survey/start?surveyId=32655971&userId=294&code=20260800000001
```

The server validates that `surveyId` and the 14-digit internal `code` identify the same live survey, creates a cryptographically random 10-character RID containing uppercase, lowercase and numeric characters, stores `initiated_at` plus the request IP, and redirects to the canonical `/survey/start?rid=...` form.

The Projects table's **Copy link** button returns this platform pre-screener URL with a `[%%userId%%]` placeholder. The InnovateMR `entryLink` remains server-side redirect data and is used only after the pre-screener is submitted.

On submit it stores question answers, takes the exact InnovateMR `entryLink`, replaces its PID with RID, adds `trackId=RID` and available `QuestionKey=OptionId` values, marks the attempt redirected, and sends the browser to InnovateMR.

Configure these four return outcomes with InnovateMR (use the deployed HTTPS hostname in production):

```text
/survey?status=1&rid=%%pid%%   # Completed
/survey?status=2&rid=%%pid%%   # Terminated
/survey?status=3&rid=%%pid%%   # Over quota
/survey?status=4&rid=%%pid%%   # Quality terminated
```

Each callback captures its first arrival time, callback IP, callback count and LOI in seconds from `initiated_at`. Browser callbacks remain `is_verified=false`; a trusted InnovateMR S2S notification or redirect hash must verify them before financial reconciliation.

## Multi-client API integrations

Open `/client-integrations/` with the `clients.view` permission. Managers can create clients and connections, save a write-only encrypted API token, configure the supplier code and one-minute-or-longer schedule, test credentials, and queue an immediate sync. Celery Beat dispatches only integrations that are due; survey identity is unique by `(integration, source_id)`.

Set a stable `INTEGRATION_CREDENTIAL_ENCRYPTION_KEY` before storing UI credentials. Changing a token immediately clears supplier links and cached upstream data for that integration only; saving the same token preserves them. `credential_env_key` remains supported for legacy deployments.

## Verification

```powershell
python manage.py check
python manage.py test
python manage.py spectacular --file schema.yml --validate
python manage.py collectstatic --noinput
```

See [architecture](docs/architecture.md), [client integrations](docs/client-integrations.md), and [synchronization runbook](docs/synchronization.md) for the internal design and operations contract.
