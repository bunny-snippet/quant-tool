"""Surveys application configuration and cache-invalidation signal startup."""

from django.apps import AppConfig


class SurveysConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "surveys"
    verbose_name = "Survey inventory"

    def ready(self):
        from . import signals  # noqa: F401
