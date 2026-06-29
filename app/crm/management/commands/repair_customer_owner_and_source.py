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
from crm.models import (
    Customer,
    merge_region_city,
    merge_wechat_values,
    normalize_phone_number_for_region,
    resolve_or_create_user_by_feishu,
    split_phone_and_wechat,
)
from crm.options import GRADE_LABEL_TO_CODE, canonical_customer_type, canonical_demands, canonical_source


class Command(BaseCommand):
    help = "一次性修复历史客户数据：客户经理绑定、电话格式、账号来源合并、联系时间补全。"

    def add_arguments(self, parser):
        parser.add_argument("export_path", nargs="?", help="可选：飞书客户系统导出目录或压缩包，用于按飞书用户标识绑定客户经理并补日期")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不写入数据库")
        parser.add_argument("--force-move-contact", action="store_true", help="即使已有客户经理，也把联系人强制迁移到客户经理")
        parser.add_argument("--overwrite-dates", action="store_true", help="允许用飞书导出覆盖已有最后联系时间/下次联系时间")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        force_move_contact = options["force_move_contact"]
        overwrite_dates = options["overwrite_dates"]
        export_bundle = self._load_export_bundle(options.get("export_path"))
        stats = {
            "seen": 0,
            "moved_owner": 0,
            "bound_owner": 0,
            "merged_source": 0,
            "cleared_account_source": 0,
            "normalized_phone": 0,
            "filled_last_contact": 0,
            "filled_next_contact": 0,
            "matched_export": 0,
            "merged_location": 0,
            "moved_wechat": 0,
            "restored_grade": 0,
            "filled_customer_type": 0,
            "filled_demand": 0,
            "matched_by_customer_no": 0,
        }
        with transaction.atomic():
            for customer in Customer.objects.filter(is_active=True).select_related("owner"):
                stats["seen"] += 1
                changed_fields = []
                export_values = self._match_export_customer(customer, export_bundle)
                if export_values:
                    stats["matched_export"] += 1
                    if export_values.get("_matched_by_customer_no"):
                        stats["matched_by_customer_no"] += 1
                    owner_name = export_values.get("owner_name")
                    if owner_name and customer.owner_name != owner_name:
                        customer.owner_name = owner_name
                        changed_fields.append("owner_name")

                    if export_values.get("source_channel") and not customer.source_channel:
                        customer.source_channel = export_values["source_channel"]
                        changed_fields.append("source_channel")

                    if export_values.get("last_contact_at") and (overwrite_dates or not customer.last_contact_at):
                        customer.last_contact_at = export_values["last_contact_at"]
                        changed_fields.append("last_contact_at")
                        stats["filled_last_contact"] += 1

                    if export_values.get("next_contact_at") and (overwrite_dates or not customer.next_contact_at):
                        customer.next_contact_at = export_values["next_contact_at"]
                        changed_fields.append("next_contact_at")
                        stats["filled_next_contact"] += 1

                    if export_values.get("historical_created_at") and not customer.historical_created_at:
                        customer.historical_created_at = export_values["historical_created_at"]
                        changed_fields.append("historical_created_at")

                    if export_values.get("grade") and customer.grade != export_values["grade"]:
                        customer.grade = export_values["grade"]
                        changed_fields.append("grade")
                        stats["restored_grade"] += 1
                        if customer.grade == Customer.Grade.INVALID and customer.status != Customer.Status.INVALID:
                            customer.status = Customer.Status.INVALID
                            changed_fields.append("status")

                    if export_values.get("customer_type") and not customer.customer_type:
                        customer.customer_type = export_values["customer_type"]
                        changed_fields.append("customer_type")
                        stats["filled_customer_type"] += 1

                    if export_values.get("demand") and not customer.demand:
                        customer.demand = export_values["demand"]
                        changed_fields.append("demand")
                        stats["filled_demand"] += 1

                    if export_values.get("phone") and not customer.phone:
                        customer.phone = export_values["phone"]
                        changed_fields.append("phone")

                    if export_values.get("wechat") and not customer.wechat:
                        customer.wechat = export_values["wechat"]
                        changed_fields.append("wechat")
                        stats["moved_wechat"] += 1

                    if export_values.get("region") and not customer.region:
                        customer.region = export_values["region"]
                        changed_fields.append("region")

                should_move_contact = bool(customer.contact_name) and (
                    force_move_contact
                    or customer.contact_name == customer.owner_name
                    or customer.contact_name in export_bundle.get("sales_names", set())
                    or (not customer.owner_id and not customer.owner_name)
                )
                if should_move_contact:
                    if not customer.owner_name:
                        customer.owner_name = customer.contact_name
                        changed_fields.append("owner_name")
                    customer.contact_name = ""
                    changed_fields.append("contact_name")
                    stats["moved_owner"] += 1

                if customer.owner_name and not customer.owner_id:
                    owner_info = export_values or export_bundle.get("sales_by_name", {}).get(customer.owner_name, {})
                    owner = resolve_or_create_user_by_feishu(
                        customer.owner_name,
                        owner_info.get("owner_feishu_id", ""),
                        owner_info.get("owner_email", ""),
                    )
                    if owner:
                        customer.owner = owner
                        changed_fields.append("owner")
                        stats["bound_owner"] += 1

                if customer.account_source:
                    merged_source = customer.source_channel or canonical_source(customer.account_source)
                    if merged_source != customer.source_channel:
                        customer.source_channel = merged_source
                        changed_fields.append("source_channel")
                        stats["merged_source"] += 1
                    if not customer.source_channel or customer.source_channel == merged_source:
                        customer.account_source = ""
                        changed_fields.append("account_source")
                        stats["cleared_account_source"] += 1

                merged_location = merge_region_city(customer.region, customer.city)
                if merged_location != customer.region or customer.city:
                    customer.region = merged_location
                    customer.city = ""
                    changed_fields.extend(["region", "city"])
                    stats["merged_location"] += 1

                normalized_phone = normalize_phone_number_for_region(customer.phone, customer.region, customer.name)
                normalized_phone, phone_wechat = split_phone_and_wechat(normalized_phone, customer.region, customer.name)
                merged_wechat = merge_wechat_values(customer.wechat, phone_wechat)
                if merged_wechat != customer.wechat:
                    customer.wechat = merged_wechat
                    changed_fields.append("wechat")
                    stats["moved_wechat"] += 1
                if normalized_phone != customer.phone:
                    customer.phone = normalized_phone
                    changed_fields.append("phone")
                    stats["normalized_phone"] += 1

                if changed_fields:
                    changed_fields.append("updated_at")
                    customer.save(update_fields=sorted(set(changed_fields)))

            if dry_run:
                transaction.set_rollback(True)

        action = "预演" if dry_run else "修复"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action}完成：检查 {stats['seen']} 条，联系人迁移 {stats['moved_owner']} 条，"
                f"绑定客户经理 {stats['bound_owner']} 条，来源合并 {stats['merged_source']} 条，"
                f"清空旧账号来源 {stats['cleared_account_source']} 条，电话标准化 {stats['normalized_phone']} 条，"
                f"电话栏微信迁移 {stats['moved_wechat']} 条，"
                f"地区合并 {stats['merged_location']} 条，"
                f"匹配飞书导出 {stats['matched_export']} 条，其中按客户编号匹配 {stats['matched_by_customer_no']} 条，"
                f"补最后联系时间 {stats['filled_last_contact']} 条，"
                f"补下次联系时间 {stats['filled_next_contact']} 条，恢复客户级别 {stats['restored_grade']} 条，"
                f"补客户类型 {stats['filled_customer_type']} 条，补客户需求 {stats['filled_demand']} 条。"
            )
        )

    def _load_export_bundle(self, export_path):
        if not export_path:
            return {"records_by_key": {}, "ambiguous_keys": set(), "sales_names": set(), "sales_by_name": {}}

        source_path = Path(export_path).expanduser()
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
            return self._read_export_root(export_root)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _find_export_root(self, root):
        if (root / "manifest.json").exists():
            return root
        for child in root.iterdir():
            if child.is_dir() and (child / "manifest.json").exists():
                return child
        return root

    def _read_export_root(self, export_root):
        bundle = {"records_by_key": {}, "ambiguous_keys": set(), "sales_names": set(), "sales_by_name": {}}
        for table_dir in export_root.iterdir():
            if not table_dir.is_dir():
                continue
            if table_dir.name.startswith("table_销售人员管理_"):
                self._read_sales_table(table_dir, bundle)
        for table_dir in export_root.iterdir():
            if not table_dir.is_dir():
                continue
            if table_dir.name.startswith("table_客户管理_"):
                self._read_customer_table(table_dir, Customer.RecordKind.CUSTOMER, bundle)
            elif table_dir.name.startswith("table_线索管理_"):
                self._read_customer_table(table_dir, Customer.RecordKind.LEAD, bundle)
        return bundle

    def _read_sales_table(self, table_dir, bundle):
        raw_records = self._load_raw_records(table_dir / "records.json")
        for record in raw_records.values():
            for field_name in ("销售", "销售经理"):
                name, user_id, email = self._user_value(record.get(field_name))
                if name:
                    bundle["sales_names"].add(name)
                    bundle["sales_by_name"][name] = {
                        "owner_name": name,
                        "owner_feishu_id": user_id,
                        "owner_email": email,
                    }
            formula_name = str(record.get("销售姓名") or "").strip()
            if formula_name:
                bundle["sales_names"].add(formula_name)

    def _read_customer_table(self, table_dir, source_kind, bundle):
        csv_path = table_dir / "records.csv"
        if not csv_path.exists():
            return
        raw_records = self._load_raw_records(table_dir / "records.json")
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
            for row in csv.DictReader(fp):
                raw_record = raw_records.get(self._text(row, "record_id"), {})
                values = self._row_to_export_values(row, raw_record, source_kind)
                for key in self._export_keys(values):
                    self._add_export_key(bundle, key, values)

    def _add_export_key(self, bundle, key, values):
        if key in bundle["ambiguous_keys"]:
            return
        existing = bundle["records_by_key"].get(key)
        if not existing:
            bundle["records_by_key"][key] = values
            return
        if existing.get("record_id") != values.get("record_id"):
            bundle["ambiguous_keys"].add(key)
            bundle["records_by_key"].pop(key, None)

    def _load_raw_records(self, records_path):
        if not records_path.exists():
            return {}
        with records_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        records = data if isinstance(data, list) else data.get("items") or data.get("records") or data.get("data", {}).get("items") or []
        result = {}
        for record in records:
            record_id = str(record.get("record_id") or record.get("id") or "").strip()
            if record_id:
                result[record_id] = record
        return result

    def _row_to_export_values(self, row, raw_record, source_kind):
        raw_fields = raw_record.get("fields") if isinstance(raw_record.get("fields"), dict) else raw_record
        owner_name, owner_feishu_id, owner_email = self._owner_identity(row, raw_fields, source_kind)
        name = self._text(row, "客户名称") or self._text(row, "客户名称/昵称") or self._text(row, "原始客户名称")
        original_name = self._text(row, "原始客户名称") or self._text(row, "客户名称/昵称")
        region = self._text(row, "地区")
        phone, phone_wechat = split_phone_and_wechat(self._text(row, "联系电话") or self._text(row, "客户电话"), region, name)
        wechat = merge_wechat_values(self._text(row, "微信号") or self._text(row, "微信"), phone_wechat)
        grade = self._grade_code(self._text(row, "客户级别") or self._text(row, "OA客户级别") or self._text(row, "客户等级"))
        customer_type = self._customer_type_value(self._text(row, "客户类型") or self._text(row, "OA客户类型"))
        demand = self._demand_value(self._text(row, "客户需求") or self._text(row, "OA客户需求") or self._text(row, "需求"))
        return {
            "source_kind": source_kind,
            "record_id": self._text(row, "record_id"),
            "customer_no": self._text(row, "系统客户编号"),
            "legacy_customer_no": self._text(row, "客户编号"),
            "lead_no": self._text(row, "线索编号"),
            "name": name,
            "original_name": original_name,
            "phone": phone,
            "wechat": wechat,
            "email": self._text(row, "邮箱"),
            "region": region,
            "source_channel": canonical_source(self._text(row, "线索来源") or self._text(row, "账号来源") or self._text(row, "账户来源")),
            "grade": grade,
            "customer_type": customer_type,
            "demand": demand,
            "owner_name": owner_name,
            "owner_feishu_id": owner_feishu_id,
            "owner_email": owner_email,
            "historical_created_at": parse_dt(self._text(row, "历史创建时间") or self._text(row, "录入时间") or self._text(row, "创建时间")),
            "last_contact_at": parse_dt(
                self._text(row, "最后联系时间")
                or self._text(row, "最近跟进时间")
                or self._text(row, "OA最后联系时间")
                or self._text(row, "OA最近跟进时间")
                or self._text(row, "最后跟进时间")
            ),
            "next_contact_at": parse_dt(self._text(row, "下次联系时间") or self._text(row, "OA下次联系时间")),
        }

    def _owner_identity(self, row, raw_fields, source_kind):
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

    def _match_export_customer(self, customer, bundle):
        for key in self._customer_number_keys(customer):
            if key in bundle.get("ambiguous_keys", set()):
                continue
            found = bundle["records_by_key"].get(key)
            if found:
                result = dict(found)
                result["_matched_by_customer_no"] = True
                return result
        if self._has_customer_number(customer):
            return {}
        for key in self._customer_fallback_keys(customer):
            if key in bundle.get("ambiguous_keys", set()):
                continue
            found = bundle["records_by_key"].get(key)
            if found:
                return found
        return {}

    def _customer_keys(self, customer):
        return self._customer_number_keys(customer) + self._customer_fallback_keys(customer)

    def _customer_number_keys(self, customer):
        return self._number_keys(
            customer.customer_no,
            customer.legacy_customer_no,
            customer.lead_no,
        )

    def _customer_fallback_keys(self, customer):
        phone, phone_wechat = split_phone_and_wechat(customer.phone, customer.region, customer.city, customer.name)
        wechat = merge_wechat_values(customer.wechat, phone_wechat)
        values = {
            "phone": phone,
            "wechat": wechat,
            "email": customer.email,
            "name": customer.name,
            "original_name": customer.original_name,
        }
        return self._export_fallback_keys(values)

    def _has_customer_number(self, customer):
        return any(str(value or "").strip() for value in (customer.customer_no, customer.legacy_customer_no, customer.lead_no))

    def _export_keys(self, values):
        return self._number_keys(values.get("customer_no"), values.get("legacy_customer_no"), values.get("lead_no")) + self._export_fallback_keys(values)

    def _number_keys(self, *values):
        keys = []
        for field, raw_value in zip(("customer_no", "legacy_customer_no", "lead_no"), values):
            value = str(raw_value or "").strip()
            if value:
                keys.append(f"{field}:{value.lower()}")
                keys.append(f"any_no:{value.lower()}")
        return keys

    def _export_fallback_keys(self, values):
        keys = []
        for field in ("phone", "wechat", "email"):
            value = str(values.get(field) or "").strip()
            if value:
                keys.append(f"{field}:{value.lower()}")
        for alias in self._name_aliases(values.get("name"), values.get("original_name")):
            keys.append(f"name:{alias.lower()}")
            normalized = self._normalized_name(alias)
            if normalized:
                keys.append(f"name_norm:{normalized}")
        return keys

    def _text(self, row, key):
        return str(row.get(key) or "").strip()

    def _grade_code(self, value):
        text = str(value or "").strip()
        if not text:
            return ""
        return GRADE_LABEL_TO_CODE.get(text, text if text in dict(Customer.Grade.choices) else "")

    def _customer_type_value(self, value):
        text = str(value or "").strip()
        return canonical_customer_type(text) if text else ""

    def _demand_value(self, value):
        text = str(value or "").strip()
        if not text:
            return ""
        return canonical_demands(text) or text

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

    def _normalized_name(self, value):
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"^\s*\d+[\s._-]*", "", text)
        text = re.sub(r"\s*[-–—]\s*[\u4e00-\u9fffA-Za-z ]{1,30}$", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
