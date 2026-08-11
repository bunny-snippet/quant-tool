from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from . import signals  # noqa: F401
        from django.db.models.signals import post_migrate

        from .function_catalog import sync_access_function_catalog

        post_migrate.connect(
            sync_access_function_catalog,
            sender=self,
            dispatch_uid="accounts.sync_access_function_catalog",
        )
