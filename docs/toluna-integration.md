# Toluna Integrated Panel integration

This integration is available in **Quant Tool only**. It implements the Toluna External Sample flow in four stages:

1. **Inventory:** for every configured culture, `Get Quotas` imports live Survey/Wave rows, CPI, LOI, IR, remaining capacity, quota layers and routable targeting.
2. **Prescreener:** Toluna Reference Data is converted into local questions and accepted option IDs. Age and gender are always collected because Toluna member creation requires a date of birth and gender. Every question present in a quota is mandatory locally, including questions Toluna marks `IsRoutable=true`: closed-choice questions expose only quota-qualified options, while open age/postal/value requirements are shown as answer guidance. The respondent enters an age; the adapter derives an exact current-age DOB dynamically instead of hard-coding a calendar year.
3. **Member:** after a valid prescreener submission, the vault UID is used as the stable Toluna `MemberCode`. Age is sent as `BirthDate`, an entered postal code is sent as the core `PostalCode` property, and every member-eligible required single-, multi-, or open-answer profile is sent in `RegistrationAnswers`. Open answers are paired with their Toluna envelope `AnswerID`. Payload construction fails before any upstream request when a required member field is missing or cannot be mapped, so a partial profile is never silently registered. Non-core routable/computed `RegistrationAnswers` remain local-only because Toluna can reject those attributes in member registration. A new profile is sent with `POST`; a changed reused profile is sent with `PUT`; an unchanged profile is not sent again.
4. **Invite:** the matching open quota is selected across every layer, then `Generate Invite` returns the respondent-specific live survey URL. The RID is retained as the platform callback identity.

No Toluna secret or GUID value is stored in the application database. A `ClientIntegration` stores environment-variable **names** only.

## Required environment variables

Copy the corresponding production values from the Toluna partner worksheet into the Quant VPS `.env`:

```dotenv
TOLUNA_API_AUTH_KEY=
TOLUNA_PARTNER_AUTH_KEY=
TOLUNA_HMAC_KEY=
TOLUNA_PANEL_EN_CA=
TOLUNA_PANEL_EN_GB=
TOLUNA_PANEL_EN_IN=
TOLUNA_PANEL_EN_SG=
TOLUNA_PANEL_EN_US=

CLIENT_INTEGRATION_TOLUNA_SYNC_INTERVAL_SECONDS=60
```

Toluna's terminology uses Unique Partner Code, `PartnerGUID` and `PanelGUID`
interchangeably. Member registration sends the selected survey culture's
`TOLUNA_PANEL_EN_XX` value as `PartnerGUID`; no separate
`TOLUNA_PARTNER_GUID` is required. A legacy `partner_guid` integration mapping
is still accepted as a fallback for old deployments.

## UI setup

1. Create a Toluna client in **Organization → Client catalog**.
2. Open **Client APIs → Add integration** and select **Toluna Integrated Panel**.
3. Choose Production or Sandbox and enter the environment-variable names shown above. Leave cultures without an issued PanelGUID blank.
4. Keep **Require callback HMAC** enabled for production.
5. Save, use **Test connection**, then **Sync now**.

Successful testing enables scheduled synchronization. The project inventory becomes visible after the first successful sync. Quota/targeting details are hydrated in bounded background batches and immediately on first project use if still stale.

## Provider requests

| Stage | Toluna request | Authentication |
|---|---|---|
| Cultures | `GET /IPUtilityService/ReferenceData/Cultures` | `PARTNER_AUTH_KEY` |
| Questions and answers | `POST /IPUtilityService/ReferenceData/QuestionsAndAnswersData` | `PARTNER_AUTH_KEY` |
| Inventory | `GET /IPExternalSamplingService/ExternalSample/{PanelGUID}/Quotas?includeRoutables=true` | `API_AUTH_KEY` |
| Member create/update | `POST` or `PUT /IntegratedPanelService/api/Respondent` | Toluna member contract |
| Invite | `GET /IPExternalSamplingService/ExternalSample/{PanelGUID}/{MemberCode}/Invite/{QuotaID}` | `API_AUTH_KEY` |

The returned invite's SurveyID and WaveID must match the local project before redirect. `PartnerAmount`, LOI and IR are snapshotted on the attempt so later upstream changes cannot rewrite historical commercial data.

Member synchronization uses a shared-cache single-flight lock per integration + `MemberCode`. This prevents concurrent web workers from duplicating registration and enforces Toluna's minimum delay between two calls for the same member without serializing different respondents.

Toluna's common Age question is intentionally absent from its Reference Data API. The adapter therefore includes Toluna's documented common Age/Gender IDs as a guarded fallback for every configured culture. All other profile mappings continue to come from the live Reference Data response.

## Callback setup

Configure Toluna's outcome/end pages to return to the Quant production host with `status`, `rid` and `hash`. Quant accepts the complete Toluna result set: Qualified (S1), Terminated (S2), Quota Full (S3), Fraud Terminate (S4), Survey Not Available (S7), No Surveys (S8), No Cookies (S9), Max Surveys Reached (S10), Not Qualified (S11), and Survey Taken (S12). The exact placeholder names are partner-configuration values, so confirm them in the partner worksheet before launch. The platform verifies HMAC-SHA256 over the exact final URL, including the documented trailing `&`, before accepting the callback.

Do not disable callback HMAC in production. The callback status cannot be considered verified when the integration toggle is off.

## Swagger and diagnostics

An authenticated admin/super-admin can use `/api/docs/` → **Toluna APIs** to test:

- connection and panel configuration;
- reference cultures;
- the question/answer library;
- panel settings;
- live surveys and quotas for a configured culture.

Swagger injects credentials server-side and redacts secret fields. Member creation and invite generation remain part of the respondent flow instead of being exposed as arbitrary test mutations.

## Official references

- Get Quotas: <https://docs.integratedpanel.toluna.com/externalsample/api/getquotas.html>
- Sampling rules: <https://docs.integratedpanel.toluna.com/externalsample/samplingrules.html>
- Quotas FAQ (`IsRoutable`): <https://docs.integratedpanel.toluna.com/faq/externalsample/quotas.html>
- Generate Invite: <https://docs.integratedpanel.toluna.com/externalsample/api/generateinvite.html>
- Add Member: <https://docs.integratedpanel.toluna.com/membermanagement/v2/add.html>
- Questions and Answers: <https://docs.integratedpanel.toluna.com/mapping/referencedataapi/questionsandanswers.html>
- Standard Encryption: <https://docs.integratedpanel.toluna.com/memberrouting/encryption.html>
