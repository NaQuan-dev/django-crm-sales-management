from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.management.commands.import_eoffice_customers import Command as EofficeCustomerImportCommand
from crm.models import ContactLog
from crm.options import canonical_customer_statuses
from crm.services import update_customer_after_contact


class Command(BaseCommand):
    help = "把 e-office 客户沟通记录导入为跟进日志。"

    def add_arguments(self, parser):
        parser.add_argument("excel_path", help="e-office 客户信息 .xlsx 文件路径")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不写入数据库")

    def handle(self, *args, **options):
        excel_path = Path(options["excel_path"]).expanduser()
        if not excel_path.exists():
            raise CommandError(f"找不到 Excel 文件: {excel_path}")
        if excel_path.suffix.lower() != ".xlsx":
            raise CommandError("当前命令只支持 .xlsx 文件。")

        rows = EofficeCustomerImportCommand()._load_rows(excel_path)
        stats = self.import_rows(rows, dry_run=options["dry_run"])
        action = "预演" if options["dry_run"] else "写入"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action}完成：读取 {stats['seen']} 行，有沟通记录 {stats['with_summary']} 行，"
                f"匹配客户 {stats['matched']} 行，新增跟进 {stats['created']} 条，"
                f"重复跳过 {stats['duplicates']} 条，缺客户 {stats['missing_customer']} 行，空记录 {stats['missing_summary']} 行。"
            )
        )

    def import_rows(self, rows, dry_run=False):
        helper = EofficeCustomerImportCommand()
        stats = {
            "seen": 0,
            "with_summary": 0,
            "matched": 0,
            "created": 0,
            "duplicates": 0,
            "missing_customer": 0,
            "missing_summary": 0,
        }
        with transaction.atomic():
            for row in rows:
                stats["seen"] += 1
                summary = self._text(row, "沟通记录")
                if not summary:
                    stats["missing_summary"] += 1
                    continue
                stats["with_summary"] += 1

                customer_no = self._text(row, "客户编号")
                customer = helper._find_customer(customer_no) if customer_no else None
                if customer is None:
                    stats["missing_customer"] += 1
                    continue
                stats["matched"] += 1

                contact_at = (
                    helper._parse_datetime(self._text(row, "最后联系时间"))
                    or helper._parse_datetime(self._text(row, "创建时间"))
                    or customer.created_time_display
                    or timezone.now()
                )
                next_contact_at = helper._parse_datetime(self._text(row, "下次联系时间"))
                follower_name = self._text(row, "客户经理") or customer.owner_display
                result = canonical_customer_statuses(self._text(row, "客户状态")) or self._text(row, "客户状态")

                if self._exists(customer, contact_at, summary, follower_name):
                    stats["duplicates"] += 1
                    continue

                if dry_run:
                    stats["created"] += 1
                    continue

                log = ContactLog.objects.create(
                    customer=customer,
                    contact_at=contact_at,
                    next_contact_at=next_contact_at,
                    method=self._method(row, summary),
                    source=ContactLog.Source.IMPORT,
                    follower_name=follower_name,
                    summary=summary,
                    result=result[:160],
                )
                update_customer_after_contact(customer, log)
                stats["created"] += 1
            if dry_run:
                transaction.set_rollback(True)
        return stats

    def _exists(self, customer, contact_at, summary, follower_name):
        return ContactLog.objects.filter(
            customer=customer,
            source=ContactLog.Source.IMPORT,
            contact_at=contact_at,
            summary=summary,
            follower_name=follower_name,
        ).exists()

    def _method(self, row, summary):
        text = " ".join(
            [summary, self._text(row, "客户状态"), self._text(row, "微信"), self._text(row, "客户电话")]
        )
        if "微信" in text or self._text(row, "微信"):
            return ContactLog.Method.WECHAT
        if self._text(row, "客户电话"):
            return ContactLog.Method.PHONE
        return ContactLog.Method.OTHER

    def _text(self, row, key):
        return str(row.get(key) or "").strip()
