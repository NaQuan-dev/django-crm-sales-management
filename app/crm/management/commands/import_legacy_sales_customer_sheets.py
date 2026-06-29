import re
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from crm.feishu_sync import (
    allocate_customer_no,
    build_customer_no_allocator,
    build_customer_phone_index,
    phone_lookup_tokens,
)
from crm.models import (
    ContactLog,
    Customer,
    merge_phone_values,
    merge_region_city,
    merge_wechat_values,
    resolve_or_create_user_by_feishu,
    split_phone_and_wechat,
)
from crm.options import GRADE_LABEL_TO_CODE, canonical_customer_statuses, canonical_demands, canonical_source
from crm.services import update_customer_after_contact


XML_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

SKIP_SONG_SHEETS = {"常用文字", "对话知识点"}
SKIP_HE_SHEETS = {"询盘有效线索判断及沟通方式", "话术"}
GRADE_BY_SHEET = {
    "待孵化客户": Customer.Grade.INCUBATING,
    "潜在客户": Customer.Grade.POTENTIAL,
    "一般客户": Customer.Grade.NORMAL,
    "意向客户": Customer.Grade.INTENTION,
    "重点客户": Customer.Grade.KEY,
    "无效客户": Customer.Grade.INVALID,
}


class Command(BaseCommand):
    help = "导入销售B、销售A历史销售客户表。"

    def add_arguments(self, parser):
        parser.add_argument("--song", default="/app/imports/legacy_song_customer_info.xlsx")
        parser.add_argument("--he", default="/app/imports/legacy_he_xiaofang_inquiry.xlsx")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        song_path = Path(options["song"])
        he_path = Path(options["he"])
        if not song_path.exists():
            raise CommandError(f"找不到销售B客户表：{song_path}")
        if not he_path.exists():
            raise CommandError(f"找不到销售A客户表：{he_path}")

        importer = LegacySalesImporter(dry_run=options["dry_run"], stdout=self.stdout)
        importer.import_song(song_path)
        importer.import_he(he_path)
        self.stdout.write(
            "历史销售表%s：读取 %s 行，跳过 %s 行，忽略异常时间 %s 个，新建客户 %s 条，合并客户 %s 条，导入跟进日志 %s 条。"
            % (
                "预览" if options["dry_run"] else "导入完成",
                importer.stats["rows"],
                importer.stats["skipped"],
                importer.stats["ignored_times"],
                importer.stats["created"],
                importer.stats["merged"],
                importer.stats["logs"],
            )
        )


