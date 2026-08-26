#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/htdocs/quest-tool}"
ENV_FILE="$APP_DIR/.env"
SUPERVISOR_CONFIG="$APP_DIR/deploy/supervisord.conf"
SUPERVISOR_CONFIG="${SUPERVISOR_CONFIG_OVERRIDE:-$SUPERVISOR_CONFIG}"
SUPERVISORCTL="$APP_DIR/.venv/bin/supervisorctl"
SUPERVISORD="$APP_DIR/.venv/bin/supervisord"
HEALTH_PORT="${HEALTH_PORT:-8091}"
APP_LABEL="${APP_LABEL:-Quest Tool}"

cd "$APP_DIR"
test -f "$ENV_FILE" || { echo "Missing $ENV_FILE" >&2; exit 1; }
grep -Eqi '^DB_ENGINE=mysql$' "$ENV_FILE" || { echo "DB_ENGINE must be mysql" >&2; exit 1; }
grep -Eq '^DB_PASSWORD=.+$' "$ENV_FILE" || { echo "DB_PASSWORD is empty" >&2; exit 1; }
grep -Eq '^DJANGO_DEBUG=false$' "$ENV_FILE" || { echo "DJANGO_DEBUG must be false" >&2; exit 1; }
if grep -Eqi '^PRESCREENER_VAULT_ENABLED=(true|1|yes|on)$' "$ENV_FILE"; then
  grep -Eqi '^PRESCREENER_DB_ENGINE=mysql$' "$ENV_FILE" || { echo "PRESCREENER_DB_ENGINE must be mysql" >&2; exit 1; }
  grep -Eq '^PRESCREENER_DB_NAME=.+$' "$ENV_FILE" || { echo "PRESCREENER_DB_NAME is empty" >&2; exit 1; }
  grep -Eq '^PRESCREENER_DB_USER=.+$' "$ENV_FILE" || { echo "PRESCREENER_DB_USER is empty" >&2; exit 1; }
  grep -Eq '^PRESCREENER_DB_PASSWORD=.+$' "$ENV_FILE" || { echo "PRESCREENER_DB_PASSWORD is empty" >&2; exit 1; }
fi

# The installer may be invoked while this application's venv is active. Remove
# it from PATH before the fallback deletes/recreates .venv, otherwise python3
# would still resolve to the interpreter that has just been removed.
if [[ -n "${VIRTUAL_ENV:-}" ]] && \
  [[ "$(realpath -m "$VIRTUAL_ENV")" = "$(realpath -m "$APP_DIR/.venv")" ]]; then
  PATH="${PATH#"$VIRTUAL_ENV/bin:"}"
  export PATH
  unset VIRTUAL_ENV
  hash -r
fi
PYTHON_BIN="$(command -v python3)"

mkdir -p "$HOME/logs" "$HOME/tmp"
chmod 700 "$HOME/tmp"
chmod 600 "$ENV_FILE"

if ! test -x .venv/bin/python || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  if ! "$PYTHON_BIN" -m venv .venv; then
    venv_path="$(realpath -m "$APP_DIR/.venv")"
    test "$venv_path" = "$(realpath -m "$APP_DIR")/.venv" || { echo "Unsafe venv path" >&2; exit 1; }
    rm -rf -- "$venv_path"
    virtualenv_zipapp="$HOME/tmp/virtualenv.pyz"
    curl --fail --silent --show-error --location https://bootstrap.pypa.io/virtualenv.pyz --output "$virtualenv_zipapp"
    "$PYTHON_BIN" "$virtualenv_zipapp" .venv
    rm -f -- "$virtualenv_zipapp"
  fi
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate --noinput
if grep -Eqi '^PRESCREENER_VAULT_ENABLED=(true|1|yes|on)$' "$ENV_FILE"; then
  .venv/bin/python manage.py migrate --database=prescreener_vault --noinput
fi
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py check --deploy

public_static_dir="${PUBLIC_STATIC_DIR:-$HOME/htdocs/api.exchange-ip.com/static}"
if test -d "$(dirname "$public_static_dir")"; then
  mkdir -p "$public_static_dir"
  cp -a staticfiles/. "$public_static_dir/"
  chmod -R u=rwX,g=rX,o= "$public_static_dir"
fi

if "$SUPERVISORCTL" -c "$SUPERVISOR_CONFIG" pid >/dev/null 2>&1; then
  "$SUPERVISORCTL" -c "$SUPERVISOR_CONFIG" reread
  "$SUPERVISORCTL" -c "$SUPERVISOR_CONFIG" update
  "$SUPERVISORCTL" -c "$SUPERVISOR_CONFIG" restart all
else
  "$SUPERVISORD" -c "$SUPERVISOR_CONFIG"
fi

cron_line="@reboot $SUPERVISORD -c $SUPERVISOR_CONFIG"
existing_cron="$(crontab -l 2>/dev/null || true)"
if ! grep -Fqx "$cron_line" <<<"$existing_cron"; then
  { printf '%s\n' "$existing_cron"; printf '%s\n' "$cron_line"; } | sed '/^[[:space:]]*$/d' | crontab -
fi

sleep 6
"$SUPERVISORCTL" -c "$SUPERVISOR_CONFIG" status
curl --fail --silent --show-error --head "http://127.0.0.1:$HEALTH_PORT/login/" >/dev/null
echo "$APP_LABEL is healthy on 127.0.0.1:$HEALTH_PORT"
