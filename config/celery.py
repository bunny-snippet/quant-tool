import os

from celery import Celery
from celery.signals import beat_init, worker_ready

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("survey_workspace")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


def reconcile_credentials_on_startup(**kwargs):
    """Baseline/detect environment-backed credentials when a worker or beat starts."""
    try:
        from vendors.credentials import reconcile_all_integration_credentials
        reconcile_all_integration_credentials()
    except Exception:
        # Database tables may not exist yet during first deploy/migration.
        pass


beat_init.connect(reconcile_credentials_on_startup, weak=False)
worker_ready.connect(reconcile_credentials_on_startup, weak=False)
