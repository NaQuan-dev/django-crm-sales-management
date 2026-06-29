import json
import os
import urllib.request
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import AuditLog, ContactLog, Contract, Customer, Lead, OperationLog, Quote, Reminder, Tag, TaskReminder, VisitPlan
from .options import canonical_customer_statuses, parse_multi_value


GRADE_NEXT_CONTACT_DAYS = {
    Customer.Grade.KEY: 3,
    Customer.Grade.INTENTION: 7,
    Customer.Grade.NORMAL: 14,
    Customer.Grade.POTENTIAL: 21,
    Customer.Grade.INCUBATING: 30,
    Customer.Grade.UNCERTAIN: 14,
    Customer.Grade.INVALID: 0,
}

WEEKLY_UNCONTACTED_REMINDER_DAYS = 7
PROTECTED_CUSTOMER_REMINDER_DAYS = 60
PUBLIC_POOL_EXEMPT_STATUS_LABELS = {"报价中", "已报价", "合同中", "待收款"}
DEAL_STATUS_LABELS = {"已成交", "已下单", "合同已签待预付"}


def _active_profile_role(user):
    if not user.is_authenticated:
        return ""
    from .models import Profile
    profile = Profile.objects.filter(user=user, active=True).only("role").first()
    return profile.role if profile else ""


