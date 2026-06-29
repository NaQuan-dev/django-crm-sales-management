import csv
import io
import json
import re
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import (
    ContactForm,
    ContactLogCreateForm,
    ContactLogForm,
    ContractForm,
    CustomerForm,
    CustomerMergeForm,
    CustomerTransferForm,
    LeadForm,
    OpportunityForm,
    PaymentForm,
    QuoteForm,
    QuotePlanForm,
    TaskReminderForm,
    VisitPlanForm,
    WorkOrderLinkForm,
)
from .models import (
    Attachment,
    Contact,
    ContactLog,
    Contract,
    Customer,
    Lead,
    OperationLog,
    Opportunity,
    Payment,
    Quote,
    QuotePlan,
    Reminder,
    SampleTest,
    TaskReminder,
    VisitPlan,
    WorkOrderLink,
    merge_region_city,
    is_system_customer_no,
    merge_wechat_values,
    split_phone_and_wechat,
)
from .options import (
    CUSTOMER_STATUS_OPTIONS,
    CUSTOMER_TYPE_OPTIONS,
    DEMAND_OPTIONS,
    GRADE_LABEL_TO_CODE,
    parse_multi_value,
    SOURCE_OPTIONS,
    canonical_customer_statuses,
    canonical_customer_type,
    canonical_demands,
    canonical_source,
)
from .services import (
    apply_auto_tags,
    can_assign,
    can_view_all,
    contact_log_queryset_for,
    contract_queryset_for,
    customer_queryset_for,
    lead_queryset_for,
    is_public_pool_customer,
    public_pool_customer_q,
    public_pool_stale_cutoff,
    recycled_customer_q,
    update_customer_after_contact,
)


def _choice_label(choices, value):
    return dict(choices).get(value, value or "未填写")


def _bar_rows(grouped, label_getter, max_items=8):
    rows = list(grouped)
    total = sum(row["total"] for row in rows)
    rows = rows[:max_items]
    result = []
    for row in rows:
        count = row["total"]
        percent = round((count / total) * 100, 1) if total else 0
        result.append({"label": label_getter(row), "total": count, "percent": percent})
    return result


def _bar_rows_from_counts(count_rows, max_items=8):
    rows = list(count_rows)
    total = sum(count for _, count in rows)
    result = []
    for label, count in rows[:max_items]:
        percent = round((count / total) * 100, 1) if total else 0
        result.append({"label": label, "total": count, "percent": percent})
    return result

PIE_COLORS = ["#176b87", "#12b76a", "#f79009", "#7a5af8", "#2e90fa", "#f04438", "#667085", "#0e9384"]


def _pie_chart(title, count_rows, max_items=8):
    rows = []
    for label, count in count_rows:
        if count:
            rows.append({"label": label or "未填写", "total": int(count)})
    rows = sorted(rows, key=lambda row: (-row["total"], row["label"]))
    if len(rows) > max_items:
        head = rows[: max_items - 1]
        tail_total = sum(row["total"] for row in rows[max_items - 1 :])
        rows = head + [{"label": "其他", "total": tail_total}]
    total = sum(row["total"] for row in rows)
    cursor = 0
    gradient_parts = []
    for index, row in enumerate(rows):
        percent = round((row["total"] / total) * 100, 1) if total else 0
        start = cursor
        end = 100 if index == len(rows) - 1 else cursor + ((row["total"] / total) * 100 if total else 0)
        color = PIE_COLORS[index % len(PIE_COLORS)]
        row.update({"percent": percent, "color": color})
        gradient_parts.append(f"{color} {round(start, 3)}% {round(end, 3)}%")
        cursor = end
    style = f"background: conic-gradient({', '.join(gradient_parts)});" if gradient_parts else "background: #eef2f6;"
    return {"title": title, "rows": rows, "total": total, "style": style}


def _count_rows(grouped, label_getter):
    return [(label_getter(row), row["total"]) for row in grouped]


def _demand_count_rows(records):
    counts = {item: 0 for item in DEMAND_OPTIONS}
    other = 0
    empty = 0
    for raw_value in records.values_list("demand", flat=True):
        text = str(raw_value or "").strip()
        if not text:
            empty += 1
            continue
        normalized = canonical_demands(text)
        items = [item for item in normalized.split(",") if item]
        if not items:
            other += 1
            continue
        for item in items:
            counts[item] += 1
    rows = [(label, count) for label, count in counts.items() if count]
    if other:
        rows.append(("其他", other))
    if empty:
        rows.append(("未填写", empty))
    return sorted(rows, key=lambda item: (-item[1], item[0]))

OWNER_CODE_NAME_ALIASES = {
    "SALES001": "销售A",
    "SALES002": "销售B",
    "SALES003": "销售C",
    "SALES004": "销售D",
}
OWNER_NAME_ALIASES = {
    "销售D旧名": "销售D",
}
OWNER_FILTER_LABEL_PREFIX = "owner_label:"


def _owner_display_aliases():
    aliases = dict(OWNER_CODE_NAME_ALIASES)
    for user in User.objects.only("username", "first_name", "last_name"):
        display_name = f"{user.last_name or ''}{user.first_name or ''}".strip()
        if display_name:
            aliases[user.username.upper()] = display_name
    return aliases


def _normalize_owner_label(value, aliases=None):
    text = str(value or "").strip()
    if not text:
        return "未分配"
    aliases = aliases or OWNER_CODE_NAME_ALIASES
    label = aliases.get(text.upper(), text)
    return OWNER_NAME_ALIASES.get(label, label)


def _owner_label(row, aliases=None):
    name = f"{row.get('owner__last_name') or ''}{row.get('owner__first_name') or ''}".strip()
    return _normalize_owner_label(name or row.get("owner__username") or row.get("owner_name"), aliases)


def _customer_owner_label(row, aliases=None):
    name = f"{row.get('customer__owner__last_name') or ''}{row.get('customer__owner__first_name') or ''}".strip()
    return _normalize_owner_label(name or row.get("customer__owner__username") or row.get("customer__owner_name"), aliases)


def _logical_created_before_q(cutoff):
    return Q(historical_created_at__lt=cutoff) | Q(historical_created_at__isnull=True, created_at__lt=cutoff)


def _status_label(row):
    return row.get("customer_status_text") or "未填写"


OWNERSHIP_STATUS_LABELS = {"私有客户", "公海客户", "成交客户", "回收站客户", "回收站"}


def _business_status_items(value):
    canonical = canonical_customer_statuses(value)
    items = parse_multi_value(canonical) if canonical else []
    if not items:
        items = parse_multi_value(value)
    return [item for item in items if item and item not in OWNERSHIP_STATUS_LABELS]


def _status_count_rows(records):
    counts = {item: 0 for item in CUSTOMER_STATUS_OPTIONS}
    empty = 0
    other = {}
    for raw_value in records.values_list("customer_status_text", flat=True):
        items = _business_status_items(raw_value)
        if not items:
            empty += 1
            continue
        for item in items:
            if item in counts:
                counts[item] += 1
            else:
                other[item] = other.get(item, 0) + 1
    rows = [(label, count) for label, count in counts.items() if count]
    rows.extend(sorted(other.items(), key=lambda item: (-item[1], item[0])))
    if empty:
        rows.append(("未填写", empty))
    return sorted(rows, key=lambda item: (-item[1], item[0]))
CUSTOMER_PROGRESS_STAGES = ["已加联系方式", "未报价", "已报价", "待拜访", "方案设计沟通中", "待补充状态"]
CUSTOMER_LIST_PAGE_SIZE = 8
CUSTOMER_LIST_MIN_PAGE_SIZE = 3
CUSTOMER_LIST_MAX_PAGE_SIZE = 30
EXCEL_MAIN_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
EXCEL_RELS_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}

IMPORT_FIELD_ALIASES = {
    "customer_no": ["客户编号", "系统客户编号", "线索编号", "编号"],
    "name": ["客户名称", "客户名称/昵称", "客户名", "名称", "昵称"],
    "owner_name": ["客户经理", "客户负责人", "负责人", "销售", "跟进人"],
    "grade": ["客户级别", "客户等级", "等级"],
    "customer_type": ["客户类型", "客户类别", "类型"],
    "demand": ["客户需求", "需求", "产品需求"],
    "customer_status_text": ["客户状态", "状态", "跟进状态"],
    "phone": ["客户电话", "联系电话", "电话", "手机号码", "手机号"],
    "wechat": ["微信", "微信号", "微信号码"],
    "email": ["邮箱", "电子邮箱", "邮件"],
    "last_contact_at": ["最后联系时间", "最近跟进时间", "最后跟进时间", "最近联系时间"],
    "next_contact_at": ["下次联系时间", "下次跟进时间"],
    "region": ["地区", "城市", "国家/地区", "所在地区"],
    "source_channel": ["线索来源", "账号来源", "账户来源", "来源"],
    "contact_name": ["联系人", "客户联系人"],
    "historical_created_at": ["历史创建时间", "创建时间", "录入时间"],
    "notes": ["沟通记录", "备注", "跟进记录", "说明"],
    "attachment_note": ["图片", "附件", "图片/附件说明"],
}

ADMIN_INLINE_CUSTOMER_FIELDS = {"owner_id", "status"}
INLINE_CUSTOMER_FIELDS = {
    "name": {"label": "客户名称", "type": "text"},
    "official_name": {"label": "正式名称", "type": "text"},
    "grade": {"label": "客户级别", "type": "select"},
    "customer_type": {"label": "客户类型", "type": "select"},
    "demand": {"label": "客户需求", "type": "textarea"},
    "customer_status_text": {"label": "客户状态", "type": "textarea"},
    "is_deal": {"label": "成交状态", "type": "select"},
    "owner_id": {"label": "客户经理", "type": "select"},
    "status": {"label": "客户归属", "type": "select"},
    "phone": {"label": "客户电话", "type": "text"},
    "wechat": {"label": "微信", "type": "text"},
    "email": {"label": "邮箱", "type": "text"},
    "last_contact_at": {"label": "最后联系时间", "type": "date"},
    "next_contact_at": {"label": "下次联系时间", "type": "date"},
    "region": {"label": "地区", "type": "text"},
    "source_channel": {"label": "线索来源", "type": "select"},
    "contact_name": {"label": "联系人", "type": "text"},
}

INLINE_CONTACT_LOG_FIELDS = {
    "contact_at": {"label": "跟进时间", "type": "datetime"},
    "method": {"label": "跟进形式", "type": "select"},
    "follower_name": {"label": "跟进人", "type": "select"},
    "summary": {"label": "跟进内容", "type": "textarea"},
    "result": {"label": "跟进结果", "type": "multi"},
    "next_contact_at": {"label": "下次联系时间", "type": "date"},
}

ADMIN_INLINE_CONTRACT_FIELDS = {"signed_by_id"}
INLINE_CONTRACT_FIELDS = {
    "customer_id": {"label": "关联客户", "type": "select"},
    "signed_by_id": {"label": "签约人员", "type": "select"},
    "signed_date": {"label": "签约日期", "type": "date"},
    "amount": {"label": "合同金额", "type": "number"},
}

def _multi_values(value):
    return parse_multi_value(value)


def _has_customer_status(customer, label):
    return label in _multi_values(customer.customer_status_text)


def _is_effective_customer(customer):
    return customer.grade != Customer.Grade.INVALID and bool(
        customer.phone
        or customer.wechat
        or customer.email
        or _has_customer_status(customer, "已加联系方式")
    )


def _customer_needs_followup_q(now=None):
    cutoff = (now or timezone.now()) - timedelta(days=14)
    stale_created_q = Q(historical_created_at__lt=cutoff) | Q(historical_created_at__isnull=True, created_at__lt=cutoff)
    return (
        Q(customer_status_text__icontains="已加联系方式")
        & (Q(last_contact_at__lt=cutoff) | (Q(last_contact_at__isnull=True) & stale_created_q))
        & Q(is_deal=False)
        & ~Q(status=Customer.Status.DEAL)
        & ~Q(customer_status_text__icontains="已下单")
        & ~Q(customer_status_text__icontains="合同已签待预付")
    )

def _parse_api_datetime(value):
    if value in ("", None):
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = None
    if numeric_value is not None and 20000 <= numeric_value <= 80000:
        return timezone.make_aware(
            datetime.combine((datetime(1899, 12, 30) + timedelta(days=numeric_value)).date(), datetime.min.time()),
            timezone.get_current_timezone(),
        )
    parsed = parse_datetime(str(value))
    if parsed:
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
    date_value = parse_date(str(value))
    if date_value:
        return timezone.make_aware(datetime.combine(date_value, datetime.min.time()), timezone.get_current_timezone())
    return None


def _read_customer_import_rows(uploaded_file):
    filename = (uploaded_file.name or "").lower()
    data = uploaded_file.read()
    if filename.endswith(".csv"):
        return _read_csv_rows(data)
    if filename.endswith(".xlsx"):
        return _read_xlsx_rows(data)
    if filename.endswith(".xls"):
        raise ValueError("暂不支持老式 .xls 二进制文件，请先另存为 .xlsx 或 .csv 后再导入。")
    raise ValueError("只支持 .csv、.xlsx 文件；.xls 请先另存为 .xlsx。")


def _read_csv_rows(data):
    text = None
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法识别 CSV 文件编码。")
    reader = csv.DictReader(io.StringIO(text))
    return [{str(key or "").strip(): str(value or "").strip() for key, value in row.items()} for row in reader if any(row.values())]


def _read_xlsx_rows(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared_strings = _xlsx_shared_strings(archive)
        sheet_path = _xlsx_first_sheet_path(archive)
        rows = _xlsx_sheet_rows(archive, sheet_path, shared_strings)
    if len(rows) < 2:
        raise ValueError("Excel 中没有找到有效表头。")
    header_index = _xlsx_header_index(rows)
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


def _xlsx_shared_strings(archive):
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(t.text or "" for t in item.findall(".//a:t", EXCEL_MAIN_NS)) for item in root.findall("a:si", EXCEL_MAIN_NS)]


def _xlsx_first_sheet_path(archive):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("rel:Relationship", EXCEL_RELS_NS)}
    sheet = workbook.find("a:sheets/a:sheet", EXCEL_MAIN_NS)
    if sheet is None:
        raise ValueError("Excel 中没有找到工作表。")
    rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    target = rel_map.get(rel_id, "worksheets/sheet1.xml").lstrip("/")
    return target if target.startswith("xl/") else f"xl/{target}"


def _xlsx_sheet_rows(archive, sheet_path, shared_strings):
    root = ET.fromstring(archive.read(sheet_path))
    rows = []
    for row in root.findall(".//a:sheetData/a:row", EXCEL_MAIN_NS):
        values = {}
        for cell in row.findall("a:c", EXCEL_MAIN_NS):
            values[_xlsx_column_index(cell.attrib.get("r", ""))] = _xlsx_cell_text(cell, shared_strings)
        rows.append(values)
    return rows


