"""Invalidate Projects metadata when access/pricing configuration changes."""

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from accounts.access import invalidate_activity_visibility_cache
from accounts.models import EmployeeProfile, RoleFunctionPermission, UserFunctionOverride
from vendors.models import (
    OrganizationClientAccess,
    OrganizationUnit,
    VendorAPIKey,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorSurveyAllocation,
)

from .project_cache import invalidate_project_cache
from .report_cache import invalidate_report_metadata_cache


def _invalidate_now_and_after_commit(callback, *, using=None):
    """Invalidate again after atomic scope changes become visible."""

    callback()
    connection = transaction.get_connection(using=using)
    if connection.in_atomic_block:
        transaction.on_commit(callback, using=using)


@receiver(post_save, sender=EmployeeProfile)
@receiver(post_save, sender=RoleFunctionPermission)
@receiver(post_save, sender=UserFunctionOverride)
@receiver(post_save, sender=OrganizationClientAccess)
@receiver(post_save, sender=VendorClientAllocation)
@receiver(post_save, sender=VendorCommercialProfile)
@receiver(post_save, sender=VendorSurveyAllocation)
@receiver(post_delete, sender=EmployeeProfile)
@receiver(post_delete, sender=RoleFunctionPermission)
@receiver(post_delete, sender=UserFunctionOverride)
@receiver(post_delete, sender=OrganizationClientAccess)
@receiver(post_delete, sender=VendorClientAllocation)
@receiver(post_delete, sender=VendorCommercialProfile)
@receiver(post_delete, sender=VendorSurveyAllocation)
def invalidate_projects_after_scope_change(**kwargs):
    def invalidate():
        invalidate_project_cache()
        invalidate_report_metadata_cache()

    _invalidate_now_and_after_commit(invalidate, using=kwargs.get("using"))


@receiver(post_save, sender=OrganizationUnit)
@receiver(post_delete, sender=OrganizationUnit)
def invalidate_activity_visibility_after_hierarchy_change(**kwargs):
    def invalidate():
        invalidate_activity_visibility_cache()
        invalidate_project_cache()
        invalidate_report_metadata_cache()

    _invalidate_now_and_after_commit(invalidate, using=kwargs.get("using"))


@receiver(m2m_changed, sender=VendorAPIKey.client_allocations.through)
def invalidate_project_counts_after_api_key_scope_change(*, action, **kwargs):
    """Expire key-scoped pagination totals when its client grants change."""

    if action in {"post_add", "post_remove", "post_clear"}:
        _invalidate_now_and_after_commit(
            lambda: invalidate_project_cache(filters=False, counts=True),
            using=kwargs.get("using"),
        )
