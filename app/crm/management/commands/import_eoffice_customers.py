import re
import shutil
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.feishu_sync import parse_bool, parse_dt
from crm.models import (
    Customer,
    clean_customer_name_contacts,
    merge_phone_values,
    merge_region_city,
    merge_wechat_values,
    is_system_customer_no,
    split_region_from_customer_name,
    split_phone_and_wechat,
)
from crm.options import (
    GRADE_LABEL_TO_CODE,
    canonical_customer_statuses,
    canonical_customer_type,
    canonical_demands,
    canonical_source,
)


EXCEL_MAIN_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
EXCEL_RELS_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
GRADE_ALIASES = {
    "成交客户": Customer.Grade.KEY,
    "成交": Customer.Grade.KEY,
}
CLASSIFICATION_FIELD_NAMES = {"grade", "customer_type", "demand"}
CORE_FIELD_NAMES = {
    *CLASSIFICATION_FIELD_NAMES,
    "customer_status_text",
    "source_channel",
    "region",
    "notes",
    "last_contact_at",
    "next_contact_at",
    "historical_created_at",
}
FIELD_LABELS = {
    "grade": "客户级别",
    "customer_type": "客户类型",
    "demand": "客户需求",
    "customer_status_text": "客户状态",
    "source_channel": "线索来源",
    "region": "地区/城市",
    "notes": "沟通记录",
    "last_contact_at": "最后联系时间",
    "next_contact_at": "下次联系时间",
    "historical_created_at": "创建时间",
}
RAW_FIELD_NAMES = {
    "grade": ("客户级别",),
    "customer_type": ("客户类型",),
    "demand": ("客户需求",),
    "customer_status_text": ("客户状态",),
    "source_channel": ("线索来源", "账号来源", "账户来源", "客户来源"),
    "region": ("地区", "城市"),
    "notes": ("沟通记录",),
    "last_contact_at": ("最后联系时间",),
    "next_contact_at": ("下次联系时间",),
    "historical_created_at": ("创建时间",),
}