def _xlsx_cell_text(cell, shared_strings):
    cell_type = cell.attrib.get("t")
    value = cell.find("a:v", EXCEL_MAIN_NS)
    text = "" if value is None else (value.text or "")
    if cell_type == "s" and text:
        return shared_strings[int(text)].strip()
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(".//a:t", EXCEL_MAIN_NS)).strip()
    return str(text or "").strip()


def _xlsx_column_index(ref):
    letters = "".join(ch for ch in ref if ch.isalpha())
    number = 0
    for char in letters:
        number = number * 26 + ord(char.upper()) - 64
    return number


def _xlsx_header_index(rows):
    known_headers = {alias for aliases in IMPORT_FIELD_ALIASES.values() for alias in aliases}
    best_index = 0
    best_count = 0
    for index, row in enumerate(rows[:20]):
        count = sum(1 for value in row.values() if str(value).strip() in known_headers)
        if count > best_count:
            best_index = index
            best_count = count
    if best_count == 0:
        raise ValueError("Excel 中没有找到可识别的客户表头。")
    return best_index


def _import_text(row, field):
    return _import_text_with_mapping(row, field, None)


def _import_text_with_mapping(row, field, mapping=None):
    if mapping and mapping.get(field):
        value = row.get(mapping[field])
        return str(value or "").strip()
    for header in IMPORT_FIELD_ALIASES[field]:
        value = row.get(header)
        if value not in ("", None):
            return str(value).strip()
    return ""


def _row_to_customer_import_values(row, mapping=None):
    name = _import_text_with_mapping(row, "name", mapping)
    region = _import_text_with_mapping(row, "region", mapping)
    phone, phone_wechat = split_phone_and_wechat(_import_text_with_mapping(row, "phone", mapping), region, name)
    wechat = merge_wechat_values(_import_text_with_mapping(row, "wechat", mapping), phone_wechat)
    grade_text = _import_text_with_mapping(row, "grade", mapping)
    grade = GRADE_LABEL_TO_CODE.get(grade_text, grade_text if grade_text in dict(Customer.Grade.choices) else "")
    imported_customer_no = _import_text_with_mapping(row, "customer_no", mapping)
    values = {
        "customer_no": imported_customer_no if is_system_customer_no(imported_customer_no) else "",
        "legacy_customer_no": "" if is_system_customer_no(imported_customer_no) else imported_customer_no,
        "name": name,
        "owner_name": _import_text_with_mapping(row, "owner_name", mapping),
        "grade": grade,
        "customer_type": canonical_customer_type(_import_text_with_mapping(row, "customer_type", mapping)),
        "demand": canonical_demands(_import_text_with_mapping(row, "demand", mapping)),
        "customer_status_text": canonical_customer_statuses(_import_text_with_mapping(row, "customer_status_text", mapping)),
        "phone": phone,
        "wechat": wechat,
        "email": _import_text_with_mapping(row, "email", mapping),
        "last_contact_at": _parse_api_datetime(_import_text_with_mapping(row, "last_contact_at", mapping)),
        "next_contact_at": _parse_api_datetime(_import_text_with_mapping(row, "next_contact_at", mapping)),
        "region": region,
        "source_channel": canonical_source(_import_text_with_mapping(row, "source_channel", mapping)),
        "contact_name": _import_text_with_mapping(row, "contact_name", mapping),
        "historical_created_at": _parse_api_datetime(_import_text_with_mapping(row, "historical_created_at", mapping)),
        "notes": _import_text_with_mapping(row, "notes", mapping),
        "attachment_note": _import_text_with_mapping(row, "attachment_note", mapping),
    }
    return values


def _default_import_mapping(headers):
    mapping = {}
    for field, aliases in IMPORT_FIELD_ALIASES.items():
        mapping[field] = next((header for header in headers if header in aliases), "")
    return mapping


def _import_field_specs():
    labels = {
        "customer_no": "客户编号",
        "name": "客户名称",
        "owner_name": "客户经理",
        "grade": "客户级别",
        "customer_type": "客户类型",
        "demand": "客户需求",
        "customer_status_text": "客户状态",
        "phone": "客户电话",
        "wechat": "微信",
        "email": "邮箱",
        "last_contact_at": "最后联系时间",
        "next_contact_at": "下次联系时间",
        "region": "地区",
        "source_channel": "线索来源",
        "contact_name": "联系人",
        "historical_created_at": "创建时间",
        "notes": "沟通记录",
        "attachment_note": "图片/附件说明",
    }
    return [{"field": field, "label": labels[field]} for field in IMPORT_FIELD_ALIASES]


def _has_import_identity(values):
    return any(values.get(field) for field in ("customer_no", "legacy_customer_no", "name", "phone", "wechat", "email"))


def _find_customer_for_import(values):
    for value in (values.get("customer_no"), values.get("legacy_customer_no")):
        if value:
            found = Customer.objects.filter(
                Q(customer_no=value) | Q(legacy_customer_no=value) | Q(lead_no=value),
                is_active=True,
            ).first()
            if found:
                return found
    for field in ("phone", "wechat", "email"):
        value = values.get(field)
        if value:
            found = Customer.objects.filter(**{field: value}, is_active=True).first()
            if found:
                return found
    return None


def _apply_import_values(customer, values, overwrite=False):
    changed = []
    for field, value in values.items():
        if field == "historical_created_at":
            continue
        if value in ("", None):
            continue
        current = getattr(customer, field, None)
        should_set = overwrite or current in ("", None)
        if field == "grade" and value and current == Customer.Grade.POTENTIAL:
            should_set = True
        if should_set and current != value:
            setattr(customer, field, value)
            changed.append(field)
    return changed


def _historical_created_at_import_needed(customer, value):
    return value not in ("", None) and customer.historical_created_at != value


def _save_imported_historical_created_at(customer, value):
    if value in ("", None):
        return False
    if customer.historical_created_at == value:
        return False
    Customer.objects.filter(pk=customer.pk).update(historical_created_at=value)
    customer.historical_created_at = value
    return True


def _inline_user_options(blank_label="未分配"):
    options = [{"value": "", "label": blank_label}]
    aliases = _owner_display_aliases()
    for user in User.objects.filter(is_active=True).order_by("username"):
        label = _normalize_owner_label(user.get_full_name() or user.username, aliases)
        options.append({"value": str(user.pk), "label": label})
    return options


def _inline_user_name_options(user, blank_label="请选择"):
    options = [{"value": "", "label": blank_label}]
    seen = {""}
    for active_user in User.objects.filter(is_active=True).order_by("username"):
        label = active_user.get_full_name() or active_user.username
        if label not in seen:
            options.append({"value": label, "label": label})
            seen.add(label)
    names = (
        contact_log_queryset_for(user)
        .exclude(follower_name="")
        .values_list("follower_name", flat=True)
        .distinct()
        .order_by("follower_name")[:500]
    )
    for name in names:
        if name not in seen:
            options.append({"value": name, "label": name})
            seen.add(name)
    return options


def _inline_owner_options():
    return _inline_user_options("未分配")


def _inline_options(user=None):
    options = {
        "grade": [{"value": value, "label": label} for value, label in Customer.Grade.choices],
        "customer_type": [{"value": item, "label": item} for item in CUSTOMER_TYPE_OPTIONS],
        "source_channel": [{"value": item, "label": item} for item in SOURCE_OPTIONS],
        "demand": DEMAND_OPTIONS,
        "customer_status_text": CUSTOMER_STATUS_OPTIONS,
        "is_deal": [{"value": "1", "label": "已成交"}, {"value": "0", "label": "未成交"}],
    }
    if user is not None and can_assign(user):
        options["owner_id"] = _inline_owner_options()
        options["status"] = [{"value": value, "label": label} for value, label in Customer.Status.choices]
    return options

