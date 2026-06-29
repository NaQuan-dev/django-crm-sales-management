import os

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from crm.models import Profile


class Command(BaseCommand):
    help = "根据环境变量创建或修复默认管理员账号。"

    def handle(self, *args, **options):
        username = os.getenv("CRM_ADMIN_USERNAME")
        password = os.getenv("CRM_ADMIN_PASSWORD")
        email = os.getenv("CRM_ADMIN_EMAIL", "")
        if not username or not password:
            self.stdout.write("未设置默认管理员账号或密码环境变量，已跳过。")
            return
        user, created = User.objects.get_or_create(username=username, defaults={"email": email, "is_staff": True, "is_superuser": True})
        if created:
            user.set_password(password)
            update_fields = ["password"]
        else:
            update_fields = []
        if email and user.email != email:
            user.email = email
            update_fields.append("email")
        for field in ("is_active", "is_staff", "is_superuser"):
            if not getattr(user, field):
                setattr(user, field, True)
                update_fields.append(field)
        if update_fields:
            user.save(update_fields=sorted(set(update_fields)))
        admin_group, _ = Group.objects.get_or_create(name="管理员")
        user.groups.add(admin_group)
        profile, _ = Profile.objects.get_or_create(user=user)
        if profile.role != Profile.Role.ADMIN:
            profile.role = Profile.Role.ADMIN
            profile.save(update_fields=["role"])
        self.stdout.write(self.style.SUCCESS(f"管理员账号已就绪：{username}"))
