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

## Request validation

Toluna's documented notification contract does not include a custom request
header. Quant therefore authenticates the transport using the static source
addresses supplied by the Toluna integration team:

```dotenv
# Current Toluna production notification sources confirmed for this integration.
TOLUNA_NOTIFICATION_IP_ALLOWLIST=54.211.6.36/32,34.199.200.42/32,3.228.103.61/32

# Direct reverse-proxy peers only. The bundled same-host Nginx uses loopback.
TOLUNA_NOTIFICATION_TRUSTED_PROXY_IPS=127.0.0.1/32,::1/128
```

Both settings accept comma-separated individual IPs or CIDR networks. The
application fails closed when the Toluna allowlist is empty or invalid.
Forwarding headers are ignored unless `REMOTE_ADDR` belongs to the explicit
trusted-proxy list. Trusted proxy hops are stripped from the right of
`X-Forwarded-For`; a caller-prepended leftmost value is never trusted.

Toluna member completion and member termination notifications receive a second
validation layer. Ask Toluna to configure **Standard Encryption: HMAC SHA256**
with the same secret configured on Quant:

```dotenv
TOLUNA_NOTIFICATION_HMAC_KEY=<strong secret configured with Toluna>
```

When that setting is omitted, Quant falls back to the existing
`TOLUNA_HMAC_KEY`. For member-status JSON, Quant verifies `EncryptedValue`
against HMAC-SHA256 of the exact concatenation
`SurveyID + WaveID + UniqueCode`, with no separators. Missing or invalid member
signatures are rejected before storage. Operational notifications that do not
carry `EncryptedValue` (quota status, survey closed, enhanced termination and
reconciliation) remain protected by the source-IP allowlist.

Every request must use `Content-Type: application/json`.
Every event must include both `SurveyID` and `WaveID`; the pair is Toluna's
unique survey-interaction identity and Quant never falls back to another wave.

These three production `/32` addresses and the bundled same-host proxy values
are application defaults, while explicit environment values replace each list
in full. The public Toluna documentation does not publish a notification IP
list; obtain any sandbox addresses or future production changes from the Toluna
representative and update the environment before Toluna changes its senders.

## Processing rules

- Every accepted payload is kept as an immutable provider audit record.
- Completion callbacks are idempotent by Toluna Survey ID, Wave ID and
  UniqueCode, as Toluna can send a completion again after an end-page refresh.
- Other exact duplicate payloads are counted but not applied twice.
- `AdditionalData` is inspected for the platform `rid`; the Toluna
  `UniqueCode` is used as a safe fallback for matching a respondent journey.
- Member outcomes update the matching Traffic Report journey.
- Quota status notifications update only the exact local Toluna
  `SurveyID + WaveID + QuotaID`; a quota ID is never matched globally.
- Survey closed notifications close only the exact local Toluna
  `SurveyID + WaveID`.
- A valid but currently unmatched notification remains visible and auditable;
  it is never discarded. Pending rows are retried after inventory/detail sync
  and on duplicate delivery.
- Provider timestamps prevent an older quota delivery from overwriting a newer
  notification. Pending rows are drained in bounded batches. After inventory
  or detail replacement, only applied events received at or after that
  inventory boundary are replayed, so an old notification cannot permanently
  override a fresher quota snapshot.

## Official Toluna references

- [Notifications overview](https://docs.integratedpanel.toluna.com/notifications/)
- [Member status notification bodies](https://docs.integratedpanel.toluna.com/notifications/memberstatus.html)
- [Standard Encryption and `EncryptedValue`](https://docs.integratedpanel.toluna.com/memberrouting/encryption.html)
- [Enhanced termination notifications](https://docs.integratedpanel.toluna.com/notifications/etns.html)
- [Quota status notifications](https://docs.integratedpanel.toluna.com/notifications/quotastatus.html)
- [Survey closed notifications](https://docs.integratedpanel.toluna.com/notifications/surveyclosed.html)
- [General FAQ, including IP allowlisting guidance](https://docs.integratedpanel.toluna.com/faq/general/)

## Term Reports

Open **Term Reports**, select **Toluna** in the Provider filter, and apply the
filter. The page displays separate tabs for completion, member termination,
enhanced termination, quota status, survey closed and reconciliation events.
Details expose normalized fields only; raw JSON remains backend audit data.
The existing Export action adds a `Toluna Notifications` worksheet for the
selected Toluna event tab.
