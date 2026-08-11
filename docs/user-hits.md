# User Hits reporting

## Purpose

`/user-hits/` provides a responsive team-performance report backed by `SurveyAttempt`. The same data is available to internal consumers at `GET /api/v1/user-hits/` and is documented in Swagger.

## Metric definitions

- A **hit** is a survey attempt grouped by its `initiated_at` calendar date in `Asia/Kolkata`.
- A **complete** is an attempt in that hit cohort whose terminal status is `1`.
- Device splits use `entry_device`, so hits and completes for one respondent stay in the same device bucket.
- Desktop includes captured desktop/laptop values, mobile includes mobile/phone values, and tablet includes tablet/tab values.
- Missing or unsupported historical device values are counted as `unclassified`; they remain included in totals.
- Conversion is `completes / hits × 100`, rounded to one decimal place.

## Organization labels

Branch uses `EmployeeProfile.company_name`. When a subordinate profile has no company, the closest company in its `created_by` ownership chain is inherited. Sub-branch uses `EmployeeProfile.department` and otherwise falls back to the resolved branch. Both values can be maintained from the Access Control user modal.

## API filters

| Parameter | Meaning |
| --- | --- |
| `search` | User name, username, email, branch or sub-branch text |
| `user` | Comma-separated platform user IDs |
| `branch` | Comma-separated exact branch labels |
| `sub_branch` | Comma-separated exact sub-branch labels |
| `from_date` | Inclusive IST date in `YYYY-MM-DD` |
| `from_time` | Optional inclusive IST time; requires `from_date` |
| `to_date` | Inclusive IST date in `YYYY-MM-DD` |
| `to_time` | Optional inclusive IST time; requires `to_date` |
| `page` | 1-based result page |
| `page_size` | 1–100 aggregated rows |

Blank start/end times retain the full-day boundary for backward compatibility. The response contains paginated user-day rows plus a `summary` calculated across the complete filtered result, not only the current page.

## Access control

The page, API and sidebar item require `user_hits.view`. Team Lead, Manager, Admin and Super Admin roles receive it by default. Individual allow/deny overrides continue to take precedence, and non-superusers only see themselves and users below them in the `created_by` hierarchy.
