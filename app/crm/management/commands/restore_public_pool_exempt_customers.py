from django.core.management.base import BaseCommand

from crm.models import Customer
from crm.services import is_deal_customer, public_pool_exempt_customer_q


class Command(BaseCommand):
    help = "把不应进入公海的报价中/成交客户恢复为私有或成交客户。"

    def handle(self, *args, **options):
        stats = {"seen": 0, "restored_private": 0, "restored_deal": 0}
        qs = Customer.objects.filter(
            is_active=True,
            status=Customer.Status.PUBLIC,
        ).filter(public_pool_exempt_customer_q()).select_related("owner")
        for customer in qs:
            stats["seen"] += 1
            if is_deal_customer(customer):
                customer.status = Customer.Status.DEAL
                stats["restored_deal"] += 1
            else:
                customer.status = Customer.Status.PRIVATE
                stats["restored_private"] += 1
            customer.release_warned_at = None
            customer.save(update_fields=["status", "owner", "owner_name", "release_warned_at", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"报价中/成交客户公海恢复完成：{stats}"))