def _serialize_inline_value(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _coerce_inline_value(field, value):
    text = str(value or "").strip()
    if field == "owner_id":
        if not text:
            return None
        try:
            owner_id = int(text)
        except (TypeError, ValueError):
            raise ValueError("客户经理选项无效。")
        if not User.objects.filter(pk=owner_id, is_active=True).exists():
            raise ValueError("客户经理选项无效。")
        return owner_id
    if field == "status":
        allowed = {value for value, _label in Customer.Status.choices}
        if text not in allowed:
            raise ValueError("客户归属选项无效。")
        return text
    if field == "is_deal":
        if text in {"1", "true", "True", "是", "已成交", "成交", "yes", "on"}:
            return True
        if text in {"0", "false", "False", "否", "未成交", "no", "off", ""}:
            return False
        raise ValueError("成交状态选项无效。")
    if field in {"last_contact_at", "next_contact_at"}:
        return _parse_api_datetime(text)
    if field == "grade":
        allowed = {value for value, _label in Customer.Grade.choices}
        if text not in allowed:
            raise ValueError("客户级别选项无效。")
        return text
    if field == "customer_type":
        return canonical_customer_type(text)
    if field == "demand":
        return canonical_demands(text)
    if field == "customer_status_text":
        return canonical_customer_statuses(text)
    if field == "source_channel":
        return canonical_source(text)
    return text


def _customer_select_options(user):
    options = [{"value": "", "label": "未关联客户"}]
    customers = customer_queryset_for(user).order_by("customer_no", "name")[:5000]
    for customer in customers:
        label = f"{customer.customer_no} ｜ {customer}"
        options.append({"value": str(customer.pk), "label": label})
    return options


def _contact_log_inline_options(user):
    return {
        "method": [{"value": value, "label": label} for value, label in ContactLog.Method.choices],
        "follower_name": _inline_user_name_options(user),
        "result": [{"value": item, "label": item} for item in CUSTOMER_STATUS_OPTIONS],
    }


def _contract_inline_options(user):
    options = {"customer_id": _customer_select_options(user)}
    if can_assign(user):
        options["signed_by_id"] = _inline_user_options("未分配")
    return options


def _coerce_contact_log_inline_value(field, value):
    text = str(value or "").strip()
    if field in {"contact_at", "next_contact_at"}:
        if not text and field == "contact_at":
            raise ValueError("跟进时间不能为空。")
        return _parse_api_datetime(text)
    if field == "method":
        allowed = {value for value, _label in ContactLog.Method.choices}
        if text not in allowed:
            raise ValueError("跟进形式选项无效。")
        return text
    if field == "result":
        return canonical_customer_statuses(text)
    if field == "summary" and not text:
        raise ValueError("跟进内容不能为空。")
    return text


def _coerce_contract_inline_value(field, value, user):
    text = str(value or "").strip()
    if field == "customer_id":
        if not text:
            return None
        try:
            customer_id = int(text)
        except (TypeError, ValueError):
            raise ValueError("关联客户选项无效。")
        if not customer_queryset_for(user).filter(pk=customer_id).exists():
            raise ValueError("关联客户选项无效。")
        return customer_id
    if field == "signed_by_id":
        if not can_assign(user):
            raise PermissionDenied
        if not text:
            return None
        try:
            signed_by_id = int(text)
        except (TypeError, ValueError):
            raise ValueError("签约人员选项无效。")
        if not User.objects.filter(pk=signed_by_id, is_active=True).exists():
            raise ValueError("签约人员选项无效。")
        return signed_by_id
    if field == "signed_date":
        if not text:
            return None
        parsed = parse_date(text)
        if not parsed:
            raise ValueError("签约日期格式无效。")
        return parsed
    if field == "amount":
        try:
            amount = Decimal(text or "0")
        except InvalidOperation:
            raise ValueError("合同金额必须是数字。")
        if amount < 0:
            raise ValueError("合同金额不能小于 0。")
        return amount
    return text
def _push_customer_history(request, entry):
    undo_stack = request.session.get("customer_undo_stack", [])
    undo_stack.append(entry)
    request.session["customer_undo_stack"] = undo_stack[-50:]
    request.session["customer_redo_stack"] = []
    request.session.modified = True


def _apply_customer_history_entry(entry, use_new_value):
    customer = Customer.objects.get(pk=entry["pk"])
    field = entry["field"]
    raw_value = entry["new"] if use_new_value else entry["old"]
    setattr(customer, field, _coerce_inline_value(field, raw_value))
    update_fields = [field, "updated_at"]
    if field == "is_deal" and "old_status" in entry and "new_status" in entry:
        new_is_deal = bool(_coerce_inline_value(field, raw_value))
        customer.status = entry["new_status"] if use_new_value else entry["old_status"]
        customer.deal_status = Customer.DealStatus.WON if new_is_deal else Customer.DealStatus.OPEN
        if new_is_deal:
            customer.customer_level = Customer.CustomerLevel.DEAL
            customer.follow_status = Customer.FollowStatus.DEAL
        else:
            if customer.customer_level == Customer.CustomerLevel.DEAL:
                customer.customer_level = Customer.CustomerLevel.PENDING
            if customer.follow_status == Customer.FollowStatus.DEAL:
                customer.follow_status = Customer.FollowStatus.CONTACTED
        for extra_field in ("status", "deal_status", "customer_level", "follow_status"):
            if extra_field not in update_fields:
                update_fields.append(extra_field)
    customer.save(update_fields=update_fields)
    return customer


def _selected_list(querydict, name):
    return [value for value in querydict.getlist(name) if str(value).strip()]


def _customer_list_page_size(request):
    raw_value = request.GET.get("per_page", CUSTOMER_LIST_PAGE_SIZE)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = CUSTOMER_LIST_PAGE_SIZE
    return max(CUSTOMER_LIST_MIN_PAGE_SIZE, min(CUSTOMER_LIST_MAX_PAGE_SIZE, value))


def _customer_filter_state_from_query(querydict):
    return {
        "q": querydict.get("q", "").strip(),
        "kind": querydict.get("kind", "").strip(),
        "status": querydict.get("status", "").strip(),
        "quick": querydict.get("quick", "").strip(),
        "grades": _selected_list(querydict, "grade"),
        "types": _selected_list(querydict, "customer_type"),
        "demands": _selected_list(querydict, "demand"),
        "customer_statuses": _selected_list(querydict, "customer_status"),
        "sources": _selected_list(querydict, "source_channel"),
        "owners": _selected_list(querydict, "owner"),
        "trade_types": _selected_list(querydict, "trade_type"),
        "levels": _selected_list(querydict, "customer_level"),
        "follow_statuses": _selected_list(querydict, "follow_status"),
        "quote_status": querydict.get("quote_status", "").strip(),
        "uncontacted_days": querydict.get("uncontacted_days", "").strip(),
        "expected_close_month": querydict.get("expected_close_month", "").strip(),
        "products": _selected_list(querydict, "product_interest"),
        "country_region": querydict.get("country_region", "").strip(),
        "has_unpaid": querydict.get("has_unpaid", "").strip(),
        "has_visit": querydict.get("has_visit", "").strip(),
        "date_field": querydict.get("date_field", "").strip(),
        "date_from": querydict.get("date_from", "").strip(),
        "date_to": querydict.get("date_to", "").strip(),
        "sort_field": querydict.get("sort_field", "created").strip() or "created",
        "sort_direction": querydict.get("sort_direction", "desc").strip() or "desc",
    }


def _customer_filter_state(request):
    return _customer_filter_state_from_query(request.GET)


def _customer_has_active_filters(filters):
    return any(
        [
            filters["q"],
            filters["kind"],
            filters["status"],
            filters["quick"],
            filters["grades"],
            filters["types"],
            filters["demands"],
            filters["customer_statuses"],
            filters["sources"],
            filters["owners"],
            filters["trade_types"],
            filters["levels"],
            filters["follow_statuses"],
            filters["quote_status"],
            filters["uncontacted_days"],
            filters["expected_close_month"],
            filters["products"],
            filters["country_region"],
            filters["has_unpaid"],
            filters["has_visit"],
            filters["date_field"],
            filters["date_from"],
            filters["date_to"],
        ]
    )


def _apply_customer_filters(qs, filters, user):
    defaults = {
        "q": "", "kind": "", "status": "", "quick": "", "grades": [], "types": [], "demands": [],
        "customer_statuses": [], "sources": [], "owners": [], "trade_types": [], "levels": [], "follow_statuses": [],
        "quote_status": "", "uncontacted_days": "", "expected_close_month": "", "products": [],
        "country_region": "", "has_unpaid": "", "has_visit": "", "date_field": "", "date_from": "",
        "date_to": "", "sort_field": "created", "sort_direction": "desc",
    }
    filters = {**defaults, **filters}
    qs = qs.annotate(sort_created_at=Coalesce("historical_created_at", "created_at"))
    q = filters["q"]
    if q:
        qs = qs.filter(
            Q(customer_no__icontains=q)
            | Q(legacy_customer_no__icontains=q)
            | Q(lead_no__icontains=q)
            | Q(name__icontains=q)
            | Q(nickname__icontains=q)
            | Q(official_name__icontains=q)
            | Q(company_name__icontains=q)
            | Q(contact_name__icontains=q)
            | Q(main_contact_name__icontains=q)
            | Q(owner_name__icontains=q)
            | Q(phone__icontains=q)
            | Q(wechat__icontains=q)
            | Q(whatsapp__icontains=q)
            | Q(email__icontains=q)
            | Q(source_channel__icontains=q)
            | Q(account_source__icontains=q)
            | Q(related_lead__icontains=q)
        )
    if filters["kind"]:
        qs = qs.filter(source_kind=filters["kind"])
    if filters["status"]:
        qs = qs.filter(status=filters["status"])
    if filters["quick"] == "due":
        qs = qs.filter(_customer_needs_followup_q())
    elif filters["quick"] == "key":
        qs = qs.filter(Q(grade__in=[Customer.Grade.KEY, Customer.Grade.INTENTION]) | Q(customer_level=Customer.CustomerLevel.INTENTION))
    elif filters["quick"] == "quoted":
        qs = qs.filter(quotes__status__in=[Quote.Status.SENT, Quote.Status.VIEWED]).exclude(deal_status=Customer.DealStatus.WON)
    elif filters["quick"] == "unpaid":
        qs = qs.filter(contracts__is_active=True).exclude(deal_status=Customer.DealStatus.CANCELED)
    if filters["grades"]:
        qs = qs.filter(grade__in=filters["grades"])
    if filters["types"]:
        qs = qs.filter(customer_type__in=filters["types"])
    if filters["demands"]:
        demand_query = Q()
        for item in filters["demands"]:
            demand_query |= Q(demand__icontains=item) | Q(product_interest__icontains=item)
        qs = qs.filter(demand_query)
    if filters["products"]:
        product_query = Q()
        for item in filters["products"]:
            product_query |= Q(product_interest__icontains=item) | Q(demand__icontains=item)
        qs = qs.filter(product_query)
    if filters["customer_statuses"]:
        status_query = Q()
        for item in filters["customer_statuses"]:
            status_query |= Q(customer_status_text__icontains=item)
        qs = qs.filter(status_query)
    if filters["sources"]:
        qs = qs.filter(source_channel__in=filters["sources"])
    if filters["trade_types"]:
        qs = qs.filter(trade_type__in=filters["trade_types"])
    if filters["levels"]:
        qs = qs.filter(customer_level__in=filters["levels"])
    if filters["follow_statuses"]:
        qs = qs.filter(follow_status__in=filters["follow_statuses"])
    if filters["quote_status"]:
        qs = qs.filter(quotes__status=filters["quote_status"])
    if filters["country_region"]:
        qs = qs.filter(Q(country_region__icontains=filters["country_region"]) | Q(region__icontains=filters["country_region"]))
    if filters["has_unpaid"] == "1":
        qs = qs.filter(contracts__is_active=True).exclude(contracts__status=Contract.Status.CANCELED)
    if filters["has_visit"] == "1":
        qs = qs.filter(visit_plans__visit_date__gte=timezone.localdate(), visit_plans__status__in=[VisitPlan.Status.PENDING, VisitPlan.Status.CONFIRMED])
    if filters["uncontacted_days"]:
        try:
            days = int(filters["uncontacted_days"])
        except ValueError:
            days = 0
        if days > 0:
            cutoff = timezone.now() - timedelta(days=days)
            stale_created_q = Q(historical_created_at__lt=cutoff) | Q(historical_created_at__isnull=True, created_at__lt=cutoff)
            qs = qs.filter(Q(last_contact_at__lt=cutoff) | (Q(last_contact_at__isnull=True) & stale_created_q))
    if filters["expected_close_month"]:
        today = timezone.localdate()
        current_month = today.strftime("%Y-%m")
        next_month_date = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        next_month = next_month_date.strftime("%Y-%m")
        if filters["expected_close_month"] == "this":
            qs = qs.filter(expected_close_month=current_month)
        elif filters["expected_close_month"] == "next":
            qs = qs.filter(expected_close_month=next_month)
        elif filters["expected_close_month"] == "future":
            qs = qs.filter(expected_close_month__gt=next_month)
        elif filters["expected_close_month"] == "unknown":
            qs = qs.filter(expected_close_month="")
    if filters["owners"] and can_view_all(user):
        owner_query = Q()
        owner_aliases = _owner_display_aliases()
        for owner in filters["owners"]:
            label = _owner_filter_label(owner)
            if label:
                owner_query |= _owner_label_query(label, owner_aliases)
                continue
            if str(owner).isdigit():
                owner_query |= Q(owner_id=owner)
            owner_query |= Q(owner_name=owner)
        qs = qs.filter(owner_query)
    date_field_map = {
        "lastContact": "last_contact_at__date",
        "nextContact": "next_contact_at__date",
        "nextFollow": "next_follow_at__date",
        "created": "sort_created_at__date",
    }
    date_lookup = date_field_map.get(filters["date_field"])
    if date_lookup and filters["date_from"]:
        qs = qs.filter(**{f"{date_lookup}__gte": filters["date_from"]})
    if date_lookup and filters["date_to"]:
        qs = qs.filter(**{f"{date_lookup}__lte": filters["date_to"]})
    sort_map = {
        "created": "sort_created_at",
        "lastContact": "last_contact_at",
        "nextContact": "next_contact_at",
        "nextFollow": "next_follow_at",
        "days": "last_contact_at",
        "grade": "grade",
        "type": "customer_type",
        "demand": "demand",
        "status": "customer_status_text",
        "source": "source_channel",
        "name": "name",
    }
    sort_field = sort_map.get(filters["sort_field"], "sort_created_at")
    prefix = "" if filters["sort_direction"] == "asc" else "-"
    return qs.distinct().order_by(f"{prefix}{sort_field}", "-id")


def _customer_scope_queryset(request, querydict=None):
    querydict = querydict or request.GET
    base_qs = customer_queryset_for(request.user)
    scope = querydict.get("scope", "").strip() or ("all" if can_view_all(request.user) else "my")
    qs = base_qs
    public_q = public_pool_customer_q()
    recycled_q = recycled_customer_q()
    if scope == "public":
        qs = qs.filter(public_q)
    elif scope == "recycled":
        qs = qs.filter(recycled_q)
    elif scope == "my":
        if can_view_all(request.user):
            qs = qs.exclude(public_q).exclude(recycled_q)
        else:
            qs = qs.filter(owner=request.user).exclude(recycled_q)
    else:
        qs = qs.exclude(public_q).exclude(recycled_q)
    return base_qs, qs, scope


def _filtered_customer_queryset_from_querystring(request, querystring):
    querydict = QueryDict(querystring or "", mutable=True)
    _base_qs, scoped_qs, _scope = _customer_scope_queryset(request, querydict)
    filters = _customer_filter_state_from_query(querydict)
    return _apply_customer_filters(scoped_qs, filters, request.user)


def _owner_filter_value(label):
    return f"{OWNER_FILTER_LABEL_PREFIX}{label}"


def _owner_filter_label(value):
    text = str(value or "").strip()
    if text.startswith(OWNER_FILTER_LABEL_PREFIX):
        return text[len(OWNER_FILTER_LABEL_PREFIX) :].strip()
    return ""


def _customer_owner_options(user):
    qs = Customer.objects.filter(is_active=True)
    if not can_view_all(user):
        qs = qs.filter(owner=user)
    rows = qs.values("owner_id", "owner__username", "owner__first_name", "owner__last_name", "owner_name").distinct()
    aliases = _owner_display_aliases()
    labels = {}
    for row in rows:
        raw_label = (
            f"{row.get('owner__last_name') or ''}{row.get('owner__first_name') or ''}".strip()
            or row.get("owner__username")
            or row.get("owner_name")
            or ""
        )
        label = _normalize_owner_label(raw_label, aliases)
        if not label or label == "未分配":
            continue
        labels.setdefault(label, {"value": _owner_filter_value(label), "label": label})
    return [labels[label] for label in sorted(labels)]


def _owner_label_query(label, aliases=None):
    aliases = aliases or _owner_display_aliases()
    label = _normalize_owner_label(label, aliases)
    candidate_owner_names = {label}
    candidate_owner_names.update(alias for alias, canonical in OWNER_NAME_ALIASES.items() if canonical == label)
    owner_query = Q()
    has_match = False
    matching_user_ids = []
    for user in User.objects.filter(is_active=True).only("id", "username", "first_name", "last_name"):
        raw_label = user.get_full_name() or user.username
        if _normalize_owner_label(raw_label, aliases) == label:
            matching_user_ids.append(user.pk)
    if matching_user_ids:
        owner_query |= Q(owner_id__in=matching_user_ids)
        has_match = True
    matching_owner_names = []
    for owner_name in Customer.objects.exclude(owner_name="").values_list("owner_name", flat=True).distinct():
        if owner_name in candidate_owner_names or _normalize_owner_label(owner_name, aliases) == label:
            matching_owner_names.append(owner_name)
    if matching_owner_names:
        owner_query |= Q(owner_name__in=matching_owner_names)
        has_match = True
    if not has_match:
        owner_query |= Q(owner_name__in=list(candidate_owner_names))
    return owner_query

def _owner_options_from_rows(rows, id_key="owner_id", username_key="owner__username", first_name_key="owner__first_name", last_name_key="owner__last_name", fallback_key="owner_name"):
    result = []
    seen = set()
    aliases = _owner_display_aliases()
    for row in rows:
        value = str(row.get(id_key) or row.get(fallback_key) or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        raw_label = (
            f"{row.get(last_name_key) or ''}{row.get(first_name_key) or ''}".strip()
            or row.get(username_key)
            or row.get(fallback_key)
            or "未分配"
        )
        result.append({"value": value, "label": _normalize_owner_label(raw_label, aliases)})
    return result


def _contact_method(value):
    text = str(value or "").strip().lower()
    label_map = {label: code for code, label in ContactLog.Method.choices}
    if text in dict(ContactLog.Method.choices):
        return text
    if text in label_map:
        return label_map[text]
    alias_map = {
        "电话": ContactLog.Method.PHONE,
        "phone": ContactLog.Method.PHONE,
        "wechat": ContactLog.Method.WECHAT,
        "微信": ContactLog.Method.WECHAT,
        "邮件": ContactLog.Method.EMAIL,
        "email": ContactLog.Method.EMAIL,
        "拜访": ContactLog.Method.VISIT,
    }
    return alias_map.get(text, ContactLog.Method.WECHAT)


def _find_customer_for_intake(payload):
    customer_no = str(payload.get("customer_no") or payload.get("客户编号") or "").strip()
    if customer_no:
        customer = Customer.objects.filter(customer_no=customer_no, is_active=True).first()
        if customer:
            return customer, []

    region_hint = merge_region_city(payload.get("region") or payload.get("地区"), payload.get("city") or payload.get("城市"))
    phone, phone_wechat = split_phone_and_wechat(payload.get("phone") or payload.get("客户电话") or payload.get("联系电话") or "", region_hint)
    if phone:
        customer = Customer.objects.filter(phone=phone, is_active=True).first()
        if customer:
            return customer, []

    wechat = merge_wechat_values(payload.get("wechat") or payload.get("微信") or payload.get("微信号") or "", phone_wechat)
    if wechat:
        customer = Customer.objects.filter(wechat=wechat, is_active=True).first()
        if customer:
            return customer, []

    name = str(payload.get("customer_name") or payload.get("客户名称") or payload.get("name") or "").strip()
    if name:
        matches = list(Customer.objects.filter(name__iexact=name, is_active=True)[:6])
        if len(matches) == 1:
            return matches[0], []
        if matches:
            return None, matches
    return None, []


def _lock_owner_field(form, user):
    if "owner" in form.fields and not can_assign(user):
        form.fields["owner"].disabled = True
        form.fields["owner"].help_text = "只有领导或管理员可以分配负责人。"


def _lock_signed_by_field(form, user):
    if "signed_by" in form.fields and not can_assign(user):
        form.fields["signed_by"].disabled = True
        form.fields["signed_by"].help_text = "只有领导或管理员可以改签约人员。"


def _set_default_owner(instance, user):
    if not instance.owner_id and not can_assign(user):
        instance.owner = user

def _profile_role(user):
    profile = getattr(user, "profile", None)
    return getattr(profile, "role", "") if profile else ""


def quote_queryset_for(user):
    qs = Quote.objects.select_related("customer", "lead", "opportunity", "quoted_by").prefetch_related("plans")
    if can_view_all(user) or _profile_role(user) in {"finance"}:
        return qs
    return qs.filter(Q(customer__owner=user) | Q(customer__co_owners=user) | Q(quoted_by=user)).distinct()


def payment_queryset_for(user):
    qs = Payment.objects.select_related("customer", "contract")
    if can_view_all(user) or _profile_role(user) in {"finance"}:
        return qs
    return qs.filter(Q(customer__owner=user) | Q(customer__co_owners=user) | Q(contract__signed_by=user) | Q(contract__sales_user=user)).distinct()


def opportunity_queryset_for(user):
    qs = Opportunity.objects.select_related("customer", "owner")
    if can_view_all(user):
        return qs
    return qs.filter(Q(owner=user) | Q(customer__owner=user) | Q(customer__co_owners=user)).distinct()


def visit_queryset_for(user):
    qs = VisitPlan.objects.select_related("customer").prefetch_related("reception_users", "technician_users")
    if can_view_all(user) or _profile_role(user) in {"technician"}:
        return qs
    return qs.filter(Q(customer__owner=user) | Q(customer__co_owners=user) | Q(reception_users=user) | Q(technician_users=user)).distinct()


def task_queryset_for(user):
    qs = TaskReminder.objects.select_related("customer", "lead", "quote", "contract", "assigned_to")
    if can_view_all(user):
        return qs
    return qs.filter(Q(assigned_to=user) | Q(customer__owner=user) | Q(customer__co_owners=user)).distinct()


@login_required
def dashboard(request):
    records = customer_queryset_for(request.user)
    contracts = contract_queryset_for(request.user)
    logs = contact_log_queryset_for(request.user)
    now = timezone.now()
    today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    public_days = getattr(settings, "CRM_PUBLIC_POOL_DAYS", 30)
    stale_cutoff = public_pool_stale_cutoff(now, public_days)
    stale_created_q = _logical_created_before_q(stale_cutoff)
    invalid_source_quality_q = (
        Q(status=Customer.Status.INVALID)
        | Q(grade=Customer.Grade.INVALID)
        | Q(customer_status_text__icontains="无效")
        | Q(customer_status_text__icontains="待定")
        | Q(lead_status__icontains="无效")
        | Q(lead_status__icontains="待定")
    )
    two_week_cutoff = now - timedelta(days=14)
    hot_grades = [Customer.Grade.KEY, Customer.Grade.INTENTION]
    records_for_created_metrics = records.annotate(logical_created_at=Coalesce("historical_created_at", "created_at"))
    owner_aliases = _owner_display_aliases()
    customer_list_url = reverse("customer_list")
    contract_list_url = reverse("contract_list")
    today_date = today_start.date().isoformat()
    dashboard_links = {
        "customers": customer_list_url,
        "contracts": contract_list_url,
        "today_due": f"{customer_list_url}?{urlencode({'date_field': 'nextContact', 'date_from': today_date, 'date_to': today_date, 'sort_field': 'nextContact', 'sort_direction': 'asc'})}",
        "two_week_follow": f"{customer_list_url}?{urlencode({'quick': 'due'})}",
        "public_pool": f"{customer_list_url}?{urlencode({'scope': 'public'})}",
        "contact_logs": reverse("contact_log_list"),
    }

    lead_records = records.filter(source_kind=Customer.RecordKind.LEAD)
    customer_records = records.filter(source_kind=Customer.RecordKind.CUSTOMER)
    contract_total = contracts.aggregate(total=Sum("amount"))["total"] or 0
    dashboard_records = records.exclude(recycled_customer_q())

    status_grouped = records.values("customer_status_text", "status").annotate(total=Count("id")).order_by("-total", "customer_status_text", "status")
    customer_grade_grouped = (
        records.values("grade")
        .annotate(total=Count("id"))
        .order_by("-total", "grade")
    )
    source_grouped = (
        records.values("source_channel")
        .annotate(total=Count("id"))
        .order_by("-total", "source_channel")
    )
    owner_grouped = (
        records.values("owner__username", "owner__first_name", "owner__last_name", "owner_name")
        .annotate(total=Count("id"))
        .order_by("-total", "owner__username", "owner_name")
    )
    status_count_rows = _status_count_rows(dashboard_records)
    pie_grade_grouped = dashboard_records.values("grade").annotate(total=Count("id")).order_by("-total", "grade")
    pie_type_grouped = dashboard_records.values("customer_type").annotate(total=Count("id")).order_by("-total", "customer_type")
    dashboard_pies = [
        _pie_chart("客户需求占比", _demand_count_rows(dashboard_records)),
        _pie_chart("客户状态占比", status_count_rows),
        _pie_chart("客户级别占比", _count_rows(pie_grade_grouped, lambda row: _choice_label(Customer.Grade.choices, row["grade"]))),
        _pie_chart("客户类型占比", _count_rows(pie_type_grouped, lambda row: row["customer_type"] or "未填写")),
    ]
    owner_action_rows = (
        records.values("owner__username", "owner__first_name", "owner__last_name", "owner_name")
        .annotate(
            total=Count("id"),
            due=Count("id", filter=Q(next_contact_at__isnull=False, next_contact_at__lte=now)),
            stale=Count("id", filter=Q(last_contact_at__lt=stale_cutoff) | (Q(last_contact_at__isnull=True) & stale_created_q)),
            hot=Count("id", filter=Q(grade__in=hot_grades)),
            missing_contact=Count("id", filter=Q(phone="", wechat="", email="")),
        )
        .order_by("-due", "-stale", "-hot", "-total")
    )
    log_owner_rows = (
        logs.filter(contact_at__gte=week_start)
        .values("customer__owner__username", "customer__owner__first_name", "customer__owner__last_name", "customer__owner_name")
        .annotate(total=Count("id"))
    )
    weekly_logs_by_owner = {}
    for row in log_owner_rows:
        label = _customer_owner_label(row, owner_aliases)
        weekly_logs_by_owner[label] = weekly_logs_by_owner.get(label, 0) + row["total"]
    owner_totals = {}
    for row in owner_action_rows:
        label = _owner_label(row, owner_aliases)
        current = owner_totals.setdefault(
            label,
            {
                "label": label,
                "total": 0,
                "due": 0,
                "stale": 0,
                "hot": 0,
                "missing_contact": 0,
                "week_logs": 0,
            },
        )
        for field in ("total", "due", "stale", "hot", "missing_contact"):
            current[field] += row[field]
    owner_rows_for_table = []
    for label, row in owner_totals.items():
        row["week_logs"] = weekly_logs_by_owner.get(label, 0)
        owner_rows_for_table.append(row)
    owner_rows_for_table = sorted(
        owner_rows_for_table,
        key=lambda row: (-row["due"], -row["stale"], -row["hot"], -row["total"], row["label"]),
    )[:10]
    source_quality_grouped = (
        records.values("source_channel")
        .annotate(
            total=Count("id"),
            invalid=Count("id", filter=invalid_source_quality_q),
            hot=Count("id", filter=Q(grade__in=hot_grades)),
            deal=Count("id", filter=Q(is_deal=True) | Q(status=Customer.Status.DEAL)),
        )
        .order_by("-total", "source_channel")
    )
    source_quality_rows = []
    for row in source_quality_grouped:
        total = row["total"]
        invalid = row["invalid"]
        effective = max(total - invalid, 0)
        row["effective"] = effective
        row["effective_rate"] = round((effective / total) * 100, 1) if total else 0
        source_quality_rows.append(row)

    due_customers = records.filter(next_contact_at__isnull=False, next_contact_at__lte=now).select_related("owner")[:10]
    stale_customers = (
        records.filter(Q(last_contact_at__lt=stale_cutoff) | (Q(last_contact_at__isnull=True) & stale_created_q))
        .select_related("owner")[:10]
    )
    hot_missing_next_customers = records.filter(grade__in=hot_grades, next_contact_at__isnull=True).select_related("owner")[:10]
    unassigned_customers = records.filter(owner__isnull=True).select_related("owner")[:10]
    missing_contact_customers = records.filter(phone="", wechat="", email="").select_related("owner")[:10]
    dashboard_customers = list(dashboard_records.select_related("owner").order_by("-updated_at", "-id"))
    owned_dashboard_customers = dashboard_customers

    leads = lead_queryset_for(request.user)
    quotes = quote_queryset_for(request.user)
    payments = payment_queryset_for(request.user)
    visits = visit_queryset_for(request.user)
    tasks = task_queryset_for(request.user)
    opportunities = opportunity_queryset_for(request.user)
    signed_contract_amount = contracts.aggregate(total=Sum("contract_amount"))["total"] or contract_total
    paid_amount_total = payments.aggregate(total=Sum("actual_received_amount"))["total"] or Decimal("0")
    unpaid_amount_total = sum((contract.unpaid_amount for contract in contracts[:1000]), Decimal("0"))
    quote_followup_qs = quotes.filter(status__in=[Quote.Status.SENT, Quote.Status.VIEWED]).exclude(customer__deal_status=Customer.DealStatus.WON)
    today_visits = visits.filter(visit_date=today_start.date())
    week_visits = visits.filter(visit_date__gte=week_start.date(), visit_date__lte=(week_start + timedelta(days=6)).date())
    owner_load_rows = []
    active_users = User.objects.filter(is_active=True).order_by("username")
    for user in active_users:
        label = user.get_full_name() or user.username
        user_customers = records.exclude(recycled_customer_q()).filter(Q(owner=user) | Q(co_owners=user)).distinct()
        user_leads = leads.filter(Q(owner=user) | Q(co_owners=user)).distinct()
        owner_load_rows.append({
            "label": label,
            "pending_leads": user_leads.filter(status__in=[Lead.Status.NEW, Lead.Status.PENDING_ASSIGN]).count(),
            "open_leads": user_leads.exclude(status__in=[Lead.Status.CONVERTED, Lead.Status.INVALID, Lead.Status.DUPLICATE]).count(),
            "intent_customers": user_customers.filter(customer_level=Customer.CustomerLevel.INTENTION).count(),
            "quoted_followups": quote_followup_qs.filter(Q(quoted_by=user) | Q(customer__owner=user) | Q(customer__co_owners=user)).distinct().count(),
            "today_due": user_customers.filter(Q(next_contact_at__gte=today_start, next_contact_at__lt=tomorrow_start) | Q(next_follow_at__gte=today_start, next_follow_at__lt=tomorrow_start)).count(),
            "payment_customers": user_customers.filter(contracts__is_active=True).distinct().count(),
        })
    owner_load_rows = [row for row in owner_load_rows if any(row[key] for key in row if key != "label")][:20]
    dashboard_records = records.exclude(recycled_customer_q())
    context = {
        "dashboard_links": dashboard_links,
        "dashboard_pies": dashboard_pies,
        "record_count": dashboard_records.count(),
        "customer_count": customer_records.count(),
        "lead_count": lead_records.count(),
        "new_lead_count": leads.filter(created_at__gte=month_start).count(),
        "pending_lead_count": leads.filter(status__in=[Lead.Status.NEW, Lead.Status.PENDING_ASSIGN]).count(),
        "contract_count": contracts.count(),
        "contract_total": contract_total,
        "signed_contract_amount": signed_contract_amount,
        "paid_amount_total": paid_amount_total,
        "unpaid_amount_total": unpaid_amount_total,
        "quote_followup_count": quote_followup_qs.count(),
        "recycled_customer_count": records.filter(recycled_customer_q()).count(),
        "today_visit_count": today_visits.count(),
        "week_visit_count": week_visits.count(),
        "month_contract_count": contracts.filter(signed_date__gte=month_start.date()).count(),
        "month_contract_total": contracts.filter(signed_date__gte=month_start.date()).aggregate(total=Sum("amount"))["total"] or 0,
        "today_new_count": records_for_created_metrics.filter(logical_created_at__gte=today_start).count(),
        "week_new_count": records_for_created_metrics.filter(logical_created_at__gte=week_start).count(),
        "log_count": logs.count(),
        "today_log_count": logs.filter(contact_at__gte=today_start, contact_at__lt=tomorrow_start).count(),
        "week_log_count": logs.filter(contact_at__gte=week_start).count(),
        "xiaoquan_week_log_count": logs.filter(contact_at__gte=week_start, source=ContactLog.Source.XIAOQUAN).count(),
        "due_count": dashboard_records.filter(next_contact_at__isnull=False, next_contact_at__lte=now).count(),
        "today_due_count": dashboard_records.filter(next_contact_at__gte=today_start, next_contact_at__lt=tomorrow_start).count(),
        "overdue_count": dashboard_records.filter(next_contact_at__lt=today_start).count(),
        "stale_count": dashboard_records.filter(
            Q(last_contact_at__lt=stale_cutoff)
            | (Q(last_contact_at__isnull=True) & stale_created_q)
        ).count(),
        "public_pool_count": records.filter(public_pool_customer_q()).count(),
        "unassigned_count": records.filter(public_pool_customer_q()).count(),
        "missing_contact_count": records.filter(phone="", wechat="", email="").count(),
        "hot_missing_next_count": records.filter(grade__in=hot_grades, next_contact_at__isnull=True).count(),
        "status_rows": _bar_rows_from_counts(status_count_rows),
        "customer_grade_rows": _bar_rows(customer_grade_grouped, lambda row: _choice_label(Customer.Grade.choices, row["grade"])),
        "source_rows": _bar_rows(source_grouped, lambda row: row["source_channel"] or "未填写", max_items=10),
        "owner_rows": _bar_rows(owner_grouped, _owner_label, max_items=10),
        "owner_action_rows": owner_rows_for_table,
        "source_quality_rows": source_quality_rows,
        "source_quality_has_more": len(source_quality_rows) > 10,
        "due_customers": due_customers,
        "stale_customers": stale_customers,
        "hot_missing_next_customers": hot_missing_next_customers,
        "unassigned_customers": unassigned_customers,
        "missing_contact_customers": missing_contact_customers,
        "recent_customers": records.select_related("owner")[:8],
        "recent_logs": logs[:8],
        "recent_contracts": contracts[:8],
        "owner_load_rows": owner_load_rows,
        "quote_followup_rows": quote_followup_qs.select_related("customer", "quoted_by")[:10],
        "payment_board_contracts": [contract for contract in contracts[:80] if contract.unpaid_amount > 0][:10],
        "today_visits": today_visits[:10],
        "week_visits": week_visits[:10],
        "task_overdue_count": tasks.filter(status=TaskReminder.Status.OVERDUE).count(),
    }
    return render(request, "crm/dashboard.html", context)


@login_required
def customer_list(request):
    base_qs, scoped_qs, scope = _customer_scope_queryset(request)
    total_record_count = base_qs.count()
    filters = _customer_filter_state(request)
    qs = _apply_customer_filters(scoped_qs.select_related("owner").prefetch_related("tags", "contact_logs"), filters, request.user)
    page_size = _customer_list_page_size(request)
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    owner_aliases = _owner_display_aliases()
    for customer in page_obj.object_list:
        raw_owner_label = customer.owner.get_full_name() if customer.owner_id else ""
        raw_owner_label = raw_owner_label or (customer.owner.username if customer.owner_id else customer.owner_name)
        customer.owner_display_label = _normalize_owner_label(raw_owner_label, owner_aliases)
        customer.is_public_pool_row = is_public_pool_customer(customer)
        customer.ownership_value = Customer.Status.PUBLIC if customer.is_public_pool_row else customer.status
        customer.ownership_display = "公海客户" if customer.is_public_pool_row else customer.get_status_display()
    query_params = request.GET.copy()
    query_params.pop("page", None)
    clear_querystring = urlencode({"scope": scope, "per_page": page_size})
    today = timezone.localdate()
    needs_followup_q = _customer_needs_followup_q()
    scoped_for_metrics = scoped_qs.annotate(sort_created_at=Coalesce("historical_created_at", "created_at"))
    return render(
        request,
        "crm/customer_list.html",
        {
            "customers": page_obj,
            "page_obj": page_obj,
            "paginator": paginator,
            "total_record_count": total_record_count,
            "filtered_record_count": paginator.count,
            "today_new_count": scoped_for_metrics.filter(sort_created_at__date=today).count(),
            "today_due_count": scoped_for_metrics.filter(needs_followup_q).count(),
            "key_customer_count": scoped_for_metrics.filter(grade__in=[Customer.Grade.KEY, Customer.Grade.INTENTION]).count(),
            "q": filters["q"],
            "kind": filters["kind"],
            "status": filters["status"],
            "scope": scope,
            "filters": filters,
            "show_owner_column": can_view_all(request.user),
            "list_title": "公海" if scope == "public" else ("客户管理总表" if can_view_all(request.user) else "我的客户"),
            "kind_choices": Customer.RecordKind.choices,
            "grade_choices": Customer.Grade.choices,
            "customer_type_options": CUSTOMER_TYPE_OPTIONS,
            "demand_options": DEMAND_OPTIONS,
            "customer_status_options": CUSTOMER_STATUS_OPTIONS,
            "source_options": SOURCE_OPTIONS,
            "trade_type_choices": Customer.TradeType.choices,
            "customer_level_choices": Customer.CustomerLevel.choices,
            "follow_status_choices": Customer.FollowStatus.choices,
            "quote_status_choices": Quote.Status.choices,
            "product_options": DEMAND_OPTIONS,
            "owner_options": _customer_owner_options(request.user) if can_view_all(request.user) else [],
            "inline_options": _inline_options(request.user),
            "can_undo": bool(request.session.get("customer_undo_stack")),
            "can_redo": bool(request.session.get("customer_redo_stack")),
            "base_querystring": query_params.urlencode(),
            "clear_querystring": clear_querystring,
            "has_active_filters": _customer_has_active_filters(filters),
            "page_size": page_size,
            "min_page_size": CUSTOMER_LIST_MIN_PAGE_SIZE,
            "max_page_size": CUSTOMER_LIST_MAX_PAGE_SIZE,
        },
    )


@login_required
def customer_export(request):
    base_qs, scoped_qs, _scope = _customer_scope_queryset(request)
    filters = _customer_filter_state(request)
    qs = _apply_customer_filters(scoped_qs.select_related("owner"), filters, request.user)
    ids = _selected_list(request.GET, "ids")
    if ids:
        qs = qs.filter(pk__in=ids)
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = 'attachment; filename="customers.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(
        [
            "客户编号",
            "客户名称",
            "客户经理",
            "客户级别",
            "客户类型",
            "客户需求",
            "客户状态",
            "客户电话",
            "微信",
            "邮箱",
            "最后联系时间",
            "未联系天数",
            "下次联系时间",
            "地区",
            "线索来源",
            "联系人",
            "创建时间",
        ]
    )
    for customer in qs[:5000]:
        writer.writerow(
            [
                customer.customer_no,
                customer.name,
                customer.owner_display,
                customer.get_grade_display(),
                customer.customer_type,
                customer.demand,
                customer.customer_status_text,
                customer.phone,
                customer.wechat,
                customer.email,
                customer.last_contact_at.strftime("%Y-%m-%d") if customer.last_contact_at else "",
                customer.uncontacted_days if customer.uncontacted_days is not None else "",
                customer.next_contact_at.strftime("%Y-%m-%d") if customer.next_contact_at else "",
                customer.location_display,
                customer.source_channel,
                customer.contact_name,
                customer.created_time_display.strftime("%Y-%m-%d %H:%M") if customer.created_time_display else "",
            ]
        )
    return response


def _run_customer_import(rows, mapping, duplicate_mode, overwrite, user):
    stats = {"seen": 0, "created": 0, "updated": 0, "skipped": 0, "duplicates": 0}
    for row in rows:
        stats["seen"] += 1
        values = _row_to_customer_import_values(row, mapping)
        if not _has_import_identity(values):
            stats["skipped"] += 1
            continue
        customer = _find_customer_for_import(values)
        creating = customer is None
        if customer and duplicate_mode != "merge":
            stats["duplicates"] += 1
            stats["skipped"] += 1
            continue
        customer = customer or Customer()
        if creating and not can_assign(user):
            customer.owner = user
        historical_created_at = values.get("historical_created_at")
        historical_changed = _historical_created_at_import_needed(customer, historical_created_at)
        changed = _apply_import_values(customer, values, overwrite=overwrite or creating)
        if creating:
            customer.created_by = user
        if changed or creating or historical_changed:
            customer.save()
            if historical_changed:
                _save_imported_historical_created_at(customer, historical_created_at)
            apply_auto_tags(customer)
            stats["created" if creating else "updated"] += 1
            if not creating:
                stats["duplicates"] += 1
        else:
            stats["skipped"] += 1
    return stats


def _customer_import_preview_context(rows, headers, mapping, filename, duplicate_mode, overwrite):
    matched_headers = {header for header in mapping.values() if header}
    preview_rows = [{"cells": [row.get(header, "") for header in headers]} for row in rows[:8]]
    field_specs = []
    for spec in _import_field_specs():
        item = dict(spec)
        item["selected"] = mapping.get(spec["field"], "")
        field_specs.append(item)
    return {
        "filename": filename,
        "row_count": len(rows),
        "headers": headers,
        "field_specs": field_specs,
        "preview_rows": preview_rows,
        "duplicate_mode": duplicate_mode,
        "overwrite": overwrite,
        "matched_count": len([value for value in mapping.values() if value]),
        "ignored_headers": [header for header in headers if header not in matched_headers],
    }


@login_required
@require_POST
def customer_import(request):
    uploaded_file = request.FILES.get("import_file")
    duplicate_mode = request.POST.get("duplicate_mode", "merge")
    overwrite = request.POST.get("overwrite") == "1"
    if not uploaded_file:
        messages.error(request, "请选择要导入的表格文件。")
        return redirect("customer_list")
    try:
        rows = _read_customer_import_rows(uploaded_file)
    except (ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        messages.error(request, f"导入失败：{exc}")
        return redirect("customer_list")

    if not rows:
        messages.error(request, "表格里没有可导入的数据。")
        return redirect("customer_list")

    headers = list(rows[0].keys())
    mapping = _default_import_mapping(headers)
    request.session["customer_import_payload"] = {
        "rows": rows,
        "headers": headers,
        "filename": uploaded_file.name,
        "duplicate_mode": duplicate_mode,
        "overwrite": overwrite,
    }
    request.session.modified = True
    return render(
        request,
        "crm/customer_import_preview.html",
        _customer_import_preview_context(rows, headers, mapping, uploaded_file.name, duplicate_mode, overwrite),
    )


@login_required
@require_POST
def customer_import_confirm(request):
    payload = request.session.get("customer_import_payload")
    if not payload:
        messages.error(request, "导入预览已失效，请重新选择表格文件。")
        return redirect("customer_list")

    rows = payload.get("rows") or []
    headers = payload.get("headers") or []
    mapping = {
        spec["field"]: request.POST.get(f"map_{spec['field']}", "").strip()
        for spec in _import_field_specs()
    }
    allowed_headers = set(headers)
    mapping = {field: header if header in allowed_headers else "" for field, header in mapping.items()}
    duplicate_mode = request.POST.get("duplicate_mode") or payload.get("duplicate_mode") or "merge"
    overwrite = request.POST.get("overwrite") == "1"

    if not any(mapping.values()):
        messages.error(request, "请至少匹配一个表头后再导入。")
        return render(
            request,
            "crm/customer_import_preview.html",
            _customer_import_preview_context(
                rows,
                headers,
                mapping,
                payload.get("filename", ""),
                duplicate_mode,
                overwrite,
            ),
        )

    stats = _run_customer_import(rows, mapping, duplicate_mode, overwrite, request.user)
    request.session.pop("customer_import_payload", None)
    request.session.modified = True
    messages.success(
        request,
        f"导入完成：读取 {stats['seen']} 行，新增 {stats['created']} 条，更新 {stats['updated']} 条，"
        f"跳过 {stats['skipped']} 条，匹配重复 {stats['duplicates']} 条。",
    )
    return redirect("customer_list")


@login_required
@require_POST
def customer_bulk_action(request):
    action = request.POST.get("action", "").strip()
    ids = request.POST.getlist("ids")
    next_url = request.POST.get("next", "")
    is_filtered_selection = request.POST.get("selection_scope") == "filtered"
    selected_qs = None
    if is_filtered_selection:
        selected_ids = list(
            _filtered_customer_queryset_from_querystring(
                request,
                request.POST.get("selection_querystring", ""),
            ).values_list("pk", flat=True)
        )
        selected_qs = customer_queryset_for(request.user).filter(pk__in=selected_ids)
    elif ids:
        selected_qs = customer_queryset_for(request.user).filter(pk__in=ids)
    if selected_qs is None:
        messages.error(request, "请先选择客户。")
    elif action == "delete":
        qs = selected_qs
        if not can_view_all(request.user):
            qs = qs.filter(owner=request.user).exclude(status=Customer.Status.PUBLIC)
        customers = list(qs.only("id", "customer_no", "status", "owner_name", "is_recycled"))
        now = timezone.now()
        count = qs.update(is_recycled=True, recycled_at=now, recycle_reason="主动删除", is_public=False, updated_by=request.user, updated_at=now)
        for customer in customers:
            OperationLog.objects.create(
                user=request.user,
                customer=customer,
                action_type=OperationLog.ActionType.RECYCLE,
                before_data={"status": customer.status, "is_recycled": customer.is_recycled},
                after_data={"is_recycled": True},
                remark="批量删除进入回收站",
            )
        messages.success(request, f"已删除 {count} 条客户资料，已移入回收站。")
    elif action == "claim":
        owner_name = request.user.get_full_name() or request.user.username
        qs = selected_qs.filter(public_pool_customer_q())
        count = 0
        for customer in qs:
            before = {"status": customer.status, "owner": customer.owner_name, "is_public": customer.is_public}
            customer.owner = request.user
            customer.owner_name = owner_name
            customer.status = Customer.Status.PRIVATE
            customer.is_public = False
            customer.release_warned_at = None
            customer.save(update_fields=["owner", "owner_name", "status", "is_public", "release_warned_at", "updated_at"])
            OperationLog.objects.create(user=request.user, customer=customer, action_type=OperationLog.ActionType.CLAIM, before_data=before, after_data={"owner": owner_name, "status": customer.status}, remark="从公海认领")
            count += 1
        messages.success(request, f"已认领 {count} 条公海客户。")
    else:
        messages.error(request, "未知批量操作。")
    if next_url.startswith("/customers"):
        return redirect(next_url)
    return redirect("customer_list")


@login_required
def customer_create(request):
    if request.method == "POST":
        form = CustomerForm(request.POST, request.FILES)
        _lock_owner_field(form, request.user)
        if form.is_valid():
            customer = form.save(commit=False)
            customer.created_by = request.user
            _set_default_owner(customer, request.user)
            customer.save()
            form.save_m2m()
            apply_auto_tags(customer)
            messages.success(request, "资料已保存。")
            return redirect("customer_detail", pk=customer.pk)
    else:
        initial = {}
        if request.GET.get("kind") == Customer.RecordKind.LEAD:
            initial["source_kind"] = Customer.RecordKind.LEAD
            initial["lead_status"] = "待确认"
        if not can_assign(request.user):
            initial["owner"] = request.user
        form = CustomerForm(initial=initial)
    _lock_owner_field(form, request.user)
    return render(request, "crm/customer_form.html", {"form": form, "title": "新增客户/线索"})


@login_required
def customer_edit(request, pk):
    customer = get_object_or_404(customer_queryset_for(request.user), pk=pk)
    if not can_view_all(request.user) and (customer.owner_id != request.user.id or customer.status == Customer.Status.PUBLIC):
        raise PermissionDenied
    if request.method == "POST":
        form = CustomerForm(request.POST, request.FILES, instance=customer)
        _lock_owner_field(form, request.user)
        if form.is_valid():
            customer = form.save()
            apply_auto_tags(customer)
            messages.success(request, "资料已更新。")
            return redirect("customer_detail", pk=customer.pk)
    else:
        form = CustomerForm(instance=customer)
    _lock_owner_field(form, request.user)
    return render(request, "crm/customer_form.html", {"form": form, "title": "编辑客户/线索", "customer": customer})


@login_required
@require_POST
def customer_inline_update(request, pk):
    customer = get_object_or_404(customer_queryset_for(request.user), pk=pk)
    if not can_view_all(request.user) and (customer.owner_id != request.user.id or customer.status == Customer.Status.PUBLIC):
        raise PermissionDenied
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "请求内容必须是 JSON。"}, status=400)
    field = str(payload.get("field") or "").strip()
    if field not in INLINE_CUSTOMER_FIELDS:
        return JsonResponse({"ok": False, "error": "该字段不支持行内编辑。"}, status=400)
    if field in ADMIN_INLINE_CUSTOMER_FIELDS and not can_assign(request.user):
        raise PermissionDenied
    old_value = getattr(customer, field)
    try:
        new_value = _coerce_inline_value(field, payload.get("value"))
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    if old_value == new_value:
        return JsonResponse({"ok": True, "changed": False})
    old_status = customer.status
    setattr(customer, field, new_value)
    update_fields = [field, "updated_at"]
    if field == "is_deal":
        if new_value:
            customer.status = Customer.Status.DEAL
            customer.deal_status = Customer.DealStatus.WON
            customer.customer_level = Customer.CustomerLevel.DEAL
            customer.follow_status = Customer.FollowStatus.DEAL
        else:
            customer.deal_status = Customer.DealStatus.OPEN
            if customer.status == Customer.Status.DEAL:
                customer.status = Customer.Status.PRIVATE
            if customer.customer_level == Customer.CustomerLevel.DEAL:
                customer.customer_level = Customer.CustomerLevel.PENDING
            if customer.follow_status == Customer.FollowStatus.DEAL:
                customer.follow_status = Customer.FollowStatus.CONTACTED
        for extra_field in ("status", "deal_status", "customer_level", "follow_status"):
            if extra_field not in update_fields:
                update_fields.append(extra_field)
    customer.save(update_fields=update_fields)
    apply_auto_tags(customer)
    history_entry = {
        "pk": customer.pk,
        "field": field,
        "old": _serialize_inline_value(old_value),
        "new": _serialize_inline_value(new_value),
        "label": INLINE_CUSTOMER_FIELDS[field]["label"],
        "customer": str(customer),
    }
    if field == "is_deal":
        history_entry["old_status"] = old_status
        history_entry["new_status"] = customer.status
    _push_customer_history(request, history_entry)
    return JsonResponse({"ok": True, "changed": True})


