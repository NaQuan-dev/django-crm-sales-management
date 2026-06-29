import csv
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.feishu_sync import parse_dt
from crm.models import Customer, is_system_customer_no, merge_wechat_values, resolve_or_create_user_by_feishu, split_phone_and_wechat
from crm.options import (
    GRADE_LABEL_TO_CODE,
    canonical_customer_statuses,
    canonical_customer_type,
    canonical_demands,
    canonical_source,
)


class Command(BaseCommand):
    help = "从飞书多维表格本地导出包查漏补缺客户资料；不覆盖非空字段，除非指定覆盖参数。"

    def add_arguments(self, parser):
        parser.add_argument("export_path", help="飞书客户系统导出目录或压缩包路径")
        parser.add_argument("--dry-run", action="store_true", help="只统计将要变更的数量，不写入数据库")
        parser.add_argument("--overwrite", action="store_true", help="允许用导出表覆盖客户系统中已有的非空字段")

    def handle(self, *args, **options):
        source_path = Path(options["export_path"]).expanduser()
        if not source_path.exists():
            raise CommandError(f"找不到导出路径: {source_path}")

        temp_dir = None
        try:
            export_root = source_path
            if source_path.is_file() and source_path.suffix.lower() == ".zip":
                temp_dir = Path(tempfile.mkdtemp(prefix="feishu-crm-export-"))
                with zipfile.ZipFile(source_path) as archive:
                    archive.extractall(temp_dir)
                export_root = self._find_export_root(temp_dir)

            stats = self.import_export(export_root, dry_run=options["dry_run"], overwrite=options["overwrite"])
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        action = "预演" if options["dry_run"] else "写入"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action}完成：读取 {stats['seen']} 条，新增 {stats['created']} 条，更新 {stats['updated']} 条，跳过 {stats['skipped']} 条。"
            )
        )

    def _find_export_root(self, root):
        if (root / "manifest.json").exists():
            return root
        for child in root.iterdir():
            if child.is_dir() and (child / "manifest.json").exists():
                return child
        return root

    def import_export(self, export_root, dry_run=False, overwrite=False):
        customer_dirs = [p for p in export_root.iterdir() if p.is_dir() and p.name.startswith("table_客户管理_")]
        lead_dirs = [p for p in export_root.iterdir() if p.is_dir() and p.name.startswith("table_线索管理_")]
        if not customer_dirs and not lead_dirs:
            raise CommandError("导出目录中没有找到客户管理或线索管理表。")

        stats = {"seen": 0, "created": 0, "updated": 0, "skipped": 0}
        with transaction.atomic():
            for table_dir in customer_dirs:
                self._import_table(table_dir, Customer.RecordKind.CUSTOMER, stats, overwrite)
            for table_dir in lead_dirs:
                self._import_table(table_dir, Customer.RecordKind.LEAD, stats, overwrite)
            if dry_run:
                transaction.set_rollback(True)
        return stats

    def _import_table(self, table_dir, source_kind, stats, overwrite):
        csv_path = table_dir / "records.csv"
        if not csv_path.exists():
            return
        raw_records = self._load_raw_records(table_dir / "records.json")
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                stats["seen"] += 1
                raw_record = raw_records.get(self._text(row, "record_id"), {})
                values = self._row_to_values(row, source_kind, raw_record, table_dir)
                if not self._has_identity(values):
                    stats["skipped"] += 1
                    continue
                customer = self._find_customer(values)
                creating = customer is None
                customer = customer or Customer()
                historical_created_at = values.get("historical_created_at")
                historical_changed = historical_created_at not in ("", None) and customer.historical_created_at != historical_created_at
                changed = self._apply_values(customer, values, overwrite)
                owner = resolve_or_create_user_by_feishu(customer.owner_name, values.get("_owner_feishu_id"), values.get("_owner_email"))
                owner_changed = bool(owner and customer.owner_id != owner.id)
                if not changed and not creating and not owner_changed and not historical_changed:
                    stats["skipped"] += 1
                    continue
                if changed or creating or historical_changed:
                    customer.save()
                if historical_changed:
                    Customer.objects.filter(pk=customer.pk).update(historical_created_at=historical_created_at)
                    customer.historical_created_at = historical_created_at
                if owner_changed:
                    customer.owner = owner
                    customer.save(update_fields=["owner", "updated_at"])
                stats["created" if creating else "updated"] += 1

    def _load_raw_records(self, records_path):
        if not records_path.exists():
            return {}
        with records_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, dict):
            records = data.get("items") or data.get("records") or data.get("data", {}).get("items") or []
        else:
            records = data if isinstance(data, list) else []
        result = {}
        for record in records:
            record_id = str(record.get("record_id") or record.get("id") or "").strip()
            if record_id:
                result[record_id] = record
        return result

    def _row_to_values(self, row, source_kind, raw_record=None, table_dir=None):
        owner_name, owner_feishu_id, owner_email = self._owner_identity(row, raw_record or {}, source_kind)
        name = self._text(row, "客户名称") or self._text(row, "客户名称/昵称") or self._text(row, "原始客户名称")
        original_name = self._text(row, "原始客户名称") or self._text(row, "客户名称/昵称")
        region = self._text(row, "地区")
        phone, phone_wechat = split_phone_and_wechat(self._text(row, "联系电话") or self._text(row, "客户电话"), region, name)
        wechat = merge_wechat_values(self._text(row, "微信号") or self._text(row, "微信"), phone_wechat)
        grade = GRADE_LABEL_TO_CODE.get(self._text(row, "客户级别"), "")
        status_text = canonical_customer_statuses(self._text(row, "客户状态"))
        demand = canonical_demands(self._text(row, "客户需求"))
        source_channel = canonical_source(self._text(row, "线索来源") or self._text(row, "账号来源") or self._text(row, "账户来源"))
        customer_type = canonical_customer_type(self._text(row, "客户类型"))
        old_customer_no = self._text(row, "客户编号")
        old_lead_no = self._text(row, "线索编号")
        system_customer_no = self._text(row, "系统客户编号")
        primary_customer_no = system_customer_no if is_system_customer_no(system_customer_no) else ""
        legacy_customer_no = old_customer_no or (system_customer_no if system_customer_no and not is_system_customer_no(system_customer_no) else "")
        values = {
            "_owner_feishu_id": owner_feishu_id,
            "_owner_email": owner_email,
            "source_kind": source_kind,
            "customer_no": primary_customer_no,
            "legacy_customer_no": legacy_customer_no,
            "lead_no": old_lead_no,
            "name": name,
            "original_name": original_name,
            "owner_name": owner_name,
            "contact_name": self._text(row, "联系人"),
            "phone": phone,
            "wechat": wechat,
            "email": self._text(row, "邮箱"),
            "region": region,
            "source_channel": source_channel,
            "customer_type": customer_type,
            "demand": demand,
            "lead_status": self._text(row, "线索状态"),
            "customer_status_text": status_text,
            "grade": grade,
            "duplicate_checked": self._bool_text(row, "已查重"),
            "duplicate_customer_no": self._text(row, "重复客户编号"),
            "attachment_note": self._text(row, "附件"),
            "historical_created_at": parse_dt(self._text(row, "历史创建时间") or self._text(row, "录入时间") or self._text(row, "创建时间")),
            "original_assigned_at": parse_dt(self._text(row, "原始分配时间")),
            "last_contact_at": parse_dt(self._text(row, "最后联系时间")),
            "next_contact_at": parse_dt(self._text(row, "下次联系时间")),
            "feishu_source_name": table_dir.name if table_dir else "",
            "feishu_record_id": self._text(row, "record_id"),
        }
        if values["grade"] == Customer.Grade.INVALID:
            values["status"] = Customer.Status.INVALID
        return values

    def _owner_identity(self, row, raw_record, source_kind):
        raw_fields = raw_record.get("fields") if isinstance(raw_record.get("fields"), dict) else raw_record
        user_field_names = ["客户负责人", "客户经理", "负责人"] if source_kind == Customer.RecordKind.CUSTOMER else ["分配给", "客户负责人", "客户经理", "负责人"]
        for field_name in user_field_names:
            name, user_id, email = self._user_value(raw_fields.get(field_name))
            if name or user_id or email:
                return name or self._text(row, field_name), user_id, email
        return self._text(row, "客户负责人") or self._text(row, "分配给"), "", ""

    def _user_value(self, value):
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            name = str(value.get("name") or value.get("text") or value.get("en_name") or value.get("email") or "").strip()
            user_id = str(value.get("open_id") or value.get("id") or value.get("user_id") or "").strip()
            email = str(value.get("email") or "").strip()
            return name, user_id, email
        return str(value or "").strip(), "", ""

    def _apply_values(self, customer, values, overwrite):
        changed = False
        for field, value in values.items():
            if field.startswith("_"):
                continue
            if field == "historical_created_at":
                continue
            if value in ("", None):
                continue
            current = getattr(customer, field, None)
            should_set = overwrite or current in ("", None)
            if field == "grade" and value and current == Customer.Grade.POTENTIAL:
                should_set = True
            if field == "owner_name" and value and (not current or current == getattr(customer, "contact_name", "")):
                should_set = True
            if field == "contact_name" and value == getattr(customer, "owner_name", ""):
                should_set = False
            if should_set and current != value:
                setattr(customer, field, value)
                changed = True
        manager = values.get("owner_name")
        if manager and customer.contact_name == manager:
            customer.contact_name = ""
            changed = True
        return changed

    def _find_customer(self, values):
        record_id = values.get("feishu_record_id")
        source_name = values.get("feishu_source_name")
        if record_id and source_name:
            found = Customer.objects.filter(feishu_source_name=source_name, feishu_record_id=record_id).first()
            if found:
                return found

        for value in self._number_values(values):
            found = (
                Customer.objects.filter(customer_no=value).first()
                or Customer.objects.filter(legacy_customer_no=value).first()
                or Customer.objects.filter(lead_no=value).first()
            )
            if found:
                return found

        lookups = [
            ("phone", values.get("phone")),
            ("wechat", values.get("wechat")),
            ("email", values.get("email")),
        ]
        for field, value in lookups:
            if not value:
                continue
            found = Customer.objects.filter(**{field: value}).first()
            if found:
                return found
        for alias in self._name_aliases(values.get("name"), values.get("original_name")):
            found = Customer.objects.filter(name__iexact=alias).first() or Customer.objects.filter(original_name__iexact=alias).first()
            if found:
                return found
        return None

    def _has_identity(self, values):
        return any(values.get(field) for field in ["customer_no", "legacy_customer_no", "lead_no", "name", "phone", "wechat", "email", "feishu_record_id"])

    def _text(self, row, key):
        return str(row.get(key) or "").strip()

    def _bool_text(self, row, key):
        text = self._text(row, key)
        if not text:
            return None
        return text in {"是", "true", "True", "1", "已查重"}

    def _number_values(self, values):
        result = []
        seen = set()
        for field in ("customer_no", "legacy_customer_no", "lead_no"):
            value = str(values.get(field) or "").strip()
            if value and value.lower() not in seen:
                seen.add(value.lower())
                result.append(value)
        return result

    def _name_aliases(self, *values):
        aliases = []
        seen = set()
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            without_serial = re.sub(r"^\s*\d+[\s._-]*", "", text).strip()
            without_region = re.sub(r"\s*[-–—]\s*[\u4e00-\u9fffA-Za-z ]{1,30}$", "", text).strip()
            candidates = {
                text,
                without_serial,
                without_region,
                re.sub(r"\s*[-–—]\s*[\u4e00-\u9fffA-Za-z ]{1,30}$", "", without_serial).strip(),
            }
            for item in candidates:
                if not item:
                    continue
                key = item.lower()
                if key not in seen:
                    seen.add(key)
                    aliases.append(item)
        return aliases
