from django.contrib.auth.models import User
from django.db import OperationalError, ProgrammingError
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=User)
def ensure_profile(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        Profile.objects.get_or_create(user=instance)
    except (OperationalError, ProgrammingError):
        # Keep Django admin user creation available while a deployment is
        # catching up the CRM profile table/columns.
        return
