from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm"
    verbose_name = "客户系统"

    def ready(self):
        from . import signals  # noqa: F401
