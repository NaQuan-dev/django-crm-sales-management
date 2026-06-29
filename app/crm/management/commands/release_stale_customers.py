from django.db.models import Count, Q
from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.models import Customer
from crm.services import public_pool_stale_cutoff, release_stale_customers


class Command(BaseCommand):
    help = "把超过公海规则期限未联系的客户移入公海。"

    def _stale_status_counts(self):
        cutoff = public_pool_stale_cutoff()
        stale_created_q = Q(historical_created_at__lt=cutoff) | Q(historical_created_at__isnull=True, created_at__lt=cutoff)
        stale_q = Q(last_contact_at__lt=cutoff) | (Q(last_contact_at__isnull=True) & stale_created_q)
        rows = (
            Customer.objects.filter(is_active=True)
            .filter(stale_q)
            .values("status")
            .annotate(total=Count("id"))
            .order_by("status")
        )
        return {row["status"]: row["total"] for row in rows}

    def handle(self, *args, **options):
        before = self._stale_status_counts()
        released = release_stale_customers()
        after = self._stale_status_counts()
        generated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
        self.stdout.write(self.style.SUCCESS(f"{generated_at} 已移入公海={released}"))
        self.stdout.write(f"执行前应入公海={before}")
        self.stdout.write(f"执行后仍应入公海={after}")
