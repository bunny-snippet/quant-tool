#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/htdocs/quest-tool-uat}"
ENV_FILE="$APP_DIR/.env"

cd "$APP_DIR"
test -f "$ENV_FILE" || { echo "Missing $ENV_FILE" >&2; exit 1; }
grep -Eq '^DEPLOYMENT_ENVIRONMENT=uat$' "$ENV_FILE" || {
  echo "Refusing deployment: DEPLOYMENT_ENVIRONMENT must be uat" >&2
  exit 1
}
grep -Eq '^DB_NAME=.*uat.*$' "$ENV_FILE" || {
  echo "Refusing deployment: UAT DB_NAME must contain uat" >&2
  exit 1
}

export APP_DIR
export SUPERVISOR_CONFIG_OVERRIDE="$APP_DIR/deploy/supervisord-uat.conf"
export PUBLIC_STATIC_DIR="${PUBLIC_STATIC_DIR:-$HOME/htdocs/asi.exchange-ip.com/static}"
export HEALTH_PORT=8092
export APP_LABEL="Quest Tool UAT"

exec "$APP_DIR/deploy/rootless-install.sh"