def can_view_all(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return _active_profile_role(user) in {"admin", "leader"}


def can_assign(user):
    if user.is_superuser:
        return True
    return _active_profile_role(user) in {"admin", "leader"}


def recycled_customer_q():
    return (
        Q(is_recycled=True)
        | Q(status=Customer.Status.INVALID)
        | Q(grade=Customer.Grade.INVALID)
        | Q(customer_level=Customer.CustomerLevel.INVALID)
        | Q(follow_status=Customer.FollowStatus.INVALID_CLOSED)
    )


def _public_pool_stale_created_q(cutoff):
    return Q(historical_created_at__lt=cutoff) | Q(historical_created_at__isnull=True, created_at__lt=cutoff)


def public_pool_stale_customer_q(now=None, public_days=None):
    stale_cutoff = public_pool_stale_cutoff(now, public_days)
    stale_created_q = _public_pool_stale_created_q(stale_cutoff)
    stale_q = Q(last_contact_at__lt=stale_cutoff) | (Q(last_contact_at__isnull=True) & stale_created_q)
    return stale_q & ~public_pool_exempt_customer_q()


def public_pool_customer_q(now=None, public_days=None):
    explicit_public_q = Q(owner__isnull=True) | Q(status=Customer.Status.PUBLIC) | Q(is_public=True)
    return (explicit_public_q | public_pool_stale_customer_q(now, public_days)) & ~recycled_customer_q()


def is_recycled_customer(customer):
    return bool(
        getattr(customer, "is_recycled", False)
        or getattr(customer, "status", "") == Customer.Status.INVALID
        or getattr(customer, "grade", "") == Customer.Grade.INVALID
        or getattr(customer, "customer_level", "") == Customer.CustomerLevel.INVALID
        or getattr(customer, "follow_status", "") == Customer.FollowStatus.INVALID_CLOSED
    )


def is_public_pool_customer(customer, now=None, public_days=None):
    if not customer or is_recycled_customer(customer):
        return False
    if not getattr(customer, "owner_id", None) or customer.status == Customer.Status.PUBLIC or customer.is_public:
        return True
    if is_public_pool_exempt_customer(customer):
        return False
    stale_cutoff = public_pool_stale_cutoff(now, public_days)
    base_time = customer.last_contact_at or customer.historical_created_at or customer.created_at
    return bool(base_time and base_time < stale_cutoff)


def customer_queryset_for(user):
    qs = Customer.objects.filter(is_active=True)
    if can_view_all(user):
        return qs
    return qs.filter(Q(owner=user) | Q(co_owners=user) | public_pool_customer_q()).distinct()


def lead_queryset_for(user):
    qs = Lead.objects.filter(is_active=True)
    if can_view_all(user):
        return qs
    return qs.filter(Q(owner=user) | Q(co_owners=user) | Q(owner__isnull=True)).distinct()


def contact_log_queryset_for(user):
    qs = ContactLog.objects.select_related("customer", "created_by")
    if can_view_all(user):
        return qs
    return qs.filter(Q(customer__owner=user) | Q(customer__co_owners=user) | Q(created_by=user)).distinct()


def contract_queryset_for(user):
    qs = Contract.objects.filter(is_active=True).select_related("customer", "signed_by", "sales_user")
    if can_view_all(user) or _active_profile_role(user) in {"finance"}:
        return qs
    return qs.filter(Q(customer__owner=user) | Q(customer__co_owners=user) | Q(signed_by=user) | Q(sales_user=user)).distinct()


def next_contact_time(grade, base_time=None):
    base_time = base_time or timezone.now()
    days = GRADE_NEXT_CONTACT_DAYS.get(grade, 14)
    if days <= 0:
        return None
    return base_time + timedelta(days=days)


def suggest_tags_text(text):
    text = (text or "").lower()
    tags = set()
    rules = {
        "短视频": ["抖音", "视频号", "快手", "短视频", "直播"],
        "网站询盘": ["网站", "官网", "独立站"],
        "展会": ["展会", "展览"],
        "转介绍": ["介绍", "推荐", "老客户"],
        "设备咨询": ["灌装", "封口", "灌封", "设备", "产线"],
        "价格敏感": ["价格", "报价", "多少钱", "预算"],
        "近期意向": ["近期", "马上", "尽快", "采购", "下单"],
        "海外客户": ["美国", "加拿大", "澳洲", "欧洲", "越南", "泰国", "india", "usa"],
        "联系信息不足": ["无电话", "没电话", "没有联系方式"],
        "疑似无效": ["无效", "错误", "打不通", "不是客户"],
    }
    for tag, keywords in rules.items():
        if any(keyword in text for keyword in keywords):
            tags.add(tag)
    return sorted(tags)


def get_or_create_tags(names, category="自动标签"):
    result = []
    for name in names:
        tag, _ = Tag.objects.get_or_create(name=name, defaults={"category": category})
        result.append(tag)
    return result


def apply_auto_tags(instance):
    text = " ".join(
        [
            getattr(instance, "name", ""),
            getattr(instance, "contact_name", ""),
            getattr(instance, "lead_status", ""),
            getattr(instance, "customer_status_text", ""),
            getattr(instance, "source_channel", ""),
            getattr(instance, "customer_type", ""),
            getattr(instance, "demand", ""),
            getattr(instance, "related_lead", ""),
            getattr(instance, "region", ""),
            getattr(instance, "city", ""),
            getattr(instance, "industry", ""),
            getattr(instance, "notes", ""),
        ]
    )
    tags = get_or_create_tags(suggest_tags_text(text))
    if tags:
        instance.tags.add(*tags)


def _customer_level_from_grade(grade):
    return {
        Customer.Grade.INVALID: Customer.CustomerLevel.INVALID,
        Customer.Grade.KEY: Customer.CustomerLevel.INTENTION,
        Customer.Grade.INTENTION: Customer.CustomerLevel.INTENTION,
        Customer.Grade.NORMAL: Customer.CustomerLevel.NORMAL,
        Customer.Grade.UNCERTAIN: Customer.CustomerLevel.PENDING,
        Customer.Grade.POTENTIAL: Customer.CustomerLevel.PENDING,
        Customer.Grade.INCUBATING: Customer.CustomerLevel.PENDING,
    }.get(grade, Customer.CustomerLevel.PENDING)


def _grade_from_customer_level(level):
    return {
        Customer.CustomerLevel.INVALID: Customer.Grade.INVALID,
        Customer.CustomerLevel.DEAL: Customer.Grade.KEY,
        Customer.CustomerLevel.INTENTION: Customer.Grade.INTENTION,
        Customer.CustomerLevel.NORMAL: Customer.Grade.NORMAL,
        Customer.CustomerLevel.NO_INTENT: Customer.Grade.INCUBATING,
        Customer.CustomerLevel.PENDING: Customer.Grade.POTENTIAL,
    }.get(level, Customer.Grade.POTENTIAL)


def _follow_status_from_customer_status_text(status_text):
    values = set(parse_multi_value(status_text))
    if "合同已签待预付" in values:
        return Customer.FollowStatus.PAYMENT_PENDING
    if "已下单" in values:
        return Customer.FollowStatus.DEAL
    if "报价中" in values:
        return Customer.FollowStatus.QUOTING
    if "已报价" in values:
        return Customer.FollowStatus.QUOTED
    if "未报价" in values:
        return Customer.FollowStatus.NOT_QUOTED
    if "方案设计沟通中" in values or "待拜访" in values:
        return Customer.FollowStatus.DEMAND_CONFIRMING
    if "已加联系方式" in values:
        return Customer.FollowStatus.CONTACT_ADDED
    if "未加联系方式" in values:
        return Customer.FollowStatus.NEW_INQUIRY
    if "微信未通过" in values:
        return Customer.FollowStatus.PAUSED
    return ""


def _apply_contact_log_level(customer, level_after):
    value = str(level_after or "").strip()
    if not value:
        return
    grade_values = {choice for choice, _label in Customer.Grade.choices}
    level_values = {choice for choice, _label in Customer.CustomerLevel.choices}
    if value in grade_values:
        customer.grade = value
        customer.customer_level = _customer_level_from_grade(value)
    elif value in level_values:
        customer.customer_level = value
        customer.grade = _grade_from_customer_level(value)


def _apply_contact_log_status(customer, status_after, result):
    value = str(status_after or "").strip()
    follow_values = {choice for choice, _label in Customer.FollowStatus.choices}
    if value in follow_values:
        customer.follow_status = value
        return
    source = value or result
    status_text = canonical_customer_statuses(source)
    if status_text:
        customer.customer_status_text = status_text
        follow_status = _follow_status_from_customer_status_text(status_text)
        if follow_status:
            customer.follow_status = follow_status


def update_customer_after_contact(customer, contact_log):
    customer.last_contact_at = contact_log.contact_at
    next_time = contact_log.next_contact_at or next_contact_time(customer.grade, contact_log.contact_at)
    customer.next_contact_at = next_time
    customer.next_follow_at = next_time
    customer.next_action = contact_log.next_action or contact_log.result or customer.next_action
    if contact_log.demand_update:
        customer.demand_summary = contact_log.demand_update
    _apply_contact_log_level(customer, contact_log.level_after)
    _apply_contact_log_status(customer, contact_log.status_after, contact_log.result)
    customer.release_warned_at = None
    if customer.status == Customer.Status.PUBLIC and customer.owner:
        customer.status = Customer.Status.PRIVATE
        customer.is_public = False
    customer.save(update_fields=[
        "last_contact_at", "next_contact_at", "next_follow_at", "next_action", "demand_summary",
        "grade", "customer_status_text", "customer_level", "follow_status", "release_warned_at", "status", "is_public", "updated_at",
    ])


def customer_status_values(customer):
    return {item.strip() for item in str(customer.customer_status_text or "").split(",") if item.strip()}


def is_deal_customer(customer):
    return (
        customer.is_deal
        or customer.status == Customer.Status.DEAL
        or customer.deal_status == Customer.DealStatus.WON
        or bool(customer_status_values(customer) & DEAL_STATUS_LABELS)
    )


def customer_has_unpaid_contract(customer):
    return bool(getattr(customer, "unpaid_amount", 0) and customer.unpaid_amount > 0)


def customer_has_active_visit(customer):
    today = timezone.localdate()
    return VisitPlan.objects.filter(
        customer=customer,
        visit_date__gte=today,
        status__in=[VisitPlan.Status.PENDING, VisitPlan.Status.CONFIRMED],
    ).exists()


def is_public_pool_exempt_customer(customer):
    protected_follow_statuses = {
        Customer.FollowStatus.QUOTING,
        Customer.FollowStatus.QUOTED,
        Customer.FollowStatus.CONTRACTING,
        Customer.FollowStatus.PAYMENT_PENDING,
        Customer.FollowStatus.DEAL,
    }
    if is_deal_customer(customer) or customer.follow_status in protected_follow_statuses:
        return True
    if bool(customer_status_values(customer) & PUBLIC_POOL_EXEMPT_STATUS_LABELS):
        return True
    if customer.quotes.exclude(status__in=[Quote.Status.DRAFT, Quote.Status.EXPIRED]).exists():
        return True
    if customer.contracts.filter(is_active=True).exclude(status=Contract.Status.CANCELED).exists():
        return True
    if customer_has_unpaid_contract(customer):
        return True
    if customer_has_active_visit(customer):
        return True
    return False


def public_pool_exempt_customer_q():
    status_q = Q(is_deal=True) | Q(status=Customer.Status.DEAL) | Q(deal_status=Customer.DealStatus.WON)
    status_q |= Q(follow_status__in=[
        Customer.FollowStatus.QUOTING,
        Customer.FollowStatus.QUOTED,
        Customer.FollowStatus.CONTRACTING,
        Customer.FollowStatus.PAYMENT_PENDING,
        Customer.FollowStatus.DEAL,
    ])
    for label in PUBLIC_POOL_EXEMPT_STATUS_LABELS | DEAL_STATUS_LABELS:
        status_q |= Q(customer_status_text__icontains=label)
    status_q |= Q(quotes__status__in=[Quote.Status.SENT, Quote.Status.VIEWED, Quote.Status.DEAL])
    status_q |= Q(contracts__is_active=True)
    status_q |= Q(visit_plans__visit_date__gte=timezone.localdate(), visit_plans__status__in=[VisitPlan.Status.PENDING, VisitPlan.Status.CONFIRMED])
    return status_q

def customer_uncontacted_days(customer, now=None):
    base_time = customer.last_contact_at or customer.historical_created_at or customer.created_at
    if not base_time:
        return None, None
    now = now or timezone.now()
    base_date = timezone.localtime(base_time).date()
    return (timezone.localdate(now) - base_date).days, base_date


def create_once_reminder(customer=None, lead=None, assignee=None, reminder_type=None, due_at=None, message=""):
    existing = Reminder.objects.filter(
        customer=customer,
        lead=lead,
        assignee=assignee,
        reminder_type=reminder_type,
        status__in=[Reminder.Status.PENDING, Reminder.Status.SENT],
    ).first()
    if existing:
        return existing, False
    return Reminder.objects.create(
        customer=customer,
        lead=lead,
        assignee=assignee,
        reminder_type=reminder_type,
        due_at=due_at or timezone.now(),
        message=message,
    ), True


def create_interval_reminder(customer, assignee, reminder_type, threshold_days, base_date, days, due_at, message):
    threshold_key = f"超过 {threshold_days} 天"
    base_key = f"自 {base_date.isoformat()} 起"
    existing = (
        Reminder.objects.filter(
            customer=customer,
            assignee=assignee,
            reminder_type=reminder_type,
            status__in=[Reminder.Status.PENDING, Reminder.Status.SENT, Reminder.Status.DONE],
        )
        .filter(message__contains=threshold_key)
        .filter(message__contains=base_key)
        .first()
    )
    if existing:
        return existing, False
    return Reminder.objects.create(
        customer=customer,
        assignee=assignee,
        reminder_type=reminder_type,
        due_at=due_at,
        message=message,
    ), True


def create_once_task_reminder(customer=None, lead=None, quote=None, contract=None, assignee=None, reminder_type=None, due_at=None, title="", content="", priority=None):
    existing = TaskReminder.objects.filter(
        customer=customer,
        lead=lead,
        quote=quote,
        contract=contract,
        assigned_to=assignee,
        reminder_type=reminder_type,
        status__in=[TaskReminder.Status.PENDING, TaskReminder.Status.OVERDUE],
    ).first()
    if existing:
        return existing, False
    return TaskReminder.objects.create(
        customer=customer,
        lead=lead,
        quote=quote,
        contract=contract,
        assigned_to=assignee,
        reminder_type=reminder_type,
        due_at=due_at or timezone.now(),
        title=title,
        content=content,
        priority=priority or TaskReminder.Priority.MEDIUM,
    ), True


def create_quote_followup_reminders(now=None):
    now = now or timezone.now()
    cutoff = timezone.localdate(now) - timedelta(days=7)
    created_count = 0
    quotes = (
        Quote.objects.filter(status__in=[Quote.Status.SENT, Quote.Status.VIEWED], quote_date__lte=cutoff)
        .exclude(customer__deal_status=Customer.DealStatus.WON)
        .exclude(customer__status=Customer.Status.DEAL)
        .select_related("customer", "quoted_by", "customer__owner")
    )
    for quote in quotes:
        assignee = quote.quoted_by or quote.customer.owner
        if not assignee:
            continue
        title = f"已报价未跟：{quote.customer}"
        content = f"报价 {quote.quote_no} 于 {quote.quote_date:%Y-%m-%d} 发出，金额 {quote.total_amount} {quote.currency}，请补充客户反馈和下一步动作。"
        task, task_created = create_once_task_reminder(
            customer=quote.customer,
            quote=quote,
            assignee=assignee,
            reminder_type=TaskReminder.ReminderType.QUOTE,
            due_at=now,
            title=title,
            content=content,
            priority=TaskReminder.Priority.HIGH,
        )
        reminder, reminder_created = create_once_reminder(
            customer=quote.customer,
            assignee=assignee,
            reminder_type=Reminder.ReminderType.QUOTE_FOLLOWUP,
            due_at=now,
            message=content,
        )
        if task_created or reminder_created:
            created_count += 1
    return created_count


def create_payment_collection_reminders(now=None):
    now = now or timezone.now()
    created_count = 0
    contracts = (
        Contract.objects.filter(is_active=True)
        .exclude(status=Contract.Status.CANCELED)
        .select_related("customer", "sales_user", "signed_by", "customer__owner")
    )
    for contract in contracts:
        if contract.unpaid_amount <= 0:
            continue
        assignee = contract.sales_user or contract.signed_by or (contract.customer.owner if contract.customer_id else None)
        title = f"待收款：{contract.customer or contract.customer_name}"
        content = f"合同 {contract.contract_no} 未收款 {contract.unpaid_amount} {contract.currency}，请跟进收款节点。"
        task, task_created = create_once_task_reminder(
            customer=contract.customer,
            contract=contract,
            assignee=assignee,
            reminder_type=TaskReminder.ReminderType.PAYMENT,
            due_at=now,
            title=title,
            content=content,
            priority=TaskReminder.Priority.HIGH,
        )
        reminder, reminder_created = create_once_reminder(
            customer=contract.customer,
            assignee=assignee,
            reminder_type=Reminder.ReminderType.PAYMENT_COLLECTION,
            due_at=now,
            message=content,
        )
        if task_created or reminder_created:
            created_count += 1
    return created_count


def create_lead_followup_reminders(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(hours=24)
    created_count = 0
    leads = (
        Lead.objects.filter(is_active=True, owner__isnull=False, assigned_at__isnull=False, assigned_at__lte=cutoff, first_contact_at__isnull=True)
        .exclude(status__in=[Lead.Status.CONTACTED, Lead.Status.CONVERTED, Lead.Status.INVALID, Lead.Status.DUPLICATE])
        .select_related("owner")
    )
    for lead in leads:
        title = f"线索待首次联系：{lead}"
        content = f"线索 {lead.lead_no} 已分配超过 24 小时，请尽快首次联系并记录跟进。"
        task, task_created = create_once_task_reminder(
            lead=lead,
            assignee=lead.owner,
            reminder_type=TaskReminder.ReminderType.LEAD,
            due_at=now,
            title=title,
            content=content,
            priority=TaskReminder.Priority.HIGH,
        )
        reminder, reminder_created = create_once_reminder(
            lead=lead,
            assignee=lead.owner,
            reminder_type=Reminder.ReminderType.LEAD_FOLLOWUP,
            due_at=now,
            message=content,
        )
        if task_created or reminder_created:
            created_count += 1
    return created_count


def create_visit_prep_reminders(now=None):
    now = now or timezone.now()
    today = timezone.localdate(now)
    week_end = today + timedelta(days=7)
    created_count = 0
    visits = (
        VisitPlan.objects.filter(visit_date__gte=today, visit_date__lte=week_end, status__in=[VisitPlan.Status.PENDING, VisitPlan.Status.CONFIRMED])
        .select_related("customer", "customer__owner")
        .prefetch_related("reception_users")
    )
    for visit in visits:
        assignees = list(visit.reception_users.all()) or ([visit.customer.owner] if visit.customer.owner_id else [])
        for assignee in assignees:
            title = f"客户来访准备：{visit.customer}"
            content = f"{visit.visit_date:%Y-%m-%d} 来访，设备 {visit.visit_equipment or '待确认'}，到达时间 {visit.arrival_time or '待确认'}。"
            task, task_created = create_once_task_reminder(
                customer=visit.customer,
                assignee=assignee,
                reminder_type=TaskReminder.ReminderType.VISIT,
                due_at=now,
                title=title,
                content=content,
                priority=TaskReminder.Priority.MEDIUM,
            )
            reminder, reminder_created = create_once_reminder(
                customer=visit.customer,
                assignee=assignee,
                reminder_type=Reminder.ReminderType.VISIT_PREP,
                due_at=now,
                message=content,
            )
            if task_created or reminder_created:
                created_count += 1
    return created_count

def send_feishu_webhook(text):
    webhook = getattr(settings, "FEISHU_WEBHOOK_URL", "") or os.getenv("FEISHU_WEBHOOK_URL", "")
    if not webhook:
        return False
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return 200 <= response.status < 300


def public_pool_stale_cutoff(now=None, public_days=None):
    now = now or timezone.now()
    public_days = public_days if public_days is not None else getattr(settings, "CRM_PUBLIC_POOL_DAYS", 30)
    cutoff_date = timezone.localdate(now) - timedelta(days=public_days)
    local_cutoff = datetime.combine(cutoff_date, time.min)
    return timezone.make_aware(local_cutoff, timezone.get_current_timezone())


def release_stale_customers(now=None, public_days=None):
    now = now or timezone.now()
    public_days = public_days if public_days is not None else getattr(settings, "CRM_PUBLIC_POOL_DAYS", 30)
    stale_cutoff = public_pool_stale_cutoff(now, public_days)
    stale_created_q = _public_pool_stale_created_q(stale_cutoff)
    stale_customers = Customer.objects.filter(
        is_active=True,
    ).exclude(status=Customer.Status.PUBLIC).exclude(public_pool_exempt_customer_q()).filter(
        Q(last_contact_at__lt=stale_cutoff) | (Q(last_contact_at__isnull=True) & stale_created_q)
    ).distinct()

    released_count = 0
    for customer in stale_customers.select_related("owner"):
        old_owner = customer.owner
        old_owner_name = old_owner_id(old_owner) or customer.owner_name
        customer.owner = None
        customer.owner_name = old_owner_name
        customer.status = Customer.Status.PUBLIC
        customer.is_public = True
        customer.public_at = now
        customer.release_warned_at = None
        customer.save(update_fields=["owner", "owner_name", "status", "is_public", "public_at", "release_warned_at", "updated_at"])
        Reminder.objects.filter(
            customer=customer,
            reminder_type=Reminder.ReminderType.PUBLIC_POOL_WARNING,
            status=Reminder.Status.PENDING,
        ).update(status=Reminder.Status.DONE, sent_at=now)
        Reminder.objects.create(
            customer=customer,
            assignee=old_owner,
            reminder_type=Reminder.ReminderType.PUBLIC_POOL_RELEASED,
            due_at=now,
            message=f"{customer} 已因超过 {public_days} 天未联系自动进入公海。",
            status=Reminder.Status.SENT,
            sent_at=now,
        )
        AuditLog.objects.create(
            actor=None,
            action="自动移入公海",
            target_type="客户",
            target_id=str(customer.pk),
            detail=f"原负责人 {old_owner_name} 超过 {public_days} 天未联系，系统自动移入公海。",
        )
        OperationLog.objects.create(
            user=None,
            customer=customer,
            action_type=OperationLog.ActionType.PUBLIC_POOL,
            before_data={"owner": old_owner_name, "status": Customer.Status.PRIVATE},
            after_data={"owner": "", "status": Customer.Status.PUBLIC},
            remark=f"超过 {public_days} 天未联系，系统自动移入公海。",
        )
        released_count += 1
    return released_count


def create_uncontacted_rule_reminders(now=None, public_days=None):
    now = now or timezone.now()
    public_days = public_days if public_days is not None else getattr(settings, "CRM_PUBLIC_POOL_DAYS", 30)
    created_count = 0
    customers = Customer.objects.filter(
        is_active=True,
        owner__isnull=False,
    ).exclude(status=Customer.Status.PUBLIC).select_related("owner")
    for customer in customers:
        days, base_date = customer_uncontacted_days(customer, now)
        if days is None:
            continue
        if is_public_pool_exempt_customer(customer):
            if days <= PROTECTED_CUSTOMER_REMINDER_DAYS:
                continue
            reminder, created = create_interval_reminder(
                customer=customer,
                assignee=customer.owner,
                reminder_type=Reminder.ReminderType.PROTECTED_CUSTOMER_IDLE,
                threshold_days=PROTECTED_CUSTOMER_REMINDER_DAYS,
                base_date=base_date,
                days=days,
                due_at=now,
                message=f"{customer} 自 {base_date.isoformat()} 起已 {days} 天未联系，超过 {PROTECTED_CUSTOMER_REMINDER_DAYS} 天，请确认报价或成交客户的后续维护。",
            )
            if created:
                created_count += 1
            continue
        if days <= WEEKLY_UNCONTACTED_REMINDER_DAYS or days > public_days:
            continue
        threshold_days = ((days - 1) // WEEKLY_UNCONTACTED_REMINDER_DAYS) * WEEKLY_UNCONTACTED_REMINDER_DAYS
        if threshold_days <= 0:
            continue
        reminder, created = create_interval_reminder(
            customer=customer,
            assignee=customer.owner,
            reminder_type=Reminder.ReminderType.UNCONTACTED_RULE,
            threshold_days=threshold_days,
            base_date=base_date,
            days=days,
            due_at=now,
            message=f"{customer} 自 {base_date.isoformat()} 起已 {days} 天未联系，超过 {threshold_days} 天，请及时跟进；超过 {public_days} 天将自动进入公海。",
        )
        if created:
            created_count += 1
    return created_count


def run_daily_rules(now=None):
    now = now or timezone.now()
    public_days = getattr(settings, "CRM_PUBLIC_POOL_DAYS", 30)

    stats = {
        "next_contact_reminders": 0,
        "uncontacted_rule_reminders": 0,
        "public_pool_warnings": 0,
        "public_pool_released": 0,
        "quote_followup_reminders": 0,
        "payment_collection_reminders": 0,
        "lead_followup_reminders": 0,
        "visit_prep_reminders": 0,
        "notifications_sent": 0,
    }

    due_customers = Customer.objects.filter(
        is_active=True,
        owner__isnull=False,
        next_contact_at__isnull=False,
        next_contact_at__lte=now,
        status=Customer.Status.PRIVATE,
    )
    for customer in due_customers:
        reminder, created = create_once_reminder(
            customer=customer,
            assignee=customer.owner,
            reminder_type=Reminder.ReminderType.NEXT_CONTACT,
            due_at=customer.next_contact_at,
            message=f"{customer} 已到跟进时间，请及时联系。",
        )
        if created:
            stats["next_contact_reminders"] += 1

    stats["uncontacted_rule_reminders"] = create_uncontacted_rule_reminders(now=now, public_days=public_days)
    stats["quote_followup_reminders"] = create_quote_followup_reminders(now=now)
    stats["payment_collection_reminders"] = create_payment_collection_reminders(now=now)
    stats["lead_followup_reminders"] = create_lead_followup_reminders(now=now)
    stats["visit_prep_reminders"] = create_visit_prep_reminders(now=now)
    stats["public_pool_released"] = release_stale_customers(now=now, public_days=public_days)

    TaskReminder.objects.filter(status=TaskReminder.Status.PENDING, due_at__lt=now).update(status=TaskReminder.Status.OVERDUE)

    pending = Reminder.objects.filter(status=Reminder.Status.PENDING, due_at__lte=now).select_related("assignee")
    for reminder in pending:
        if reminder.assignee:
            assignee = reminder.assignee.get_full_name() or reminder.assignee.username
        else:
            assignee = "未分配"
        text = f"CRM助手提醒：{assignee}，{reminder.message}"
        try:
            if send_feishu_webhook(text):
                stats["notifications_sent"] += 1
        except Exception:
            pass
        reminder.status = Reminder.Status.SENT
        reminder.sent_at = now
        reminder.save(update_fields=["status", "sent_at"])

    return stats


def old_owner_id(user):
    if not user:
        return ""
    return user.username
