import shutil
import tempfile
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.management.commands.import_feishu_crm_export import Command as ImportCustomerCommand
from crm.models import ArchivedCustomerSnapshot, Customer


class Command(BaseCommand):
    help = "归档当前客户表后，清空客户表，并从旧飞书导出重新导入客户。"

    def add_arguments(self, parser):
        parser.add_argument("export_path", help="旧飞书客户系统导出目录或压缩包路径")
        parser.add_argument("--dry-run", action="store_true", help="只预演统计，不写入数据库")
        parser.add_argument(
            "--confirm-delete-current",
            action="store_true",
            help="确认先归档再删除当前客户表，并用旧飞书导出重建",
        )
        parser.add_argument(
            "--reason",
            default="从旧飞书客户系统导出重建",
            help="写入归档表的操作原因",
        )

    def handle(self, *args, **options):
        source_path = Path(options["export_path"]).expanduser()
        if not source_path.exists():
            raise CommandError(f"找不到导出路径: {source_path}")
        if not options["dry_run"] and not options["confirm_delete_current"]:
            raise CommandError("正式执行必须增加 --confirm-delete-current，避免误删当前客户表。")

        temp_dir = None
        importer = ImportCustomerCommand()
        try:
            export_root = source_path
            if source_path.is_file() and source_path.suffix.lower() == ".zip":
                temp_dir = Path(tempfile.mkdtemp(prefix="feishu-crm-rebuild-"))
                with zipfile.ZipFile(source_path) as archive:
                    archive.extractall(temp_dir)
                export_root = importer._find_export_root(temp_dir)

            stats = self.rebuild_from_export(
                importer=importer,
                export_root=export_root,
                dry_run=options["dry_run"],
                reason=options["reason"],
            )
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        action = "预演" if options["dry_run"] else "重建"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action}完成：归档当前客户 {stats['archived']} 条，删除当前客户 {stats['deleted_customers']} 条，"
                f"导入读取 {stats['import_seen']} 条，新增 {stats['import_created']} 条，"
                f"更新 {stats['import_updated']} 条，跳过 {stats['import_skipped']} 条，归档批次 {stats['batch_id']}。"
            )
        )
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("这是预演，数据库没有保留任何变更。"))

    def rebuild_from_export(self, importer, export_root, dry_run=False, reason=""):
        batch_id = timezone.localtime(timezone.now()).strftime("rebuild-%Y%m%d%H%M%S")
        with transaction.atomic():
            archived = self._archive_current_customers(batch_id, reason)
            deleted_customers = Customer.objects.count()
            Customer.objects.all().delete()
            import_stats = importer.import_export(export_root, dry_run=False, overwrite=True)
            if dry_run:
                transaction.set_rollback(True)
        return {
            "batch_id": batch_id,
            "archived": archived,
            "deleted_customers": deleted_customers,
            "import_seen": import_stats["seen"],
            "import_created": import_stats["created"],
            "import_updated": import_stats["updated"],
            "import_skipped": import_stats["skipped"],
        }

    def _archive_current_customers(self, batch_id, reason):
        archived = 0
        customers = Customer.objects.prefetch_related("tags", "contact_logs", "contracts", "reminders")
        for customer in customers:
            ArchivedCustomerSnapshot.objects.create(
                batch_id=batch_id,
                original_customer_id=customer.pk,
                customer_no=customer.customer_no,
                name=customer.name,
                source_kind=customer.source_kind,
                reason=reason,
                payload=self._serialize_model(customer),
                related_payload={
                    "tags": [{"id": tag.pk, "name": tag.name, "category": tag.category} for tag in customer.tags.all()],
                    "contact_logs": [self._serialize_model(item) for item in customer.contact_logs.all()],
                    "contracts": [self._serialize_model(item) for item in customer.contracts.all()],
                    "reminders": [self._serialize_model(item) for item in customer.reminders.all()],
                },
            )
            archived += 1
        return archived

    def _serialize_model(self, instance):
        result = {}
        for field in instance._meta.concrete_fields:
            result[field.attname] = self._json_value(getattr(instance, field.attname))
        return result

    def _json_value(self, value):
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "name"):
            return value.name
        return value