@login_required
@require_POST
def customer_history_action(request):
    action = request.POST.get("action", "").strip()
    undo_stack = request.session.get("customer_undo_stack", [])
    redo_stack = request.session.get("customer_redo_stack", [])
    try:
        if action == "undo" and undo_stack:
            entry = undo_stack.pop()
            _apply_customer_history_entry(entry, use_new_value=False)
            redo_stack.append(entry)
            message = f"已撤回：{entry.get('customer', '')} / {entry.get('label', '')}"
        elif action == "redo" and redo_stack:
            entry = redo_stack.pop()
            _apply_customer_history_entry(entry, use_new_value=True)
            undo_stack.append(entry)
            message = f"已恢复：{entry.get('customer', '')} / {entry.get('label', '')}"
        else:
            return JsonResponse({"ok": False, "error": "没有可执行的历史操作。"}, status=400)
    except Customer.DoesNotExist:
        return JsonResponse({"ok": False, "error": "客户记录不存在。"}, status=404)
    request.session["customer_undo_stack"] = undo_stack[-50:]
    request.session["customer_redo_stack"] = redo_stack[-50:]
    request.session.modified = True
    return JsonResponse({"ok": True, "message": message})


@login_required
@require_POST
def customer_claim(request, pk):
    customer = get_object_or_404(
        customer_queryset_for(request.user).filter(public_pool_customer_q()),
        pk=pk,
    )
    before = {"status": customer.status, "owner": customer.owner_name, "is_public": customer.is_public}
    customer.owner = request.user
    customer.owner_name = request.user.get_full_name() or request.user.username
    customer.status = Customer.Status.PRIVATE
    customer.is_public = False
    customer.release_warned_at = None
    customer.save(update_fields=["owner", "owner_name", "status", "is_public", "release_warned_at", "updated_at"])
    OperationLog.objects.create(user=request.user, customer=customer, action_type=OperationLog.ActionType.CLAIM, before_data=before, after_data={"owner": customer.owner_name, "status": customer.status}, remark="从公海认领")
    messages.success(request, f"已认领客户：{customer}")
    return redirect("customer_list")


