import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Tag",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=64, unique=True)),
                ("category", models.CharField(blank=True, max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["category", "name"],
            },
        ),
        migrations.CreateModel(
            name="Profile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("admin", "管理员"),
                            ("leader", "领导"),
                            ("sales", "销售"),
                            ("marketing", "新媒体"),
                        ],
                        default="sales",
                        max_length=20,
                    ),
                ),
                ("feishu_open_id", models.CharField(blank=True, max_length=128)),
                ("active", models.BooleanField(default=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Customer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("customer_no", models.CharField(blank=True, max_length=32, unique=True)),
                ("name", models.CharField(blank=True, max_length=160)),
                ("contact_name", models.CharField(blank=True, max_length=80)),
                ("phone", models.CharField(blank=True, max_length=80)),
                ("wechat", models.CharField(blank=True, max_length=120)),
                ("email", models.CharField(blank=True, max_length=254)),
                ("region", models.CharField(blank=True, max_length=160)),
                ("source_channel", models.CharField(blank=True, max_length=80)),
                ("customer_type", models.CharField(blank=True, max_length=80)),
                ("demand", models.CharField(blank=True, max_length=160)),
                (
                    "grade",
                    models.CharField(
                        choices=[
                            ("incubating", "待孵化"),
                            ("potential", "潜在"),
                            ("normal", "一般"),
                            ("intention", "意向"),
                            ("key", "重点"),
                            ("uncertain", "待定"),
                            ("deal", "成交"),
                            ("invalid", "无效"),
                        ],
                        default="potential",
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("private", "私有客户"),
                            ("public", "公海客户"),
                            ("invalid", "无效客户"),
                            ("deal", "成交客户"),
                        ],
                        default="private",
                        max_length=20,
                    ),
                ),
                ("notes", models.TextField(blank=True)),
                ("last_contact_at", models.DateTimeField(blank=True, null=True)),
                ("next_contact_at", models.DateTimeField(blank=True, null=True)),
                ("release_warned_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_customers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="customers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("tags", models.ManyToManyField(blank=True, related_name="customers", to="crm.tag")),
            ],
            options={
                "ordering": ["-updated_at"],
                "permissions": [
                    ("view_all_customers", "Can view all customers"),
                    ("assign_customer", "Can assign customers"),
                ],
            },
        ),
        migrations.CreateModel(
            name="Lead",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("lead_no", models.CharField(blank=True, max_length=32, unique=True)),
                ("name", models.CharField(blank=True, max_length=160)),
                ("contact_name", models.CharField(blank=True, max_length=80)),
                ("phone", models.CharField(blank=True, max_length=80)),
                ("wechat", models.CharField(blank=True, max_length=120)),
                ("email", models.CharField(blank=True, max_length=254)),
                ("region", models.CharField(blank=True, max_length=160)),
                ("source_channel", models.CharField(blank=True, max_length=80)),
                ("customer_type", models.CharField(blank=True, max_length=80)),
                ("demand", models.CharField(blank=True, max_length=160)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("new", "待确认"),
                            ("assigned", "已分配"),
                            ("converted", "已转客户"),
                            ("duplicate", "重复"),
                            ("invalid", "无效"),
                        ],
                        default="new",
                        max_length=20,
                    ),
                ),
                ("notes", models.TextField(blank=True)),
                ("next_contact_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_leads",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="leads",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "related_customer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="leads",
                        to="crm.customer",
                    ),
                ),
                ("tags", models.ManyToManyField(blank=True, related_name="leads", to="crm.tag")),
            ],
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    ("view_all_leads", "Can view all leads"),
                    ("assign_lead", "Can assign leads"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ContactLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("contact_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "method",
                    models.CharField(
                        choices=[
                            ("phone", "电话"),
                            ("wechat", "微信"),
                            ("email", "邮件"),
                            ("visit", "拜访"),
                            ("other", "其他"),
                        ],
                        default="wechat",
                        max_length=20,
                    ),
                ),
                ("summary", models.TextField()),
                ("result", models.CharField(blank=True, max_length=160)),
                ("next_contact_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="contact_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="contact_logs",
                        to="crm.customer",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-contact_at"],
            },
        ),
        migrations.CreateModel(
            name="Reminder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "reminder_type",
                    models.CharField(
                        choices=[
                            ("next_contact", "到期跟进"),
                            ("public_pool_warning", "公海释放提醒"),
                            ("public_pool_released", "已进入公海"),
                        ],
                        max_length=32,
                    ),
                ),
                ("due_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("message", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "待提醒"),
                            ("sent", "已发送"),
                            ("done", "已完成"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "assignee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reminders",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reminders",
                        to="crm.customer",
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reminders",
                        to="crm.lead",
                    ),
                ),
            ],
            options={
                "ordering": ["due_at"],
            },
        ),
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=80)),
                ("target_type", models.CharField(max_length=40)),
                ("target_id", models.CharField(max_length=40)),
                ("detail", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