class LegacySalesImporter:
    def __init__(self, dry_run=False, stdout=None):
        self.dry_run = dry_run
        self.stdout = stdout
        self.phone_index = build_customer_phone_index()
        self.customer_no_allocator = build_customer_no_allocator()
        self.stats = {"rows": 0, "skipped": 0, "ignored_times": 0, "created": 0, "merged": 0, "logs": 0}

    def import_song(self, path):
        for sheet_name, rows in read_xlsx(path).items():
            if sheet_name in SKIP_SONG_SHEETS:
                continue
            if not rows or len(rows) < 3:
                continue
            self._import_rows(path.name, sheet_name, rows, owner_name="销售B", workbook_kind="song")

    def import_he(self, path):
        for sheet_name, rows in read_xlsx(path).items():
            if sheet_name in SKIP_HE_SHEETS or "CBCE" in sheet_name.upper():
                continue
            if not rows or len(rows) < 3:
                continue
            self._import_rows(path.name, sheet_name, rows, owner_name="销售A", workbook_kind="he")

    def _import_rows(self, filename, sheet_name, rows, owner_name, workbook_kind):
        headers = rows[0]
        subheaders = rows[1] if len(rows) > 1 else []
        positions = header_positions(headers)
        follow_columns = followup_columns(headers, subheaders)
        owner = None if self.dry_run else resolve_or_create_user_by_feishu(owner_name)
        grade_from_sheet = GRADE_BY_SHEET.get(sheet_name)
        if self.stdout:
            self.stdout.write(f"{owner_name} / {sheet_name}：开始处理 {max(len(rows) - 2, 0)} 行")
        for row_number, row in enumerate(rows[2:], start=3):
            if not row_has_value(row):
                continue
            values, logs = self._row_values(
                filename,
                sheet_name,
                row_number,
                row,
                positions,
                follow_columns,
                owner_name,
                owner,
                grade_from_sheet,
                workbook_kind,
            )
            if not self._has_useful_customer_data(values, logs):
                self.stats["skipped"] += 1
                continue
            self.stats["rows"] += 1
            if self.dry_run:
                if self._find_customer(values).pk is None:
                    self.stats["created"] += 1
                else:
                    self.stats["merged"] += 1
                self.stats["logs"] += len([item for item in logs if clean_text(item.get("summary"))])
                continue
            with transaction.atomic():
                customer, created = self._upsert_customer(values)
                if created:
                    self.stats["created"] += 1
                else:
                    self.stats["merged"] += 1
                self._import_logs(customer, logs, owner_name)

    def _row_values(self, filename, sheet_name, row_number, row, positions, follow_columns, owner_name, owner, grade_from_sheet, workbook_kind):
        get = lambda *names: first_value(row, positions, *names)
        raw_contact_time = get("时间", "联系时间")
        contact_time = parse_import_datetime(raw_contact_time)
        if workbook_kind == "song" and not is_valid_song_contact_time(contact_time):
            if clean_text(raw_contact_time):
                self.stats["ignored_times"] += 1
            contact_time = None
        assignment = clean_text(get("分配信息", "陈广林发的信息"))
        assignment_info = parse_assignment_info(assignment, owner_name)
        source = clean_text(get("客户来源", "获客渠道")) or assignment_info["source"]
        source = canonical_source(source)
        company_name = clean_text(get("公司名称", "客户名称"))
        contact_name = clean_text(get("联系人/微信名称", "联系人"))
        phone_raw = clean_text(get("电话"))
        email = ""
        if "@" in phone_raw and not re.search(r"\d{7,}", phone_raw):
            email = phone_raw
            phone_raw = ""
        phone_raw = merge_phone_values(phone_raw, assignment_info["phone"], hints=(get("国家/地区", "地区"), company_name, contact_name))
        wechat = merge_wechat_values(assignment_info["wechat"])
        phone, phone_wechat = split_phone_and_wechat(phone_raw, get("国家/地区", "地区"), company_name, contact_name)
        wechat = merge_wechat_values(wechat, phone_wechat)
        region = merge_region_city(get("国家/地区", "地区"), "")
        demand_raw = clean_text(get("感兴趣产品"))
        demand = canonical_demands(demand_raw) or demand_raw
        grade_text = clean_text(get("客户类型", "客户等级", "客户意向"))
        grade = grade_from_sheet or grade_from_text(grade_text)
        is_deal = is_deal_value(grade_text) or is_deal_value(get("是否成交"))
        status = Customer.Status.DEAL if is_deal else Customer.Status.PRIVATE
        if grade_text and "无效" in grade_text:
            status = Customer.Status.INVALID
        notes = self._notes(filename, sheet_name, row_number, assignment_info["note"], row, positions, workbook_kind)
        name = company_name
        if workbook_kind == "song" and not name:
            name = contact_name
        values = {
            "name": name,
            "original_name": company_name or contact_name,
            "contact_name": contact_name,
            "phone": phone,
            "wechat": wechat,
            "email": email,
            "region": region,
            "source_channel": source,
            "demand": demand,
            "grade": grade,
            "status": status,
            "is_deal": is_deal,
            "customer_status_text": customer_status_text(grade_text, get("是否成交")),
            "historical_created_at": contact_time,
            "owner": owner,
            "owner_name": owner_name,
            "source_kind": Customer.RecordKind.CUSTOMER,
            "notes": notes,
        }
        logs = self._row_logs(row, follow_columns, contact_time, owner_name)
        first_summary = clean_text(get("第1次跟进", "第一次跟进"))
        if first_summary:
            logs.append({"contact_at": contact_time, "summary": first_summary})
        return values, logs

    def _notes(self, filename, sheet_name, row_number, assignment_note, row, positions, workbook_kind):
        parts = [f"来源文件：{filename} / {sheet_name} / 第{row_number}行"]
        for label in ("报价编号", "报价金额", "客户预算", "报价", "价格", "客户规模", "成交金额", "没有成交原因", "行业类型"):
            value = clean_text(first_value(row, positions, label))
            if value:
                parts.append(f"{label}：{value}")
        if assignment_note:
            parts.append(f"分配信息：{assignment_note}")
        if workbook_kind == "he":
            code = clean_text(first_value(row, positions, "分配信息", "陈广林发的信息"))
            if code and not is_assignment_code_only(code) and code != assignment_note:
                parts.append(f"原分配信息：{code}")
        return "\n".join(dedupe(parts))

    def _row_logs(self, row, follow_columns, created_at, owner_name):
        logs = []
        for item in follow_columns:
            summary = clean_text(cell(row, item["summary"]))
            if not summary or summary == "#########":
                continue
            contact_at = parse_import_datetime(cell(row, item.get("date")), default_year=created_at.year if created_at else None)
            logs.append({"contact_at": contact_at or created_at, "summary": summary})
        return logs

    def _has_useful_customer_data(self, values, logs):
        return any(values.get(field) for field in ("name", "contact_name", "phone", "wechat", "email", "region", "demand"))

    def _upsert_customer(self, values):
        customer = self._find_customer(values)
        created = customer.pk is None
        if created and self.customer_no_allocator:
            customer.customer_no = allocate_customer_no(self.customer_no_allocator)
        overwrite_fields = {"owner", "owner_name", "source_kind", "status", "is_deal"}
        fill_fields = {
            "name",
            "original_name",
            "contact_name",
            "phone",
            "wechat",
            "email",
            "region",
            "source_channel",
            "demand",
            "grade",
            "customer_status_text",
            "historical_created_at",
            "notes",
        }
        for field in fill_fields | overwrite_fields:
            value = values.get(field)
            if value in ("", None):
                continue
            current = getattr(customer, field, None)
            if field == "notes" and current:
                value = merge_note_text(current, value)
            if field in overwrite_fields or current in ("", None):
                setattr(customer, field, value)
        customer.save()
        if customer.phone:
            for token in phone_lookup_tokens(customer.phone):
                self.phone_index[token] = customer.pk
        return customer, created

    def _find_customer(self, values):
        phone_tokens = phone_lookup_tokens(values.get("phone"))
        for token in phone_tokens:
            customer_id = self.phone_index.get(token)
            if customer_id:
                found = Customer.objects.filter(pk=customer_id, is_active=True).first()
                if found:
                    return found
        name = values.get("name")
        owner_name = values.get("owner_name")
        if name:
            found = Customer.objects.filter(name=name, owner_name=owner_name, is_active=True).first()
            if found:
                return found
        return Customer()

    def _import_logs(self, customer, logs, owner_name):
        for item in logs:
            summary = clean_text(item.get("summary"))
            if not summary:
                continue
            contact_at = item.get("contact_at") or customer.historical_created_at or timezone.now()
            if ContactLog.objects.filter(customer=customer, contact_at=contact_at, summary=summary).exists():
                continue
            log = ContactLog.objects.create(
                customer=customer,
                contact_at=contact_at,
                follower_name=owner_name,
                method=ContactLog.Method.WECHAT,
                source=ContactLog.Source.IMPORT,
                summary=summary,
            )
            update_customer_after_contact(customer, log)
            self.stats["logs"] += 1


