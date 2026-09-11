"""User lifecycle signal that guarantees an EmployeeProfile row."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .access import (
    invalidate_activity_visibility_cache,
    invalidate_effective_permission_cache,
)
from .models import (
    AccessFunction,
    EmployeeProfile,
    Role,
    RoleFunctionPermission,
    UserFunctionOverride,
)


@receiver(post_save, sender=get_user_model())
def ensure_employee_profile(sender, instance, created, **kwargs):
    if not created:
        return
    role_slug = "super-admin" if instance.is_superuser else "employee"
    role = Role.objects.filter(slug=role_slug).first()
    EmployeeProfile.objects.get_or_create(user=instance, defaults={"role": role})


def _invalidate_now_and_after_commit(callback, *, using=None):
    """Rotate immediately for local readers and once more after commit.

    The post-commit rotation closes the authorization race where a concurrent
    request could observe the pre-commit hierarchy under the new generation.
    Outside an atomic block the immediate rotation is already sufficient.
    """

    callback()
    connection = transaction.get_connection(using=using)
    if connection.in_atomic_block:
        transaction.on_commit(callback, using=using)


def _invalidate_permissions(**kwargs):
    # Incrementing a namespace generation is O(1) and works across every
    # Gunicorn/Celery process. Stale snapshots expire naturally by TTL.
    _invalidate_now_and_after_commit(
        invalidate_effective_permission_cache,
        using=kwargs.get("using"),
    )


def _invalidate_activity_visibility(**kwargs):
    _invalidate_now_and_after_commit(
        invalidate_activity_visibility_cache,
        using=kwargs.get("using"),
    )


for permission_model in (
    AccessFunction,
    Role,
    RoleFunctionPermission,
    EmployeeProfile,
    UserFunctionOverride,
):
    post_save.connect(
        _invalidate_permissions,
        sender=permission_model,
        dispatch_uid=f"accounts.permission-cache.save.{permission_model._meta.label_lower}",
    )
    post_delete.connect(
        _invalidate_permissions,
        sender=permission_model,
        dispatch_uid=f"accounts.permission-cache.delete.{permission_model._meta.label_lower}",
    )


for visibility_model in (Role, EmployeeProfile):
    post_save.connect(
        _invalidate_activity_visibility,
        sender=visibility_model,
        dispatch_uid=f"accounts.activity-visibility.save.{visibility_model._meta.label_lower}",
    )
    post_delete.connect(
        _invalidate_activity_visibility,
        sender=visibility_model,
        dispatch_uid=f"accounts.activity-visibility.delete.{visibility_model._meta.label_lower}",
    )
