# Prescreener response vault

Prescreener submissions are stored in a dedicated database so reusable profile
data is isolated from the operational survey database. A submission has one
stable 10-character RID and one independently generated UID in the form
`XXXX-XXXX-XXXX-XXXX` (16 mixed alphanumeric characters plus separators).

## Stored snapshot

Each submission stores only its UID/RID, country and language at the time of the
hit, submission time, raw answers, and recognized profile dimensions. It does
not copy client, provider, survey/project, or user identity into the vault.
Every question is also normalized into its own row with the exact question
text/key/type/category, raw answer values, display labels, and upstream values.
Individual values are indexed by country and canonical attribute for future
matching. Repeated answers from different survey links remain separate because
each attempt receives a new RID and UID.

The currently recognized reusable dimensions are age/date of birth, age group,
gender, ethnicity/race, postal/ZIP code, country, and language. Unknown questions
are still stored completely and can be mapped later without losing data.

## Production configuration

Add the following to `.env`, using the new MySQL database and its own restricted
database user. Do not reuse the operational database name or credentials.

```dotenv
PRESCREENER_VAULT_ENABLED=true
PRESCREENER_DB_ENGINE=mysql
PRESCREENER_DB_NAME=your-new-database
PRESCREENER_DB_USER=your-new-database-user
PRESCREENER_DB_PASSWORD=your-new-database-password
PRESCREENER_DB_HOST=127.0.0.1
PRESCREENER_DB_PORT=3306
PRESCREENER_DB_CONN_MAX_AGE=60
PRESCREENER_DB_CONNECT_TIMEOUT=10
```

Apply operational and vault migrations separately:

```bash
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py migrate --database=prescreener_vault --noinput
```

## Existing-data migration

First copy and verify without deleting the operational source:

```bash
.venv/bin/python manage.py backfill_prescreener_vault --batch-size 500
```

The command is idempotent. Once `failed=0` has been verified, run it again with
`--clear-source`; each main-database answer payload is cleared only after its
UID/RID vault record has been successfully verified:

```bash
.venv/bin/python manage.py backfill_prescreener_vault --batch-size 500 --clear-source
```

For a large database, resume in bounded ranges with `--after-id` and `--limit`.
Do not remove the old database backup until counts and sample UID/RID mappings
have been checked.

## Failure behavior

The provider redirect occurs only after a successful vault write. If the vault
is temporarily unavailable, the attempt remains initiated and the respondent
can submit the same page again; the provider journey and RID are not corrupted.
Retries with the same UID and identical payload are idempotent. Provider URLs do
not receive the new UID yet; adding it is provider-specific and must be enabled
only after that client's accepted parameter contract is confirmed.
