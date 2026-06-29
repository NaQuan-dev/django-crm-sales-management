from django.core.management.base import BaseCommand

from crm.models import Payment


class Command(BaseCommand):
    help = "修复到款记录的客户关联和实收金额。"

    def handle(self, *args, **options):
        updated = 0
        for payment in Payment.objects.select_related("contract", "customer").iterator():
            fields = []
            if payment.contract_id and payment.contract.customer_id and payment.customer_id != payment.contract.customer_id:
                payment.customer = payment.contract.customer
                fields.append("customer")
            if not payment.actual_received_amount and payment.amount:
                payment.actual_received_amount = payment.amount - payment.bank_fee
                fields.append("actual_received_amount")
            if fields:
                payment.save(update_fields=fields + ["updated_at"])
                updated += 1
        self.stdout.write(self.style.SUCCESS(f"已修复到款记录 {updated} 条。"))