from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Count

from crm.feishu_sync import build_customer_phone_index, phone_lookup_tokens
from crm.management.commands.fill_eoffice_account_source import (
    Command as EofficeSourceCommand,
    CUSTOMER_NO_HEADERS,
    SOURCE_HEADERS as EOFFICE_SOURCE_HEADERS,
)
from crm.management.commands.import_legacy_sales_customer_sheets import (
    SKIP_HE_SHEETS,
    SKIP_SONG_SHEETS,
    clean_text,
    first_value,
    header_positions,
    parse_assignment_info,
    read_xlsx,
    row_has_value,
)
from crm.models import Customer, Lead, merge_phone_values, split_phone_and_wechat
from crm.options import canonical_source


FEISHU_SOURCE_HEADERS = (
    "获客渠道",
    "线索来源",
    "账号来源",
    "账户来源",
    "客户来源",
    "客户来源/渠道",
)
RECOVERABLE_CURRENT_VALUES = {
    "",
    "短视频",
    "短视频其他",
    "抖音其他",
    "视频号其他",
    "国外社媒",
    "国外社媒其他",
    "其他",
}
BROAD_REPLACEMENTS = {
    "短视频": "短视频其他",
    "国外社媒": "国外社媒其他",
}


class Command(BaseCommand):
    help = "从飞书原始记录和历史导入表恢复更细的线索来源。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="只生成报告，不写入数据库。")
        parser.add_argument("--eoffice", default="/app/imports/e-office_客户信息_20260623170146.xlsx")
        parser.add_argument("--song", default="/app/imports/legacy_song_customer_info.xlsx")
        parser.add_argument("--he", default="/app/imports/legacy_he_xiaofang_inquiry.xlsx")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        stats = Counter()
        raw_sources = Counter()
        applied_sources = Counter()
        blocked = Counter()
        missing_paths = []
        self.dry_run = dry_run
        self.phone_index = build_customer_phone_index()

        self._restore_from_feishu(stats, raw_sources, applied_sources, blocked)
        self._restore_from_eoffice(Path(options["eoffice"]), stats, raw_sources, applied_sources, blocked, missing_paths)
        self._restore_from_legacy(Path(options["song"]), Path(options["he"]), stats, raw_sources, applied_sources, blocked, missing_paths)
        self._replace_broad_leftovers(stats)

        action = "预览" if dry_run else "完成"
        self.stdout.write(f"细分线索来源恢复{action}。")
        self.stdout.write(
            "检查记录：飞书 %s 条，e-office %s 条，历史销售表 %s 条。"
            % (stats["feishu_seen"], stats["eoffice_seen"], stats["legacy_seen"])
        )
        self.stdout.write(
            "有效来源：飞书 %s 条，e-office %s 条，历史销售表 %s 条；更新客户 %s 条，跳过已有更具体来源 %s 条，兜底替换大类 %s 条。"
            % (
                stats["feishu_with_source"],
                stats["eoffice_with_source"],
                stats["legacy_with_source"],
                stats["customers_updated"],
                sum(blocked.values()),
                stats["broad_replaced"],
            )
        )
        if missing_paths:
            self.stdout.write("未找到导入文件：" + "；".join(str(path) for path in missing_paths))
        self.stdout.write("恢复来源分布：" + self._format_counter(applied_sources))
        self.stdout.write("原始来源 Top：" + self._format_counter(raw_sources))
        if blocked:
            self.stdout.write("未覆盖的已有来源：" + self._format_counter(blocked))
        self.stdout.write("当前线索来源分布：" + self._format_current_distribution())

    def _restore_from_feishu(self, stats, raw_sources, applied_sources, blocked):
        from crm.models import FeishuSyncRecord

        records = (
            FeishuSyncRecord.objects.select_related("customer")
            .filter(customer__isnull=False)
            .only("raw_fields", "customer__id", "customer__source_channel")
        )
        for record in records.iterator(chunk_size=500):
            stats["feishu_seen"] += 1
            raw_value = self._first_raw_value(record.raw_fields or {}, FEISHU_SOURCE_HEADERS)
            source = self._canonical_source(raw_value, raw_sources)
            if not source:
                continue
            stats["feishu_with_source"] += 1
            self._apply_source(record.customer, source, stats, applied_sources, blocked)

    def _restore_from_eoffice(self, path, stats, raw_sources, applied_sources, blocked, missing_paths):
        if not path.exists():
            missing_paths.append(path)
            return
        command = EofficeSourceCommand()
        for row in command._load_rows(path):
            stats["eoffice_seen"] += 1
            raw_value = command._first_text(row, EOFFICE_SOURCE_HEADERS)
            source = self._canonical_source(raw_value, raw_sources)
            if not source:
                continue
            stats["eoffice_with_source"] += 1
            customer = self._find_customer_by_number(command._first_text(row, CUSTOMER_NO_HEADERS))
            if customer is None:
                stats["eoffice_missing_customer"] += 1
                continue
            self._apply_source(customer, source, stats, applied_sources, blocked)

    def _restore_from_legacy(self, song_path, he_path, stats, raw_sources, applied_sources, blocked, missing_paths):
        self._restore_legacy_workbook(song_path, "销售B", "song", SKIP_SONG_SHEETS, stats, raw_sources, applied_sources, blocked, missing_paths)
        self._restore_legacy_workbook(he_path, "销售A", "he", SKIP_HE_SHEETS, stats, raw_sources, applied_sources, blocked, missing_paths)

    def _restore_legacy_workbook(self, path, owner_name, workbook_kind, skip_sheets, stats, raw_sources, applied_sources, blocked, missing_paths):
        if not path.exists():
            missing_paths.append(path)
            return
        for sheet_name, rows in read_xlsx(path).items():
            if sheet_name in skip_sheets or (workbook_kind == "he" and "CBCE" in sheet_name.upper()):
                continue
            if not rows or len(rows) < 3:
                continue
            positions = header_positions(rows[0])
            for row in rows[2:]:
                if not row_has_value(row):
                    continue
                stats["legacy_seen"] += 1
                raw_source, assignment_info = self._legacy_raw_source(row, positions, owner_name)
                source = self._canonical_source(raw_source, raw_sources)
                if not source:
                    continue
                stats["legacy_with_source"] += 1
                customer = self._find_legacy_customer(row, positions, owner_name, assignment_info)
                if customer is None:
                    stats["legacy_missing_customer"] += 1
                    continue
                self._apply_source(customer, source, stats, applied_sources, blocked)

    def _legacy_raw_source(self, row, positions, owner_name):
        assignment = clean_text(first_value(row, positions, "分配信息", "陈广林发的信息"))
        assignment_info = parse_assignment_info(assignment, owner_name)
        raw_source = clean_text(first_value(row, positions, "客户来源", "获客渠道")) or assignment_info["source"]
        return raw_source, assignment_info

    def _find_legacy_customer(self, row, positions, owner_name, assignment_info):
        name = clean_text(first_value(row, positions, "公司名称", "客户名称"))
        contact_name = clean_text(first_value(row, positions, "联系人/微信名称", "联系人"))
        region = clean_text(first_value(row, positions, "国家/地区", "地区"))
        phone_raw = clean_text(first_value(row, positions, "电话"))
        phone_raw = merge_phone_values(phone_raw, assignment_info.get("phone"), hints=(region, name, contact_name))
        phone, _ = split_phone_and_wechat(phone_raw, region, name, contact_name)
        for token in phone_lookup_tokens(phone or phone_raw):
            customer_id = self.phone_index.get(token)
            if not customer_id:
                continue
            found = Customer.objects.filter(pk=customer_id, is_active=True).first()
            if found:
                return found
        name = name or contact_name
        if name:
            return Customer.objects.filter(name=name, owner_name=owner_name, is_active=True).first()
        return None

    def _canonical_source(self, raw_value, raw_sources):
        raw_text = clean_text(raw_value)
        if not raw_text:
            return ""
        source = canonical_source(raw_text)
        if not source or source == "其他":
            return ""
        raw_sources[f"{raw_text} -> {source}"] += 1
        return source

    def _apply_source(self, customer, source, stats, applied_sources, blocked):
        if customer is None or not source:
            return False
        current = customer.source_channel or ""
        if current == source:
            stats["customers_same"] += 1
            return False
        if current not in RECOVERABLE_CURRENT_VALUES:
            blocked[f"{current} 保留，未改为 {source}"] += 1
            return False
        if not self.dry_run:
            customer.source_channel = source
            customer.save(update_fields=["source_channel", "updated_at"])
        applied_sources[source] += 1
        stats["customers_updated"] += 1
        return True

    def _replace_broad_leftovers(self, stats):
        for model, fields in ((Customer, ("source_channel", "account_source")), (Lead, ("source_channel",))):
            for field_name in fields:
                for old_value, new_value in BROAD_REPLACEMENTS.items():
                    queryset = model.objects.filter(**{field_name: old_value})
                    if self.dry_run:
                        stats["broad_replaced"] += queryset.count()
                    else:
                        stats["broad_replaced"] += queryset.update(**{field_name: new_value})

    def _first_raw_value(self, data, headers):
        for header in headers:
            value = data.get(header)
            if clean_text(value):
                return value
        return ""

    def _find_customer_by_number(self, customer_no):
        customer_no = clean_text(customer_no)
        if not customer_no:
            return None
        return (
            Customer.objects.filter(customer_no=customer_no).first()
            or Customer.objects.filter(legacy_customer_no=customer_no).first()
            or Customer.objects.filter(lead_no=customer_no).first()
        )

    def _format_counter(self, counter):
        if not counter:
            return "无"
        return "；".join(f"{key}×{value}" for key, value in counter.most_common(30))

    def _format_current_distribution(self):
        rows = (
            Customer.objects.exclude(source_channel="")
            .values("source_channel")
            .annotate(total=Count("id"))
            .order_by("-total", "source_channel")[:40]
        )
        return "；".join(f"{row['source_channel']}×{row['total']}" for row in rows) or "无"
