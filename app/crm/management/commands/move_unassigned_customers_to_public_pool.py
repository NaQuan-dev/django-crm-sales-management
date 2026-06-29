from django.core.management.base import BaseCommand

from crm.models import Customer
from crm.services import public_pool_exempt_customer_q


class Command(BaseCommand):
    help = "把没有实际客户经理绑定的客户移入公海，报价中和成交客户除外。"

    def handle(self, *args, **options):
        qs = (
            Customer.objects.filter(is_active=True, owner__isnull=True)
            .exclude(status=Customer.Status.PUBLIC)
            .exclude(public_pool_exempt_customer_q())
        )
        count = 0
        for customer in qs:
            customer.status = Customer.Status.PUBLIC
            customer.release_warned_at = None
            customer.save(update_fields=["status", "owner", "owner_name", "release_warned_at", "updated_at"])
            count += 1
        self.stdout.write(self.style.SUCCESS(f"未分配客户已移入公海：{count} 条。"))
