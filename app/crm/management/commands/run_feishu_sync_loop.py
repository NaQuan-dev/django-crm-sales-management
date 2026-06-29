from django.core.management.base import BaseCommand

from crm.feishu_sync import run_sync_loop


class Command(BaseCommand):
    help = "持续运行飞书同步。"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=None)

    def handle(self, *args, **options):
        run_sync_loop(interval_seconds=options.get("interval"))