@login_required
@require_POST
def customer_delete(request, pk):
    customer = get_object_or_404(customer_queryset_for(request.user), pk=pk)
    if customer.owner_id != request.user.id and not can_view_all(request.user):
        raise PermissionDenied
    before = {"status": customer.status, "is_recycled": customer.is_recycled}
    customer.is_recycled = True
    customer.recycled_at = timezone.now()
    customer.recycle_reason = request.POST.get("reason", "主动删除")[:240]
    customer.is_public = False
    if customer.status == Customer.Status.PUBLIC:
        customer.status = Customer.Status.PRIVATE
    customer.updated_by = request.user
    customer.save(update_fields=["is_recycled", "recycled_at", "recycle_reason", "is_public", "status", "updated_by", "updated_at"])
    OperationLog.objects.create(user=request.user, customer=customer, action_type=OperationLog.ActionType.RECYCLE, before_data=before, after_data={"is_recycled": True, "status": customer.status}, remark=customer.recycle_reason)
    messages.success(request, f"客户已移入回收站：{customer}")
    return redirect("customer_list")


@login_required
@require_POST
def customer_restore(request, pk):
    qs = Customer.objects.filter(is_active=False)
    if not can_view_all(request.user):
        qs = qs.filter(owner=request.user)
    customer = get_object_or_404(qs, pk=pk)
    customer.is_active = True
    customer.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"客户已恢复：{customer}")
    return redirect("deleted_list")


