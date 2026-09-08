# Dashboard access and periods

Access Control → Roles → Edit role:

- Grant `dashboard.view` for the main page, then choose individual `dashboard.card.*` and `dashboard.chart.*` functions. No existing employee role is silently granted revenue or other new cards.
- Grant `dashboard.client.view` and/or `dashboard.supplier.view` independently. Direct page and JSON requests enforce these grants, as does sidebar navigation.
- `dashboard.filter.date` and `dashboard.filter.client` enable the partner dashboards' date and client/supplier controls. `dashboard.chart.world_map` plus the completes metric enables the client country map.
- The owner can configure Top performers as suppliers/branches, suppliers only, individual visible users, or own-team employees, and optionally restrict supplier/branch selections. Requires `dashboard.chart.top_users`.
- Own-team mode reveals only names and completed counts within the viewer's assigned shift/sub-branch/branch and its descendants, restricted to the same workspace. With no assigned team it falls back to self only. It does not broaden traffic reports, cards, client dashboards, revenue or individual respondent access. Other modes always intersect existing activity visibility.

Client and supplier defaults are Today (IST midnight through now), compared with yesterday's complete IST calendar day. Current month is month-start through now, compared with the entire previous calendar month. Other periods: 48h, 7/15/21/28 days, 3m, 6m, and financial year. Windows are half-open to avoid double counting midnight. The main dashboard's comparison definitions remain unchanged.

Partner reports cache for up to 60 seconds per permission/visibility-scoped request. Current and previous summaries share a query; trends use grouped buckets; map hover and country selection require no database requests. Static map geometry is stored locally and served with the static manifest. Local datasets may legitimately have no completed journeys.

Combined date/time report filters (not supplier scheduling/expiry forms) show current IST time on opening an empty picker. Selecting a different past date resets it to midnight; subsequently editing its time is respected. Cancelling an untouched empty picker leaves the filter empty.

Deployment: apply Quant accounts migration 0033, synchronize the permission catalog through migrate, collect static files to the configured public STATIC_ROOT, and reload web workers. Do not copy stale checkout staticfiles over the public directory, reset configured grants or import synthetic traffic. Check changed JavaScript, CSS, date filter script and world map manifest URLs return 200.
