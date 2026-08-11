#!/usr/bin/env bash
set -euo pipefail

cd /opt/quest-tool
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate --noinput
if grep -Eqi '^PRESCREENER_VAULT_ENABLED=(true|1|yes|on)$' .env; then
  .venv/bin/python manage.py migrate --database=prescreener_vault --noinput
fi
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py check --deploy
sudo systemctl restart quest-tool-web quest-tool-worker quest-tool-beat
sudo systemctl --no-pager --full status quest-tool-web quest-tool-worker quest-tool-beat