@login_required
def customer_detail(request, pk):
    customer = get_object_or_404(
        customer_queryset_for(request.user).select_related("owner").prefetch_related(
            "tags", "contacts", "contact_logs", "quotes__plans", "contracts__payments", "payments",
            "operation_logs", "task_reminders",
        ),
        pk=pk,
    )
    if request.method == "POST":
        form = ContactLogForm(request.POST, request.FILES)
        if form.is_valid():
            log = form.save(commit=False)
            log.customer = customer
            log.created_by = request.user
            if not log.followed_by_id:
                log.followed_by = request.user
            if not log.follower_name:
                log.follower_name = request.user.get_full_name() or request.user.username
            log.save()
            update_customer_after_contact(customer, log)
            OperationLog.objects.create(
                user=request.user,
                customer=customer,
                action_type=OperationLog.ActionType.CREATE,
                after_data={"contact_log": log.pk, "contact_at": log.contact_at.isoformat(), "next_action": log.next_action},
                remark="新增联系记录",
            )
            messages.success(request, "跟进日志已保存，最后联系时间和下一步动作已更新。")
            return redirect("customer_detail", pk=customer.pk)
    else:
        form = ContactLogForm(initial={
            "contact_at": timezone.now(),
            "followed_by": request.user,
            "follower_name": request.user.get_full_name() or request.user.username,
            "level_after": customer.grade,
            "status_after": customer.customer_status_text,
        })
    logs = customer.contact_logs.select_related("created_by", "followed_by").order_by("-contact_at")[:80]
    contacts = customer.contacts.all()
    quotes = customer.quotes.select_related("quoted_by", "lead", "opportunity").prefetch_related("plans__items")[:50]
    contracts = customer.contracts.select_related("signed_by", "sales_user", "quote", "opportunity").prefetch_related("payments")[:30]
    payments = customer.payments.select_related("contract")[:50]
    operation_logs = customer.operation_logs.select_related("user")[:80]
    task_reminders = customer.task_reminders.select_related("assigned_to")[:50]
    summary = {
        "latest_quote_amount": customer.latest_quote_amount,
        "paid_amount": customer.paid_amount,
        "unpaid_amount": customer.unpaid_amount,
        "contract_status": contracts[0].get_status_display() if contracts else "暂无合同",
    }
    return render(
        request,
        "crm/customer_detail.html",
        {
            "customer": customer,
            "contacts": contacts,
            "logs": logs,
            "quotes": quotes,
            "contracts": contracts,
            "payments": payments,
            "operation_logs": operation_logs,
            "task_reminders": task_reminders,
            "summary": summary,
            "form": form,
        },
    )


@login_required
def contact_log_list(request):
    qs = contact_log_queryset_for(request.user)
    q = request.GET.get("q", "").strip()
    customer_id = request.GET.get("customer", "").strip()
    contact_date = request.GET.get("contact_date", "").strip()
    method = request.GET.get("method", "").strip()
    result_filters = _selected_list(request.GET, "result")
    if q:
        qs = qs.filter(
            Q(customer__customer_no__icontains=q)
            | Q(customer__name__icontains=q)
            | Q(summary__icontains=q)
            | Q(result__icontains=q)
            | Q(follower_name__icontains=q)
        )
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    if contact_date:
        qs = qs.filter(contact_at__date=contact_date)
    if method:
        qs = qs.filter(method=method)
    if result_filters:
        result_q = Q()
        for item in result_filters:
            result_q |= Q(result__icontains=item)
        qs = qs.filter(result_q)
    visible_customers = customer_queryset_for(request.user).order_by("customer_no", "name")
    existing_results = []
    for value in (
        contact_log_queryset_for(request.user)
        .exclude(result="")
        .values_list("result", flat=True)
        .distinct()
        .order_by("result")[:80]
    ):
        existing_results.extend(_multi_values(value))
    result_options = []
    seen_results = set()
    for item in list(CUSTOMER_STATUS_OPTIONS) + existing_results:
        if item and item not in seen_results:
            result_options.append(item)
            seen_results.add(item)
    return render(
        request,
        "crm/contact_log_list.html",
        {
            "logs": qs[:300],
            "q": q,
            "customer_id": customer_id,
            "contact_date": contact_date,
            "method": method,
            "result": result_filters,
            "customer_options": visible_customers,
            "method_choices": ContactLog.Method.choices,
            "result_options": result_options,
            "inline_options": _contact_log_inline_options(request.user),
        },
    )


@login_required
def contact_log_create(request):
    if request.method == "POST":
        form = ContactLogCreateForm(request.POST, request.FILES)
        form.fields["customer"].queryset = customer_queryset_for(request.user)
        if form.is_valid():
            log = form.save(commit=False)
            log.created_by = request.user
            if not log.followed_by_id:
                log.followed_by = request.user
            if not log.follower_name:
                log.follower_name = request.user.get_full_name() or request.user.username
            log.save()
            update_customer_after_contact(log.customer, log)
            messages.success(request, "跟进日志已保存。")
            return redirect("contact_log_list")
    else:
        initial = {"contact_at": timezone.now(), "follower_name": request.user.get_full_name() or request.user.username}
        customer_id = request.GET.get("customer")
        if customer_id:
            customer = customer_queryset_for(request.user).filter(pk=customer_id).first()
            if customer:
                initial["customer"] = customer
                initial["level_after"] = customer.grade
                initial["status_after"] = customer.customer_status_text
        form = ContactLogCreateForm(initial=initial)
        form.fields["customer"].queryset = customer_queryset_for(request.user)
    return render(request, "crm/contact_log_form.html", {"form": form, "title": "新增跟进日志"})


@login_required
def contact_log_edit(request, pk):
    log = get_object_or_404(contact_log_queryset_for(request.user), pk=pk)
    if request.method == "POST":
        form = ContactLogForm(request.POST, request.FILES, instance=log)
        if form.is_valid():
            log = form.save()
            update_customer_after_contact(log.customer, log)
            messages.success(request, "跟进日志已更新。")
            return redirect("contact_log_list")
    else:
        form = ContactLogForm(instance=log)
    return render(request, "crm/contact_log_form.html", {"form": form, "title": "编辑跟进日志", "log": log})

@login_required
@require_POST
def contact_log_inline_update(request, pk):
    log = get_object_or_404(contact_log_queryset_for(request.user), pk=pk)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "请求内容必须是 JSON。"}, status=400)
    field = str(payload.get("field") or "").strip()
    if field not in INLINE_CONTACT_LOG_FIELDS:
        return JsonResponse({"ok": False, "error": "该字段不支持行内编辑。"}, status=400)
    old_value = getattr(log, field)
    try:
        new_value = _coerce_contact_log_inline_value(field, payload.get("value"))
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    if old_value == new_value:
        return JsonResponse({"ok": True, "changed": False})
    setattr(log, field, new_value)
    log.save(update_fields=[field])
    update_customer_after_contact(log.customer, log)
    return JsonResponse({"ok": True, "changed": True})

@login_required
@require_POST
def contact_log_delete(request, pk):
    log = get_object_or_404(contact_log_queryset_for(request.user), pk=pk)
    customer = log.customer
    log.delete()
    messages.success(request, f"已删除 {customer} 的跟进日志。")
    return redirect("contact_log_list")


@login_required
def contract_list(request):
    qs = contract_queryset_for(request.user)
    q = request.GET.get("q", "").strip()
    amount_min = request.GET.get("amount_min", "").strip()
    amount_max = request.GET.get("amount_max", "").strip()
    signed_date = request.GET.get("signed_date", "").strip()
    signer = request.GET.get("signer", "").strip()
    if q:
        qs = qs.filter(
            Q(contract_no__icontains=q)
            | Q(customer__customer_no__icontains=q)
            | Q(customer__name__icontains=q)
            | Q(customer_name__icontains=q)
            | Q(signed_by_name__icontains=q)
            | Q(attachment_note__icontains=q)
        )
    for value, lookup in ((amount_min, "amount__gte"), (amount_max, "amount__lte")):
        if value:
            try:
                qs = qs.filter(**{lookup: Decimal(value)})
            except InvalidOperation:
                messages.error(request, "合同金额筛选必须填写数字。")
    if signed_date:
        qs = qs.filter(signed_date=signed_date)
    if signer:
        qs = qs.filter(Q(signed_by_id=signer) | Q(signed_by_name=signer))
    signer_options = []
    seen_signers = set()
    for item in (
        contract_queryset_for(request.user)
        .values("signed_by_id", "signed_by__username", "signed_by__first_name", "signed_by__last_name", "signed_by_name")
        .distinct()
    ):
        value = str(item["signed_by_id"] or item["signed_by_name"] or "").strip()
        if not value or value in seen_signers:
            continue
        seen_signers.add(value)
        label = (
            f"{item.get('signed_by__last_name') or ''}{item.get('signed_by__first_name') or ''}".strip()
            or item.get("signed_by__username")
            or item.get("signed_by_name")
            or "未填写"
        )
        signer_options.append({"value": value, "label": label})
    return render(
        request,
        "crm/contract_list.html",
        {
            "contracts": qs[:300],
            "q": q,
            "amount_min": amount_min,
            "amount_max": amount_max,
            "signed_date": signed_date,
            "signer": signer,
            "signer_options": signer_options,
            "inline_options": _contract_inline_options(request.user),
            "can_assign_contracts": can_assign(request.user),
        },
    )


@login_required
def contract_create(request):
    if request.method == "POST":
        form = ContractForm(request.POST, request.FILES)
        form.fields["customer"].queryset = customer_queryset_for(request.user)
        _lock_signed_by_field(form, request.user)
        if form.is_valid():
            contract = form.save(commit=False)
            contract.created_by = request.user
            if not contract.signed_by_id and not can_assign(request.user):
                contract.signed_by = request.user
            if contract.customer and not contract.customer_name:
                contract.customer_name = str(contract.customer)
            contract.save()
            messages.success(request, "合同已保存。")
            return redirect("contract_list")
    else:
        form = ContractForm(initial={"signed_by": request.user})
        form.fields["customer"].queryset = customer_queryset_for(request.user)
    _lock_signed_by_field(form, request.user)
    return render(request, "crm/contract_form.html", {"form": form, "title": "新增合同"})


@login_required
def contract_edit(request, pk):
    contract = get_object_or_404(contract_queryset_for(request.user), pk=pk)
    if request.method == "POST":
        form = ContractForm(request.POST, request.FILES, instance=contract)
        form.fields["customer"].queryset = customer_queryset_for(request.user)
        _lock_signed_by_field(form, request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "合同已更新。")
            return redirect("contract_list")
    else:
        form = ContractForm(instance=contract)
        form.fields["customer"].queryset = customer_queryset_for(request.user)
    _lock_signed_by_field(form, request.user)
    return render(request, "crm/contract_form.html", {"form": form, "title": "编辑合同", "contract": contract})

@login_required
@require_POST
def contract_inline_update(request, pk):
    contract = get_object_or_404(contract_queryset_for(request.user), pk=pk)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "请求内容必须是 JSON。"}, status=400)
    field = str(payload.get("field") or "").strip()
    if field not in INLINE_CONTRACT_FIELDS:
        return JsonResponse({"ok": False, "error": "该字段不支持行内编辑。"}, status=400)
    if field in ADMIN_INLINE_CONTRACT_FIELDS and not can_assign(request.user):
        raise PermissionDenied
    old_value = getattr(contract, field)
    try:
        new_value = _coerce_contract_inline_value(field, payload.get("value"), request.user)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    if old_value == new_value:
        return JsonResponse({"ok": True, "changed": False})
    setattr(contract, field, new_value)
    if field == "customer_id" and new_value:
        customer = Customer.objects.get(pk=new_value)
        contract.customer_name = str(customer)
    if field == "signed_by_id":
        contract.signed_by_name = ""
    contract.save()
    return JsonResponse({"ok": True, "changed": True})

@login_required
@require_POST
def contract_delete(request, pk):
    contract = get_object_or_404(contract_queryset_for(request.user), pk=pk)
    contract.is_active = False
    contract.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"合同已移入最近删除：{contract}")
    return redirect("contract_list")