def read_xlsx(path):
    with zipfile.ZipFile(path) as zf:
        shared_strings = parse_shared_strings(zf)
        date_style_ids = parse_date_style_ids(zf)
        rels = parse_workbook_rels(zf)
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        result = {}
        for sheet in workbook.findall(".//main:sheet", XML_NS):
            name = sheet.attrib.get("name", "")
            rel_id = sheet.attrib.get(f"{{{XML_NS['rel']}}}id")
            target = rels.get(rel_id)
            if not target:
                continue
            sheet_path = "xl/" + target.lstrip("/")
            result[name] = parse_sheet(zf, sheet_path, shared_strings, date_style_ids)
        return result


def parse_shared_strings(zf):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("main:si", XML_NS):
        texts = [node.text or "" for node in item.findall(".//main:t", XML_NS)]
        strings.append("".join(texts))
    return strings


def parse_workbook_rels(zf):
    root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    result = {}
    for rel in root.findall("pkgrel:Relationship", XML_NS):
        result[rel.attrib["Id"]] = rel.attrib["Target"]
    return result


def parse_date_style_ids(zf):
    if "xl/styles.xml" not in zf.namelist():
        return set()
    root = ET.fromstring(zf.read("xl/styles.xml"))
    custom_date_ids = set()
    for fmt in root.findall(".//main:numFmt", XML_NS):
        fmt_id = int(fmt.attrib.get("numFmtId", "0"))
        code = fmt.attrib.get("formatCode", "").lower()
        if any(token in code for token in ("yy", "mm", "dd", "hh", "ss", "年", "月", "日")):
            custom_date_ids.add(fmt_id)
    builtin_date_ids = set(range(14, 23)) | {45, 46, 47}
    date_ids = builtin_date_ids | custom_date_ids
    style_ids = set()
    cell_xfs = root.find("main:cellXfs", XML_NS)
    if cell_xfs is None:
        return style_ids
    for index, xf in enumerate(cell_xfs.findall("main:xf", XML_NS)):
        if int(xf.attrib.get("numFmtId", "0")) in date_ids:
            style_ids.add(index)
    return style_ids


