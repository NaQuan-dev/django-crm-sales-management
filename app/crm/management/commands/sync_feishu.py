import json

from django.core.management.base import BaseCommand

from crm.feishu_sync import sync_all_sources


class Command(BaseCommand):
    help = "把已配置的飞书表格同步到客户系统。"

    def handle(self, *args, **options):
        results = sync_all_sources()
        self.stdout.write(json.dumps(results, ensure_ascii=False, indent=2))
