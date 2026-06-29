import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import (
    AuditLog,
    ContactLog,
    Contract,
    Customer,
    FeishuSyncRecord,
    FeishuSyncSource,
    clean_customer_name_contacts,
    merge_phone_values,
    merge_region_city,
    merge_wechat_values,
    normalize_phone_number_for_region,
    resolve_or_create_user_by_feishu,
    split_region_from_customer_name,
    split_phone_and_wechat,
)
from .options import GRADE_LABEL_TO_CODE, canonical_customer_statuses, canonical_demands, canonical_source


CUSTOMER_FIELD_MAP = {
    "序号": "external_no",
    "用户名": "name",
    "分配时间": "original_assigned_at",
    "获客渠道": "source_channel",
    "图片": "attachment_note",
    "资料类型": "source_kind",
    "线索编号": "lead_no",
    "客户编号": "legacy_customer_no",
    "系统客户编号": "customer_no",
    "客户名称": "name",
    "客户名称/昵称": "name",
    "原始客户名称": "original_name",
    "联系人": "contact_name",
    "客户联系人": "contact_name",
    "客户方联系人": "contact_name",
    "客户对接人职位": "contact_position",
    "联系电话": "phone",
    "客户电话": "phone",
    "电话": "phone",
    "微信": "wechat",
    "微信号": "wechat",
    "邮箱": "email",
    "地区": "region",
    "城市": "city",
    "行业": "industry",
    "线索来源": "source_channel",
    "账号来源": "source_channel",
    "账户来源": "source_channel",
    "客户类型": "customer_type",
    "客户需求": "demand",
    "关联线索": "related_lead",
    "线索状态": "lead_status",
    "客户状态": "customer_status_text",
    "客户级别": "grade",
    "归属状态": "status",
    "是否成交": "is_deal",
    "客户经理": "owner_name",
    "客户负责人": "owner_name",
    "负责人": "owner_name",
    "分配给": "owner_name",
    "录入人": "created_by_name",
    "已查重": "duplicate_checked",
    "重复客户编号": "duplicate_customer_no",
    "历史创建时间": "historical_created_at",
    "录入时间": "historical_created_at",
    "创建时间": "historical_created_at",
    "原始分配时间": "original_assigned_at",
    "最后联系时间": "last_contact_at",
    "最近跟进时间": "last_contact_at",
    "下次联系时间": "next_contact_at",
    "附件": "attachment_note",
    "备注": "notes",
}

CONTACT_LOG_FIELD_MAP = {
    "客户名称": "customer_name",
    "跟进人": "follower_name",
    "跟进人员": "follower_name",
    "跟进形式": "method",
    "跟进时间": "contact_at",
    "跟进内容": "summary",
    "跟进内容照片": "photo_note",
    "跟进妙记链接": "minutes_link",
}

CONTRACT_FIELD_MAP = {
    "合同编号": "contract_no",
    "客户名称": "customer_name",
    "签约人员": "signed_by_name",
    "签约日期": "signed_date",
    "合同金额": "amount",
    "合同附件": "attachment_note",
}

GRADE_LABELS = {label: value for value, label in Customer.Grade.choices}
STATUS_LABELS = {label: value for value, label in Customer.Status.choices}
KIND_LABELS = {label: value for value, label in Customer.RecordKind.choices}
METHOD_LABELS = {label: value for value, label in ContactLog.Method.choices}
INQUIRY_SYNC_LOGIC_VERSION = "inquiry_desc_clean_assign_time_20260624"
CUSTOMER_CREATE_CONTENT_FIELDS = {
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
    "lead_status",
    "related_lead",
    "customer_status_text",
    "notes",
    "attachment_note",
}