def parse_sheet(zf, sheet_path, shared_strings, date_style_ids):
    root = ET.fromstring(zf.read(sheet_path))
    rows = []
    for row_node in root.findall(".//main:sheetData/main:row", XML_NS):
        values = []
        for cell_node in row_node.findall("main:c", XML_NS):
            ref = cell_node.attrib.get("r", "")
            col_index = column_index(ref)
            while len(values) <= col_index:
                values.append("")
            values[col_index] = parse_cell(cell_node, shared_strings, date_style_ids)
        rows.append(trim_row(values))
    return rows


def parse_cell(cell_node, shared_strings, date_style_ids):
    cell_type = cell_node.attrib.get("t", "")
    value_node = cell_node.find("main:v", XML_NS)
    if cell_type == "inlineStr":
        texts = [node.text or "" for node in cell_node.findall(".//main:t", XML_NS)]
        return "".join(texts)
    if value_node is None:
        return ""
    raw = value_node.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return raw
    style_id = int(cell_node.attrib.get("s", "0") or 0)
    if style_id in date_style_ids:
        try:
            return excel_serial_to_datetime(float(raw))
        except ValueError:
            return raw
    if re.fullmatch(r"-?\d+\.0", raw):
        return raw[:-2]
    return raw


def excel_serial_to_datetime(value):
    parsed = datetime(1899, 12, 30) + timedelta(days=value)
    if parsed.time() == time(0, 0):
        return parsed.date()
    return parsed


def column_index(ref):
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    total = 0
    for char in letters.group(1):
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total - 1


def trim_row(row):
    while row and row[-1] in ("", None):
        row.pop()
    return row


def row_has_value(row):
    return any(clean_text(value) for value in row)


def header_positions(headers):
    result = {}
    for index, value in enumerate(headers):
        text = clean_text(value)
        if text and text not in result:
            result[text] = index
    return result


def followup_columns(headers, subheaders):
    columns = []
    for index, value in enumerate(subheaders):
        sub = clean_text(value)
        if sub not in {"情况", "进展情况", "跟进情况"}:
            continue
        date_col = None
        for nearby in (index - 1, index + 1):
            if nearby < 0:
                continue
            nearby_sub = clean_text(cell(subheaders, nearby))
            if nearby_sub in {"时间", "日期", "跟进时间"}:
                date_col = nearby
                break
        columns.append({"summary": index, "date": date_col})
    return columns


