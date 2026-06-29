import re
from uuid import uuid4

from django.core.management.base import BaseCommand
from django.db import transaction

from crm.models import CUSTOMER_NO_PREFIX, CUSTOMER_NO_WIDTH, Customer, is_system_customer_no


LEGACY_SPLIT_RE = re.compile(r"\s*[,，/;；]\s*")


def merge_legacy_customer_no(existing, old_no):
    existing = str(existing or "").strip()
    old_no = str(old_no or "").strip()
    if not old_no or is_system_customer_no(old_no):
        return existing
    parts = [part.strip() for part in LEGACY_SPLIT_RE.split(existing) if part.strip()]
    if old_no.lower() in {part.lower() for part in parts}:
        return existing
    candidate = f"{existing},{old_no}" if existing else old_no
    if len(candidate) <= Customer._meta.get_field("legacy_customer_no").max_length:
        return candidate
    return existing


class Command(BaseCommand):
    help = "统一重编客户编号为 NQKH 开头，并保留旧编号到历史客户编号。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="只预览，不写入数据库")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        customers = list(Customer.objects.order_by("id"))
        width = max(CUSTOMER_NO_WIDTH, len(str(len(customers) or 1)))
        planned = [(customer, f"{CUSTOMER_NO_PREFIX}{index:0{width}d}") for index, customer in enumerate(customers, start=1)]

        if dry_run:
            self.stdout.write(f"将重编客户编号 {len(planned)} 条，格式从 {CUSTOMER_NO_PREFIX}{1:0{width}d} 开始。")
            for customer, new_no in planned[:5]:
                self.stdout.write(f"{customer.pk}: {customer.customer_no} -> {new_no}")
            return

        batch = uuid4().hex[:8].upper()
        with transaction.atomic():
            locked = list(Customer.objects.select_for_update().order_by("id"))
            width = max(CUSTOMER_NO_WIDTH, len(str(len(locked) or 1)))
            for index, customer in enumerate(locked, start=1):
                temp_no = f"TMP{batch}{index:06d}"
                legacy_no = merge_legacy_customer_no(customer.legacy_customer_no, customer.customer_no)
                Customer.objects.filter(pk=customer.pk).update(customer_no=temp_no, legacy_customer_no=legacy_no)
            for index, customer in enumerate(locked, start=1):
                new_no = f"{CUSTOMER_NO_PREFIX}{index:0{width}d}"
                Customer.objects.filter(pk=customer.pk).update(customer_no=new_no)

        if locked:
            self.stdout.write(
                f"客户编号重编完成：{len(locked)} 条，范围 {CUSTOMER_NO_PREFIX}{1:0{width}d} - {CUSTOMER_NO_PREFIX}{len(locked):0{width}d}。"
            )
        else:
            self.stdout.write("客户编号重编完成：0 条。")