class Command(BaseCommand):
    help = "从 e-office 客户信息表按客户编号补全或更新客户资料。"

    def add_arguments(self, parser):
        parser.add_argument("excel_path", help="e-office 客户信息 .xlsx 文件路径")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不写入数据库")
        parser.add_argument("--overwrite", action="store_true", help="允许用 e-office 表覆盖客户系统已有非空字段")
        parser.add_argument("--no-create-missing", action="store_true", help="只更新已有客户，不新增缺失客户")
        parser.add_argument("--field-report", action="store_true", help="只输出字段级解析/更新诊断，不写入数据库")
        parser.add_argument("--only-classification", action="store_true", help="只按客户编号补客户级别、客户类型、客户需求")
        parser.add_argument(
            "--only-core-fields",
            action="store_true",
            help="只按客户编号补客户级别、客户类型、客户需求、客户状态、线索来源、地区、沟通记录、最后联系时间、下次联系时间、历史创建时间",
        )

    def handle(self, *args, **options):
        excel_path = Path(options["excel_path"]).expanduser()
        if not excel_path.exists():
            raise CommandError(f"找不到 Excel 文件: {excel_path}")
        if excel_path.suffix.lower() != ".xlsx":
            raise CommandError("当前命令只支持 .xlsx 文件。")
        if options["only_classification"] and options["only_core_fields"]:
            raise CommandError("--only-classification 和 --only-core-fields 不能同时使用。")

        rows = self._load_rows(excel_path)
        if options["field_report"]:
            stats = self.field_report(
                rows,
                overwrite=options["overwrite"],
                only_classification=options["only_classification"],
                only_core_fields=options["only_core_fields"],
            )
            self._write_field_report(stats)
            return
        stats = self.import_rows(
            rows,
            dry_run=options["dry_run"],
            overwrite=options["overwrite"],
            create_missing=not options["no_create_missing"]
            and not options["only_classification"]
            and not options["only_core_fields"],
            only_classification=options["only_classification"],
            only_core_fields=options["only_core_fields"],
        )
        action = "预演" if options["dry_run"] else "写入"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action}完成：读取 {stats['seen']} 条，匹配 {stats['matched']} 条，新增 {stats['created']} 条，"
                f"更新 {stats['updated']} 条，跳过 {stats['skipped']} 条，缺客户编号 {stats['missing_customer_no']} 条。"
            )
        )

    def import_rows(
        self,
        rows,
        dry_run=False,
        overwrite=False,
        create_missing=True,
        only_classification=False,
        only_core_fields=False,
    ):
        stats = {
            "seen": 0,
            "matched": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "missing_customer_no": 0,
        }
        with transaction.atomic():
            for row in rows:
                stats["seen"] += 1
                values = self._row_to_values(row)
                lookup_no = values.get("legacy_customer_no") or values.get("customer_no") or values.get("lead_no")
                if not lookup_no:
                    stats["missing_customer_no"] += 1
                    stats["skipped"] += 1
                    continue
                customer = self._find_customer(lookup_no)
                creating = customer is None
                if creating and not create_missing:
                    stats["skipped"] += 1
                    continue
                customer = customer or Customer(source_kind=Customer.RecordKind.CUSTOMER)
                if not creating:
                    stats["matched"] += 1
                if only_classification:
                    values = {field: values.get(field) for field in CLASSIFICATION_FIELD_NAMES}
                elif only_core_fields:
                    values = {field: values.get(field) for field in CORE_FIELD_NAMES}

                historical_created_at = values.pop("historical_created_at", None)
                historical_changed = self._should_update_historical_created_at(
                    customer,
                    historical_created_at,
                    overwrite=overwrite,
                )
                changed = self._apply_values(customer, values, overwrite=overwrite)
                if not changed and not creating and not historical_changed:
                    stats["skipped"] += 1
                    continue
                if changed or creating:
                    customer.save()
                if historical_changed:
                    Customer.objects.filter(pk=customer.pk).update(historical_created_at=historical_created_at)
                    customer.historical_created_at = historical_created_at
                stats["created" if creating else "updated"] += 1
            if dry_run:
                transaction.set_rollback(True)
        return stats

    def field_report(self, rows, overwrite=False, only_classification=False, only_core_fields=False):
        field_names = self._selected_field_names(only_classification, only_core_fields)
        stats = {
            "seen": 0,
            "matched": 0,
            "missing_customer_no": 0,
            "missing_customer": 0,
            "fields": {
                field: {
                    "raw": 0,
                    "parsed": 0,
                    "would_update": 0,
                    "same": 0,
                    "blocked_existing": 0,
                    "missing_customer": 0,
                    "unmatched_raw": Counter(),
                }
                for field in field_names
            },
        }
        for row in rows:
            stats["seen"] += 1
            values = self._row_to_values(row)
            customer_no = self._text(row, "客户编号")
            if not customer_no:
                stats["missing_customer_no"] += 1
                continue
            customer = self._find_customer(customer_no)
            if customer is None:
                stats["missing_customer"] += 1
            else:
                stats["matched"] += 1

            for field in field_names:
                field_stats = stats["fields"][field]
                raw_value = self._raw_value(row, field)
                value = values.get(field)
                if raw_value:
                    field_stats["raw"] += 1
                if value not in ("", None):
                    field_stats["parsed"] += 1
                elif raw_value:
                    field_stats["unmatched_raw"][raw_value] += 1
                    continue
                else:
                    continue

                if customer is None:
                    field_stats["missing_customer"] += 1
                    continue
                action = self._field_update_action(customer, field, value, overwrite)
                field_stats[action] += 1
        return stats

    def _write_field_report(self, stats):
        self.stdout.write(
            f"字段诊断：读取 {stats['seen']} 条，匹配客户 {stats['matched']} 条，"
            f"缺客户编号 {stats['missing_customer_no']} 条，未找到客户 {stats['missing_customer']} 条。"
        )
        for field, field_stats in stats["fields"].items():
            self.stdout.write(
                f"{FIELD_LABELS.get(field, field)}：原表有值 {field_stats['raw']}，解析成功 {field_stats['parsed']}，"
                f"将更新 {field_stats['would_update']}，已相同 {field_stats['same']}，"
                f"被已有值挡住 {field_stats['blocked_existing']}，未找到客户 {field_stats['missing_customer']}。"
            )
            if field_stats["unmatched_raw"]:
                samples = "；".join(f"{value}×{count}" for value, count in field_stats["unmatched_raw"].most_common(8))
                self.stdout.write(f"  未匹配原值：{samples}")

    def _selected_field_names(self, only_classification=False, only_core_fields=False):
        if only_classification:
            return [field for field in FIELD_LABELS if field in CLASSIFICATION_FIELD_NAMES]
        if only_core_fields:
            return [field for field in FIELD_LABELS if field in CORE_FIELD_NAMES]
        return [field for field in FIELD_LABELS if field in CORE_FIELD_NAMES]

    def _raw_value(self, row, field):
        return " ".join(
            self._text(row, key)
            for key in RAW_FIELD_NAMES.get(field, (field,))
            if self._text(row, key)
        ).strip()

    def _field_update_action(self, customer, field, value, overwrite=False):
        current = getattr(customer, field, None)
        if field == "historical_created_at":
            if current == value:
                return "same"
            if self._should_update_historical_created_at(customer, value, overwrite):
                return "would_update"
            return "blocked_existing"
        should_set = overwrite or current in ("", None)
        if field == "grade" and value and current == Customer.Grade.POTENTIAL:
            should_set = True
        if current == value:
            return "same"
        if should_set:
            return "would_update"
        return "blocked_existing"

    def _find_customer(self, customer_no):
        return (
            Customer.objects.filter(customer_no=customer_no).first()
            or Customer.objects.filter(legacy_customer_no=customer_no).first()
            or Customer.objects.filter(lead_no=customer_no).first()
        )

    def _row_to_values(self, row):
        customer_no = self._text(row, "客户编号")
        name = self._text(row, "客户名称")
        region = merge_region_city(self._text(row, "地区"), self._text(row, "城市"))
        name, name_phone, name_wechat = clean_customer_name_contacts(name, region)
        name, name_region = split_region_from_customer_name(name)
        region = merge_region_city(region, name_region)
        phone, phone_wechat = split_phone_and_wechat(self._text(row, "客户电话"), region, name)
        phone = merge_phone_values(phone, name_phone, hints=(region, name)) if name_phone else phone
        wechat = merge_wechat_values(self._text(row, "微信"), name_wechat, phone_wechat)
        email = self._text(row, "邮箱") or self._text(row, "电子邮箱")
        customer_status_text = canonical_customer_statuses(self._text(row, "客户状态"))
        source_channel = canonical_source(
            self._text(row, "线索来源")
            or self._text(row, "账号来源")
            or self._text(row, "账户来源")
            or self._text(row, "客户来源")
        )
        owner_name = self._text(row, "客户经理") or self._text(row, "客户负责人") or self._text(row, "负责人")
        values = {
            "source_kind": Customer.RecordKind.CUSTOMER,
            "customer_no": customer_no if is_system_customer_no(customer_no) else "",
            "legacy_customer_no": customer_no,
            "name": name,
            "customer_status_text": customer_status_text,
            "related_lead": self._text(row, "关联线索"),
            "source_channel": source_channel,
            "customer_type": canonical_customer_type(self._text(row, "客户类型")),
            "grade": self._grade_code(self._text(row, "客户级别")),
            "demand": canonical_demands(self._text(row, "客户需求")),
            "contact_name": self._text(row, "联系人"),
            "phone": phone,
            "wechat": wechat,
            "email": email,
            "region": region,
            "notes": self._text(row, "沟通记录"),
            "next_contact_at": self._parse_datetime(self._text(row, "下次联系时间")),
            "historical_created_at": self._parse_datetime(self._text(row, "创建时间")),
            "last_contact_at": self._parse_datetime(self._text(row, "最后联系时间")),
            "created_by_name": self._text(row, "创建人"),
            "attachment_note": self._attachment_note(row),
        }
        if owner_name:
            values["owner_name"] = owner_name
        if parse_bool(self._text(row, "是否成交")):
            values["is_deal"] = True
            values["status"] = Customer.Status.DEAL
        if "成交" in self._text(row, "客户级别"):
            values["status"] = Customer.Status.DEAL
            values["is_deal"] = True
        if any(item in customer_status_text.split(",") for item in ("已下单", "合同已签待预付")):
            values["status"] = Customer.Status.DEAL
            values["is_deal"] = True
        if values["grade"] == Customer.Grade.INVALID:
            values["status"] = Customer.Status.INVALID
        return values

    def _grade_code(self, value):
        text = str(value or "").strip()
        if not text:
            return ""
        direct = GRADE_LABEL_TO_CODE.get(text)
        if direct:
            return direct
        normalized = re.sub(r"[（(]\s*\d+\s*级\s*[）)]", "", text).strip()
        return GRADE_LABEL_TO_CODE.get(normalized, GRADE_ALIASES.get(normalized, ""))

    def _should_update_historical_created_at(self, customer, value, overwrite=False):
        if value in ("", None):
            return False
        current = customer.historical_created_at
        return current != value

    def _apply_values(self, customer, values, overwrite=False):
        changed = False
        for field, value in values.items():
            if value in ("", None):
                continue
            current = getattr(customer, field, None)
            should_set = overwrite or current in ("", None)
            if field == "grade" and value and current == Customer.Grade.POTENTIAL:
                should_set = True
            if field == "wechat" and current:
                merged = merge_wechat_values(current, value)
                if merged != current:
                    setattr(customer, field, merged)
                    changed = True
                continue
            if should_set and current != value:
                setattr(customer, field, value)
                changed = True
        return changed

    def _attachment_note(self, row):
        parts = []
        account = self._text(row, "账号")
        image = self._text(row, "图片")
        if account:
            parts.append(f"账号：{account}")
        if image:
            parts.append(f"图片：{image}")
        return "\n".join(parts)

    def _parse_datetime(self, value):
        if not value:
            return None
        text = str(value).strip()
        try:
            number = float(text)
        except ValueError:
            return parse_dt(text)
        if 20000 <= number <= 80000:
            return parse_dt((datetime(1899, 12, 30) + timedelta(days=number)).strftime("%Y-%m-%d %H:%M:%S"))
        return parse_dt(number)

    def _text(self, row, key):
        return str(row.get(key) or "").strip()

    def _load_rows(self, path):
        temp_dir = None
        try:
            workbook_path = path
            if path.is_file():
                temp_dir = Path(tempfile.mkdtemp(prefix="eoffice-xlsx-"))
                with zipfile.ZipFile(path) as archive:
                    archive.extractall(temp_dir)
                workbook_path = temp_dir
            shared_strings = self._shared_strings(workbook_path)
            sheet_path = self._first_sheet_path(workbook_path)
            rows = self._sheet_rows(sheet_path, shared_strings)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
        if len(rows) < 2:
            raise CommandError("Excel 中没有找到有效表头。")
        header_index = self._header_index(rows)
        headers = rows[header_index]
        result = []
        for raw_row in rows[header_index + 1 :]:
            item = {}
            has_value = False
            for index, header in headers.items():
                if not header:
                    continue
                value = raw_row.get(index, "")
                if value:
                    has_value = True
                item[header] = value
            if has_value:
                result.append(item)
        return result

    def _shared_strings(self, workbook_path):
        shared_path = workbook_path / "xl" / "sharedStrings.xml"
        if not shared_path.exists():
            return []
        root = ET.fromstring(shared_path.read_bytes())
        return ["".join(t.text or "" for t in item.findall(".//a:t", EXCEL_MAIN_NS)) for item in root.findall("a:si", EXCEL_MAIN_NS)]

    def _first_sheet_path(self, workbook_path):
        workbook = ET.fromstring((workbook_path / "xl" / "workbook.xml").read_bytes())
        rels = ET.fromstring((workbook_path / "xl" / "_rels" / "workbook.xml.rels").read_bytes())
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("rel:Relationship", EXCEL_RELS_NS)}
        sheet = workbook.find("a:sheets/a:sheet", EXCEL_MAIN_NS)
        if sheet is None:
            raise CommandError("Excel 中没有找到工作表。")
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = rel_map.get(rel_id, "worksheets/sheet1.xml").lstrip("/")
        if target.startswith("xl/"):
            return workbook_path / target
        return workbook_path / "xl" / target

    def _sheet_rows(self, sheet_path, shared_strings):
        root = ET.fromstring(sheet_path.read_bytes())
        rows = []
        for row in root.findall(".//a:sheetData/a:row", EXCEL_MAIN_NS):
            values = {}
            for cell in row.findall("a:c", EXCEL_MAIN_NS):
                values[self._column_index(cell.attrib.get("r", ""))] = self._cell_text(cell, shared_strings)
            rows.append(values)
        return rows

    def _cell_text(self, cell, shared_strings):
        cell_type = cell.attrib.get("t")
        value = cell.find("a:v", EXCEL_MAIN_NS)
        text = "" if value is None else (value.text or "")
        if cell_type == "s" and text:
            return shared_strings[int(text)].strip()
        if cell_type == "inlineStr":
            return "".join(t.text or "" for t in cell.findall(".//a:t", EXCEL_MAIN_NS)).strip()
        return str(text or "").strip()

    def _column_index(self, ref):
        letters = "".join(ch for ch in ref if ch.isalpha())
        number = 0
        for char in letters:
            number = number * 26 + ord(char.upper()) - 64
        return number

    def _header_index(self, rows):
        for index, row in enumerate(rows):
            headers = set(row.values())
            if "客户编号" in headers and (
                "客户名称" in headers
                or {"客户电话", "微信", "邮箱", "客户经理"}.intersection(headers)
            ):
                return index
        raise CommandError("Excel 中没有找到包含“客户编号”的客户信息表头行。")
