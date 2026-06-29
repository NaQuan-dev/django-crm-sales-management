from django.core.management.base import BaseCommand

from crm.models import Customer


class Command(BaseCommand):
    help = "解除公海客户的实际负责人绑定，同时保留客户经理名称。"

    def handle(self, *args, **options):
        count = 0
        for customer in Customer.objects.filter(status=Customer.Status.PUBLIC, owner__isnull=False).select_related("owner"):
            if not customer.owner_name:
                customer.owner_name = customer.owner.get_full_name() or customer.owner.username
            customer.owner = None
            customer.save(update_fields=["owner", "owner_name", "updated_at"])
            count += 1
        self.stdout.write(self.style.SUCCESS(f"公海客户实际负责人绑定已清理，客户经理名称已保留：{count} 条。"))
