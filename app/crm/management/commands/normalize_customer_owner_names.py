from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.models import Customer, display_owner_name


class Command(BaseCommand):
    help = "Normalize customer owner_name from usernames or legacy owner codes to salesperson display names."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only report changes without writing data.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        checked = 0
        updated = 0
        samples = []
        now = timezone.now()
        qs = Customer.objects.select_related("owner").all().order_by("id")
        for customer in qs.iterator(chunk_size=500):
            checked += 1
            if customer.owner_id:
                raw = customer.owner.get_full_name() or customer.owner.username
                normalized = display_owner_name(raw, resolve_user=True)
            else:
                normalized = display_owner_name(customer.owner_name, resolve_user=True)
            if not normalized or normalized == customer.owner_name:
                continue
            updated += 1
            if len(samples) < 20:
                samples.append(f"{customer.customer_no or customer.pk}: {customer.owner_name or '空'} -> {normalized}")
            if not dry_run:
                Customer.objects.filter(pk=customer.pk).update(owner_name=normalized, updated_at=now)
        for sample in samples:
            self.stdout.write(sample)
        mode = "DRY_RUN" if dry_run else "UPDATED"
        self.stdout.write(self.style.SUCCESS(f"{mode}: checked={checked}, changed={updated}"))