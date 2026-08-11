# Hostinger VPS deployment

The production layout uses Nginx -> Gunicorn -> Django, MySQL for durable data, and Redis + Celery Worker + Celery Beat for background synchronization.

Gunicorn listens privately on `127.0.0.1:8091` by default and Nginx proxies public HTTP/HTTPS traffic to it. Change `GUNICORN_BIND` in `/etc/quest-tool/quest-tool.env` and the matching `proxy_pass` in the Nginx site if a different internal port is required; never expose Gunicorn directly to the internet.

## Required server values

- VPS public IP and SSH user
- domain name, if available
- MySQL password for database/user `api-tool`
- InnovateMR API token

Never commit the production environment file. Store it at `/etc/quest-tool/quest-tool.env` with mode `600`.

## First installation (Ubuntu)

```bash
sudo apt update
sudo apt install -y git nginx redis-server python3 python3-venv python3-dev build-essential default-libmysqlclient-dev pkg-config
sudo adduser --system --group --home /opt/quest-tool questtool
sudo git clone https://github.com/bunny-snippet/quest-tool.git /opt/quest-tool
sudo chown -R questtool:www-data /opt/quest-tool
sudo -u questtool python3 -m venv /opt/quest-tool/.venv
sudo -u questtool /opt/quest-tool/.venv/bin/pip install --upgrade pip
sudo -u questtool /opt/quest-tool/.venv/bin/pip install -r /opt/quest-tool/requirements.txt
sudo install -d -m 750 -o questtool -g questtool /etc/quest-tool
sudo install -m 600 -o questtool -g questtool /opt/quest-tool/deploy/quest-tool.env.example /etc/quest-tool/quest-tool.env
sudo nano /etc/quest-tool/quest-tool.env
```

Replace the domain/IP, database password, Django secret and InnovateMR token in the environment file. The existing MySQL database should use `utf8mb4`, and user `api-tool` must have privileges on database `api-tool`.

```bash
sudo -u questtool /opt/quest-tool/.venv/bin/python /opt/quest-tool/manage.py migrate
sudo -u questtool /opt/quest-tool/.venv/bin/python /opt/quest-tool/manage.py collectstatic --noinput
sudo cp /opt/quest-tool/deploy/systemd/quest-tool-*.service /etc/systemd/system/
sudo cp /opt/quest-tool/deploy/nginx/quest-tool.conf /etc/nginx/sites-available/quest-tool
sudo nano /etc/nginx/sites-available/quest-tool
sudo ln -s /etc/nginx/sites-available/quest-tool /etc/nginx/sites-enabled/quest-tool
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now redis-server quest-tool-web quest-tool-worker quest-tool-beat nginx
```

Verify the private application port before configuring DNS:

```bash
curl -I -H 'Host: your-domain.example' http://127.0.0.1:8091/login/
sudo ss -ltnp | grep ':8091'
```

Add HTTPS after DNS resolves:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example
```

After HTTPS is working, set `DJANGO_SECURE_HSTS_SECONDS=31536000` only when the domain is permanently HTTPS-only, then restart the web service.

## Deploying later updates

```bash
cd /opt/quest-tool
sudo bash deploy/update.sh
```

## Periodic InnovateMR synchronization

This project does not use Linux cron. `quest-tool-beat` is the scheduler and publishes due tasks to Redis; `quest-tool-worker` consumes and executes them.

- `INNOVATEMR_INVENTORY_SYNC_INTERVAL_SECONDS=60`: every 60 seconds, calls both `getAllocatedSurveys` and `getAllocatedSurveysPaged`, merges duplicate survey IDs using the newest modified timestamp, and updates MySQL.
- `INNOVATEMR_DETAIL_SYNC_INTERVAL_SECONDS=60`: every 60 seconds, selects a bounded set of stale live surveys and calls quota plus targeting endpoints.
- `INNOVATEMR_DETAIL_REFRESH_BATCH=20`: processes at most 20 stale surveys during one detail run.

Change these values in `/etc/quest-tool/quest-tool.env`, then run:

```bash
sudo systemctl restart quest-tool-beat
```

Restarting the worker is not required for interval-only changes, but restarting both is harmless. Only one Beat instance must run. A five-minute database lease prevents overlapping inventory/detail jobs; if the previous job is still running, the next occurrence is recorded as skipped instead of duplicating work.

Useful operational checks:

```bash
sudo systemctl status quest-tool-web quest-tool-worker quest-tool-beat redis-server nginx
sudo journalctl -u quest-tool-beat -u quest-tool-worker -f
sudo -u questtool /opt/quest-tool/.venv/bin/python /opt/quest-tool/manage.py check --deploy
```

## Restricted Hostinger SSH user (no root)

When Hostinger already proxies the assigned application port and the SSH user cannot install system packages or systemd units, run the bundled rootless installer from `$HOME/htdocs/quest-tool`. It uses the pure-Python PyMySQL driver and a user-owned Supervisor process, so Python/MySQL development headers are not required. If Ubuntu's `python3-venv` package is unavailable, the installer bootstraps the environment with PyPA's official `virtualenv.pyz`. Cron starts Supervisor after a VPS reboot; Supervisor keeps Gunicorn, Celery Worker and the single Celery Beat process alive.

```bash
cd "$HOME/htdocs/quest-tool"
git pull --ff-only origin main
chmod +x deploy/rootless-install.sh
./deploy/rootless-install.sh
```

The `.env` in the repository root must be mode `600`, use MySQL, have `DJANGO_DEBUG=false`, and contain a non-empty database password. The installer also publishes collected files to `$HOME/htdocs/api.exchange-ip.com/static`, which is the Hostinger Nginx static root for this site. Override `PUBLIC_STATIC_DIR` when deploying under another domain. Useful rootless checks:

```bash
.venv/bin/supervisorctl -c deploy/supervisord.conf status
tail -f "$HOME/logs/quest-tool-web-error.log"
curl -I http://127.0.0.1:8091/login/
```

For the independent Quant Tool VPS, clone into `$HOME/htdocs/quant-tool` and use the dedicated wrapper/config. It keeps Supervisor sockets, logs, worker identity and Celery Beat state separate from Quest Tool:

```bash
cd "$HOME/htdocs/quant-tool"
chmod +x deploy/rootless-install.sh deploy/rootless-install-quant.sh
PUBLIC_STATIC_DIR="$HOME/htdocs/your-quant-domain.example/static" ./deploy/rootless-install-quant.sh
.venv/bin/supervisorctl -c deploy/supervisord-quant.conf status
```
