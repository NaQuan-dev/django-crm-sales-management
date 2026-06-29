from django.core.management.base import BaseCommand

from crm.models import Contract, Customer, Lead, Opportunity, Payment, Quote, SampleTest, VisitPlan


class Command(BaseCommand):
    help = "为缺少编号的 CRM 对象补齐编号。"

    def handle(self, *args, **options):
        specs = [
            (Customer, "customer_no"),
            (Lead, "lead_no"),
            (Quote, "quote_no"),
            (Contract, "contract_no"),
            (Payment, "payment_no"),
            (SampleTest, "sample_no"),
            (VisitPlan, "visit_no"),
            (Opportunity, "opportunity_no"),
        ]
        total = 0
        for model, field in specs:
            count = 0
            for obj in model.objects.filter(**{field: ""}).iterator():
                obj.save()
                count += 1
            total += count
            self.stdout.write(f"{model.__name__}: {count}")
        self.stdout.write(self.style.SUCCESS(f"已补编号 {total} 条。"))