from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "创建客户系统角色组和权限。"

    def handle(self, *args, **options):
        group_names = ["管理员", "领导", "销售", "新媒体", "财务", "技术"]
        for name in group_names:
            Group.objects.get_or_create(name=name)

        admin_group = Group.objects.get(name="管理员")
        leader = Group.objects.get(name="领导")
        sales = Group.objects.get(name="销售")
        marketing = Group.objects.get(name="新媒体")
        finance = Group.objects.get(name="财务")
        technician = Group.objects.get(name="技术")

        def crm_permission(codename):
            return Permission.objects.filter(content_type__app_label="crm", codename=codename).first()

        view_customer = crm_permission("view_customer")
        change_customer = crm_permission("change_customer")
        add_customer = crm_permission("add_customer")
        view_lead = crm_permission("view_lead")
        change_lead = crm_permission("change_lead")
        add_lead = crm_permission("add_lead")
        view_log = crm_permission("view_contactlog")
        change_log = crm_permission("change_contactlog")
        add_log = crm_permission("add_contactlog")
        view_contract = crm_permission("view_contract")
        change_contract = crm_permission("change_contract")
        add_contract = crm_permission("add_contract")

        sales_permissions = [
            view_customer,
            change_customer,
            add_customer,
            view_lead,
            change_lead,
            add_lead,
            view_log,
            change_log,
            add_log,
            view_contract,
            change_contract,
            add_contract,
        ]
        sales.permissions.set([permission for permission in sales_permissions if permission])
        marketing.permissions.set([permission for permission in [view_lead, change_lead, add_lead, view_customer, add_log] if permission])
        finance.permissions.set(
            Permission.objects.filter(
                content_type__app_label="crm",
                content_type__model__in=["contract", "payment", "attachment"],
            )
        )
        technician.permissions.set(
            Permission.objects.filter(
                content_type__app_label="crm",
                content_type__model__in=["sampletest", "visitplan", "attachment", "workorderlink"],
            )
        )
        leader.permissions.set(Permission.objects.filter(content_type__app_label="crm"))
        admin_group.permissions.set(Permission.objects.all())

        self.stdout.write(self.style.SUCCESS("客户系统角色组已就绪。"))