@login_required
@require_POST
def contract_restore(request, pk):
    qs = Contract.objects.filter(is_active=False).select_related("customer", "signed_by")
    if not can_view_all(request.user):
        qs = qs.filter(Q(customer__owner=request.user) | Q(signed_by=request.user))
    contract = get_object_or_404(qs, pk=pk)
    contract.is_active = True
    contract.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"合同已恢复：{contract}")
    return redirect("deleted_list")


@login_required
def deleted_list(request):
    q = request.GET.get("q", "").strip()
    record_type = request.GET.get("record_type", "").strip()
    owner = request.GET.get("owner", "").strip()
    deleted_date = request.GET.get("deleted_date", "").strip()
    customers = Customer.objects.filter(is_active=False).select_related("owner")
    contracts = Contract.objects.filter(is_active=False).select_related("customer", "signed_by")
    if not can_view_all(request.user):
        customers = customers.filter(owner=request.user)
        contracts = contracts.filter(Q(customer__owner=request.user) | Q(signed_by=request.user))
    if q:
        customers = customers.filter(
            Q(customer_no__icontains=q)
            | Q(name__icontains=q)
            | Q(owner_name__icontains=q)
            | Q(phone__icontains=q)
            | Q(wechat__icontains=q)
        )
        contracts = contracts.filter(
            Q(contract_no__icontains=q)
            | Q(customer__customer_no__icontains=q)
            | Q(customer__name__icontains=q)
            | Q(customer_name__icontains=q)
            | Q(signed_by_name__icontains=q)
        )
    if owner:
        customers = customers.filter(Q(owner_id=owner) | Q(owner_name=owner))
        contracts = contracts.filter(Q(signed_by_id=owner) | Q(signed_by_name=owner))
    if deleted_date:
        customers = customers.filter(updated_at__date=deleted_date)
        contracts = contracts.filter(updated_at__date=deleted_date)
    if record_type == "customer":
        contracts = Contract.objects.none()
    elif record_type == "contract":
        customers = Customer.objects.none()
    owner_options = _owner_options_from_rows(
        Customer.objects.filter(is_active=False)
        .values("owner_id", "owner__username", "owner__first_name", "owner__last_name", "owner_name")
        .distinct()
    )
    return render(
        request,
        "crm/deleted_list.html",
        {
            "customers": customers[:200],
            "contracts": contracts[:200],
            "q": q,
            "record_type": record_type,
            "owner": owner,
            "deleted_date": deleted_date,
            "owner_options": owner_options,
        },
    )


