# UAT deployment

UAT runs from the `codex/vendor-allocation-uat` branch and must never share its database with production.

## Isolation contract

- Application directory: `$HOME/htdocs/quest-tool-uat`
- Web port: `8092`
- Database name must contain `uat`
- Redis broker/result databases: `4` and `5` by default
- Supervisor socket, PID, logs and Celery beat schedule use UAT-specific names
- Scheduled upstream jobs are disabled by default with `ENABLE_SCHEDULED_JOBS=false`
- UAT static destination: `$HOME/htdocs/asi.exchange-ip.com/static`

## First deployment

```bash
cd "$HOME/htdocs"
git clone --branch codex/vendor-allocation-uat https://github.com/bunny-snippet/quest-tool.git quest-tool-uat
cd quest-tool-uat
cp .env.uat.example .env
chmod 600 .env
```

Create the separate MySQL database/user, replace every placeholder in `.env`, configure the UAT hostname, then run:

```bash
chmod +x deploy/rootless-install.sh deploy/rootless-install-uat.sh
./deploy/rootless-install-uat.sh
```

## Updates

```bash
cd "$HOME/htdocs/quest-tool-uat"
git pull --ff-only origin codex/vendor-allocation-uat
./deploy/rootless-install-uat.sh
```

Do not enable scheduled jobs until a dedicated/test upstream API token has been installed. When enabled, keep UAT intervals conservative to avoid duplicating production's minute-level traffic.
