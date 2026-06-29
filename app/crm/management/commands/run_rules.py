from django.core.management.base import BaseCommand

from crm.services import run_daily_rules


class Command(BaseCommand):
    help = "执行一次客户系统提醒和公海规则。"

    def handle(self, *args, **options):
        stats = run_daily_rules()
        self.stdout.write(self.style.SUCCESS(f"自动规则执行完成：{stats}"))
