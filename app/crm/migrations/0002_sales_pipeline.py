import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="employee_no",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="profile",
            name="responsible_region",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="profile",
            name="manager",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="managed_sales_profiles",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="original_name",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="customer",
            name="contact_position",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="customer",
            name="industry",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="customer",
            name="historical_created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="is_deal",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="lead",
            name="duplicate_checked",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="lead",
            name="duplicate_customer_no",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="lead",
            name="original_assigned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="Opportunity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("opportunity_no", models.CharField(blank=True, max_length=32, unique=True)),
                ("name", models.CharField(blank=True, max_length=160)),
                ("value", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                (
                    "stage",
                    models.CharField(
                        choices=[
                            ("initial", "初步沟通"),
                            ("qualified", "需求确认"),
                            ("quoting", "报价中"),
                            ("proposal", "方案设计"),
                            ("negotiating", "商务谈判"),
                            ("won", "已成交"),
                            ("lost", "已丢单"),
                            ("paused", "暂缓"),
                        ],
                        default="initial",
                        max_length=24,
                    ),
                ),
                ("expected_close_date", models.DateField(blank=True, null=True)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_opportunities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="opportunities",
                        to="crm.customer",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="opportunities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at"],
                "permissions": [
                    ("view_all_opportunities", "Can view all opportunities"),
                    ("assign_opportunity", "Can assign opportunities"),
                ],
            },
        ),
        migrations.CreateModel(
            name="Contract",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("contract_no", models.CharField(blank=True, max_length=32, unique=True)),
                ("signed_date", models.DateField(blank=True, null=True)),
                ("amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("attachment_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_contracts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="contracts",
                        to="crm.customer",
                    ),
                ),
                (
                    "opportunity",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="contracts",
                        to="crm.opportunity",
                    ),
                ),
                (
                    "signed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="signed_contracts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-signed_date", "-updated_at"],
                "permissions": [
                    ("view_all_contracts", "Can view all contracts"),
                ],
            },
        ),
    ]
