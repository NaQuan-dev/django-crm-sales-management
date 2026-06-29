import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import Customer
from crm.options import canonical_source


EXCEL_MAIN_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
EXCEL_RELS_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
CUSTOMER_NO_HEADERS = ("客户编号", "系统客户编号", "线索编号")
SOURCE_HEADERS = ("账号来源", "账户来源", "线索来源", "客户来源")
KNOWN_HEADERS = {
    "客户经理",
    "客户编号",
    "系统客户编号",
    "线索编号",
    "客户名称",
    "客户状态",
    "客户类型",
    "客户级别",
    "客户需求",
    "客户电话",
    "微信",
    "邮箱",
    "账号来源",
    "账户来源",
    "线索来源",
    "客户来源",
    "创建时间",
}


class Command(BaseCommand):
    help = "按 e-office 表格里的账号或账户来源填充客户线索来源。"

    def add_arguments(self, parser):
        parser.add_argument("excel_path", help="e-office 客户信息 .xlsx 文件路径")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不写入数据库")
        parser.add_argument("--overwrite", action="store_true", help="允许用 e-office 来源覆盖客户系统已有线索来源")

    def handle(self, *args, **options):
        excel_path = Path(options["excel_path"]).expanduser()
        if not excel_path.exists():
            raise CommandError(f"找不到 Excel 文件: {excel_path}")
        if excel_path.suffix.lower() != ".xlsx":
            raise CommandError("当前命令只支持 .xlsx 文件。")

        rows = self._load_rows(excel_path)
        stats = self.fill_sources(rows, dry_run=options["dry_run"], overwrite=options["overwrite"])
        action = "预演" if options["dry_run"] else "填充"
        top_sources = "；".join(f"{value}×{count}" for value, count in stats["source_counter"].most_common(12))
        self.stdout.write(
            self.style.SUCCESS(
                f"{action}完成：读取 {stats['seen']} 条，有来源 {stats['with_source']} 条，匹配 {stats['matched']} 条，"
                f"更新 {stats['updated']} 条，已相同 {stats['same']} 条，已有值未覆盖 {stats['blocked_existing']} 条，"
                f"缺来源 {stats['missing_source']} 条，未找到客户 {stats['missing_customer']} 条。"
            )
        )
        if top_sources:
            self.stdout.write(f"来源分布：{top_sources}")

    def fill_sources(self, rows, dry_run=False, overwrite=False):
        stats = {
            "seen": 0,
            "with_source": 0,
            "matched": 0,
            "updated": 0,
            "same": 0,
            "blocked_existing": 0,
            "missing_source": 0,
            "missing_customer": 0,
            "source_counter": Counter(),
        }
        with transaction.atomic():
            for row in rows:
                stats["seen"] += 1
                customer_no = self._first_text(row, CUSTOMER_NO_HEADERS)
                source = canonical_source(self._first_text(row, SOURCE_HEADERS))
                if not source:
                    stats["missing_source"] += 1
                    continue
                stats["with_source"] += 1
                stats["source_counter"][source] += 1
                customer = self._find_customer(customer_no)
                if customer is None:
                    stats["missing_customer"] += 1
                    continue
                stats["matched"] += 1
                current = customer.source_channel or ""
                if current == source:
                    stats["same"] += 1
                    continue
                if current and not overwrite:
                    stats["blocked_existing"] += 1
                    continue
                customer.source_channel = source
                customer.save(update_fields=["source_channel", "updated_at"])
                stats["updated"] += 1
            if dry_run:
                transaction.set_rollback(True)
        return stats

    def _find_customer(self, customer_no):
        if not customer_no:
            return None
        queryset = Customer.objects.select_for_update()
        return (
            queryset.filter(customer_no=customer_no).first()
            or queryset.filter(legacy_customer_no=customer_no).first()
            or queryset.filter(lead_no=customer_no).first()
        )

    def _load_rows(self, path):
        with zipfile.ZipFile(path) as archive:
            shared_strings = self._shared_strings(archive)
            sheet_path = self._first_sheet_path(archive)
            rows = self._sheet_rows(archive, sheet_path, shared_strings)
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
                item[str(header).strip()] = str(value or "").strip()
            if has_value:
                result.append(item)
        return result

    def _header_index(self, rows):
        best_index = 0
        best_count = 0
        for index, row in enumerate(rows[:20]):
            count = sum(1 for value in row.values() if str(value).strip() in KNOWN_HEADERS)
            if count > best_count:
                best_index = index
                best_count = count
        if best_count == 0:
            raise CommandError("Excel 中没有找到可识别的客户表头。")
        return best_index

    def _shared_strings(self, archive):
        try:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        except KeyError:
            return []
        return ["".join(t.text or "" for t in item.findall(".//a:t", EXCEL_MAIN_NS)) for item in root.findall("a:si", EXCEL_MAIN_NS)]

    def _first_sheet_path(self, archive):
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("rel:Relationship", EXCEL_RELS_NS)}
        sheet = workbook.find("a:sheets/a:sheet", EXCEL_MAIN_NS)
        if sheet is None:
            raise CommandError("Excel 中没有找到工作表。")
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = rel_map.get(rel_id, "worksheets/sheet1.xml").lstrip("/")
        return target if target.startswith("xl/") else f"xl/{target}"

    def _sheet_rows(self, archive, sheet_path, shared_strings):
        root = ET.fromstring(archive.read(sheet_path))
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

    def _first_text(self, row, headers):
        for header in headers:
            value = str(row.get(header) or "").strip()
            if value:
                return value
        return ""
