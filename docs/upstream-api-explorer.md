# Upstream client API explorer

The Swagger UI at `/api/docs/` contains separate **Client API catalog**,
**InnovateMR APIs**, **RFG APIs** and **RFG Callbacks** sections. They test
configured provider connections without copying credentials into the browser.

## Authentication

Both gates are mandatory:

1. Sign in to the workspace with an active Django staff/superuser account or an
   active application role whose slug is `admin` or `super-admin`.
2. Complete the browser HTTP Basic prompt using `API_DOCS_BASIC_USERNAME` and
   `API_DOCS_BASIC_PASSWORD` from the server environment.

The schema, Swagger UI and ReDoc are all protected by the same two gates. If the
Basic variables are missing, the documentation fails closed with HTTP 503.

## Credential handling

- InnovateMR-compatible integrations resolve `credential_env_key` (or the
  existing encrypted credential) on the server and inject the configured auth
  header.
- RFG resolves the `apid` and `secret` environment-variable references stored
  in `credential_env_keys`, then calculates `apid`, `time` and `hash` on the
  server for every signed request.
- Credential values, Authorization headers and RFG signatures are never added
  to OpenAPI, response metadata or safe provider errors.
- The catalog reports only environment-variable *names* and whether all
  required values are configured.

## Workflow

1. `GET /api/v1/vendors/upstream-explorer/?search=innovate` searches active clients.
2. `GET /api/v1/vendors/upstream-explorer/{client_code}/` shows the provider base URL,
   exact upstream endpoint/command, official documentation link, required
   parameters and optional query parameters for every supported operation.
3. Use the provider-specific operation, for example
   `GET /api/v1/vendors/upstream-explorer/innovate/innovatemr/inventory/` or
   `GET /api/v1/vendors/upstream-explorer/rfg/rfg/targeting/?survey_id=...`.

The stable client code/name replaces the database integration ID. Built-in
aliases include `innovate`, `innovatemr`, `innovate-mr`, `rfg` and
`research-for-good`. When one alias matches more than one active connection,
the API returns HTTP 409 and lists the integration names accepted by the
`integration` query parameter.

Provider sections are database-driven. If no active client integration exists
for a provider, that provider's operations, callbacks and tag are omitted from
the generated Swagger schema. Enabling and testing an active integration makes
its section appear automatically; no documentation code change is required.

List responses are limited to 50 rows by default and 200 rows maximum so a
large inventory cannot freeze Swagger. The wrapper reports the original row
count and whether the displayed payload was truncated. This does not change
the normal scheduled synchronization flow.

## Built-in provider operations

InnovateMR includes allocated/paged/high-priority inventory, survey-by-ID,
inventory/closed-survey date lookups, quota, targeting, transaction lookups,
availability, stats, redirect lookup/configuration, panelist profile read/write, recontact PIDs,
question categories/questions/answers, core metadata, termination categories,
unique respondent checks, respondent pre-check and personalized inventory.

RFG includes signed connection test, inventory, targeting, quota extraction,
datapoint list/details, create-link, single/bulk duplicate checks, project log,
project stats and postal-code geography lookup. RFG
quota data is part of the documented `livealert/targeting/1` response, so the
quota action calls that command and returns its `quotas` collection.

Only explicitly allow-listed operations are exposed. Some documented
eligibility/look-up operations use POST upstream, but they do not update provider
configuration or profiling data. InnovateMR redirect/profile writes have their
own POST actions and require the literal body field
`confirm_upstream_mutation: true`; otherwise no upstream request is sent.
These operations should be contract-tested with mocks and invoked live only by
an administrator who intends to modify the provider account.

The real RFG callback remains source-IP protected. Use the callback guide and
callback-preview actions to understand result fields safely; they do not mutate
an RID or weaken production callback verification.

## Future provider read APIs

Core inventory/quota/targeting/transaction endpoints use the existing
`ClientIntegration` fields. Extra same-origin GET endpoints can be added to an
integration's non-secret `config` without accepting an arbitrary URL from the
browser:

```json
{
  "read_api_operations": [
    {
      "code": "markets",
      "label": "Markets",
      "description": "List available markets.",
      "endpoint": "/v1/markets/{country}",
      "documentation_url": "https://provider.example/docs/markets",
      "required_parameters": ["country"],
      "query_parameters": ["language"]
    }
  ]
}
```

Operation codes must be lowercase alphanumeric/underscore identifiers. An
absolute configured endpoint is accepted only when its scheme and host match
the integration base URL. Runtime requests cannot supply a URL, which prevents
the explorer from becoming an arbitrary server-side request proxy.
