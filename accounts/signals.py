from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import EmployeeProfile, Role


@receiver(post_save, sender=get_user_model())
def ensure_employee_profile(sender, instance, created, **kwargs):
    if not created:
        return
    role_slug = "super-admin" if instance.is_superuser else "employee"
    role = Role.objects.filter(slug=role_slug).first()
    EmployeeProfile.objects.get_or_create(user=instance, defaults={"role": role})

