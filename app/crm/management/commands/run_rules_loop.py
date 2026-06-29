import time
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.services import run_daily_rules


class Command(BaseCommand):
    help = "持续运行客户系统自动规则，用于容器规则服务。"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=300)

    def handle(self, *args, **options):
        interval = options["interval"]
        self.stdout.write(self.style.SUCCESS(f"客户系统自动规则已启动，间隔 {interval} 秒。"))
        while True:
            stats = run_daily_rules()
            message = f"自动规则执行完成：{stats}"
            self.stdout.write(message)
            self._write_report(stats)
            time.sleep(interval)

    def _write_report(self, stats):
        try:
            report_dir = Path("imports")
            report_dir.mkdir(exist_ok=True)
            report_path = report_dir / "public_pool_rules_report.txt"
            generated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
            report_path.write_text("{}\n{}\n".format(generated_at, stats), encoding="utf-8")
        except Exception:
            pass