def first_value(row, positions, *names):
    for name in names:
        if name in positions:
            value = cell(row, positions[name])
            if clean_text(value):
                return value
    return ""


def cell(row, index):
    if index is None or index < 0 or index >= len(row):
        return ""
    return row[index]


def clean_text(value):
    if value in ("", None):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_import_datetime(value, default_year=None):
    if value in ("", None, "#########"):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time(8))
    else:
        text = clean_text(value)
        parsed = parse_datetime(text)
        if parsed is None:
            date_value = parse_date(text)
            if date_value:
                parsed = datetime.combine(date_value, time(8))
        if parsed is None:
            for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y年%m月%d日", "%Y/%m/%d %H:%M"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    pass
        if parsed is None:
            match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日?", text)
            if match and default_year:
                parsed = datetime(default_year, int(match.group(1)), int(match.group(2)), 8, 0)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def is_valid_song_contact_time(value):
    if not value:
        return False
    local_date = timezone.localtime(value).date()
    today = timezone.localdate()
    return date(2020, 1, 1) <= local_date <= today + timedelta(days=30)


def parse_assignment_info(value, owner_name):
    text = clean_text(value)
    if not text or is_assignment_code_only(text):
        return {"source": "", "phone": "", "wechat": "", "note": ""}
    phone, wechat = split_phone_and_wechat(text)
    source = infer_source_from_assignment(text, owner_name)
    note = remove_assignment_codes(text)
    return {"source": source, "phone": phone, "wechat": wechat, "note": note}


def is_assignment_code_only(value):
    text = clean_text(value)
    return bool(re.fullmatch(r"(?:XS|XSDJ|DJ|NQ|NQKH)?\s*\d{3,8}", text, flags=re.IGNORECASE))


def remove_assignment_codes(value):
    text = re.sub(r"\b(?:XS|XSDJ|DJ|NQ|NQKH)\s*\d{3,8}\b", "", clean_text(value), flags=re.IGNORECASE)
    return text.strip(" -_，,;；")


def infer_source_from_assignment(value, owner_name):
    text = clean_text(value)
    if "视频号" in text:
        if "账号A" in text or "宋" in text:
            return "视频号账号A"
        if "账号C" in text or "何" in text or owner_name == "销售A":
            return "视频号账号C"
        return "视频号账号C" if owner_name == "销售A" else "视频号账号A"
    if "抖音" in text:
        if "账号A" in text or "宋" in text:
            return "抖音账号A"
        if "账号C" in text or "何" in text or owner_name == "销售A":
            return "抖音账号C"
        return "抖音账号C" if owner_name == "销售A" else "抖音账号A"
    if "展会" in text:
        return "展会"
    if "阿里" in text:
        return "阿里巴巴国际站"
    if "巨量" in text:
        return "巨量广告"
    if "账号E" in text:
        return "账号E分配"
    return ""


def grade_from_text(value):
    text = clean_text(value)
    if "成交" in text:
        return Customer.Grade.KEY
    if "重复" in text:
        return Customer.Grade.POTENTIAL
    return GRADE_LABEL_TO_CODE.get(text, Customer.Grade.POTENTIAL)


def is_deal_value(value):
    text = clean_text(value)
    return text in {"是", "成交", "已成交", "成交客户"} or ("成交" in text and "未成交" not in text and "没有成交" not in text)


def customer_status_text(*values):
    items = []
    for value in values:
        text = clean_text(value)
        if not text:
            continue
        if "成交" in text and "未成交" not in text:
            items.append("已下单")
        elif "报价" in text:
            items.append(text)
        elif text in {"无效客户", "重复客户"}:
            items.append(text)
    canonical = canonical_customer_statuses(",".join(items))
    return canonical or ",".join(dedupe(items))


def merge_note_text(current, incoming):
    current = clean_text(current)
    incoming = clean_text(incoming)
    if not current:
        return incoming
    if incoming and incoming not in current:
        return f"{current}\n{incoming}"
    return current


def dedupe(items):
    result = []
    seen = set()
    for item in items:
        text = clean_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
