#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/htdocs/quant-tool}"
export APP_DIR
export SUPERVISOR_CONFIG_OVERRIDE="${SUPERVISOR_CONFIG_OVERRIDE:-$APP_DIR/deploy/supervisord-quant.conf}"
export HEALTH_PORT="${HEALTH_PORT:-8091}"
export APP_LABEL="${APP_LABEL:-Quant Tool}"
export PUBLIC_STATIC_DIR="${PUBLIC_STATIC_DIR:-$HOME/htdocs/exchange.api-grid.com/static}"

exec "$APP_DIR/deploy/rootless-install.sh"
