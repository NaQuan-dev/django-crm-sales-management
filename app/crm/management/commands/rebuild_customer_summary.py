from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.models import ContactLog, Contract, Customer, OperationLog


class Command(BaseCommand):
    help = "根据旧字段、跟进日志和合同重建客户摘要字段。"

    def handle(self, *args, **options):
        updated = 0
        for customer in Customer.objects.all().iterator():
            fields = []
            status_text = customer.customer_status_text or ""
            if customer.grade == Customer.Grade.INTENTION or "意向" in status_text:
                customer.customer_level = Customer.CustomerLevel.INTENTION
                fields.append("customer_level")
            elif customer.grade == Customer.Grade.INVALID or "无效" in status_text:
                customer.customer_level = Customer.CustomerLevel.INVALID
                customer.follow_status = Customer.FollowStatus.INVALID_CLOSED
                customer.is_recycled = True
                if not customer.recycled_at:
                    customer.recycled_at = timezone.now()
                fields.extend(["customer_level", "follow_status", "is_recycled", "recycled_at"])
            elif customer.grade == Customer.Grade.NORMAL:
                customer.customer_level = Customer.CustomerLevel.NORMAL
                fields.append("customer_level")
            if "报价中" in status_text:
                customer.follow_status = Customer.FollowStatus.QUOTING
                fields.append("follow_status")
            elif "已报价" in status_text:
                customer.follow_status = Customer.FollowStatus.QUOTED
                fields.append("follow_status")
            elif "未报价" in status_text:
                customer.follow_status = Customer.FollowStatus.NOT_QUOTED
                fields.append("follow_status")
            elif "已加联系方式" in status_text:
                customer.follow_status = Customer.FollowStatus.CONTACT_ADDED
                fields.append("follow_status")
            latest_log = ContactLog.objects.filter(customer=customer).order_by("-contact_at").first()
            if latest_log:
                customer.last_contact_at = latest_log.contact_at
                customer.next_contact_at = latest_log.next_contact_at or customer.next_contact_at
                customer.next_follow_at = latest_log.next_contact_at or customer.next_follow_at
                customer.next_action = latest_log.next_action or latest_log.result or customer.next_action
                fields.extend(["last_contact_at", "next_contact_at", "next_follow_at", "next_action"])
            if Contract.objects.filter(customer=customer, is_active=True).exclude(status=Contract.Status.CANCELED).exists():
                customer.is_deal = True
                customer.status = Customer.Status.DEAL
                customer.deal_status = Customer.DealStatus.WON
                customer.customer_level = Customer.CustomerLevel.DEAL
                customer.follow_status = Customer.FollowStatus.DEAL
                fields.extend(["is_deal", "status", "deal_status", "customer_level", "follow_status"])
            if not customer.product_interest and customer.demand:
                customer.product_interest = customer.demand
                fields.append("product_interest")
            if not customer.demand_summary and customer.notes:
                customer.demand_summary = customer.notes[:500]
                fields.append("demand_summary")
            if not fields:
                continue
            customer.save(update_fields=sorted(set(fields + ["updated_at"])))
            updated += 1
        self.stdout.write(self.style.SUCCESS(f"已重建客户摘要 {updated} 条。"))