class FeishuAPIError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self, app_id=None, app_secret=None):
        self.app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")
        self._tenant_access_token = None

    def request_json(self, method, url, payload=None, headers=None):
        body = None
        headers = headers or {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise FeishuAPIError(f"Feishu API HTTP {exc.code}: {text[:500]}") from exc
        except urllib.error.URLError as exc:
            raise FeishuAPIError(f"Feishu API network error: {exc}") from exc
        data = json.loads(text)
        if data.get("code", 0) != 0:
            raise FeishuAPIError(f"Feishu API error {data.get('code')}: {data.get('msg')}")
        return data

    def tenant_access_token(self):
        if self._tenant_access_token:
            return self._tenant_access_token
        if not self.app_id or not self.app_secret:
            raise FeishuAPIError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
        data = self.request_json(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
        )
        self._tenant_access_token = data["tenant_access_token"]
        return self._tenant_access_token

    def list_records(self, app_token, table_id):
        token = self.tenant_access_token()
        page_token = ""
        records = []
        while True:
            query = {"page_size": "500"}
            if page_token:
                query["page_token"] = page_token
            url = (
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records?"
                + urllib.parse.urlencode(query)
            )
            data = self.request_json("GET", url, headers={"Authorization": f"Bearer {token}"})
            page = data.get("data", {})
            records.extend(page.get("items", []))
            if not page.get("has_more"):
                break
            page_token = page.get("page_token") or ""
            if not page_token:
                break
        return records

    def list_sheet_records(self, spreadsheet_token, sheet_id, range_ref="A1:J5000"):
        token = self.tenant_access_token()
        range_name = range_ref
        if "!" not in range_name:
            range_name = f"{sheet_id}!{range_ref}"
        encoded_range = urllib.parse.quote(range_name, safe="")
        url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{encoded_range}"
        data = self.request_json("GET", url, headers={"Authorization": f"Bearer {token}"})
        value_range = data.get("data", {}).get("valueRange", {})
        values = value_range.get("values") or []
        if not values:
            return []
        headers = [str(normalize_cell(cell) or "").strip() for cell in values[0]]
        records = []
        for index, row in enumerate(values[1:], start=2):
            fields = {}
            has_value = False
            for col_index, header in enumerate(headers):
                if not header:
                    continue
                value = row[col_index] if col_index < len(row) else ""
                normalized = normalize_cell(value)
                if normalized not in ("", None):
                    has_value = True
                fields[header] = normalized
            if not has_value:
                continue
            sequence = str(fields.get("序号") or "").strip()
            stable_id = sequence or f"row-{index}"
            records.append({"record_id": f"{sheet_id}:{stable_id}", "row_index": index, "fields": fields})
        return records


def load_sources_from_env():
    raw = os.getenv("FEISHU_SYNC_SOURCES_JSON", "").strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuAPIError(f"FEISHU_SYNC_SOURCES_JSON 不是合法 JSON: {exc}") from exc


DISABLED_LINE_MANAGEMENT_SOURCE_NAMES = {
    "table_线索管理_tbloyS8y6swVRgOx",
    "线索管理",
}
DISABLED_LINE_MANAGEMENT_TABLE_IDS = {"tbloyS8y6swVRgOx"}


def is_disabled_line_management_source(name="", table_id=""):
    source_name = str(name or "").strip()
    source_table_id = str(table_id or "").strip()
    return (
        source_name in DISABLED_LINE_MANAGEMENT_SOURCE_NAMES
        or "线索管理" in source_name
        or source_table_id in DISABLED_LINE_MANAGEMENT_TABLE_IDS
    )


def upsert_env_sources():
    count = 0
    for item in load_sources_from_env():
        source_type = item.get("source_type", FeishuSyncSource.SourceType.BASE)
        table_id = item.get("table_id") or item.get("sheet_id", "")
        sheet_id = item.get("sheet_id", "")
        source_name = item.get("name") or table_id or sheet_id
        enabled = item.get("enabled", True)
        if is_disabled_line_management_source(source_name, table_id):
            enabled = False
        source, _ = FeishuSyncSource.objects.update_or_create(
            app_token=item["app_token"],
            table_id=table_id,
            sheet_id=sheet_id,
            source_kind=item.get("source_kind", item.get("kind", FeishuSyncSource.SourceKind.CUSTOMER)),
            defaults={
                "name": source_name,
                "source_type": source_type,
                "sheet_range": item.get("sheet_range", item.get("range", "")),
                "default_record_kind": item.get("default_record_kind", Customer.RecordKind.LEAD),
                "field_mapping": item.get("field_mapping", item.get("mapping", {})),
                "enabled": enabled,
            },
        )
        count += 1
    return count


def normalize_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [normalize_cell(item) for item in value]
        return " ".join(str(part) for part in parts if part not in ("", None)).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "en_name", "email", "phone", "value", "url", "link"):
            if key in value and value[key]:
                return normalize_cell(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def extract_feishu_user(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        name = normalize_cell(value.get("name") or value.get("text") or value.get("en_name") or value.get("email"))
        user_id = normalize_cell(value.get("open_id") or value.get("id") or value.get("user_id"))
        email = normalize_cell(value.get("email"))
        return name, user_id, email
    name = normalize_cell(value)
    return name, "", ""


def extract_owner_identity(raw_fields):
    for field_name in ("客户经理", "客户负责人", "负责人", "分配给"):
        if field_name in raw_fields:
            name, user_id, email = extract_feishu_user(raw_fields.get(field_name))
            if name or user_id or email:
                return name, user_id, email
    return "", "", ""


def is_inquiry_source(source):
    return source.source_type == FeishuSyncSource.SourceType.SHEET and "询盘" in str(source.name or "")


def extract_customer_owner_identity(raw_fields, source):
    field_names = ["客户经理", "客户负责人", "负责人", "分配给"]
    if is_inquiry_source(source):
        field_names.insert(0, "联系人")
    for field_name in field_names:
        if field_name in raw_fields:
            name, user_id, email = extract_feishu_user(raw_fields.get(field_name))
            if name or user_id or email:
                return name, user_id, email
    return "", "", ""


def parse_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"是", "true", "yes", "1", "已成交", "成交", "已查重"}


def _has_explicit_time(value):
    text = str(value or "").strip()
    return bool(
        ":" in text
        or "时" in text
        or re.search(r"\d{1,2}\s*[点:]\s*\d{0,2}", text)
        or re.search(r"\b(?:AM|PM)\b", text, flags=re.IGNORECASE)
    )


def _date_with_default_morning(date_value):
    return timezone.make_aware(datetime.combine(date_value, datetime_time(hour=8)), timezone.get_current_timezone())


def _date_with_current_hour_minute(value, now=None):
    if not value:
        return None
    dt = value if isinstance(value, datetime) else parse_dt(value)
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    now_local = timezone.localtime(now or timezone.now())
    date_value = timezone.localtime(dt).date()
    return timezone.make_aware(
        datetime.combine(date_value, datetime_time(hour=now_local.hour, minute=now_local.minute)),
        timezone.get_current_timezone(),
    )


def parse_dt(value):
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        if 20000 <= value <= 80000:
            whole_days = int(value)
            fraction = float(value) - whole_days
            date_value = datetime(1899, 12, 30) + timedelta(days=whole_days)
            if fraction:
                date_value = date_value + timedelta(days=fraction)
                return timezone.make_aware(date_value, timezone.get_current_timezone())
            return _date_with_default_morning(date_value.date())
        timestamp = value / 1000 if value > 100000000000 else value
        return datetime.fromtimestamp(timestamp, tz=timezone.get_current_timezone())
    parsed = parse_datetime(str(value))
    if parsed:
        if not _has_explicit_time(value):
            parsed = datetime.combine(parsed.date(), datetime_time(hour=8))
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
    date_value = parse_date(str(value))
    if date_value:
        return _date_with_default_morning(date_value)
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            parsed_local = datetime.strptime(str(value).strip(), fmt)
            if not _has_explicit_time(value):
                parsed_local = datetime.combine(parsed_local.date(), datetime_time(hour=8))
            return timezone.make_aware(parsed_local, timezone.get_current_timezone())
        except ValueError:
            continue
    return None


def parse_date_value(value):
    dt = parse_dt(value)
    if dt:
        return dt.date()
    return parse_date(str(value)) if value not in ("", None) else None


def parse_amount(value):
    if value in ("", None):
        return Decimal("0")
    text = str(value).replace(",", "").replace("￥", "").strip()
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def checksum_fields(fields):
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_checksum(source, raw_fields):
    if is_inquiry_source(source):
        return checksum_fields({"fields": raw_fields, "_sync_logic_version": INQUIRY_SYNC_LOGIC_VERSION})
    return checksum_fields(raw_fields)


def ordered_records_for_source(source, records):
    if is_inquiry_source(source):
        return sorted(records, key=inquiry_record_sort_key, reverse=True)
    return records


def inquiry_sequence_value(value):
    text = str(value or "").strip()
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    text = re.sub(r"^row-", "", text, flags=re.IGNORECASE)
    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


def inquiry_record_sequence(record):
    fields = record.get("fields") or {}
    sequence = inquiry_sequence_value(fields.get("序号"))
    if sequence is not None:
        return sequence
    sequence = inquiry_sequence_value(record.get("record_id") or record.get("id"))
    if sequence is not None:
        return sequence
    return inquiry_sequence_value(record.get("row_index")) or 0


def inquiry_record_sort_key(record):
    sequence = inquiry_record_sequence(record)
    return sequence, int(record.get("row_index") or 0)


def customer_current_inquiry_sequence(customer, source):
    if not customer.pk:
        return None
    if customer.feishu_app_token != source.app_token or customer.feishu_table_id != source.table_id:
        return None
    return inquiry_sequence_value(customer.feishu_record_id)


def build_customer_no_allocator():
    next_no = Customer.next_customer_no()
    match = re.search(r"(\d+)$", next_no)
    if not match:
        return None
    return {
        "prefix": next_no[: match.start(1)],
        "number": int(match.group(1)),
        "width": len(match.group(1)),
    }


def allocate_customer_no(state):
    if not state:
        return ""
    while True:
        candidate = f"{state['prefix']}{state['number']:0{state['width']}d}"
        state["number"] += 1
        if not Customer.objects.filter(customer_no=candidate).exists():
            return candidate


def mapped_fields(raw_fields, source, default_map):
    mapping = dict(default_map)
    if is_inquiry_source(source):
        mapping.update(
            {
                "用户名": "name",
                "联系人": "owner_name",
                "获客渠道": "source_channel",
                "创建时间": "historical_created_at",
                "分配时间": "original_assigned_at",
            }
        )
    mapping.update(source.field_mapping or {})
    result = {}
    for field_name, value in raw_fields.items():
        target = mapping.get(field_name)
        if not target:
            continue
        result[target] = normalize_cell(value)
    return result


def phone_lookup_tokens(value):
    tokens = set()
    text = str(value or "")
    if not text.strip():
        return tokens
    for part in re.split(r"\s*(?:/|,|，|、|;|；)\s*", text):
        part = part.strip()
        if not part:
            continue
        normalized = normalize_phone_number_for_region(part)
        for item in (part, normalized):
            digits = re.sub(r"\D", "", str(item or ""))
            if len(digits) >= 6:
                tokens.add(digits)
                if len(digits) >= 11:
                    tokens.add(digits[-11:])
    return tokens


def build_customer_phone_index():
    index = {}
    for customer_id, phone in Customer.objects.filter(is_active=True).exclude(phone="").values_list("id", "phone"):
        for token in phone_lookup_tokens(phone):
            index.setdefault(token, customer_id)
    return index


def set_if_value(obj, field, value, overwrite=True):
    if value in ("", None):
        return False
    if field == "historical_created_at":
        return False
    value = fit_model_field_value(obj, field, value)
    current = getattr(obj, field)
    if current not in ("", None) and not overwrite:
        return False
    setattr(obj, field, value)
    return True


def fit_model_field_value(obj, field, value):
    if not isinstance(value, str):
        return value
    try:
        model_field = obj._meta.get_field(field)
    except Exception:
        return value
    max_length = getattr(model_field, "max_length", None)
    if max_length and len(value) > max_length:
        return value[:max_length]
    return value


def normalize_customer_values(values, source):
    result = {}
    for key, value in values.items():
        if key in {"historical_created_at", "original_assigned_at", "last_contact_at", "next_contact_at"}:
            result[key] = parse_dt(value)
        elif key == "phone":
            phone, wechat = split_phone_and_wechat(value)
            result[key] = phone
            if wechat and not result.get("wechat"):
                result["wechat"] = wechat
        elif key in {"duplicate_checked", "is_deal"}:
            result[key] = parse_bool(value)
        elif key == "grade":
            result[key] = GRADE_LABEL_TO_CODE.get(str(value), GRADE_LABELS.get(str(value), value if value in dict(Customer.Grade.choices) else Customer.Grade.POTENTIAL))
        elif key == "status":
            result[key] = STATUS_LABELS.get(str(value), value if value in dict(Customer.Status.choices) else Customer.Status.PRIVATE)
        elif key == "source_kind":
            result[key] = KIND_LABELS.get(str(value), value if value in dict(Customer.RecordKind.choices) else source.default_record_kind)
        elif key == "source_channel":
            result[key] = canonical_source(value)
        elif key == "customer_status_text":
            result[key] = canonical_customer_statuses(value)
        elif key == "demand":
            result[key] = canonical_demands(value)
        else:
            result[key] = value
    result.setdefault("source_kind", source.default_record_kind)
    if result.get("region") or result.get("city"):
        result["region"] = merge_region_city(result.get("region"), result.get("city"))
        result["city"] = ""
    if result.get("name"):
        cleaned_name, name_phone, name_wechat = clean_customer_name_contacts(result.get("name"), result.get("region"))
        cleaned_name, name_region = split_region_from_customer_name(cleaned_name)
        if name_region:
            result["region"] = merge_region_city(result.get("region"), name_region)
        result["name"] = cleaned_name
        if name_phone:
            result["phone"] = merge_phone_values(result.get("phone"), name_phone, hints=(result.get("region"), result.get("name")))
        if name_wechat:
            result["wechat"] = merge_wechat_values(result.get("wechat"), name_wechat)
    if result.get("phone"):
        result["phone"] = normalize_phone_number_for_region(result["phone"], result.get("region"), result.get("name"))
    if is_inquiry_source(source):
        assigned_at = _date_with_current_hour_minute(result.get("original_assigned_at"))
        if assigned_at:
            result["original_assigned_at"] = assigned_at
            result["historical_created_at"] = assigned_at
        elif result.get("historical_created_at"):
            result["historical_created_at"] = _date_with_current_hour_minute(result["historical_created_at"])
    return result


def customer_values_have_create_content(values):
    for field in CUSTOMER_CREATE_CONTENT_FIELDS:
        value = values.get(field)
        if value not in ("", None, False):
            return True
    return bool(values.get("is_deal"))


def find_customer_match(values, source, record_id, phone_index=None):
    existing = Customer.objects.filter(feishu_app_token=source.app_token, feishu_table_id=source.table_id, feishu_record_id=record_id).first()
    if existing:
        return existing, "feishu_record"

    phone_tokens = phone_lookup_tokens(values.get("phone"))
    if phone_tokens:
        if phone_index is not None:
            for token in phone_tokens:
                customer_id = phone_index.get(token)
                if customer_id:
                    found = Customer.objects.filter(pk=customer_id, is_active=True).first()
                    if found:
                        return found, "phone"
        else:
            for customer in Customer.objects.filter(is_active=True).exclude(phone="").only("id", "phone"):
                if phone_tokens & phone_lookup_tokens(customer.phone):
                    return customer, "phone"

    for field in ("wechat", "email", "customer_no", "legacy_customer_no", "lead_no"):
        value = values.get(field)
        if value:
            found = Customer.objects.filter(is_active=True, **{field: value}).first()
            if found:
                return found, field
    return Customer(), "new"


def find_customer(values, source, record_id):
    return find_customer_match(values, source, record_id)[0]


@transaction.atomic
def sync_customer_record(source, record, phone_index=None, customer_no_allocator=None):
    record_id = record.get("record_id") or record.get("id")
    raw_fields = record.get("fields", {})
    values = normalize_customer_values(mapped_fields(raw_fields, source, CUSTOMER_FIELD_MAP), source)
    owner_name, owner_feishu_id, owner_email = extract_customer_owner_identity(raw_fields, source)
    if owner_name:
        values["owner_name"] = owner_name
    customer, match_type = find_customer_match(values, source, record_id, phone_index=phone_index)
    creating = not customer.pk
    if creating and not customer_values_have_create_content(values):
        return FeishuSyncRecord.objects.update_or_create(
            source=source,
            record_id=record_id,
            defaults={"customer": None, "checksum": record_checksum(source, raw_fields), "raw_fields": raw_fields},
        )[0]
    if creating and customer_no_allocator and not customer.customer_no:
        customer.customer_no = allocate_customer_no(customer_no_allocator)
    preserve_newer_inquiry = False
    if is_inquiry_source(source) and customer.pk:
        existing_sequence = customer_current_inquiry_sequence(customer, source)
        current_sequence = inquiry_record_sequence(record)
        preserve_newer_inquiry = existing_sequence is not None and existing_sequence > current_sequence
    if customer.pk and match_type == "phone":
        values["duplicate_checked"] = True
        values["duplicate_customer_no"] = customer.customer_no or customer.legacy_customer_no or customer.lead_no
    for field, value in values.items():
        if hasattr(customer, field):
            set_if_value(customer, field, value, overwrite=not preserve_newer_inquiry)
    if not preserve_newer_inquiry:
        customer.feishu_source_name = fit_model_field_value(customer, "feishu_source_name", source.name)
        customer.feishu_app_token = fit_model_field_value(customer, "feishu_app_token", source.app_token)
        customer.feishu_table_id = fit_model_field_value(customer, "feishu_table_id", source.table_id)
        customer.feishu_record_id = fit_model_field_value(customer, "feishu_record_id", record_id)
    customer.save()
    historical_created_at = values.get("historical_created_at")
    if not preserve_newer_inquiry and historical_created_at not in ("", None) and customer.historical_created_at != historical_created_at:
        Customer.objects.filter(pk=customer.pk).update(historical_created_at=historical_created_at)
        customer.historical_created_at = historical_created_at
    owner = resolve_or_create_user_by_feishu(values.get("owner_name") or customer.owner_name, owner_feishu_id, owner_email)
    if owner and not preserve_newer_inquiry and customer.owner_id != owner.id:
        customer.owner = owner
        update_fields = ["owner", "updated_at"]
        if customer.status == Customer.Status.PUBLIC:
            customer.status = Customer.Status.PRIVATE
            customer.release_warned_at = None
            update_fields.extend(["status", "release_warned_at"])
        customer.save(update_fields=update_fields)
    sync_record, _ = FeishuSyncRecord.objects.update_or_create(
        source=source,
        record_id=record_id,
        defaults={"customer": customer, "checksum": record_checksum(source, raw_fields), "raw_fields": raw_fields},
    )
    if phone_index is not None and customer.phone:
        for token in phone_lookup_tokens(customer.phone):
            phone_index[token] = customer.pk
    AuditLog.objects.create(
        actor=None,
        action="飞书同步新增客户" if creating else "飞书同步更新客户",
        target_type="客户",
        target_id=str(customer.pk),
        detail=f"{source.name}:{record_id}",
    )
    return sync_record


def customer_by_name_or_create(name, source):
    name = str(name or "").strip()
    if not name:
        return None
    customer = Customer.objects.filter(name=name).first()
    if customer:
        return customer
    customer = Customer.objects.create(name=name, source_kind=Customer.RecordKind.CUSTOMER, feishu_source_name=source.name)
    return customer


@transaction.atomic
def sync_contact_log_record(source, record):
    record_id = record.get("record_id") or record.get("id")
    raw_fields = record.get("fields", {})
    values = mapped_fields(raw_fields, source, CONTACT_LOG_FIELD_MAP)
    existing = FeishuSyncRecord.objects.filter(source=source, record_id=record_id).select_related("contact_log").first()
    log = existing.contact_log if existing and existing.contact_log else None
    if not log:
        customer = customer_by_name_or_create(values.get("customer_name"), source)
        if not customer:
            return None
        log = ContactLog(customer=customer)
    log.follower_name = values.get("follower_name", log.follower_name)
    log.method = METHOD_LABELS.get(str(values.get("method")), values.get("method") if values.get("method") in dict(ContactLog.Method.choices) else log.method)
    log.source = ContactLog.Source.FEISHU
    log.contact_at = parse_dt(values.get("contact_at")) or log.contact_at
    log.summary = values.get("summary") or log.summary or "飞书同步跟进记录"
    log.photo_note = values.get("photo_note", log.photo_note)
    log.minutes_link = values.get("minutes_link", log.minutes_link)
    log.feishu_source_name = source.name
    log.feishu_record_id = record_id
    log.save()
    update_customer_after_contact(log.customer, log)
    return FeishuSyncRecord.objects.update_or_create(
        source=source,
        record_id=record_id,
        defaults={"contact_log": log, "customer": log.customer, "checksum": record_checksum(source, raw_fields), "raw_fields": raw_fields},
    )[0]


@transaction.atomic
def sync_contract_record(source, record):
    record_id = record.get("record_id") or record.get("id")
    raw_fields = record.get("fields", {})
    values = mapped_fields(raw_fields, source, CONTRACT_FIELD_MAP)
    existing = FeishuSyncRecord.objects.filter(source=source, record_id=record_id).select_related("contract").first()
    contract = existing.contract if existing and existing.contract else None
    if not contract:
        contract = Contract()
    customer = customer_by_name_or_create(values.get("customer_name"), source)
    contract.customer = customer or contract.customer
    contract.customer_name = values.get("customer_name", contract.customer_name)
    contract.contract_no = values.get("contract_no") or contract.contract_no
    contract.signed_by_name = values.get("signed_by_name", contract.signed_by_name)
    contract.signed_date = parse_date_value(values.get("signed_date")) or contract.signed_date
    contract.amount = parse_amount(values.get("amount"))
    contract.attachment_note = values.get("attachment_note", contract.attachment_note)
    contract.feishu_source_name = source.name
    contract.feishu_app_token = source.app_token
    contract.feishu_table_id = source.table_id
    contract.feishu_record_id = record_id
    contract.save()
    return FeishuSyncRecord.objects.update_or_create(
        source=source,
        record_id=record_id,
        defaults={"contract": contract, "customer": contract.customer, "checksum": record_checksum(source, raw_fields), "raw_fields": raw_fields},
    )[0]


def sync_source(client, source, progress=None):
    if progress:
        progress(f"{source.name}: 开始读取飞书数据")
    if source.source_type == FeishuSyncSource.SourceType.SHEET:
        records = client.list_sheet_records(source.app_token, source.sheet_id or source.table_id, source.sheet_range or "A1:J5000")
    else:
        records = client.list_records(source.app_token, source.table_id)
    if progress:
        progress(f"{source.name}: 飞书读取完成 {len(records)} 条")
    records = ordered_records_for_source(source, records)
    phone_index = build_customer_phone_index() if source.source_kind == FeishuSyncSource.SourceKind.CUSTOMER else None
    customer_no_allocator = build_customer_no_allocator() if source.source_kind == FeishuSyncSource.SourceKind.CUSTOMER else None
    created_or_updated = 0
    skipped = 0
    errors = []
    for index, record in enumerate(records, start=1):
        if progress and (index == 1 or index % 100 == 0 or index == len(records)):
            progress(f"{source.name}: 正在处理 {index}/{len(records)}")
        record_id = record.get("record_id") or record.get("id")
        raw_fields = record.get("fields", {})
        checksum = record_checksum(source, raw_fields)
        existing = FeishuSyncRecord.objects.filter(source=source, record_id=record_id, checksum=checksum).first()
        if existing:
            skipped += 1
            continue
        try:
            if source.source_kind == FeishuSyncSource.SourceKind.CONTACT_LOG:
                result = sync_contact_log_record(source, record)
            elif source.source_kind == FeishuSyncSource.SourceKind.CONTRACT:
                result = sync_contract_record(source, record)
            else:
                result = sync_customer_record(source, record, phone_index=phone_index, customer_no_allocator=customer_no_allocator)
        except Exception as exc:
            errors.append({"record_id": record_id, "error": str(exc)[:240]})
            continue
        if result:
            created_or_updated += 1
    source.last_sync_at = timezone.now()
    source.last_error = ""
    source.last_result = f"读取 {len(records)} 条，更新 {created_or_updated} 条，跳过未变化 {skipped} 条，失败 {len(errors)} 条"
    if errors:
        source.last_error = json.dumps(errors[:20], ensure_ascii=False)
    source.save(update_fields=["last_sync_at", "last_error", "last_result", "updated_at"])
    return {"source": source.name, "total": len(records), "updated": created_or_updated, "skipped": skipped, "errors": errors[:20]}


def sync_all_sources():
    upsert_env_sources()
    client = FeishuClient()
    results = []
    for source in FeishuSyncSource.objects.filter(enabled=True):
        if is_disabled_line_management_source(source.name, source.table_id):
            source.enabled = False
            source.last_result = "已停用：线索管理表来源已从客户系统清理，不再同步"
            source.save(update_fields=["enabled", "last_result", "updated_at"])
            results.append({"source": source.name, "skipped": "line_management_disabled"})
            continue
        try:
            results.append(sync_source(client, source))
        except Exception as exc:
            source.last_error = str(exc)
            source.last_sync_at = timezone.now()
            source.save(update_fields=["last_error", "last_sync_at", "updated_at"])
            results.append({"source": source.name, "error": str(exc)})
    return results


def run_sync_loop(interval_seconds=None):
    interval = interval_seconds or int(os.getenv("FEISHU_SYNC_INTERVAL_SECONDS", "300"))
    while True:
        sync_all_sources()
        time.sleep(interval)
