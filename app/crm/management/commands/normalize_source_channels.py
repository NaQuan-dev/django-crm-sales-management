from collections import Counter

from django.core.management.base import BaseCommand
from crm.models import Customer, Lead
from crm.options import canonical_source


FOREIGN_SOURCE_KEYWORDS = ("ins", "instagram", "fb", "facebook", "照片墙", "脸书", "国外社媒")


class Command(BaseCommand):
    help = "清洗线索来源为标准展示值，并输出归并报告。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="只生成报告，不写入数据库。")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        stats = {
            "customers_seen": 0,
            "customers_updated": 0,
            "leads_seen": 0,
            "leads_updated": 0,
        }
        before = Counter()
        after = Counter()
        mapping = Counter()
        foreign_before = Counter()

        self._normalize_model(Customer, ["source_channel", "account_source"], dry_run, stats, before, after, mapping, foreign_before)
        self._normalize_model(Lead, ["source_channel"], dry_run, stats, before, after, mapping, foreign_before)

        action = "预览" if dry_run else "完成"
        self.stdout.write(f"线索来源清洗{action}：客户检查 {stats['customers_seen']} 条，客户更新 {stats['customers_updated']} 处；线索检查 {stats['leads_seen']} 条，线索更新 {stats['leads_updated']} 处。")
        self.stdout.write("清洗后来源分布：" + self._format_counter(after))
        self.stdout.write("主要清洗：" + self._format_counter(mapping))
        if foreign_before:
            self.stdout.write("已找到国外平台来源：" + self._format_counter(foreign_before))
        else:
            self.stdout.write("未在当前 CRM 来源字段中找到 ins / facebook / fb / Instagram / 照片墙 / 脸书 相关来源。")

    def _normalize_model(self, model, field_names, dry_run, stats, before, after, mapping, foreign_before):
        model_key = "customers" if model is Customer else "leads"
        for field_name in field_names:
            rows = (
                model.objects.exclude(**{field_name: ""})
                .values_list(field_name)
                .order_by()
            )
            counts = Counter(str(value or "").strip() for (value,) in rows if str(value or "").strip())
            for old_value, count in counts.items():
                stats[f"{model_key}_seen"] += count
                before[old_value] += count
                if self._is_foreign_source(old_value):
                    foreign_before[old_value] += count
                new_value = canonical_source(old_value)
                if not new_value:
                    continue
                after[new_value] += count
                if new_value == old_value:
                    continue
                mapping[f"{old_value} -> {new_value}"] += count
                if not dry_run:
                    updated = model.objects.filter(**{field_name: old_value}).update(**{field_name: new_value})
                    stats[f"{model_key}_updated"] += updated

    def _is_foreign_source(self, value):
        text = str(value or "").strip().lower()
        return any(keyword.lower() in text for keyword in FOREIGN_SOURCE_KEYWORDS)

    def _format_counter(self, counter):
        if not counter:
            return "无"
        return "；".join(f"{key}×{value}" for key, value in counter.most_common(30))
