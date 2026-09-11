"""Verify collected template assets locally and, optionally, on the public host.
Run after collectstatic: python deploy/verify_static.py --base-url https://HOST
Read-only: never clears files, changes manifests, or accesses business data.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.contrib.staticfiles.storage import staticfiles_storage

parser = argparse.ArgumentParser()
parser.add_argument("--base-url")
args = parser.parse_args()
templates = [
    "surveys/base.html", "surveys/dashboard.html", "surveys/studies.html",
    "surveys/termination_reasons.html", "surveys/reconciliation.html",
    "surveys/user_hits.html", "surveys/user_dashboard.html",
    "surveys/prescreened_data.html", "surveys/projects.html",
    "vendors/management.html", "includes/favicon_links.html",
]
assets = set()
for template in templates:
    text = (ROOT / "templates" / template).read_text(encoding="utf-8")
    assets.update(re.findall(r"{%\s*static\s+['\"]([^'\"]+)['\"]\s*%}", text))
for asset in sorted(assets):
    stored_name = getattr(staticfiles_storage, "stored_name", None)
    stored = stored_name(asset) if stored_name else asset
    path = Path(staticfiles_storage.path(stored))
    if not path.is_file():
        raise SystemExit("Missing collected asset: " + stored)
    try:
        url = staticfiles_storage.url(asset, force=True)
    except TypeError:
        url = staticfiles_storage.url(asset)
    if args.base_url:
        public_url = urljoin(args.base_url, url)
        with urlopen(Request(public_url, headers={"Accept-Encoding": "identity"}), timeout=25) as response:
            body = response.read()
            mime = response.headers.get("Content-Type", "").lower()
            if response.status != 200:
                raise SystemExit("Non-200: " + public_url)
        if asset.endswith(".js") and not ("javascript" in mime or "ecmascript" in mime):
            raise SystemExit("Wrong JavaScript content type: " + public_url)
        if asset.endswith(".css") and "text/css" not in mime:
            raise SystemExit("Wrong CSS content type: " + public_url)
        if hashlib.sha256(body).digest() != hashlib.sha256(path.read_bytes()).digest():
            raise SystemExit("Public asset bytes differ: " + public_url)
    print("OK", url)
print("Verified", len(assets), "collected assets" + (" and public bytes" if args.base_url else ""))
