from django.contrib import admin
from django.contrib.admin.sites import NotRegistered
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group, User
from django.urls import reverse
from django.utils.html import format_html

from .models import Attachment, AuditLog, Contact, ContactLog, Contract, Customer, FeishuSyncRecord, FeishuSyncSource, Lead, OperationLog, Opportunity, Payment, Profile, Quote, QuoteItem, QuotePlan, Reminder, SampleTest, Tag, TaskReminder, VisitPlan, WorkOrderLink

admin.site.site_header = "CRM Template Admin"
admin.site.site_title = "CRM Template Admin"
admin.site.index_title = "System Administration"
admin.site.site_url = "/"


class NoSalesDeleteMixin:
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class RowEditLinkMixin:
    def get_list_display(self, request):
        columns = list(super().get_list_display(request))
        if "edit_link" not in columns:
            columns.append("edit_link")
        return columns

    @admin.display(description="操作")
    def edit_link(self, obj):
        opts = obj._meta
        url = reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])
        return format_html('<a class="nq-admin-row-action" href="{}">编辑</a>', url)


def _is_account_admin(user):
    if not getattr(user, "is_active", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if not getattr(user, "is_staff", False):
        return False
    return Profile.objects.filter(user=user, active=True, role=Profile.Role.ADMIN).exists()


def _sync_user_role(user, role):
    profile, _ = Profile.objects.get_or_create(user=user)
    if profile.role != role or not profile.active:
        profile.role = role
        profile.active = True
        profile.save(update_fields=["role", "active"])

    role_groups = {
        Profile.Role.ADMIN: "管理员",
        Profile.Role.LEADER: "领导",
        Profile.Role.SALES: "销售",
        Profile.Role.MARKETING: "新媒体",
        Profile.Role.FINANCE: "财务",
        Profile.Role.TECHNICIAN: "技术",
    }
    crm_group_names = set(role_groups.values())
    crm_groups = list(Group.objects.filter(name__in=crm_group_names))
    if crm_groups:
        user.groups.remove(*crm_groups)
    group, _ = Group.objects.get_or_create(name=role_groups[role])
    user.groups.add(group)

    update_fields = []
    if role == Profile.Role.ADMIN and not user.is_staff:
        user.is_staff = True
        update_fields.append("is_staff")
    if not user.is_active:
        user.is_active = True
        update_fields.append("is_active")
    if update_fields:
        user.save(update_fields=update_fields)


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    extra = 0
    max_num = 1
    fields = ["role", "active", "feishu_open_id"]
    verbose_name = "客户系统员工权限"
    verbose_name_plural = "客户系统员工权限"

    def has_view_permission(self, request, obj=None):
        return _is_account_admin(request.user)

    def has_add_permission(self, request, obj=None):
        return _is_account_admin(request.user)

    def has_change_permission(self, request, obj=None):
        return _is_account_admin(request.user)

    def has_delete_permission(self, request, obj=None):
        return False


try:
    admin.site.unregister(User)
except NotRegistered:
    pass


@admin.register(User)
class CrmUserAdmin(RowEditLinkMixin, DjangoUserAdmin):
    inlines = [ProfileInline]
    list_display = ["username", "full_name", "email", "profile_role", "profile_active", "is_staff", "is_superuser", "is_active", "last_login"]
    list_filter = ["is_active", "is_staff", "is_superuser", "profile__role", "profile__active", "groups"]
    search_fields = ["username", "first_name", "last_name", "email", "profile__feishu_open_id"]
    ordering = ["username"]
    actions = [
        "activate_accounts",
        "deactivate_accounts",
        "allow_admin_login",
        "deny_admin_login",
        "set_role_admin",
        "set_role_leader",
        "set_role_sales",
        "set_role_marketing",
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("profile")

    @admin.display(description="姓名")
    def full_name(self, obj):
        return obj.get_full_name() or "-"

    @admin.display(description="客户系统角色")
    def profile_role(self, obj):
        try:
            return obj.profile.get_role_display()
        except Profile.DoesNotExist:
            return "未设置"

    @admin.display(description="客户系统启用", boolean=True)
    def profile_active(self, obj):
        try:
            return obj.profile.active
        except Profile.DoesNotExist:
            return False

    def has_module_permission(self, request):
        return _is_account_admin(request.user)

    def has_view_permission(self, request, obj=None):
        return _is_account_admin(request.user)

    def has_add_permission(self, request):
        return _is_account_admin(request.user)

    def has_change_permission(self, request, obj=None):
        return _is_account_admin(request.user)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and (obj is None or obj.pk != request.user.pk)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.is_superuser:
            return fieldsets

        hidden_fields = {"is_superuser", "user_permissions"}
        cleaned = []
        for title, options in fieldsets:
            fields = tuple(field for field in options.get("fields", ()) if field not in hidden_fields)
            opts = {**options, "fields": fields}
            cleaned.append((title, opts))
        return cleaned

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            if change and obj.pk:
                original = User.objects.get(pk=obj.pk)
                obj.is_superuser = original.is_superuser
            else:
                obj.is_superuser = False
        super().save_model(request, obj, form, change)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("delete_selected", None)
        return actions

    @admin.action(description="启用所选账号")
    def activate_accounts(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"已启用 {updated} 个账号。")

    @admin.action(description="停用所选账号")
    def deactivate_accounts(self, request, queryset):
        updated = queryset.exclude(pk=request.user.pk).update(is_active=False)
        self.message_user(request, f"已停用 {updated} 个账号；当前登录账号不会被停用。")

    @admin.action(description="允许所选账号进入系统管理")
    def allow_admin_login(self, request, queryset):
        updated = queryset.update(is_staff=True, is_active=True)
        self.message_user(request, f"已允许 {updated} 个账号进入系统管理。")

    @admin.action(description="取消所选账号进入系统管理")
    def deny_admin_login(self, request, queryset):
        queryset = queryset.exclude(pk=request.user.pk).exclude(is_superuser=True)
        updated = queryset.update(is_staff=False)
        self.message_user(request, f"已取消 {updated} 个账号的系统管理入口；当前账号和超级管理员不会被取消。")

    def _set_role_for_queryset(self, request, queryset, role, label):
        count = 0
        for user in queryset:
            _sync_user_role(user, role)
            count += 1
        self.message_user(request, f"已将 {count} 个账号设为{label}。")

    @admin.action(description="设为客户系统管理员")
    def set_role_admin(self, request, queryset):
        self._set_role_for_queryset(request, queryset, Profile.Role.ADMIN, "客户系统管理员")

    @admin.action(description="设为领导")
    def set_role_leader(self, request, queryset):
        self._set_role_for_queryset(request, queryset, Profile.Role.LEADER, "领导")

    @admin.action(description="设为销售")
    def set_role_sales(self, request, queryset):
        self._set_role_for_queryset(request, queryset, Profile.Role.SALES, "销售")

    @admin.action(description="设为新媒体")
    def set_role_marketing(self, request, queryset):
        self._set_role_for_queryset(request, queryset, Profile.Role.MARKETING, "新媒体")


@admin.register(Profile)
class ProfileAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["user", "role", "active"]
    list_editable = ["role", "active"]
    list_filter = ["role", "active"]
    search_fields = ["user__username", "user__first_name", "user__last_name"]


@admin.register(Customer)
class CustomerAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["customer_no", "display_name", "owner", "owner_name", "customer_level", "follow_status", "deal_status", "source_channel", "trade_type", "last_contact_at", "next_follow_at"]
    list_filter = ["customer_level", "follow_status", "deal_status", "trade_type", "grade", "status", "owner", "source_channel", "customer_type", "is_deal", "is_public", "is_recycled"]
    search_fields = ["customer_no", "legacy_customer_no", "lead_no", "name", "nickname", "official_name", "company_name", "contact_name", "main_contact_name", "owner_name", "phone", "wechat", "whatsapp", "email", "related_lead"]
    filter_horizontal = ["tags", "co_owners"]


@admin.register(Lead)
class LeadAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["lead_no", "name", "owner", "status", "source_channel", "created_at"]
    list_filter = ["status", "owner", "source_channel", "trade_type"]
    search_fields = ["lead_no", "name", "customer_name", "raw_nickname", "contact_name", "phone", "wechat", "whatsapp", "email"]
    filter_horizontal = ["tags", "co_owners"]


@admin.register(ContactLog)
class ContactLogAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["customer", "method", "source", "follower_name", "contact_at", "created_by"]
    list_filter = ["method", "source", "created_by", "follower_name"]
    search_fields = ["customer__name", "summary", "result", "minutes_link"]


@admin.register(Contract)
class ContractAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["contract_no", "customer", "customer_name", "signed_by", "sales_user", "signed_date", "contract_amount", "currency", "status", "payment_status"]
    list_filter = ["signed_by", "sales_user", "signed_by_name", "signed_date", "status", "currency"]
    search_fields = ["contract_no", "customer__customer_no", "customer__name", "customer__official_name", "customer_name", "signed_by_name", "work_order_no"]


@admin.register(Reminder)
class ReminderAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["reminder_type", "assignee", "status", "due_at"]
    list_filter = ["reminder_type", "status", "assignee"]
    search_fields = ["message"]


@admin.register(Tag)
class TagAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["name", "category"]
    search_fields = ["name", "category"]


@admin.register(AuditLog)
class AuditLogAdmin(RowEditLinkMixin, admin.ModelAdmin):
    list_display = ["action", "target_type", "target_id", "actor", "created_at"]
    search_fields = ["action", "target_type", "target_id", "detail"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(FeishuSyncSource)
class FeishuSyncSourceAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["name", "source_type", "source_kind", "default_record_kind", "table_id", "sheet_id", "enabled", "last_sync_at"]
    list_filter = ["source_type", "source_kind", "default_record_kind", "enabled"]
    search_fields = ["name", "app_token", "table_id", "sheet_id"]


@admin.register(FeishuSyncRecord)
class FeishuSyncRecordAdmin(RowEditLinkMixin, admin.ModelAdmin):
    list_display = ["source", "record_id", "customer", "contact_log", "contract", "last_seen_at"]
    list_filter = ["source"]
    search_fields = ["record_id", "customer__name", "contract__contract_no"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

@admin.register(Contact)
class ContactAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["customer", "name", "position", "phone", "wechat", "whatsapp", "email", "is_primary"]
    list_filter = ["is_primary", "language"]
    search_fields = ["customer__customer_no", "customer__name", "name", "phone", "wechat", "whatsapp", "email"]


class QuotePlanInline(admin.TabularInline):
    model = QuotePlan
    extra = 0


@admin.register(Quote)
class QuoteAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["quote_no", "customer", "quoted_by", "quote_date", "status", "currency", "total_amount", "valid_until"]
    list_filter = ["status", "currency", "quoted_by", "quote_date"]
    search_fields = ["quote_no", "customer__customer_no", "customer__name", "customer__official_name", "remark"]
    inlines = [QuotePlanInline]


@admin.register(QuotePlan)
class QuotePlanAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["quote", "plan_name", "equipment_model", "capacity", "price", "quantity", "subtotal"]
    search_fields = ["quote__quote_no", "plan_name", "equipment_model", "can_type"]


@admin.register(QuoteItem)
class QuoteItemAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["quote_plan", "item_type", "item_name", "quantity", "unit_price", "subtotal"]
    list_filter = ["item_type"]
    search_fields = ["quote_plan__quote__quote_no", "item_name", "specification"]


@admin.register(Opportunity)
class OpportunityAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["opportunity_no", "customer", "owner", "stage", "expected_amount", "currency", "expected_close_month", "probability", "is_fast_deal", "status"]
    list_filter = ["stage", "status", "owner", "is_fast_deal", "expected_close_month"]
    search_fields = ["opportunity_no", "customer__customer_no", "customer__name", "latest_progress", "next_action"]


@admin.register(SampleTest)
class SampleTestAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["sample_no", "customer", "can_volume", "can_type", "receive_status", "technical_judgement", "test_result", "technician"]
    list_filter = ["receive_status", "technical_judgement", "test_result", "technician"]
    search_fields = ["sample_no", "customer__customer_no", "customer__name", "can_type", "required_changeover_parts"]


@admin.register(Payment)
class PaymentAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["payment_no", "contract", "customer", "payment_stage", "payment_date", "amount", "currency", "bank_fee", "actual_received_amount"]
    list_filter = ["payment_stage", "payment_date", "currency"]
    search_fields = ["payment_no", "contract__contract_no", "customer__customer_no", "customer__name", "payment_account", "remark"]


@admin.register(VisitPlan)
class VisitPlanAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["visit_no", "customer", "country_region", "visit_date", "arrival_time", "arrival_status", "visit_equipment", "status"]
    list_filter = ["visit_date", "arrival_status", "status", "need_car", "need_demo_machine", "need_translator"]
    search_fields = ["visit_no", "customer__customer_no", "customer__name", "country_region", "visit_equipment"]
    filter_horizontal = ["reception_users", "technician_users"]


@admin.register(TaskReminder)
class TaskReminderAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["title", "reminder_type", "assigned_to", "due_at", "status", "priority"]
    list_filter = ["reminder_type", "status", "priority", "assigned_to"]
    search_fields = ["title", "content", "customer__customer_no", "customer__name", "lead__lead_no"]


@admin.register(WorkOrderLink)
class WorkOrderLinkAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["work_order_no", "customer", "contract", "order_date", "production_status", "invoice_status"]
    search_fields = ["work_order_no", "customer__customer_no", "customer__name", "contract__contract_no"]


@admin.register(Attachment)
class AttachmentAdmin(RowEditLinkMixin, NoSalesDeleteMixin, admin.ModelAdmin):
    list_display = ["file_type", "customer", "lead", "quote", "contract", "payment", "uploaded_by", "created_at"]
    list_filter = ["file_type", "uploaded_by", "created_at"]
    search_fields = ["customer__customer_no", "customer__name", "lead__lead_no", "quote__quote_no", "contract__contract_no", "payment__payment_no", "file"]


@admin.register(OperationLog)
class OperationLogAdmin(RowEditLinkMixin, admin.ModelAdmin):
    list_display = ["action_type", "user", "customer", "lead", "created_at", "remark"]
    list_filter = ["action_type", "user", "created_at"]
    search_fields = ["customer__customer_no", "customer__name", "lead__lead_no", "remark"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser