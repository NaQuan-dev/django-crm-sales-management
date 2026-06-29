import re

from django.core.management.base import BaseCommand

from crm.models import AuditLog, Customer, Lead
from crm.options import canonical_source


SOURCE_VALUE_MAP = {
    "ins": "ins",
    "instagram": "ins",
    "fb": "脸书",
    "facebook": "脸书",
}

TARGET_TYPE_MAP = {
    "customer": "客户",
    "lead": "线索",
    "contract": "合同",
    "contact_log": "跟进日志",
}

ACTION_MAP = {
    "release_to_public_pool": "自动移入公海",
    "feishu_sync_customer_created": "飞书同步新增客户",
    "feishu_sync_customer_updated": "飞书同步更新客户",
}


class Command(BaseCommand):
    help = "把系统字段中遗留的英文显示值翻译成中文；不会修改客户名称。"

    def handle(self, *args, **options):
        updated = 0
        updated += self._localize_source_fields(Customer, ["source_channel", "account_source"])
        updated += self._localize_source_fields(Lead, ["source_channel"])
        updated += self._localize_audit_logs()
        self.stdout.write(self.style.SUCCESS(f"系统英文显示值清理完成：更新 {updated} 处。"))

    def _localize_source_fields(self, model, field_names):
        count = 0
        for field_name in field_names:
            for obj in model.objects.exclude(**{field_name: ""}).only("pk", field_name):
                old_value = getattr(obj, field_name) or ""
                new_value = SOURCE_VALUE_MAP.get(str(old_value).strip().lower()) or canonical_source(old_value)
                if new_value and old_value != new_value:
                    setattr(obj, field_name, new_value)
                    obj.save(update_fields=[field_name])
                    count += 1
        return count

    def _localize_audit_logs(self):
        count = 0
        for log in AuditLog.objects.all().only("pk", "action", "target_type", "detail"):
            update_fields = []
            new_action = ACTION_MAP.get(log.action)
            if new_action:
                log.action = new_action
                update_fields.append("action")
            new_target = TARGET_TYPE_MAP.get(log.target_type)
            if new_target:
                log.target_type = new_target
                update_fields.append("target_type")
            new_detail = self._localize_audit_detail(log.detail)
            if new_detail != log.detail:
                log.detail = new_detail
                update_fields.append("detail")
            if update_fields:
                log.save(update_fields=update_fields)
                count += len(update_fields)
        return count

    def _localize_audit_detail(self, detail):
        text = str(detail or "")
        match = re.match(r"Released from owner (.*?) after more than (\d+) days no contact\.", text)
        if match:
            owner, days = match.groups()
            owner_text = owner or "未分配"
            return f"原负责人 {owner_text} 超过 {days} 天未联系，系统自动移入公海。"
        return text
