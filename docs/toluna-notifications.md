# Toluna notification ingestion

Quant Tool exposes six server-to-server JSON endpoints. The host name may be
the current Quant deployment host or another HTTPS host reverse-proxied to the
same Django application.

| Toluna event | POST path |
| --- | --- |
| Member completion | `/api/toluna/notifications/member-complete` |
| Member termination | `/api/toluna/notifications/member-terminate` |
| Survey closed | `/api/toluna/notifications/survey-closed` |
| Quota status | `/api/toluna/notifications/quota-status` |
| Enhanced termination | `/api/toluna/notifications/enhanced-termination` |
| Reconciliation | `/api/toluna/notifications/reconciliation` |

## Authentication

Configure a strong shared token in the deployment environment:

```dotenv
TOLUNA_NOTIFICATION_TOKEN=<secret agreed with Toluna>
```

Every request must use `Content-Type: application/json` and include:

```http
X-Toluna-Token: <same secret>
```

An absent or incorrect token is rejected before the payload is stored. Keep
this token outside source control and rotate it after accidental disclosure.

## Processing rules

- Every accepted payload is kept as an immutable provider audit record.
- Completion callbacks are idempotent by Toluna Survey ID, Wave ID and
  UniqueCode, as Toluna can send a completion again after an end-page refresh.
- Other exact duplicate payloads are counted but not applied twice.
- `AdditionalData` is inspected for the platform `rid`; the Toluna
  `UniqueCode` is used as a safe fallback for matching a respondent journey.
- Member outcomes update the matching Traffic Report journey.
- Quota status notifications update the matching local Toluna quota.
- Survey closed notifications close the matching local Toluna survey.
- A valid but currently unmatched notification remains visible and auditable;
  it is never discarded.

## Term Reports

Open **Term Reports**, select **Toluna** in the Provider filter, and apply the
filter. The page displays separate tabs for completion, member termination,
enhanced termination, quota status, survey closed and reconciliation events.
Details expose normalized fields only; raw JSON remains backend audit data.
The existing Export action adds a `Toluna Notifications` worksheet for the
selected Toluna event tab.

