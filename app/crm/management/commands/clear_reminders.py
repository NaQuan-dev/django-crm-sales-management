from django.core.management.base import BaseCommand

from crm.models import Reminder


class Command(BaseCommand):
    help = "清空现有跟进提醒，保留跟进日志。"

    def handle(self, *args, **options):
        total = Reminder.objects.count()
        Reminder.objects.all().delete()
        self.stdout.write(self.style.SUCCESS(f"跟进提醒清理完成：删除 {total} 条提醒。"))
