# Respondent pre-screener and redirect lifecycle

## Entry contract

`GET /survey/start?surveyId={source_id}&supplierCode=1000&userId={user_id}&code={local_id}`

All four values are required and the copied link is generated with the authenticated employee's real database user ID. Before rendering any questions, the server rejects duplicated/extra parameters and verifies that:

- the user exists, is active, and still has Projects and Copy Link access;
- `surveyId` and the 14-digit `code` resolve to the same live local survey;
- `supplierCode` matches the platform-facing `PUBLIC_SUPPLIER_CODE` setting (`1000` by default); and
- the survey has a non-empty allocated entry link.

For a vendor or an internal-vendor respondent, validation also requires a currently active client allocation with remaining quantity. An explicit survey override acts as an additional allow/block, date-window and quantity rule. Attempt creation and capacity reservation commit together, so an exhausted allocation cannot create an untracked respondent attempt.

An invalid or inconsistent user, code, survey, supplier, or additional query parameter returns the generic Invalid survey link page and creates no attempt.

The server creates a unique RID and records the validated user foreign key, a user-ID snapshot, `initiated`, initiation time/IP, browser, device, OS, user-agent, accepted language and safe client hints before redirecting to the canonical RID form. Cookies and authorization headers are never copied into this audit snapshot. RID is exactly 10 characters and always includes uppercase, lowercase and numeric characters. The canonical `?rid=` route also rejects unknown RIDs, extra parameters, and attempts whose user was removed or disabled.

## Pre-screener

Targeting questions are rendered by type:

- Single Punch → radio options
- Multi Punch → checkbox options
- AGE/Numeric Open Ended → constrained numeric input
- other open-ended questions → text input

Answers are persisted but are not used as an authoritative local rejection. InnovateMR may fill missing profile gaps and performs its own entrance checks. Age answers are mapped to the matching upstream range OptionId when available.

## Supplier redirect

The public copied link always uses the platform-facing supplier code, so an upstream/vendor supplier code is not exposed there. The exact stored `entryLink` is parsed only after validation. Its PID is replaced with RID, `trackId=RID` is added, and captured `QuestionKey=OptionId` pairs are appended. `survNum` and the real upstream `supCode` are preserved from the allocated link; they are never reconstructed from client parameters. This keeps InnovateMR routing intact while allowing the same public code to be used for future providers.

InnovateMR owns the browser redirect after the respondent leaves this application. Configure the account-level or survey-level return URLs in InnovateMR to point to the public deployment, using `%%trackId%%` as the RID, for example:

`https://survey.example.com/survey?status=1&rid=%%trackId%%`

Use status 1, 2, 3 and 4 for complete, terminate, over-quota and quality-terminate destinations respectively. A redirect to another domain such as `api.quantichamps.com` and a `code=null` value are produced by that upstream redirect configuration, not by the local Django callback route.

## Callback contract

`GET /survey?status={1|2|3|4}&rid={RID}`

The first callback sets the terminal status, callback time/exit IP, exit browser/device/OS/user-agent and `loi_seconds = callback_at - initiated_at`. Later requests only update `last_callback_at` and `callback_count`, protecting the original outcome, exit audit and LOI from refreshes.

The same transaction finalizes the vendor reservation: complete consumes the frozen quantity, while terminate, over-quota and quality-terminate release it. Reconciled upstream terminal statuses use the identical finalization service.

When a survey still has a legacy redirect configured, the browser cannot return its result to this application. As a temporary fallback, Celery polls InnovateMR's authenticated `getSurveyTransactionsByCond/{surveyId}/{PID}` endpoint for recent redirected attempts. PID and `trackId` both contain our RID, so the task can reconcile the terminal status, upstream public IP, end time and LOI without access to the legacy destination. Direct callbacks remain preferred and win any race with polling.

Status mapping:

1. Completed
2. Terminated
3. Over quota
4. Quality terminated

Pre-survey statuses are collapsed into the same five operational UI states: pending/redirected both display as Initiated, pre-survey termination maps to 2, pre-survey over-quota maps to 3, and pre-survey quality termination maps to 4.

The landing page accepts RID aliases `PID`, `pid`, `QSID`, `qsid`, and `trackId` for integration tolerance. The canonical parameter remains `rid`.

## Trust and verification

Browser redirects can be forged. Every callback starts as `is_verified=false`. Add InnovateMR server-to-server notification or redirect-hash validation before using a completion for rewards, invoices or financial reporting. The staff-only `/studies/` page, `/api/v1/survey-attempts/` endpoint and Django Admin expose the audit trail.

The Studies page applies user, status, text and entry/exit date-time filters server-side. `/api/v1/survey-attempts/export/` applies the identical filter contract but exports the complete related audit dataset rather than only the compact UI columns. Viewing requires `attempts.view`; downloading requires the independently assignable `attempts.export` function permission.
