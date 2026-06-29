from decimal import Decimal

from django import template
from django.contrib.auth.models import User
from django.db import DatabaseError
from django.db.models import Sum
from django.utils import timezone

from crm.models import ContactLog, Contract, Customer, FeishuSyncRecord, FeishuSyncSource


register = template.Library()


def compact_amount(value):
    value = value or Decimal("0")
    if value >= 10000:
        return f"{value / Decimal('10000'):.1f} 万"
    return f"{value:.0f}"


def empty_metrics():
    return {
        "customers": 0,
        "public_customers": 0,
        "unassigned_customers": 0,
        "priority_customers": 0,
        "due_today": 0,
        "contracts": 0,
        "contract_amount": "0",
        "enabled_sources": 0,
        "sync_records": 0,
        "contact_logs": 0,
        "active_users": 0,
        "last_sync_at": "暂无",
    }


@register.simple_tag
def admin_dashboard_metrics():
    try:
        today = timezone.localdate()
        customers = Customer.objects.filter(is_active=True)
        last_sync = FeishuSyncSource.objects.order_by("-last_sync_at").values_list("last_sync_at", flat=True).first()
        amount = Contract.objects.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        priority_grades = [Customer.Grade.KEY, Customer.Grade.INTENTION]
        return {
            "customers": customers.count(),
            "public_customers": customers.filter(status=Customer.Status.PUBLIC).count(),
            "unassigned_customers": customers.filter(owner__isnull=True).exclude(status=Customer.Status.PUBLIC).count(),
            "priority_customers": customers.filter(grade__in=priority_grades).count(),
            "due_today": customers.filter(next_contact_at__date__lte=today).count(),
            "contracts": Contract.objects.count(),
            "contract_amount": compact_amount(amount),
            "enabled_sources": FeishuSyncSource.objects.filter(enabled=True).count(),
            "sync_records": FeishuSyncRecord.objects.count(),
            "contact_logs": ContactLog.objects.count(),
            "active_users": User.objects.filter(is_active=True).count(),
            "last_sync_at": timezone.localtime(last_sync).strftime("%Y-%m-%d %H:%M") if last_sync else "暂无",
        }
    except DatabaseError:
        return empty_metrics()