@login_required
def customer_transfer(request, pk):
    customer = get_object_or_404(customer_queryset_for(request.user), pk=pk)
    if not can_assign(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = CustomerTransferForm(request.POST)
        if form.is_valid():
            old_owner = customer.owner
            new_owner = form.cleaned_data["new_owner"]
            customer.owner = new_owner
            customer.owner_name = new_owner.get_full_name() or new_owner.username
            customer.status = Customer.Status.PRIVATE
            customer.is_public = False
            customer.updated_by = request.user
            customer.save(update_fields=["owner", "owner_name", "status", "is_public", "updated_by", "updated_at"])
            OperationLog.objects.create(
                user=request.user,
                customer=customer,
                action_type=OperationLog.ActionType.TRANSFER,
                before_data={"owner": old_owner.get_username() if old_owner else ""},
                after_data={"owner": new_owner.get_username()},
                remark=form.cleaned_data.get("reason", ""),
            )
            messages.success(request, "客户已转移。")
            return redirect("customer_detail", pk=customer.pk)
    else:
        form = CustomerTransferForm(initial={"new_owner": customer.owner})
    return render(request, "crm/simple_form.html", {"form": form, "title": f"转移客户：{customer}", "back_url": reverse("customer_detail", args=[customer.pk])})


@login_required
@require_POST
def customer_mark_invalid(request, pk):
    customer = get_object_or_404(customer_queryset_for(request.user), pk=pk)
    if not can_assign(request.user) and customer.owner_id != request.user.id:
        raise PermissionDenied
    before = {"status": customer.status, "customer_level": customer.customer_level, "follow_status": customer.follow_status}
    customer.status = Customer.Status.INVALID
    customer.customer_level = Customer.CustomerLevel.INVALID
    customer.follow_status = Customer.FollowStatus.INVALID_CLOSED
    customer.is_recycled = True
    customer.recycled_at = timezone.now()
    customer.recycle_reason = request.POST.get("reason", "标记无效")[:240]
    customer.updated_by = request.user
    customer.save(update_fields=["status", "customer_level", "follow_status", "is_recycled", "recycled_at", "recycle_reason", "updated_by", "updated_at"])
    OperationLog.objects.create(user=request.user, customer=customer, action_type=OperationLog.ActionType.MARK_INVALID, before_data=before, after_data={"status": customer.status}, remark=customer.recycle_reason)
    messages.success(request, "客户已标记无效并进入回收站。")
    return redirect("customer_detail", pk=customer.pk)


@login_required
@require_POST
def customer_recycle_restore(request, pk):
    customer = get_object_or_404(customer_queryset_for(request.user), pk=pk)
    if not can_assign(request.user) and customer.owner_id != request.user.id:
        raise PermissionDenied
    before = {"status": customer.status, "is_recycled": customer.is_recycled}
    customer.is_recycled = False
    customer.recycled_at = None
    customer.recycle_reason = ""
    customer.status = Customer.Status.PRIVATE
    if customer.customer_level == Customer.CustomerLevel.INVALID:
        customer.customer_level = Customer.CustomerLevel.PENDING
    if customer.follow_status == Customer.FollowStatus.INVALID_CLOSED:
        customer.follow_status = Customer.FollowStatus.NEW_INQUIRY
    customer.updated_by = request.user
    customer.save(update_fields=["is_recycled", "recycled_at", "recycle_reason", "status", "customer_level", "follow_status", "updated_by", "updated_at"])
    OperationLog.objects.create(user=request.user, customer=customer, action_type=OperationLog.ActionType.RESTORE, before_data=before, after_data={"status": customer.status, "is_recycled": customer.is_recycled}, remark="从回收站恢复")
    messages.success(request, "客户已从回收站恢复。")
    return redirect("customer_detail", pk=customer.pk)


@login_required
def customer_merge(request, pk):
    source = get_object_or_404(customer_queryset_for(request.user), pk=pk)
    if not can_assign(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = CustomerMergeForm(request.POST, customer_queryset=customer_queryset_for(request.user), exclude_customer=source)
        if form.is_valid():
            target = form.cleaned_data["target_customer"]
            with transaction.atomic():
                Contact.objects.filter(customer=source).update(customer=target)
                ContactLog.objects.filter(customer=source).update(customer=target)
                Quote.objects.filter(customer=source).update(customer=target)
                Contract.objects.filter(customer=source).update(customer=target, customer_name=str(target))
                Payment.objects.filter(customer=source).update(customer=target)
                VisitPlan.objects.filter(customer=source).update(customer=target)
                SampleTest.objects.filter(customer=source).update(customer=target)
                WorkOrderLink.objects.filter(customer=source).update(customer=target)
                TaskReminder.objects.filter(customer=source).update(customer=target)
                Attachment.objects.filter(customer=source).update(customer=target)
                source.is_recycled = True
                source.recycled_at = timezone.now()
                source.recycle_reason = f"已合并到 {target.customer_no}"
                source.status = Customer.Status.INVALID
                source.is_active = True
                source.updated_by = request.user
                source.save(update_fields=["is_recycled", "recycled_at", "recycle_reason", "status", "is_active", "updated_by", "updated_at"])
                OperationLog.objects.create(
                    user=request.user,
                    customer=target,
                    action_type=OperationLog.ActionType.MERGE,
                    before_data={"source_customer": source.customer_no},
                    after_data={"target_customer": target.customer_no},
                    remark=form.cleaned_data.get("reason", ""),
                )
            messages.success(request, "客户已合并，历史联系记录、报价、合同、收款和任务已迁移到主客户。")
            return redirect("customer_detail", pk=target.pk)
    else:
        form = CustomerMergeForm(customer_queryset=customer_queryset_for(request.user), exclude_customer=source)
    return render(request, "crm/simple_form.html", {"form": form, "title": f"合并客户：{source}", "back_url": reverse("customer_detail", args=[source.pk])})


@login_required
def lead_list(request):
    qs = lead_queryset_for(request.user).select_related("owner", "converted_customer").prefetch_related("co_owners", "tags")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    tab = request.GET.get("tab", "").strip()
    if q:
        qs = qs.filter(Q(lead_no__icontains=q) | Q(customer_name__icontains=q) | Q(name__icontains=q) | Q(raw_nickname__icontains=q) | Q(phone__icontains=q) | Q(wechat__icontains=q) | Q(whatsapp__icontains=q) | Q(email__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if tab == "mine":
        qs = qs.filter(Q(owner=request.user) | Q(co_owners=request.user)).distinct()
    elif tab == "foreign":
        qs = qs.filter(trade_type=Customer.TradeType.FOREIGN)
    elif tab == "pending":
        qs = qs.filter(status__in=[Lead.Status.NEW, Lead.Status.PENDING_ASSIGN])
    owner_load = (
        lead_queryset_for(request.user).values("owner__username", "owner__first_name", "owner__last_name")
        .annotate(total=Count("id"), pending=Count("id", filter=Q(status__in=[Lead.Status.NEW, Lead.Status.PENDING_ASSIGN])))
        .order_by("-pending", "-total")
    )
    return render(request, "crm/lead_list.html", {
        "leads": qs[:300], "q": q, "status": status, "tab": tab,
        "status_choices": Lead.Status.choices,
        "new_count": lead_queryset_for(request.user).filter(status=Lead.Status.NEW).count(),
        "pending_count": lead_queryset_for(request.user).filter(status=Lead.Status.PENDING_ASSIGN).count(),
        "mine_count": lead_queryset_for(request.user).filter(Q(owner=request.user) | Q(co_owners=request.user)).distinct().count(),
        "foreign_count": lead_queryset_for(request.user).filter(trade_type=Customer.TradeType.FOREIGN).count(),
        "owner_load": owner_load,
        "can_assign_leads": can_assign(request.user),
    })


@login_required
def lead_create(request):
    if request.method == "POST":
        form = LeadForm(request.POST)
        if not can_assign(request.user):
            form.fields["owner"].disabled = True
        if form.is_valid():
            lead = form.save(commit=False)
            lead.created_by = request.user
            if not can_assign(request.user) and not lead.owner_id:
                lead.owner = request.user
                lead.assigned_at = timezone.now()
                lead.status = Lead.Status.ASSIGNED
            lead.save()
            form.save_m2m()
            OperationLog.objects.create(user=request.user, lead=lead, action_type=OperationLog.ActionType.CREATE, after_data={"lead_no": lead.lead_no}, remark="新增线索")
            messages.success(request, "线索已保存。")
            return redirect("lead_list")
    else:
        initial = {}
        if not can_assign(request.user):
            initial["owner"] = request.user
        form = LeadForm(initial=initial)
    if not can_assign(request.user):
        form.fields["owner"].disabled = True
    return render(request, "crm/lead_form.html", {"form": form, "title": "新增线索/询盘"})


@login_required
def lead_edit(request, pk):
    lead = get_object_or_404(lead_queryset_for(request.user), pk=pk)
    old_owner_id = lead.owner_id
    old_status = lead.status
    if request.method == "POST":
        form = LeadForm(request.POST, instance=lead)
        if not can_assign(request.user):
            form.fields["owner"].disabled = True
        if form.is_valid():
            lead = form.save()
            if old_owner_id != lead.owner_id or old_status != lead.status:
                OperationLog.objects.create(user=request.user, lead=lead, action_type=OperationLog.ActionType.ASSIGN_LEAD, before_data={"owner_id": old_owner_id, "status": old_status}, after_data={"owner_id": lead.owner_id, "status": lead.status}, remark="线索分配/状态变更")
            messages.success(request, "线索已更新。")
            return redirect("lead_list")
    else:
        form = LeadForm(instance=lead)
    if not can_assign(request.user):
        form.fields["owner"].disabled = True
    return render(request, "crm/lead_form.html", {"form": form, "title": "编辑线索", "lead": lead})


@login_required
@require_POST
def lead_convert(request, pk):
    lead = get_object_or_404(lead_queryset_for(request.user), pk=pk)
    customer = lead.converted_customer or lead.related_customer
    if not customer:
        customer = Customer.objects.create(
            nickname=lead.raw_nickname,
            official_name=lead.customer_name or lead.name,
            name=lead.customer_name or lead.name,
            contact_name=lead.contact_name,
            main_contact_name=lead.contact_name,
            phone=lead.phone,
            wechat=lead.wechat,
            whatsapp=lead.whatsapp,
            email=lead.email,
            instagram=lead.instagram,
            facebook=lead.facebook,
            country_region=lead.country_region or lead.region,
            region=lead.country_region or lead.region,
            language=lead.language,
            trade_type=lead.trade_type,
            source_channel=lead.source_channel,
            customer_type=lead.customer_type,
            product_interest=lead.product_demand,
            demand=lead.product_demand,
            equipment_model=lead.equipment_model,
            capacity_requirement=lead.capacity_requirement,
            can_type=lead.can_type,
            sample_can_info=lead.sample_can_info,
            is_carbonated=lead.is_carbonated,
            owner=lead.owner,
            created_by=request.user,
            customer_level=Customer.CustomerLevel.PENDING,
            follow_status=Customer.FollowStatus.NEW_INQUIRY,
        )
        customer.co_owners.set(lead.co_owners.all())
    lead.status = Lead.Status.CONVERTED
    lead.converted_customer = customer
    lead.related_customer = customer
    lead.save(update_fields=["status", "converted_customer", "related_customer", "updated_at"])
    OperationLog.objects.create(user=request.user, customer=customer, lead=lead, action_type=OperationLog.ActionType.CREATE, after_data={"customer_no": customer.customer_no, "lead_no": lead.lead_no}, remark="线索转客户")
    messages.success(request, "线索已转为客户。")
    return redirect("customer_detail", pk=customer.pk)


@login_required
@require_POST
def lead_mark_invalid(request, pk):
    lead = get_object_or_404(lead_queryset_for(request.user), pk=pk)
    old_status = lead.status
    lead.status = Lead.Status.INVALID
    lead.save(update_fields=["status", "updated_at"])
    OperationLog.objects.create(user=request.user, lead=lead, action_type=OperationLog.ActionType.MARK_INVALID, before_data={"status": old_status}, after_data={"status": lead.status}, remark="线索标记无效")
    messages.success(request, "线索已标记无效。")
    return redirect("lead_list")


@login_required
def opportunity_list(request):
    qs = opportunity_queryset_for(request.user)
    q = request.GET.get("q", "").strip()
    stage = request.GET.get("stage", "").strip()
    if q:
        qs = qs.filter(Q(opportunity_no__icontains=q) | Q(customer__name__icontains=q) | Q(latest_progress__icontains=q))
    if stage:
        qs = qs.filter(stage=stage)
    lanes = []
    for value, label in Opportunity.Stage.choices:
        lane_qs = qs.filter(stage=value)
        lanes.append({"stage": label, "total": lane_qs.count(), "items": lane_qs[:20]})
    return render(request, "crm/opportunity_list.html", {"opportunities": qs[:300], "lanes": lanes, "q": q, "stage": stage, "stage_choices": Opportunity.Stage.choices})


@login_required
def opportunity_create(request):
    if request.method == "POST":
        form = OpportunityForm(request.POST, customer_queryset=customer_queryset_for(request.user))
        if form.is_valid():
            opportunity = form.save()
            messages.success(request, "商机已保存。")
            return redirect("opportunity_list")
    else:
        initial = {"owner": request.user}
        customer_id = request.GET.get("customer")
        if customer_id:
            initial["customer"] = customer_id
        form = OpportunityForm(initial=initial, customer_queryset=customer_queryset_for(request.user))
    return render(request, "crm/opportunity_form.html", {"form": form, "title": "新增商机"})


@login_required
def opportunity_edit(request, pk):
    opportunity = get_object_or_404(opportunity_queryset_for(request.user), pk=pk)
    if request.method == "POST":
        form = OpportunityForm(request.POST, instance=opportunity, customer_queryset=customer_queryset_for(request.user))
        if form.is_valid():
            form.save()
            messages.success(request, "商机已更新。")
            return redirect("opportunity_list")
    else:
        form = OpportunityForm(instance=opportunity, customer_queryset=customer_queryset_for(request.user))
    return render(request, "crm/opportunity_form.html", {"form": form, "title": "编辑商机", "opportunity": opportunity})


@login_required
def quote_list(request):
    qs = quote_queryset_for(request.user)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    currency = request.GET.get("currency", "").strip()
    if q:
        qs = qs.filter(Q(quote_no__icontains=q) | Q(customer__customer_no__icontains=q) | Q(customer__name__icontains=q) | Q(customer__official_name__icontains=q) | Q(remark__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if currency:
        qs = qs.filter(currency=currency)
    return render(request, "crm/quote_list.html", {"quotes": qs[:300], "q": q, "status": status, "currency": currency, "status_choices": Quote.Status.choices, "currency_choices": Contract.Currency.choices})


@login_required
def quote_create(request):
    if request.method == "POST":
        form = QuoteForm(request.POST, request.FILES, customer_queryset=customer_queryset_for(request.user))
        if form.is_valid():
            quote = form.save(commit=False)
            if not quote.quoted_by_id:
                quote.quoted_by = request.user
            quote.save()
            form.save_m2m()
            messages.success(request, "报价已保存，可继续添加方案。")
            return redirect("quote_edit", pk=quote.pk)
    else:
        initial = {"quoted_by": request.user, "customer": request.GET.get("customer") or None}
        form = QuoteForm(initial=initial, customer_queryset=customer_queryset_for(request.user))
    return render(request, "crm/quote_form.html", {"form": form, "title": "新增报价"})


@login_required
def quote_edit(request, pk):
    quote = get_object_or_404(quote_queryset_for(request.user), pk=pk)
    plan_form = QuotePlanForm(initial={"quote": quote})
    if request.method == "POST":
        if request.POST.get("form_kind") == "plan":
            plan_form = QuotePlanForm(request.POST)
            if plan_form.is_valid():
                plan = plan_form.save(commit=False)
                plan.quote = quote
                plan.save()
                messages.success(request, "报价方案已保存。")
                return redirect("quote_edit", pk=quote.pk)
        else:
            form = QuoteForm(request.POST, request.FILES, instance=quote, customer_queryset=customer_queryset_for(request.user))
            if form.is_valid():
                form.save()
                messages.success(request, "报价已更新。")
                return redirect("quote_list")
    else:
        form = QuoteForm(instance=quote, customer_queryset=customer_queryset_for(request.user))
    return render(request, "crm/quote_form.html", {"form": form, "plan_form": plan_form, "quote": quote, "plans": quote.plans.prefetch_related("items"), "title": "编辑报价"})


@login_required
def payment_create(request):
    contracts = contract_queryset_for(request.user)
    customers = customer_queryset_for(request.user)
    if request.method == "POST":
        form = PaymentForm(request.POST, request.FILES, contract_queryset=contracts, customer_queryset=customers)
        if form.is_valid():
            payment = form.save(commit=False)
            if payment.contract_id and not payment.customer_id:
                payment.customer = payment.contract.customer
            payment.save()
            OperationLog.objects.create(user=request.user, customer=payment.customer, action_type=OperationLog.ActionType.PAYMENT, after_data={"payment_no": payment.payment_no, "amount": str(payment.actual_received_amount)}, remark="新增收款")
            messages.success(request, "到款记录已保存。")
            return redirect("contract_list")
    else:
        initial = {"contract": request.GET.get("contract") or None, "customer": request.GET.get("customer") or None, "payment_date": timezone.localdate()}
        form = PaymentForm(initial=initial, contract_queryset=contracts, customer_queryset=customers)
    return render(request, "crm/simple_form.html", {"form": form, "title": "新增到款记录", "back_url": reverse("contract_list")})


@login_required
def visit_list(request):
    qs = visit_queryset_for(request.user)
    status = request.GET.get("status", "").strip()
    if status:
        qs = qs.filter(status=status)
    return render(request, "crm/visit_list.html", {"visits": qs[:300], "status": status, "status_choices": VisitPlan.Status.choices})


@login_required
def visit_create(request):
    if request.method == "POST":
        form = VisitPlanForm(request.POST, customer_queryset=customer_queryset_for(request.user))
        if form.is_valid():
            form.save()
            messages.success(request, "客户来访计划已保存。")
            return redirect("visit_list")
    else:
        form = VisitPlanForm(initial={"customer": request.GET.get("customer") or None}, customer_queryset=customer_queryset_for(request.user))
    return render(request, "crm/simple_form.html", {"form": form, "title": "新增客户来访", "back_url": reverse("visit_list")})


@login_required
def reminder_list(request):
    qs = task_queryset_for(request.user)
    reminder_type = request.GET.get("type", "").strip()
    status = request.GET.get("status", "").strip()
    if reminder_type:
        qs = qs.filter(reminder_type=reminder_type)
    if status:
        qs = qs.filter(status=status)
    grouped = qs.values("reminder_type").annotate(total=Count("id")).order_by("reminder_type")
    legacy_reminders = Reminder.objects.filter(status__in=[Reminder.Status.PENDING, Reminder.Status.SENT]).select_related("customer", "assignee")[:100]
    if not can_view_all(request.user):
        legacy_reminders = legacy_reminders.filter(Q(assignee=request.user) | Q(customer__owner=request.user) | Q(customer__co_owners=request.user)).distinct()
    return render(request, "crm/reminder_list.html", {"tasks": qs[:300], "grouped": grouped, "legacy_reminders": legacy_reminders, "type": reminder_type, "status": status, "type_choices": TaskReminder.ReminderType.choices, "status_choices": TaskReminder.Status.choices})


@login_required
def task_create(request):
    if request.method == "POST":
        form = TaskReminderForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            if not task.created_by_id:
                task.created_by = request.user
            if not task.assigned_to_id:
                task.assigned_to = request.user
            task.save()
            messages.success(request, "任务已创建。")
            return redirect("reminder_list")
    else:
        initial = {"assigned_to": request.user, "due_at": timezone.now(), "customer": request.GET.get("customer") or None}
        form = TaskReminderForm(initial=initial)
    return render(request, "crm/simple_form.html", {"form": form, "title": "新建跟进任务", "back_url": reverse("reminder_list")})

@login_required
def report_analysis(request):
    customers = customer_queryset_for(request.user)
    leads = lead_queryset_for(request.user)
    quotes = quote_queryset_for(request.user)
    contracts = contract_queryset_for(request.user)
    payments = payment_queryset_for(request.user)
    month = request.GET.get("month", "").strip()
    owner = request.GET.get("owner", "").strip()
    if month:
        customers = customers.filter(created_at__date__startswith=month)
        leads = leads.filter(created_at__date__startswith=month)
        quotes = quotes.filter(quote_date__startswith=month)
        contracts = contracts.filter(signed_date__startswith=month)
        payments = payments.filter(payment_date__startswith=month)
    if owner and can_view_all(request.user):
        customers = customers.filter(owner_id=owner)
        leads = leads.filter(owner_id=owner)
        quotes = quotes.filter(quoted_by_id=owner)
        contracts = contracts.filter(Q(signed_by_id=owner) | Q(sales_user_id=owner))
    demand_rows = []
    for label in ["单头灌装", "单头封口", "1-1", "4-1", "6-1", "8-2", "12-2"]:
        demand_rows.append({"label": label, "total": customers.filter(Q(product_interest__icontains=label) | Q(demand__icontains=label)).count()})
    status_rows = customers.values("follow_status").annotate(total=Count("id")).order_by("-total")
    level_rows = customers.values("customer_level").annotate(total=Count("id")).order_by("-total")
    type_rows = customers.values("customer_type").annotate(total=Count("id")).order_by("-total")
    source_rows = []
    for row in customers.values("source_channel").annotate(total=Count("id")).order_by("-total")[:80]:
        source = row["source_channel"] or "未填写"
        source_customers = customers.filter(source_channel=row["source_channel"])
        source_rows.append({
            "source": source,
            "lead_count": leads.filter(source_channel=row["source_channel"]).count(),
            "effective_count": source_customers.exclude(customer_level__in=[Customer.CustomerLevel.INVALID, Customer.CustomerLevel.NO_INTENT]).count(),
            "intent_count": source_customers.filter(customer_level=Customer.CustomerLevel.INTENTION).count(),
            "quote_count": quotes.filter(customer__source_channel=row["source_channel"]).count(),
            "deal_count": source_customers.filter(deal_status=Customer.DealStatus.WON).count(),
            "contract_amount": contracts.filter(customer__source_channel=row["source_channel"]).aggregate(total=Sum("contract_amount"))["total"] or Decimal("0"),
        })
    salesperson_rows = []
    for user in User.objects.filter(is_active=True).order_by("username"):
        user_contracts = contracts.filter(Q(signed_by=user) | Q(sales_user=user))
        user_payments = payments.filter(Q(contract__signed_by=user) | Q(contract__sales_user=user))
        salesperson_rows.append({
            "user": user.get_full_name() or user.username,
            "new_leads": leads.filter(owner=user).count(),
            "first_contacts": leads.filter(owner=user, first_contact_at__isnull=False).count(),
            "new_intent": customers.filter(owner=user, customer_level=Customer.CustomerLevel.INTENTION).count(),
            "quote_count": quotes.filter(quoted_by=user).count(),
            "deal_count": customers.filter(owner=user, deal_status=Customer.DealStatus.WON).count(),
            "contract_amount": user_contracts.aggregate(total=Sum("contract_amount"))["total"] or Decimal("0"),
            "paid_amount": user_payments.aggregate(total=Sum("actual_received_amount"))["total"] or Decimal("0"),
            "unpaid_amount": sum((contract.unpaid_amount for contract in user_contracts[:300]), Decimal("0")),
        })
    owner_options = User.objects.filter(is_active=True).order_by("username")
    return render(request, "crm/report_analysis.html", {"month": month, "owner": owner, "owner_options": owner_options, "demand_rows": demand_rows, "status_rows": status_rows, "level_rows": level_rows, "type_rows": type_rows, "source_rows": source_rows, "salesperson_rows": salesperson_rows})

@csrf_exempt
@require_POST
def intake_contact_log_api(request):
    expected_token = getattr(settings, "CRM_INTAKE_API_TOKEN", "")
    provided_token = request.headers.get("X-CRM-INTAKE-TOKEN") or request.GET.get("token", "")
    if not expected_token:
        return JsonResponse({"ok": False, "error": "CRM_INTAKE_API_TOKEN 未配置，智能体写入接口未启用。"}, status=503)
    if provided_token != expected_token:
        return JsonResponse({"ok": False, "error": "令牌无效。"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "请求内容必须是 JSON。"}, status=400)

    summary = str(payload.get("summary") or payload.get("沟通记录") or payload.get("raw_text") or "").strip()
    if not summary:
        return JsonResponse({"ok": False, "error": "缺少跟进内容 summary。"}, status=400)

    customer, candidates = _find_customer_for_intake(payload)
    if not customer and payload.get("create_customer") is True:
        name = str(payload.get("customer_name") or payload.get("客户名称") or payload.get("name") or "").strip()
        region = merge_region_city(payload.get("region") or payload.get("地区"), payload.get("city") or payload.get("城市"))
        phone, phone_wechat = split_phone_and_wechat(payload.get("phone") or payload.get("客户电话") or payload.get("联系电话") or "", region, name)
        wechat = merge_wechat_values(payload.get("wechat") or payload.get("微信") or payload.get("微信号") or "", phone_wechat)
        if not any([name, phone, wechat]):
            return JsonResponse({"ok": False, "error": "新建客户至少需要客户名称、电话或微信之一。"}, status=400)
        customer = Customer.objects.create(
            name=name,
            phone=phone,
            wechat=wechat,
            email=str(payload.get("email") or payload.get("邮箱") or "").strip(),
            region=region,
            source_channel=str(payload.get("source_channel") or payload.get("线索来源") or "").strip(),
            owner_name=str(payload.get("follower_name") or payload.get("跟进人") or payload.get("客户经理") or "").strip(),
            customer_status_text="待确认",
            source_kind=Customer.RecordKind.CUSTOMER,
            created_by_name="CRM助手",
        )
        apply_auto_tags(customer)

    if not customer:
        if candidates:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "客户名称匹配到多条记录，请带 customer_no 重试。",
                    "candidates": [{"customer_no": item.customer_no, "name": str(item), "phone": item.phone, "wechat": item.wechat} for item in candidates],
                },
                status=409,
            )
        return JsonResponse({"ok": False, "error": "未找到客户，请带 customer_no、phone、wechat，或设置 create_customer=true。"}, status=404)

    contact_at = _parse_api_datetime(payload.get("contact_at") or payload.get("跟进时间")) or timezone.now()
    next_contact_at = _parse_api_datetime(payload.get("next_contact_at") or payload.get("下次联系时间"))
    log = ContactLog.objects.create(
        customer=customer,
        contact_at=contact_at,
        next_contact_at=next_contact_at,
        method=_contact_method(payload.get("method") or payload.get("跟进形式")),
        source=ContactLog.Source.XIAOQUAN,
        follower_name=str(payload.get("follower_name") or payload.get("跟进人") or "CRM助手").strip(),
        summary=summary,
        result=str(payload.get("result") or payload.get("跟进结果") or "").strip(),
        photo_note=str(payload.get("photo_note") or payload.get("图片说明") or "").strip(),
        minutes_link=str(payload.get("minutes_link") or payload.get("妙记链接") or "").strip(),
    )
    update_customer_after_contact(customer, log)
    return JsonResponse(
        {
            "ok": True,
            "customer": {"id": customer.pk, "customer_no": customer.customer_no, "name": str(customer)},
            "contact_log": {"id": log.pk, "contact_at": timezone.localtime(log.contact_at).strftime("%Y-%m-%d %H:%M")},
        }
    )
