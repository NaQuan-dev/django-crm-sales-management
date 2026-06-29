from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from crm.feishu_sync import FeishuClient, is_inquiry_source, sync_source, upsert_env_sources
from crm.models import AuditLog, Customer, FeishuSyncRecord, FeishuSyncSource, Reminder


class Command(BaseCommand):
    help = "重置飞书电子表格“询盘”同步客户，并可立即重新同步。"

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true", help="确认正式删除询盘同步新建的客户和旧同步记录。")
        parser.add_argument("--dry-run", action="store_true", help="只统计将要处理的数据，不写入数据库。")
        parser.add_argument("--sync-after", action="store_true", help="重置后立即重新同步飞书来源。")

    def handle(self, *args, **options):
        if not options["dry_run"] and not options["confirm"]:
            raise CommandError("正式执行必须增加 --confirm，避免误删客户资料。")

        upsert_env_sources()
        sources = [
            source
            for source in FeishuSyncSource.objects.filter(
                source_type=FeishuSyncSource.SourceType.SHEET,
                source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
            )
            if is_inquiry_source(source)
        ]
        if not sources:
            self.stdout.write("未找到启用或已配置的“询盘”飞书电子表格同步源。")
            return

        stats = self._reset_sources(sources, dry_run=options["dry_run"])
        self.stdout.write(
            "询盘重置%s：同步源 %s 个，旧同步记录 %s 条，删除询盘新建客户 %s 条，保留待重新合并客户 %s 条。"
            % (
                "预览" if options["dry_run"] else "完成",
                len(sources),
                stats["sync_records"],
                stats["deleted_customers"],
                stats["preserved_customers"],
            )
        )

        if options["sync_after"] and not options["dry_run"]:
            client = FeishuClient()
            self.stdout.write("重新同步结果：")
            for source in sources:
                self.stdout.write(str(sync_source(client, source, progress=self._progress)))

    def _progress(self, message):
        self.stdout.write(message)
        self.stdout.flush()

    @transaction.atomic
    def _reset_sources(self, sources, dry_run=False):
        source_ids = [source.id for source in sources]
        records = FeishuSyncRecord.objects.filter(source_id__in=source_ids).select_related("customer")
        linked_customer_ids = set(records.exclude(customer__isnull=True).values_list("customer_id", flat=True))

        source_marker_query = Q()
        audit_detail_query = Q()
        for source in sources:
            source_marker_query |= Q(feishu_app_token=source.app_token, feishu_table_id=source.table_id)
            audit_detail_query |= Q(detail__startswith=f"{source.name}:")
        marked_customer_ids = set(Customer.objects.filter(source_marker_query).values_list("id", flat=True)) if source_marker_query else set()
        candidate_customer_ids = linked_customer_ids | marked_customer_ids

        created_by_inquiry_ids = set()
        if candidate_customer_ids:
            created_by_inquiry_ids = {
                int(value)
                for value in AuditLog.objects.filter(
                    audit_detail_query,
                    action="飞书同步新增客户",
                    target_type="客户",
                    target_id__in=[str(pk) for pk in candidate_customer_ids],
                ).values_list("target_id", flat=True)
                if str(value).isdigit()
            }

        delete_customer_ids = candidate_customer_ids & created_by_inquiry_ids
        preserve_customer_ids = candidate_customer_ids - delete_customer_ids
        stats = {
            "sync_records": records.count(),
            "deleted_customers": len(delete_customer_ids),
            "preserved_customers": len(preserve_customer_ids),
        }
        if dry_run:
            return stats

        if preserve_customer_ids:
            Customer.objects.filter(pk__in=preserve_customer_ids).update(
                feishu_source_name="",
                feishu_app_token="",
                feishu_table_id="",
                feishu_record_id="",
            )
        if delete_customer_ids:
            Reminder.objects.filter(customer_id__in=delete_customer_ids).delete()
            Customer.objects.filter(pk__in=delete_customer_ids).delete()
        records.delete()
        for source in sources:
            source.last_result = "已重置“询盘”同步，等待重新按序号倒序清洗合并。"
            source.last_error = ""
            source.save(update_fields=["last_result", "last_error", "updated_at"])
        return stats
