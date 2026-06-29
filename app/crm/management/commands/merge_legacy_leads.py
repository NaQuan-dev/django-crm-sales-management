from django.core.management.base import BaseCommand

from crm.models import Customer, Lead


class Command(BaseCommand):
    help = "把旧线索表数据合并到统一客户表。"

    def handle(self, *args, **options):
        created = 0
        updated = 0
        for lead in Lead.objects.filter(is_active=True):
            customer = None
            if lead.lead_no:
                customer = Customer.objects.filter(lead_no=lead.lead_no).first()
            if not customer and lead.phone:
                customer = Customer.objects.filter(phone=lead.phone).first()
            if not customer and lead.wechat:
                customer = Customer.objects.filter(wechat=lead.wechat).first()
            if not customer:
                customer = Customer(source_kind=Customer.RecordKind.LEAD)
                created += 1
            else:
                updated += 1
            customer.source_kind = Customer.RecordKind.LEAD
            customer.lead_no = lead.lead_no or customer.lead_no
            customer.name = lead.name or customer.name
            customer.contact_name = lead.contact_name or customer.contact_name
            customer.phone = lead.phone or customer.phone
            customer.wechat = lead.wechat or customer.wechat
            customer.email = lead.email or customer.email
            customer.region = lead.region or customer.region
            customer.source_channel = lead.source_channel or customer.source_channel
            customer.customer_type = lead.customer_type or customer.customer_type
            customer.demand = lead.demand or customer.demand
            customer.lead_status = lead.get_status_display() or customer.lead_status
            customer.owner = lead.owner or customer.owner
            customer.notes = lead.notes or customer.notes
            customer.next_contact_at = lead.next_contact_at or customer.next_contact_at
            customer.created_by = lead.created_by or customer.created_by
            customer.historical_created_at = lead.created_at or customer.historical_created_at
            customer.save()
            if lead.tags.exists():
                customer.tags.add(*lead.tags.all())
        self.stdout.write(self.style.SUCCESS(f"旧线索合并完成：新增 {created} 条，更新 {updated} 条。"))
