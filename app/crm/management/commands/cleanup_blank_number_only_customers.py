from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.feishu_sync import (
    CUSTOMER_FIELD_MAP,
    customer_values_have_create_content,
    mapped_fields,
    normalize_customer_values,
    sync_customer_record,
)
from crm.models import Customer, FeishuSyncSource, Reminder


CORE_FIELDS_TO_CHECK = (
    "legacy_customer_no",
    "lead_no",
    "name",
    "original_name",
    "contact_name",
    "contact_position",
    "phone",
    "wechat",
    "email",
    "region",
    "city",
    "industry",
    "customer_type",
    "demand",
    "customer_status_text",
    "notes",
    "duplicate_customer_no",
    "attachment_note",
    "attachment_file",
    "image",
)


class Command(BaseCommand):
    help = "删除只有客户编号、没有实际客户资料的空白客户。"

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true", help="确认正式删除。")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不删除。")
        parser.add_argument("--limit", type=int, default=0, help="最多删除多少条；0 表示不限制。")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if not dry_run and not options["confirm"]:
            raise CommandError("正式执行必须增加 --confirm，避免误删客户资料。")

        stats = Counter()
        delete_ids = []
        delete_numbers = []
        kept_examples = []

        customers = Customer.objects.filter(is_active=True).exclude(customer_no="").order_by("id")
        for customer in customers.iterator(chunk_size=500):
            stats["seen"] += 1
            blank, reason = self._is_blank_number_only(customer, stats, dry_run)
            if not blank:
                stats[f"kept_{reason}"] += 1
                if len(kept_examples) < 12:
                    kept_examples.append(f"{customer.customer_no}:{reason}")
                continue
            stats["candidates"] += 1
            delete_ids.append(customer.pk)
            delete_numbers.append(customer.customer_no)
            if options["limit"] and len(delete_ids) >= options["limit"]:
                break

        if not dry_run and delete_ids:
            with transaction.atomic():
                Reminder.objects.filter(customer_id__in=delete_ids).delete()
                Customer.objects.filter(pk__in=delete_ids).delete()
            stats["deleted"] = len(delete_ids)

        action = "预览" if dry_run else "完成"
        self.stdout.write(
            f"空白客户清理{action}：检查 {stats['seen']} 条，有空白候选 {stats['candidates']} 条，删除 {stats['deleted']} 条，"
            f"按飞书原始记录修复 {stats['feishu_repaired']} 条。"
        )
        if delete_numbers:
            self.stdout.write("删除客户编号示例：" + "，".join(delete_numbers[:80]))
        if kept_examples:
            self.stdout.write("保留示例：" + "；".join(kept_examples))
        self.stdout.write("保留原因统计：" + self._format_reasons(stats))

    def _is_blank_number_only(self, customer, stats, dry_run):
        if self._has_core_data(customer):
            return False, "core_field"
        if customer.tags.exists():
            return False, "tag"
        if customer.contact_logs.exists():
            return False, "contact_log"
        if customer.contracts.exists():
            return False, "contract"
        if customer.leads.exists():
            return False, "related_lead"
        feishu_raw_state = self._handle_meaningful_feishu_raw(customer, stats, dry_run)
        if feishu_raw_state == "repaired":
            return False, "feishu_repaired"
        if feishu_raw_state == "raw_kept":
            return False, "feishu_raw"
        return True, "blank"

    def _has_core_data(self, customer):
        for field in CORE_FIELDS_TO_CHECK:
            value = getattr(customer, field, "")
            if str(value or "").strip():
                return True
        return False

    def _handle_meaningful_feishu_raw(self, customer, stats, dry_run):
        has_meaningful_raw = False
        for record in customer.feishu_sync_records.select_related("source").all():
            source = record.source
            if source.source_kind != FeishuSyncSource.SourceKind.CUSTOMER:
                return "raw_kept"
            raw_fields = record.raw_fields or {}
            values = normalize_customer_values(mapped_fields(raw_fields, source, CUSTOMER_FIELD_MAP), source)
            if customer_values_have_create_content(values):
                has_meaningful_raw = True
                if dry_run:
                    continue
                try:
                    customer.feishu_source_name = source.name
                    customer.feishu_app_token = source.app_token
                    customer.feishu_table_id = source.table_id
                    customer.feishu_record_id = record.record_id
                    customer.save(update_fields=["feishu_source_name", "feishu_app_token", "feishu_table_id", "feishu_record_id", "updated_at"])
                    sync_customer_record(source, {"record_id": record.record_id, "fields": raw_fields})
                    customer.refresh_from_db()
                    if self._has_core_data(customer):
                        stats["feishu_repaired"] += 1
                        return "repaired"
                except Exception:
                    stats["feishu_repair_failed"] += 1
                    return "raw_kept"
        return "raw_kept" if has_meaningful_raw else ""

    def _format_reasons(self, stats):
        items = [
            (key.replace("kept_", ""), value)
            for key, value in stats.items()
            if key.startswith("kept_") and value
        ]
        if not items:
            return "无"
        items.sort(key=lambda item: (-item[1], item[0]))
        return "；".join(f"{key}×{value}" for key, value in items)
