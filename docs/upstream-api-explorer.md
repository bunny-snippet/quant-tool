# Upstream client API explorer

The Swagger UI at `/api/docs/` contains an **Upstream client APIs** section for
testing configured survey-provider connections without copying credentials into
the browser.

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

1. `GET /api/v1/vendors/upstream-explorer/` lists active client integrations.
2. `GET /api/v1/vendors/upstream-explorer/{id}/` shows the provider base URL,
   exact upstream endpoint/command, official documentation link, required
   parameters and optional query parameters for every supported operation.
3. Use the dedicated inventory, quota and targeting actions, or execute any
   listed operation through
   `GET /api/v1/vendors/upstream-explorer/{id}/execute/{operation}/`.

List responses are limited to 50 rows by default and 200 rows maximum so a
large inventory cannot freeze Swagger. The wrapper reports the original row
count and whether the displayed payload was truncated. This does not change
the normal scheduled synchronization flow.

## Built-in provider operations

InnovateMR includes allocated/paged/high-priority inventory, survey-by-ID,
inventory/closed-survey date lookups, quota, targeting, transaction lookups,
availability, stats, redirect lookup, panelist profile, recontact PIDs,
question categories/questions/answers, core metadata, termination categories,
unique respondent checks, respondent pre-check and personalized inventory.

RFG includes signed connection test, inventory, targeting, quota extraction,
datapoint list/details, create-link, duplicate-check and project stats. RFG
quota data is part of the documented `livealert/targeting/1` response, so the
quota action calls that command and returns its `quotas` collection.

Only allow-listed, read-oriented operations are exposed. Some documented
eligibility/look-up operations use POST upstream, but they do not update provider
configuration or profiling data. Provider endpoints
that set/delete redirects, write profiling data or otherwise mutate upstream
state are intentionally excluded from the interactive explorer.

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
