import json
import os
import shutil
import tempfile
from unittest.mock import patch
from decimal import Decimal
from datetime import datetime, timedelta
from io import StringIO
from types import SimpleNamespace

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db import OperationalError, connection
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from config.middleware import NormalizeUnderscoreHostMiddleware

from .management.commands.fill_eoffice_account_source import Command as FillEofficeAccountSourceCommand
from .management.commands.import_feishu_crm_export import Command as ImportCustomerCommand
from .management.commands.import_eoffice_customers import Command as ImportEofficeCustomerCommand
from .management.commands.import_eoffice_contact_logs import Command as ImportEofficeContactLogCommand
from .management.commands.normalize_source_channels import Command as NormalizeSourceChannelsCommand
from .management.commands.repair_customer_owner_and_source import Command as RepairCustomerCommand
from .management.commands.restore_detailed_source_channels import Command as RestoreDetailedSourceChannelsCommand
from .feishu_sync import CUSTOMER_FIELD_MAP, checksum_fields, is_disabled_line_management_source, mapped_fields, ordered_records_for_source, parse_dt, record_checksum, sync_customer_record
from .forms import ContactLogForm, ContractForm
from .models import AuditLog, ContactLog, Contract, Customer, FeishuSyncRecord, FeishuSyncSource, Lead, OperationLog, Payment, PHONE_PREFIX_CHOICES, Profile, Quote, Reminder, Tag, TaskReminder, normalize_phone_number_for_region
from .options import canonical_customer_statuses
from .views import _apply_customer_filters, _customer_needs_followup_q, _customer_owner_options, _owner_filter_value, _read_csv_rows, _row_to_customer_import_values, _run_customer_import
from .services import customer_queryset_for, is_public_pool_customer, public_pool_customer_q, release_stale_customers, run_daily_rules, update_customer_after_contact
from .signals import ensure_profile


class ProfileSignalTests(SimpleTestCase):
    def test_profile_schema_issue_does_not_block_user_creation(self):
        user = User(username="schema-late")

        with patch("crm.signals.Profile.objects.get_or_create", side_effect=OperationalError("missing table")):
            ensure_profile(sender=User, instance=user, created=True)


class FeishuInquirySyncTests(SimpleTestCase):
    def test_inquiry_source_is_enabled_while_legacy_line_management_stays_disabled(self):
        self.assertFalse(is_disabled_line_management_source("示例线索表-询盘", "MCW2Lm"))
        self.assertTrue(is_disabled_line_management_source("线索管理", "tbloyS8y6swVRgOx"))
        self.assertTrue(is_disabled_line_management_source("table_线索管理_tbloyS8y6swVRgOx", "tbloyS8y6swVRgOx"))

    def test_inquiry_sheet_contact_column_maps_to_owner_name(self):
        source = SimpleNamespace(
            source_type=FeishuSyncSource.SourceType.SHEET,
            name="示例线索表-询盘",
            field_mapping={},
        )

        values = mapped_fields({"用户名": "测试客户", "联系人": "销售A"}, source, CUSTOMER_FIELD_MAP)

        self.assertEqual(values["name"], "测试客户")
        self.assertEqual(values["owner_name"], "销售A")

    def test_date_only_values_default_to_8am(self):
        dt = timezone.localtime(parse_dt("2024-01-02"))

        self.assertEqual(dt.date().isoformat(), "2024-01-02")
        self.assertEqual(dt.hour, 8)
        self.assertEqual(dt.minute, 0)

    def test_datetime_values_keep_explicit_time(self):
        dt = timezone.localtime(parse_dt("2024-01-02 09:30:00"))

        self.assertEqual(dt.date().isoformat(), "2024-01-02")
        self.assertEqual(dt.hour, 9)
        self.assertEqual(dt.minute, 30)

    def test_inquiry_checksum_includes_sync_logic_version(self):
        source = SimpleNamespace(
            source_type=FeishuSyncSource.SourceType.SHEET,
            name="示例线索表-询盘",
        )
        raw_fields = {"用户名": "测试客户", "电话": "13812345678"}

        self.assertNotEqual(record_checksum(source, raw_fields), checksum_fields(raw_fields))

    def test_inquiry_records_are_processed_by_descending_sequence(self):
        source = SimpleNamespace(
            source_type=FeishuSyncSource.SourceType.SHEET,
            name="示例线索表-询盘",
        )
        records = [
            {"record_id": "MCW2Lm:8", "row_index": 8, "fields": {"序号": "8"}},
            {"record_id": "MCW2Lm:2", "row_index": 2, "fields": {"序号": "2"}},
            {"record_id": "MCW2Lm:5", "row_index": 5, "fields": {"序号": "5"}},
        ]

        ordered = ordered_records_for_source(source, records)

        self.assertEqual([record["record_id"] for record in ordered], ["MCW2Lm:8", "MCW2Lm:5", "MCW2Lm:2"])


class FeishuInquirySyncDbTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Profile, Customer, Lead, ContactLog, Contract, Reminder, FeishuSyncSource, FeishuSyncRecord, AuditLog):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (AuditLog, FeishuSyncRecord, FeishuSyncSource, Reminder, Contract, ContactLog, Lead, Customer, Profile, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_inquiry_sync_merges_existing_customer_by_phone_and_cleans_fields(self):
        existing = Customer.objects.create(
            customer_no="NQKH000123",
            name="旧客户",
            phone="+86 13812345678",
            source_channel="老来源",
        )
        source = FeishuSyncSource.objects.create(
            name="示例线索表-询盘",
            source_type=FeishuSyncSource.SourceType.SHEET,
            app_token="spreadsheet-token",
            table_id="MCW2Lm",
            sheet_id="MCW2Lm",
            source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
            default_record_kind=Customer.RecordKind.LEAD,
        )

        with patch("crm.feishu_sync.timezone.now", return_value=timezone.make_aware(datetime(2026, 6, 24, 15, 52))):
            sync_record = sync_customer_record(
                source,
                {
                    "record_id": "MCW2Lm:2",
                    "fields": {
                        "序号": "2",
                        "用户名": "询盘客户",
                        "电话": "13812345678",
                        "获客渠道": "抖音账号A",
                        "联系人": "销售A",
                        "创建时间": "2024-01-02",
                        "分配时间": "2024/01/03",
                    },
                },
            )

        existing.refresh_from_db()
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(sync_record.customer_id, existing.id)
        self.assertEqual(existing.name, "询盘客户")
        self.assertEqual(existing.phone, "+86 13812345678")
        self.assertEqual(existing.source_channel, "抖音账号A")
        self.assertEqual(existing.owner_name, "销售A")
        self.assertTrue(existing.duplicate_checked)
        self.assertEqual(existing.duplicate_customer_no, "NQKH000123")
        self.assertEqual(timezone.localtime(existing.historical_created_at).date().isoformat(), "2024-01-03")
        self.assertEqual(timezone.localtime(existing.historical_created_at).hour, 15)
        self.assertEqual(timezone.localtime(existing.historical_created_at).minute, 52)
        self.assertEqual(timezone.localtime(existing.original_assigned_at).hour, 15)
        self.assertEqual(timezone.localtime(existing.original_assigned_at).minute, 52)

    def test_inquiry_assignment_update_claims_public_customer_for_owner(self):
        sales = User.objects.create_user(username="sales-a", first_name="销售A")
        existing = Customer.objects.create(
            customer_no="NQKH000124",
            name="公海旧客户",
            phone="+86 13912345678",
            status=Customer.Status.PUBLIC,
        )
        source = FeishuSyncSource.objects.create(
            name="示例线索表-询盘",
            source_type=FeishuSyncSource.SourceType.SHEET,
            app_token="spreadsheet-token",
            table_id="MCW2Lm",
            sheet_id="MCW2Lm",
            source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
            default_record_kind=Customer.RecordKind.LEAD,
        )

        sync_customer_record(
            source,
            {
                "record_id": "MCW2Lm:row-3",
                "fields": {
                    "用户名": "已分配询盘客户",
                    "电话": "13912345678",
                    "联系人": "销售A",
                },
            },
        )

        existing.refresh_from_db()
        self.assertEqual(existing.status, Customer.Status.PRIVATE)
        self.assertEqual(existing.owner_id, sales.id)
        self.assertEqual(existing.owner_name, "销售A")

    def test_older_inquiry_duplicate_does_not_overwrite_newer_sequence(self):
        source = FeishuSyncSource.objects.create(
            name="示例线索表-询盘",
            source_type=FeishuSyncSource.SourceType.SHEET,
            app_token="spreadsheet-token",
            table_id="MCW2Lm",
            sheet_id="MCW2Lm",
            source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
            default_record_kind=Customer.RecordKind.LEAD,
        )

        sync_customer_record(
            source,
            {
                "record_id": "MCW2Lm:10",
                "fields": {
                    "序号": "10",
                    "用户名": "最新询盘客户",
                    "电话": "13812345678",
                    "获客渠道": "抖音账号A",
                },
            },
        )
        sync_customer_record(
            source,
            {
                "record_id": "MCW2Lm:2",
                "fields": {
                    "序号": "2",
                    "用户名": "旧询盘客户",
                    "电话": "13812345678",
                    "获客渠道": "脸书",
                },
            },
        )

        customer = Customer.objects.get()
        self.assertEqual(customer.name, "最新询盘客户")
        self.assertEqual(customer.source_channel, "抖音账号A")
        self.assertEqual(customer.feishu_record_id, "MCW2Lm:10")
        self.assertEqual(FeishuSyncRecord.objects.filter(customer=customer).count(), 2)

    def test_inquiry_row_with_only_sequence_does_not_create_blank_customer(self):
        source = FeishuSyncSource.objects.create(
            name="示例线索表-询盘",
            source_type=FeishuSyncSource.SourceType.SHEET,
            app_token="spreadsheet-token",
            table_id="MCW2Lm",
            sheet_id="MCW2Lm",
            source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
            default_record_kind=Customer.RecordKind.LEAD,
        )

        sync_record = sync_customer_record(
            source,
            {
                "record_id": "MCW2Lm:88",
                "fields": {"序号": "88"},
            },
        )

        self.assertEqual(Customer.objects.count(), 0)
        self.assertIsNone(sync_record.customer_id)
        self.assertEqual(sync_record.raw_fields, {"序号": "88"})

    def test_inquiry_sync_truncates_overlong_char_fields(self):
        source = FeishuSyncSource.objects.create(
            name="示例线索表-询盘",
            source_type=FeishuSyncSource.SourceType.SHEET,
            app_token="spreadsheet-token",
            table_id="MCW2Lm",
            sheet_id="MCW2Lm",
            source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
            default_record_kind=Customer.RecordKind.LEAD,
        )

        sync_customer_record(
            source,
            {
                "record_id": "MCW2Lm:10",
                "fields": {
                    "序号": "10",
                    "用户名": "超长字段客户",
                    "电话": "13812345678",
                    "联系人": "销售" * 80,
                    "微信": "wx" * 80,
                    "获客渠道": "渠道" * 60,
                },
            },
        )

        customer = Customer.objects.get()
        self.assertLessEqual(len(customer.owner_name), 120)
        self.assertLessEqual(len(customer.wechat), 120)
        self.assertLessEqual(len(customer.source_channel), 80)

    def test_reset_inquiry_sync_deletes_only_inquiry_created_customers(self):
        source = FeishuSyncSource.objects.create(
            name="示例线索表-询盘",
            source_type=FeishuSyncSource.SourceType.SHEET,
            app_token="spreadsheet-token",
            table_id="MCW2Lm",
            sheet_id="MCW2Lm",
            source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
            default_record_kind=Customer.RecordKind.LEAD,
        )
        created_customer = Customer.objects.create(
            name="询盘新建客户",
            phone="+86 13812345678",
            feishu_source_name=source.name,
            feishu_app_token=source.app_token,
            feishu_table_id=source.table_id,
            feishu_record_id="MCW2Lm:10",
        )
        preserved_customer = Customer.objects.create(
            name="eoffice 老客户",
            phone="+86 13912345678",
            feishu_source_name=source.name,
            feishu_app_token=source.app_token,
            feishu_table_id=source.table_id,
            feishu_record_id="MCW2Lm:11",
        )
        FeishuSyncRecord.objects.create(source=source, record_id="MCW2Lm:10", customer=created_customer)
        FeishuSyncRecord.objects.create(source=source, record_id="MCW2Lm:11", customer=preserved_customer)
        Reminder.objects.create(
            customer=created_customer,
            reminder_type=Reminder.ReminderType.UNCONTACTED_RULE,
            message="询盘客户旧提醒",
        )
        AuditLog.objects.create(
            action="飞书同步新增客户",
            target_type="客户",
            target_id=str(created_customer.pk),
            detail=f"{source.name}:MCW2Lm:10",
        )
        AuditLog.objects.create(
            action="飞书同步更新客户",
            target_type="客户",
            target_id=str(preserved_customer.pk),
            detail=f"{source.name}:MCW2Lm:11",
        )

        call_command("reset_feishu_inquiry_sync", "--confirm", stdout=StringIO())

        self.assertFalse(Customer.objects.filter(pk=created_customer.pk).exists())
        preserved_customer.refresh_from_db()
        self.assertEqual(preserved_customer.name, "eoffice 老客户")
        self.assertEqual(preserved_customer.feishu_record_id, "")
        self.assertEqual(FeishuSyncRecord.objects.filter(source=source).count(), 0)


class HostnameCompatibilityTests(SimpleTestCase):
    def test_underscore_nas_hostname_is_rewritten_before_django_host_validation(self):
        request = RequestFactory().get("/", HTTP_HOST="CRM_NAS:8080")
        middleware = NormalizeUnderscoreHostMiddleware(lambda req: req.META["HTTP_HOST"])

        with patch.dict(os.environ, {"DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,192.168.1.66,CRM_NAS"}):
            rewritten_host = middleware(request)

        self.assertEqual(rewritten_host, "192.168.1.66:8080")


class AdminAccountManagementTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if Profile._meta.db_table not in existing_tables:
                schema_editor.create_model(Profile)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if Profile._meta.db_table in existing_tables:
                pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_default_admin_command_promotes_existing_account(self):
        user = User.objects.create_user(username="admin", password="old", is_staff=False, is_superuser=False)
        Profile.objects.filter(user=user).delete()
        out = StringIO()

        with patch.dict(os.environ, {"CRM_ADMIN_USERNAME": "admin", "CRM_ADMIN_PASSWORD": "new-pass", "CRM_ADMIN_EMAIL": "admin@example.com"}):
            call_command("create_default_admin", stdout=out)

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.email, "admin@example.com")
        self.assertTrue(user.groups.filter(name="管理员").exists())
        self.assertEqual(user.profile.role, Profile.Role.ADMIN)

    def test_crm_admin_role_can_manage_users_in_admin(self):
        user = User.objects.create_user(username="crm-admin", password="x", is_staff=True)
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.role = Profile.Role.ADMIN
        profile.active = True
        profile.save(update_fields=["role", "active"])
        request = RequestFactory().get("/admin/auth/user/")
        request.user = user
        model_admin = django_admin.site._registry[User]

        self.assertTrue(model_admin.has_view_permission(request))
        self.assertTrue(model_admin.has_add_permission(request))
        self.assertTrue(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_user_admin_has_clear_edit_column_and_bulk_actions(self):
        user = User.objects.create_superuser(username="admin-actions", password="x")
        request = RequestFactory().get("/admin/auth/user/")
        request.user = user
        model_admin = django_admin.site._registry[User]
        actions = model_admin.get_actions(request)

        self.assertIn("edit_link", model_admin.get_list_display(request))
        self.assertIn("activate_accounts", actions)
        self.assertIn("set_role_admin", actions)
        self.assertIn("set_role_leader", actions)
        self.assertIn("set_role_sales", actions)
        self.assertIn("set_role_marketing", actions)

    def test_user_admin_role_action_updates_profile_and_group(self):
        admin_user = User.objects.create_superuser(username="admin-role-action", password="x")
        target = User.objects.create_user(username="role-target", password="x")
        request = RequestFactory().post("/admin/auth/user/")
        request.user = admin_user
        model_admin = django_admin.site._registry[User]

        with patch.object(model_admin, "message_user"):
            model_admin.set_role_leader(request, User.objects.filter(pk=target.pk))

        target.refresh_from_db()
        self.assertEqual(target.profile.role, Profile.Role.LEADER)
        self.assertTrue(target.profile.active)
        self.assertTrue(target.groups.filter(name="领导").exists())


class LocalizeSystemTextCommandTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Customer, Lead, AuditLog):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (AuditLog, Lead, Customer, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_translates_system_values_without_changing_customer_names(self):
        customer = Customer.objects.create(name="English Customer Name", source_channel="ins", account_source="Facebook")
        lead = Lead.objects.create(name="Another English Name", source_channel="fb")
        log = AuditLog.objects.create(
            action="release_to_public_pool",
            target_type="customer",
            target_id="1",
            detail="Released from owner OY002 after more than 30 days no contact.",
        )

        call_command("localize_system_text", stdout=StringIO())

        customer.refresh_from_db()
        lead.refresh_from_db()
        log.refresh_from_db()
        self.assertEqual(customer.name, "English Customer Name")
        self.assertEqual(lead.name, "Another English Name")
        self.assertEqual(customer.source_channel, "ins")
        self.assertEqual(customer.account_source, "脸书")
        self.assertEqual(lead.source_channel, "脸书")
        self.assertEqual(log.action, "自动移入公海")
        self.assertEqual(log.target_type, "客户")
        self.assertEqual(log.detail, "原负责人 OY002 超过 30 天未联系，系统自动移入公海。")


class RepairCustomerCommandTests(SimpleTestCase):
    def test_grade_code_accepts_feishu_grade_labels(self):
        command = RepairCustomerCommand()
        self.assertEqual(command._grade_code("重点客户"), Customer.Grade.KEY)
        self.assertEqual(command._grade_code("意向客户"), Customer.Grade.INTENTION)
        self.assertEqual(command._grade_code("无效客户"), Customer.Grade.INVALID)

    def test_profile_fields_accept_feishu_labels(self):
        command = RepairCustomerCommand()
        self.assertEqual(command._customer_type_value("贸易商"), "贸易商")
        self.assertEqual(command._demand_value("8-2A灌封一体机"), "8-2A灌封一体机")

    def test_name_aliases_cover_old_customer_name_formats(self):
        command = RepairCustomerCommand()
        aliases = command._name_aliases("1 测试客户-美国")
        self.assertIn("测试客户-美国", aliases)
        self.assertIn("测试客户", aliases)
        self.assertEqual(command._normalized_name("1 测试客户-美国"), "测试客户")

    def test_customer_number_matches_across_number_fields(self):
        command = RepairCustomerCommand()
        export_values = {"record_id": "rec1", "legacy_customer_no": "OA-001", "grade": Customer.Grade.KEY}
        bundle = {"records_by_key": {}, "ambiguous_keys": set()}
        for key in command._export_keys(export_values):
            command._add_export_key(bundle, key, export_values)

        customer = Customer(customer_no="OA-001")
        matched = command._match_export_customer(customer, bundle)

        self.assertEqual(matched.get("grade"), Customer.Grade.KEY)
        self.assertTrue(matched.get("_matched_by_customer_no"))

    def test_customer_with_number_does_not_fallback_to_name(self):
        command = RepairCustomerCommand()
        bundle = {
            "records_by_key": {"name:测试客户": {"record_id": "rec1", "grade": Customer.Grade.KEY}},
            "ambiguous_keys": set(),
        }
        customer = Customer(customer_no="OA-404", name="测试客户")

        self.assertEqual(command._match_export_customer(customer, bundle), {})



class CustomerNumberTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if Tag._meta.db_table not in existing_tables:
                schema_editor.create_model(Tag)
            if Customer._meta.db_table not in existing_tables:
                schema_editor.create_model(Customer)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if Customer._meta.db_table in existing_tables:
                pass  # 表由 migrations 管理，测试结束不手动删表
            if Tag._meta.db_table in existing_tables:
                pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()
    def test_new_customer_number_uses_nqkh_sequence(self):
        Customer.objects.create(name="已有客户", customer_no="NQKH000009")

        customer = Customer.objects.create(name="新客户")

        self.assertEqual(customer.customer_no, "NQKH000010")

    def test_renumber_command_rewrites_numbers_and_preserves_legacy(self):
        first = Customer.objects.create(name="一号客户", customer_no="OA-001")
        second = Customer.objects.create(name="二号客户", customer_no="KH-20260617")
        out = StringIO()

        call_command("renumber_customer_numbers", stdout=out)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.customer_no, "NQKH000001")
        self.assertEqual(second.customer_no, "NQKH000002")
        self.assertEqual(first.legacy_customer_no, "OA-001")
        self.assertEqual(second.legacy_customer_no, "KH-20260617")
        self.assertIn("客户编号重编完成：2 条", out.getvalue())

class ImportCustomerCommandTests(SimpleTestCase):
    def test_old_customer_number_becomes_legacy_customer_number(self):
        command = ImportCustomerCommand()

        values = command._row_to_values(
            {"客户编号": "OLD-001", "系统客户编号": "KH-20260617", "客户名称": "测试客户"},
            Customer.RecordKind.CUSTOMER,
            {},
            None,
        )

        self.assertEqual(values["customer_no"], "")
        self.assertEqual(values["legacy_customer_no"], "OLD-001")

    def test_import_uses_only_nqkh_as_system_customer_number(self):
        command = ImportCustomerCommand()

        system_values = command._row_to_values(
            {"系统客户编号": "NQKH000017", "客户名称": "测试客户"},
            Customer.RecordKind.CUSTOMER,
            {},
            None,
        )
        old_system_values = command._row_to_values(
            {"系统客户编号": "KH-20260617", "客户名称": "测试客户"},
            Customer.RecordKind.CUSTOMER,
            {},
            None,
        )
        lead_values = command._row_to_values(
            {"线索编号": "XS-001", "客户名称": "测试线索"},
            Customer.RecordKind.LEAD,
            {},
            None,
        )

        self.assertEqual(system_values["customer_no"], "NQKH000017")
        self.assertEqual(old_system_values["customer_no"], "")
        self.assertEqual(old_system_values["legacy_customer_no"], "KH-20260617")
        self.assertEqual(lead_values["customer_no"], "")
        self.assertEqual(lead_values["lead_no"], "XS-001")

class ImportEofficeCustomerCommandTests(SimpleTestCase):
    def test_eoffice_row_maps_customer_fields_by_customer_number(self):
        command = ImportEofficeCustomerCommand()

        values = command._row_to_values(
            {
                "客户编号": "OA-001",
                "客户名称": "测试客户",
                "线索来源": "抖音号账号A",
                "城市": "上海",
                "客户电话": "13812345678",
                "客户级别": "重点",
                "客户需求": "8-2A灌封一体机",
                "客户状态": "未报价",
                "联系人": "客户联系人",
                "创建人": "创建员工",
                "创建时间": "2024-01-02 09:30:00",
            }
        )

        self.assertEqual(values["customer_no"], "")
        self.assertEqual(values["legacy_customer_no"], "OA-001")
        self.assertEqual(values["source_channel"], "抖音账号A")
        self.assertEqual(values["region"], "上海")
        self.assertEqual(values["phone"], "+86 13812345678")
        self.assertEqual(values["grade"], Customer.Grade.KEY)
        self.assertEqual(values["demand"], "8-2A灌封一体机")
        self.assertEqual(values["customer_status_text"], "未报价")
        self.assertEqual(values["contact_name"], "客户联系人")
        self.assertEqual(values["created_by_name"], "创建员工")
        self.assertNotIn("owner_name", values)

    def test_eoffice_account_source_aliases_map_to_source_channel(self):
        command = ImportEofficeCustomerCommand()

        account_values = command._row_to_values({"客户编号": "OA-001", "账号来源": "抖音号账号B"})
        alternate_account_values = command._row_to_values({"客户编号": "OA-002", "账户来源": "Instagram"})

        self.assertEqual(account_values["source_channel"], "抖音账号B")
        self.assertEqual(alternate_account_values["source_channel"], "ins")

    def test_eoffice_grade_labels_with_level_suffix_are_supported(self):
        command = ImportEofficeCustomerCommand()

        self.assertEqual(command._grade_code("待孵化客户（1级）"), Customer.Grade.INCUBATING)
        self.assertEqual(command._grade_code("潜在客户（2级）"), Customer.Grade.POTENTIAL)
        self.assertEqual(command._grade_code("重点客户（5级）"), Customer.Grade.KEY)
        self.assertEqual(command._grade_code("无效客户(8级）"), Customer.Grade.INVALID)
        self.assertEqual(command._grade_code("成交客户（6级）"), Customer.Grade.KEY)

    def test_eoffice_customer_status_text_maps_to_multiple_tags(self):
        self.assertEqual(canonical_customer_statuses("已加微信，未报价"), "已加联系方式,未报价")
        self.assertEqual(canonical_customer_statuses("已加Whatsapp，已报价"), "已加联系方式,已报价")
        self.assertEqual(canonical_customer_statuses("未加微信，已报价"), "未加联系方式,已报价")
        self.assertEqual(canonical_customer_statuses("待到访"), "待拜访")
        self.assertEqual(canonical_customer_statuses("合同已签待预付"), "合同已签待预付")

    def test_eoffice_order_status_marks_customer_as_deal(self):
        command = ImportEofficeCustomerCommand()

        values = command._row_to_values(
            {
                "客户编号": "OA-008",
                "客户名称": "成交客户",
                "客户状态": "已下单",
            }
        )

        self.assertEqual(values["customer_status_text"], "已下单")
        self.assertTrue(values["is_deal"])
        self.assertEqual(values["status"], Customer.Status.DEAL)

    def test_eoffice_customer_name_contact_info_moves_to_contact_fields(self):
        command = ImportEofficeCustomerCommand()

        values = command._row_to_values(
            {
                "客户编号": "OA-004",
                "客户名称": "测试客户 电话 +995599016869 微信 alexhuang1986",
                "地区": "中国",
                "客户电话": "+8613812345678",
                "微信": "oldwx",
            }
        )

        self.assertEqual(values["name"], "测试客户")
        self.assertIn("+995 599016869", values["phone"])
        self.assertIn("+86 13812345678", values["phone"])
        self.assertIn("oldwx", values["wechat"])
        self.assertIn("alexhuang1986", values["wechat"])

    def test_eoffice_customer_name_keeps_english_surname_starting_with_v(self):
        command = ImportEofficeCustomerCommand()

        values = command._row_to_values(
            {
                "客户编号": "OA-005",
                "客户名称": "Jean Carlo Vasconez-厄瓜多尔",
                "客户电话": "+593 995497794",
            }
        )

        self.assertEqual(values["name"], "Jean Carlo Vasconez")
        self.assertEqual(values["region"], "厄瓜多尔")
        self.assertEqual(values["wechat"], "")

    def test_eoffice_customer_name_region_suffix_moves_to_region(self):
        command = ImportEofficeCustomerCommand()

        values = command._row_to_values(
            {
                "客户编号": "OA-007",
                "客户名称": "Toxtle Taproom-墨西哥",
                "客户电话": "+522464648321",
            }
        )

        self.assertEqual(values["name"], "Toxtle Taproom")
        self.assertEqual(values["region"], "墨西哥")
        self.assertEqual(values["phone"], "+52 2464648321")

    def test_eoffice_customer_name_without_contact_keeps_punctuation(self):
        command = ImportEofficeCustomerCommand()

        values = command._row_to_values(
            {
                "客户编号": "OA-006",
                "客户名称": "2资深经验，办理各国签证，国际机票",
            }
        )

        self.assertEqual(values["name"], "2资深经验，办理各国签证，国际机票")

    def test_eoffice_customer_type_and_demand_from_current_sheet_are_supported(self):
        command = ImportEofficeCustomerCommand()

        values = command._row_to_values(
            {
                "客户编号": "OA-002",
                "客户名称": "测试客户",
                "客户类型": "不含气饮料",
                "客户需求": "6-1 易拉罐灌封一体机",
            }
        )

        self.assertEqual(values["customer_type"], "不含气饮料")
        self.assertEqual(values["demand"], "6-1易拉罐灌封一体机")

    def test_eoffice_row_maps_core_customer_timeline_and_notes(self):
        command = ImportEofficeCustomerCommand()

        values = command._row_to_values(
            {
                "客户编号": "OA-003",
                "客户名称": "测试客户",
                "地区": "美国",
                "城市": "Atlanta",
                "沟通记录": "已沟通设备需求",
                "客户状态": "未报价,待拜访",
                "创建时间": "2024-01-02 09:30:00",
                "最后联系时间": "2024-01-03 10:00:00",
                "下次联系时间": "2024-01-10",
            }
        )

        self.assertEqual(values["region"], "美国 Atlanta")
        self.assertEqual(values["notes"], "已沟通设备需求")
        self.assertEqual(values["customer_status_text"], "未报价,待拜访")
        self.assertEqual(timezone.localtime(values["historical_created_at"]).date().isoformat(), "2024-01-02")
        self.assertEqual(timezone.localtime(values["last_contact_at"]).date().isoformat(), "2024-01-03")
        self.assertEqual(timezone.localtime(values["next_contact_at"]).date().isoformat(), "2024-01-10")


class CustomerDueQuickFilterTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if Tag._meta.db_table not in existing_tables:
                schema_editor.create_model(Tag)
            if Customer._meta.db_table not in existing_tables:
                schema_editor.create_model(Customer)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if Customer._meta.db_table in existing_tables:
                pass  # 表由 migrations 管理，测试结束不手动删表
            if Tag._meta.db_table in existing_tables:
                pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def _filters(self):
        return {
            "q": "",
            "kind": "",
            "status": "",
            "quick": "due",
            "grades": [],
            "types": [],
            "demands": [],
            "customer_statuses": [],
            "sources": [],
            "owners": [],
            "date_field": "",
            "date_from": "",
            "date_to": "",
            "sort_field": "created",
            "sort_direction": "desc",
        }

    def test_due_quick_filter_means_contacted_status_stale_and_not_deal(self):
        now = timezone.now()
        stale = now - timedelta(days=15)
        recent = now - timedelta(days=13)
        Customer.objects.create(name="应跟进客户", customer_status_text="已加联系方式,未报价", last_contact_at=stale)
        Customer.objects.create(name="未跟进历史客户", customer_status_text="已加联系方式", historical_created_at=stale)
        Customer.objects.create(name="近期跟进客户", customer_status_text="已加联系方式", last_contact_at=recent)
        Customer.objects.create(name="未加联系方式客户", customer_status_text="未报价", last_contact_at=stale)
        Customer.objects.create(name="成交客户", customer_status_text="已加联系方式", last_contact_at=stale, status=Customer.Status.DEAL, is_deal=True)
        Customer.objects.create(name="已下单客户", customer_status_text="已加联系方式,已下单", last_contact_at=stale)

        filtered = _apply_customer_filters(Customer.objects.all(), self._filters(), User(username="sales001"))
        metric_count = Customer.objects.filter(_customer_needs_followup_q(now=now)).count()

        self.assertEqual(set(filtered.values_list("name", flat=True)), {"应跟进客户", "未跟进历史客户"})
        self.assertEqual(metric_count, 2)

    def test_customer_owner_filter_merges_same_display_name(self):
        admin = User.objects.create_superuser(username="owner-filter-admin", password="x")
        owner = User.objects.create_user(username="SALES001")
        Customer.objects.create(name="工号负责人客户", owner=owner, owner_name="SALES001")
        Customer.objects.create(name="姓名负责人客户", owner_name="销售A")
        Customer.objects.create(name="其他负责人客户", owner_name="销售B")

        options = _customer_owner_options(admin)
        labels = [option["label"] for option in options]

        self.assertEqual(labels.count("销售A"), 1)
        filters = self._filters()
        filters["quick"] = ""
        filters["owners"] = [_owner_filter_value("销售A")]
        filtered = _apply_customer_filters(Customer.objects.all(), filters, admin)

        self.assertEqual(set(filtered.values_list("name", flat=True)), {"工号负责人客户", "姓名负责人客户"})

    def test_customer_owner_filter_merges_li_weihua_name_variants(self):
        admin = User.objects.create_superuser(username="owner-filter-li-admin", password="x")
        owner = User.objects.create_user(username="SALES004")
        Customer.objects.create(name="工号销售D客户", owner=owner, owner_name="SALES004")
        Customer.objects.create(name="销售D客户", owner_name="销售D")
        Customer.objects.create(name="销售D旧名客户", owner_name="销售D旧名")

        options = _customer_owner_options(admin)
        labels = [option["label"] for option in options]

        self.assertEqual(labels.count("销售D"), 1)
        self.assertNotIn("销售D旧名", labels)
        filters = self._filters()
        filters["quick"] = ""
        filters["owners"] = [_owner_filter_value("销售D")]
        filtered = _apply_customer_filters(Customer.objects.all(), filters, admin)

        self.assertEqual(set(filtered.values_list("name", flat=True)), {"工号销售D客户", "销售D客户", "销售D旧名客户"})
        self.assertEqual(Customer(owner_name="销售D旧名").owner_display, "销售D")

class CustomerListTemplateTests(SimpleTestCase):
    def _render_customer_list_toolbar(self, q="", filters=None, has_active_filters=False):
        page_obj = Paginator([], 8).get_page(1)
        request = RequestFactory().get("/customers/?scope=my")
        request.user = User(username="sales001")
        request.user.is_staff = False
        request.resolver_match = SimpleNamespace(url_name="customer_list")
        filter_state = {
            "quick": "",
            "grades": [],
            "types": [],
            "demands": [],
            "customer_statuses": [],
            "sources": [],
            "owners": [],
            "date_field": "",
            "date_from": "",
            "date_to": "",
            "sort_field": "created",
            "sort_direction": "desc",
        }
        if filters:
            filter_state.update(filters)
        return render_to_string(
            "crm/customer_list.html",
            {
                "customers": page_obj,
                "page_obj": page_obj,
                "paginator": page_obj.paginator,
                "total_record_count": 0,
                "filtered_record_count": 0,
                "today_new_count": 0,
                "today_due_count": 0,
                "key_customer_count": 0,
                "q": q,
                "kind": "",
                "status": "",
                "scope": "my",
                "filters": filter_state,
                "show_owner_column": True,
                "list_title": "我的客户",
                "kind_choices": Customer.RecordKind.choices,
                "grade_choices": Customer.Grade.choices,
                "customer_type_options": [],
                "demand_options": [],
                "customer_status_options": [],
                "source_options": [],
                "owner_options": [],
                "inline_options": {},
                "can_undo": False,
                "can_redo": False,
                "base_querystring": "scope=my&q=%E6%B5%8B%E8%AF%95&per_page=8" if q else "",
                "clear_querystring": "scope=my&per_page=8",
                "has_active_filters": has_active_filters,
                "page_size": 8,
                "min_page_size": 3,
                "max_page_size": 30,
            },
            request=request,
        )

    def test_search_state_has_clear_link_back_to_full_current_scope(self):
        html = self._render_customer_list_toolbar(q="测试客户", has_active_filters=True)

        self.assertIn('class="button secondary clear-search-link" href="/customers/?scope=my&amp;per_page=8"', html)
        self.assertIn(">查看全部</a>", html)
        self.assertIn('class="quick-filter " href="/customers/?scope=my&amp;per_page=8">全部</a>', html)
        self.assertNotIn('href="/customers/?scope=my&amp;q=', html)

    def test_imported_core_fields_are_visible_in_customer_table(self):
        last_contact_at = timezone.make_aware(datetime(2024, 6, 1, 10, 0))
        next_contact_at = timezone.make_aware(datetime(2024, 6, 10, 9, 0))
        historical_created_at = timezone.make_aware(datetime(2024, 5, 20, 9, 0))
        customer = Customer(
            pk=1,
            customer_no="OA-001",
            name="测试客户",
            grade=Customer.Grade.KEY,
            customer_type="贸易商",
            demand="8-2A灌封一体机，滚轮",
            customer_status_text="私有客户,未报价；待拜访",
            is_deal=True,
            last_contact_at=last_contact_at,
            next_contact_at=next_contact_at,
            historical_created_at=historical_created_at,
            region="美国 Atlanta",
            source_channel="展会",
            wechat="wx-test",
            owner_name="销售001",
            status=Customer.Status.DEAL,
            attachment_note="客户附件说明",
        )
        page_obj = Paginator([customer], 8).page(1)
        request = RequestFactory().get("/customers/?scope=my")
        request.user = User(username="sales001")
        request.user.is_staff = False
        request.resolver_match = SimpleNamespace(url_name="customer_list")

        html = render_to_string(
            "crm/customer_list.html",
            {
                "customers": page_obj,
                "page_obj": page_obj,
                "paginator": page_obj.paginator,
                "total_record_count": 1,
                "filtered_record_count": 1,
                "today_new_count": 0,
                "today_due_count": 0,
                "key_customer_count": 1,
                "q": "",
                "kind": "",
                "status": "",
                "scope": "my",
                "filters": {
                    "quick": "",
                    "grades": [],
                    "types": [],
                    "demands": [],
                    "customer_statuses": [],
                    "sources": [],
                    "owners": [],
                    "date_field": "",
                    "date_from": "",
                    "date_to": "",
                    "sort_field": "created",
                    "sort_direction": "desc",
                },
                "show_owner_column": True,
                "list_title": "我的客户",
                "kind_choices": Customer.RecordKind.choices,
                "grade_choices": Customer.Grade.choices,
                "customer_type_options": [],
                "demand_options": [],
                "customer_status_options": [],
                "source_options": [],
                "owner_options": [],
                "inline_options": {},
                "can_undo": False,
                "can_redo": False,
                "base_querystring": "",
                "clear_querystring": "scope=my&per_page=8",
                "has_active_filters": False,
                "page_size": 8,
                "min_page_size": 3,
                "max_page_size": 30,
            },
            request=request,
        )

        for expected in (
            "重点客户",
            "贸易商",
            "8-2A灌封一体机",
            "滚轮",
            "未报价",
            "待拜访",
            "已成交",
            "2024-06-01",
            str(customer.uncontacted_days),
            "2024-06-10",
            "2024-05-20 09:00",
            "美国 Atlanta",
            "wx-test",
        ):
            self.assertIn(expected, html)
        self.assertIn("multiOptionSelectHtml", html)
        self.assertIn("multiCheckboxHtml", html)
        self.assertIn('field === "customer_status_text"', html)
        self.assertIn('inlineControl.innerHTML = multiCheckboxHtml(field, value)', html)
        self.assertIn('field === "demand"', html)
        self.assertRegex(html, r'field === "demand"[\s\S]*?inlineControl\.innerHTML = multiCheckboxHtml\(field, value\)')
        self.assertIn('data-multi-checkbox="1"', html)
        self.assertIn('class="inline-multi-option"', html)
        self.assertIn('input type="checkbox"', html)
        self.assertIn('querySelectorAll', html)
        self.assertIn('input[type="checkbox"]:checked', html)
        self.assertNotIn('data-detail-title="客户级别"', html)
        self.assertNotIn('data-detail-title="客户类型"', html)
        self.assertNotIn('data-detail-title="客户需求"', html)
        self.assertNotIn('data-detail-title="客户状态"', html)
        self.assertIn('class="clip detail-cell demand-detail-cell has-edit-trigger"', html)
        self.assertIn('data-edit-trigger', html)
        self.assertIn('data-edit-trigger>查看详情</button>', html)
        self.assertNotIn('cell-edit-button', html)
        self.assertIn('document.querySelectorAll("[data-edit-trigger]")', html)
        self.assertIn("vertical-align: middle", html)
        self.assertIn("display: inline-flex", html)
        self.assertIn('left: 50%', html)
        self.assertIn('background: rgba(238, 242, 246, .82)', html)
        self.assertIn('.inline-multi-options {', html)
        self.assertIn('.inline-multi-option {', html)
        self.assertIn('accent-color: var(--primary)', html)
        self.assertIn('.modal-backdrop {', html)
        self.assertIn('display: none;', html)
        self.assertIn('position: fixed;', html)
        self.assertIn('.modal-backdrop.open { display: flex; }', html)
        self.assertIn('background: #fff;', html)
        self.assertIn('event.key === "Escape" && importModal', html)
        self.assertIn('td[data-edit-field]:not(.detail-cell)::after', html)
        self.assertIn('content: "查看详情"', html)
        self.assertNotIn('content: "可修改"', html)
        self.assertIn('td[data-edit-field]:not(.detail-cell):hover::after', html)
        self.assertIn('data-col="phone" data-edit-field="phone"', html)
        self.assertIn('data-col="email" data-edit-field="email"', html)
        self.assertIn('data-col="owner" data-edit-field="owner_id"', html)
        self.assertIn('data-col="ownership" data-edit-field="status"', html)
        self.assertIn('<th data-col="deal">成交状态</th>', html)
        self.assertIn('data-col="image"', html)
        self.assertIn('data-detail-title="客户图片"', html)
        self.assertIn('data-detail-body="客户附件说明"', html)
        self.assertIn('data-detail-edit-url="/customers/1/edit/"', html)
        self.assertIn('[data-edit-field], [data-detail-title]', html)
        self.assertIn('data-col="deal" data-edit-field="is_deal"', html)
        self.assertIn('owner_id: "客户经理"', html)
        self.assertIn('status: "客户归属"', html)
        self.assertIn('is_deal: "成交状态"', html)
        self.assertIn('|| field === "owner_id" || field === "status"', html)
        self.assertIn('|| field === "status" || field === "is_deal"', html)
        self.assertIn('.customer-table [data-col="region"],', html)
        self.assertIn('.customer-table [data-col="source"],', html)
        self.assertIn('.customer-table [data-col="contact"] { min-width: 132px; width: 132px; max-width: 132px; }', html)
        self.assertIn('.customer-table [data-col="phone"],', html)
        self.assertIn('.customer-table [data-col="wechat"],', html)
        self.assertIn('.customer-table [data-col="email"] { min-width: 180px; max-width: 260px; }', html)
        self.assertIn('.customer-table [data-col="deal"] { min-width: 104px; width: 104px; max-width: 104px; }', html)
        self.assertIn('<th class="sticky-no locked-header">客户编号</th>', html)
        self.assertIn('<th class="select-col sticky-select"><input type="checkbox" id="selectAllRows"', html)
        self.assertLess(html.index('<th class="select-col sticky-select"><input type="checkbox" id="selectAllRows"'), html.index('<th class="sticky-no locked-header">客户编号</th>'))
        self.assertIn('left: 44px;', html)
        self.assertIn('return cell.dataset.col !== "actions" && cell.dataset.col !== "customer_no";', html)
        self.assertNotIn("私有客户", html)

    def test_public_customer_owner_display_keeps_original_owner_name(self):
        customer = Customer(status=Customer.Status.PUBLIC, owner_name="销售001")

        self.assertEqual(customer.owner_display, "销售001")

class DashboardNavigationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Profile, Customer, ContactLog, Contract):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Contract, ContactLog, Customer, Profile, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_dashboard_metric_cards_link_to_corresponding_pages(self):
        user = User.objects.create_superuser(username="dashboard-admin", password="x")
        self.client.force_login(user)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('class="metric metric-link" href="/customers/"', html)
        self.assertNotIn("重点推进客户", html)
        self.assertNotIn("本月可能成交", html)
        self.assertNotIn("下月可能成交", html)
        self.assertIn('href="/contracts/"', html)
        self.assertIn('href="/customers/?date_field=nextContact&amp;date_from=', html)
        self.assertIn('sort_field=nextContact&amp;sort_direction=asc"', html)
        self.assertIn('href="/customers/?quick=due"', html)
        self.assertIn('href="/customers/?scope=public"', html)
        self.assertIn('class="metric metric-link metric-flat" href="/contracts/"', html)

    def test_dashboard_shows_customer_distribution_pies(self):
        user = User.objects.create_superuser(username="dashboard-pie-admin", password="x")
        self.client.force_login(user)
        Customer.objects.create(
            name="饼图客户",
            demand="6-1易拉罐灌封一体机",
            customer_status_text="已加联系方式,已报价,未报价",
            grade=Customer.Grade.INTENTION,
            customer_type="贸易商",
            owner=user,
        )
        Customer.objects.create(name="空状态客户", status=Customer.Status.PRIVATE, owner=user)
        Customer.objects.create(
            name="公海饼图客户",
            demand="滚轮",
            customer_status_text="待拜访",
            grade=Customer.Grade.NORMAL,
            customer_type="封口机",
            status=Customer.Status.PUBLIC,
        )
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("客户分类占比", html)
        self.assertIn("客户需求占比", html)
        self.assertIn("客户状态占比", html)
        self.assertIn("客户级别占比", html)
        self.assertIn("客户类型占比", html)
        self.assertIn("pie-chart", html)
        self.assertNotIn("快成交", html)
        self.assertRegex(html, r'<div class="value">3</div><div class="label">客户总数</div>')
        self.assertIn('title="已加联系方式">已加联系方式', html)
        self.assertIn('title="已报价">已报价', html)
        self.assertIn('title="未报价">未报价', html)
        self.assertIn('title="待拜访">待拜访', html)
        self.assertIn('title="滚轮">滚轮', html)
        self.assertIn('title="封口机">封口机', html)
        self.assertNotIn("已加联系方式,已报价,未报价", html)
        self.assertNotIn("私有客户", html)

    def test_dashboard_source_quality_shows_effective_rate_and_more_button(self):
        user = User.objects.create_superuser(username="dashboard-source-admin", password="x")
        self.client.force_login(user)
        Customer.objects.create(name="有效客户", source_channel="抖音账号A")
        Customer.objects.create(name="无效客户", source_channel="抖音账号A", status=Customer.Status.INVALID)
        Customer.objects.create(name="待定客户", source_channel="抖音账号A", customer_status_text="待定")
        for index in range(10):
            Customer.objects.create(name=f"其他来源客户{index}", source_channel=f"来源{index}")

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("有效率", html)
        self.assertIn("无效/待定", html)
        self.assertIn("查看更多", html)
        self.assertRegex(html, r"<td>抖音账号A</td>\s*<td>3</td>\s*<td>33\.3%</td>\s*<td>2</td>")
        self.assertIn("data-source-quality-extra hidden", html)

    def test_dashboard_sales_load_merges_owner_code_and_name(self):
        user = User.objects.create_superuser(username="dashboard-owner-admin", password="x")
        owner = User.objects.create_user(username="SALES001")
        self.client.force_login(user)
        Customer.objects.create(name="工号负责人客户", owner=owner, owner_name="SALES001")
        Customer.objects.create(name="姓名负责人客户", owner_name="销售A")

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertRegex(html, r"<td>销售A</td>\s*<td>2</td>")
        self.assertNotIn("<td>SALES001</td>", html)

class CustomerWebImportTests(SimpleTestCase):
    def test_csv_rows_are_mapped_to_customer_import_values(self):
        data = (
            "客户编号,客户名称,客户经理,客户电话,线索来源,客户需求,客户状态,客户级别\n"
            "OA-001,测试客户,销售001,13812345678,抖音号账号A,8-2A灌封一体机,未报价,重点客户\n"
        ).encode("utf-8-sig")

        rows = _read_csv_rows(data)
        values = _row_to_customer_import_values(rows[0])

        self.assertEqual(values["customer_no"], "")
        self.assertEqual(values["legacy_customer_no"], "OA-001")
        self.assertEqual(values["name"], "测试客户")
        self.assertEqual(values["owner_name"], "销售001")
        self.assertEqual(values["phone"], "+86 13812345678")
        self.assertEqual(values["source_channel"], "抖音账号A")
        self.assertEqual(values["demand"], "8-2A灌封一体机")
        self.assertEqual(values["customer_status_text"], "未报价")
        self.assertEqual(values["grade"], Customer.Grade.KEY)

    def test_excel_serial_date_is_supported_in_customer_import_values(self):
        values = _row_to_customer_import_values({"客户名称": "测试客户", "最后联系时间": "45292"})

        self.assertIsNotNone(values["last_contact_at"])
        self.assertEqual(timezone.localtime(values["last_contact_at"]).date().isoformat(), "2024-01-01")

    def test_import_values_support_manual_header_mapping(self):
        values = _row_to_customer_import_values(
            {"名称列": "手动匹配客户", "电话列": "13812345678", "来源列": "抖音号账号A"},
            {"name": "名称列", "phone": "电话列", "source_channel": "来源列"},
        )

        self.assertEqual(values["name"], "手动匹配客户")
        self.assertEqual(values["phone"], "+86 13812345678")
        self.assertEqual(values["source_channel"], "抖音账号A")


class SourceChannelNormalizeCommandTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Customer, Lead):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Lead, Customer, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_normalize_source_channels_merges_noisy_sources(self):
        Customer.objects.create(name="短视频客户", source_channel="抖音账号A")
        Customer.objects.create(name="展会客户", source_channel="CBCE展会")
        Customer.objects.create(name="推荐客户", source_channel="老客户介绍")
        Customer.objects.create(name="国外社媒客户", source_channel="Instagram")
        Customer.objects.create(name="其他客户", source_channel="无法判断来源")
        lead = Lead.objects.create(name="脸书线索", source_channel="fb")

        NormalizeSourceChannelsCommand().handle(dry_run=False)

        values = list(Customer.objects.order_by("id").values_list("source_channel", flat=True))
        self.assertEqual(values, ["抖音账号A", "展会", "上下游推荐", "ins", "其他"])
        lead.refresh_from_db()
        self.assertEqual(lead.source_channel, "脸书")


class RestoreDetailedSourceChannelsCommandTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Customer, ContactLog, Contract, Lead, FeishuSyncSource, FeishuSyncRecord):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (FeishuSyncRecord, FeishuSyncSource, Lead, Contract, ContactLog, Customer, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_restore_recovers_detailed_sources_without_overwriting_specific_values(self):
        short_video_customer = Customer.objects.create(name="短视频客户", phone="13812345678", source_channel="短视频")
        foreign_customer = Customer.objects.create(name="国外客户", phone="13912345678", source_channel="国外社媒")
        specific_customer = Customer.objects.create(name="已有准确来源", phone="13712345678", source_channel="展会")
        fallback_customer = Customer.objects.create(name="兜底客户", source_channel="短视频")
        lead = Lead.objects.create(name="旧国外社媒线索", source_channel="国外社媒")
        source = FeishuSyncSource.objects.create(
            name="示例线索表-询盘",
            source_type=FeishuSyncSource.SourceType.SHEET,
            app_token="spreadsheet-token",
            table_id="sheet-id",
            sheet_id="sheet-id",
            source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
        )
        FeishuSyncRecord.objects.create(source=source, record_id="1", customer=short_video_customer, raw_fields={"获客渠道": "抖音账号A"})
        FeishuSyncRecord.objects.create(source=source, record_id="2", customer=foreign_customer, raw_fields={"获客渠道": "Instagram"})
        FeishuSyncRecord.objects.create(source=source, record_id="3", customer=specific_customer, raw_fields={"获客渠道": "抖音账号B"})

        RestoreDetailedSourceChannelsCommand().handle(
            dry_run=False,
            eoffice="Z:/missing-eoffice.xlsx",
            song="Z:/missing-song.xlsx",
            he="Z:/missing-he.xlsx",
        )

        short_video_customer.refresh_from_db()
        foreign_customer.refresh_from_db()
        specific_customer.refresh_from_db()
        fallback_customer.refresh_from_db()
        lead.refresh_from_db()
        self.assertEqual(short_video_customer.source_channel, "抖音账号A")
        self.assertEqual(foreign_customer.source_channel, "ins")
        self.assertEqual(specific_customer.source_channel, "展会")
        self.assertEqual(fallback_customer.source_channel, "短视频其他")
        self.assertEqual(lead.source_channel, "国外社媒其他")


class CleanupBlankNumberOnlyCustomersCommandTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Profile, Customer, Lead, ContactLog, Contract, Reminder, FeishuSyncSource, FeishuSyncRecord, AuditLog):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (AuditLog, FeishuSyncRecord, FeishuSyncSource, Reminder, Contract, ContactLog, Lead, Customer, Profile, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_cleanup_deletes_only_customers_with_no_business_data(self):
        blank = Customer.objects.create(customer_no="NQKH009001")
        empty_raw_customer = Customer.objects.create(customer_no="NQKH009002")
        named = Customer.objects.create(customer_no="NQKH009003", name="有资料客户")
        with_log = Customer.objects.create(customer_no="NQKH009004")
        ContactLog.objects.create(customer=with_log, summary="已经跟进过")
        meaningful_raw_customer = Customer.objects.create(customer_no="NQKH009005")
        source_only_customer = Customer.objects.create(customer_no="NQKH009006", source_channel="抖音其他", owner_name="销售A")
        status_only_customer = Customer.objects.create(customer_no="NQKH009007", status=Customer.Status.PUBLIC, grade=Customer.Grade.KEY)
        reminder_only_customer = Customer.objects.create(customer_no="NQKH009008")
        Reminder.objects.create(customer=reminder_only_customer, message="空客户提醒")
        source = FeishuSyncSource.objects.create(
            name="示例线索表-询盘",
            source_type=FeishuSyncSource.SourceType.SHEET,
            app_token="spreadsheet-token",
            table_id="MCW2Lm",
            sheet_id="MCW2Lm",
            source_kind=FeishuSyncSource.SourceKind.CUSTOMER,
        )
        empty_raw_record = FeishuSyncRecord.objects.create(source=source, record_id="empty", customer=empty_raw_customer, raw_fields={"序号": "2"})
        FeishuSyncRecord.objects.create(source=source, record_id="useful", customer=meaningful_raw_customer, raw_fields={"序号": "3", "用户名": "原始客户"})
        out = StringIO()

        call_command("cleanup_blank_number_only_customers", "--confirm", stdout=out)

        self.assertFalse(Customer.objects.filter(pk=blank.pk).exists())
        self.assertFalse(Customer.objects.filter(pk=empty_raw_customer.pk).exists())
        self.assertFalse(Customer.objects.filter(pk=source_only_customer.pk).exists())
        self.assertFalse(Customer.objects.filter(pk=status_only_customer.pk).exists())
        self.assertFalse(Customer.objects.filter(pk=reminder_only_customer.pk).exists())
        self.assertTrue(Customer.objects.filter(pk=named.pk).exists())
        self.assertTrue(Customer.objects.filter(pk=with_log.pk).exists())
        meaningful_raw_customer.refresh_from_db()
        self.assertEqual(meaningful_raw_customer.name, "原始客户")
        empty_raw_record.refresh_from_db()
        self.assertIsNone(empty_raw_record.customer_id)
        self.assertIn("删除 5 条", out.getvalue())


class PublicPoolRuleTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Profile, Customer, Lead, Reminder, AuditLog):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (AuditLog, Reminder, Lead, Customer, Profile, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_customers_uncontacted_more_than_30_days_release_to_public_pool(self):
        now = timezone.make_aware(datetime(2026, 6, 23, 12, 0))
        stale_customer = Customer.objects.create(
            name="超期客户",
            owner_name="销售001",
            status=Customer.Status.PRIVATE,
            last_contact_at=now - timedelta(days=31),
        )
        exactly_30_days_customer = Customer.objects.create(
            name="刚好三十天客户",
            status=Customer.Status.PRIVATE,
            last_contact_at=now - timedelta(days=30),
        )
        historical_stale_customer = Customer.objects.create(
            name="无跟进超期客户",
            status=Customer.Status.PRIVATE,
            historical_created_at=now - timedelta(days=31),
        )
        deal_stale_customer = Customer.objects.create(
            name="成交超期客户",
            status=Customer.Status.DEAL,
            is_deal=True,
            last_contact_at=now - timedelta(days=31),
        )
        quote_stale_customer = Customer.objects.create(
            name="报价中超期客户",
            status=Customer.Status.PRIVATE,
            customer_status_text="已加联系方式,报价中",
            last_contact_at=now - timedelta(days=31),
        )

        stats = run_daily_rules(now=now)

        stale_customer.refresh_from_db()
        exactly_30_days_customer.refresh_from_db()
        historical_stale_customer.refresh_from_db()
        deal_stale_customer.refresh_from_db()
        quote_stale_customer.refresh_from_db()
        self.assertEqual(stats["public_pool_released"], 2)
        self.assertEqual(stale_customer.status, Customer.Status.PUBLIC)
        self.assertIsNone(stale_customer.owner_id)
        self.assertEqual(stale_customer.owner_name, "销售001")
        self.assertEqual(historical_stale_customer.status, Customer.Status.PUBLIC)
        self.assertIsNone(historical_stale_customer.owner_id)
        self.assertEqual(deal_stale_customer.status, Customer.Status.DEAL)
        self.assertEqual(quote_stale_customer.status, Customer.Status.PRIVATE)
        self.assertEqual(exactly_30_days_customer.status, Customer.Status.PRIVATE)

    def test_public_customer_status_retains_owner_name_but_releases_assignment(self):
        user = User.objects.create_user(username="public-owner", first_name="原", last_name="经理")
        customer = Customer.objects.create(name="转公海客户", owner=user, status=Customer.Status.PRIVATE)

        customer.status = Customer.Status.PUBLIC
        customer.save(update_fields=["status", "updated_at"])

        customer.refresh_from_db()
        self.assertIsNone(customer.owner_id)
        self.assertEqual(customer.owner_name, "原 经理")
        self.assertEqual(customer.owner_display, "原 经理")

    def test_uncontacted_weekly_reminder_is_sent_once_per_threshold(self):
        now = timezone.make_aware(datetime(2026, 6, 23, 12, 0))
        user = User.objects.create_user(username="weekly-sales")
        customer = Customer.objects.create(
            name="一周未联系客户",
            owner=user,
            owner_name=user.username,
            status=Customer.Status.PRIVATE,
            last_contact_at=now - timedelta(days=8),
        )

        with patch("crm.services.send_feishu_webhook", return_value=True) as send_mock:
            stats = run_daily_rules(now=now)
            repeat_stats = run_daily_rules(now=now)

        self.assertEqual(stats["uncontacted_rule_reminders"], 1)
        self.assertEqual(stats["notifications_sent"], 1)
        self.assertEqual(repeat_stats["uncontacted_rule_reminders"], 0)
        self.assertEqual(send_mock.call_count, 1)
        text = send_mock.call_args.args[0]
        self.assertIn("CRM助手提醒", text)
        self.assertIn("超过 7 天", text)
        self.assertIn(customer.name, text)

    def test_quote_and_deal_customers_get_sixty_day_reminder_without_public_release(self):
        now = timezone.make_aware(datetime(2026, 6, 23, 12, 0))
        user = User.objects.create_user(username="protected-sales")
        quote_customer = Customer.objects.create(
            name="报价中客户",
            owner=user,
            owner_name=user.username,
            status=Customer.Status.PRIVATE,
            customer_status_text="报价中",
            last_contact_at=now - timedelta(days=61),
        )
        deal_customer = Customer.objects.create(
            name="已成交客户",
            owner=user,
            owner_name=user.username,
            status=Customer.Status.DEAL,
            is_deal=True,
            last_contact_at=now - timedelta(days=61),
        )

        with patch("crm.services.send_feishu_webhook", return_value=True) as send_mock:
            stats = run_daily_rules(now=now)

        quote_customer.refresh_from_db()
        deal_customer.refresh_from_db()
        self.assertEqual(stats["public_pool_released"], 0)
        self.assertEqual(stats["uncontacted_rule_reminders"], 2)
        self.assertEqual(send_mock.call_count, 2)
        self.assertEqual(quote_customer.status, Customer.Status.PRIVATE)
        self.assertEqual(deal_customer.status, Customer.Status.DEAL)
        messages = "\n".join(call.args[0] for call in send_mock.call_args_list)
        self.assertIn("超过 60 天", messages)
        self.assertIn("报价中客户", messages)
        self.assertIn("已成交客户", messages)

    def test_restore_public_pool_exempt_customers_command(self):
        user = User.objects.create_user(username="quote-owner")
        quote_customer = Customer.objects.create(
            name="误入公海报价客户",
            owner_name=user.username,
            status=Customer.Status.PUBLIC,
            customer_status_text="报价中",
        )
        deal_customer = Customer.objects.create(
            name="误入公海成交客户",
            owner_name=user.username,
            status=Customer.Status.PUBLIC,
            is_deal=True,
        )

        call_command("restore_public_pool_exempt_customers", stdout=StringIO())

        quote_customer.refresh_from_db()
        deal_customer.refresh_from_db()
        self.assertEqual(quote_customer.status, Customer.Status.PRIVATE)
        self.assertEqual(quote_customer.owner_id, user.id)
        self.assertEqual(deal_customer.status, Customer.Status.DEAL)
        self.assertEqual(deal_customer.owner_id, user.id)

    def test_move_unassigned_customers_to_public_pool_excludes_quote_and_deal(self):
        unassigned = Customer.objects.create(name="未分配客户", status=Customer.Status.PRIVATE)
        unassigned_with_name = Customer.objects.create(name="未绑定负责人客户", owner_name="历史销售", status=Customer.Status.PRIVATE)
        quote_customer = Customer.objects.create(name="报价中未分配客户", customer_status_text="报价中", status=Customer.Status.PRIVATE)
        deal_customer = Customer.objects.create(name="成交未分配客户", is_deal=True, status=Customer.Status.DEAL)

        call_command("move_unassigned_customers_to_public_pool", stdout=StringIO())

        unassigned.refresh_from_db()
        unassigned_with_name.refresh_from_db()
        quote_customer.refresh_from_db()
        deal_customer.refresh_from_db()
        self.assertEqual(unassigned.status, Customer.Status.PUBLIC)
        self.assertEqual(unassigned_with_name.status, Customer.Status.PUBLIC)
        self.assertEqual(unassigned_with_name.owner_name, "历史销售")
        self.assertEqual(quote_customer.status, Customer.Status.PRIVATE)
        self.assertEqual(deal_customer.status, Customer.Status.DEAL)


    def test_inline_deal_update_syncs_customer_status(self):
        user = User.objects.create_user(username="deal-sales")
        customer = Customer.objects.create(name="待成交客户", owner=user, owner_name=user.username, status=Customer.Status.PRIVATE)
        self.client.force_login(user)

        response = self.client.post(
            f"/customers/{customer.pk}/inline-update/",
            data=json.dumps({"field": "is_deal", "value": "1"}),
            content_type="application/json",
        )

        customer.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(customer.is_deal)
        self.assertEqual(customer.status, Customer.Status.DEAL)

        response = self.client.post(
            f"/customers/{customer.pk}/inline-update/",
            data=json.dumps({"field": "is_deal", "value": "0"}),
            content_type="application/json",
        )

        customer.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(customer.is_deal)
        self.assertEqual(customer.status, Customer.Status.PRIVATE)

    def test_sales_can_claim_unassigned_public_customer(self):
        user = User.objects.create_user(username="claim-sales")
        customer = Customer.objects.create(name="公海客户", status=Customer.Status.PUBLIC)
        self.client.force_login(user)

        response = self.client.post(f"/customers/{customer.pk}/claim/")

        customer.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(customer.status, Customer.Status.PRIVATE)
        self.assertEqual(customer.owner_id, user.id)
        self.assertEqual(customer.owner_name, user.username)

    def test_global_filtered_bulk_delete_uses_current_customer_filter(self):
        admin = User.objects.create_superuser(username="global-select-admin", password="x")
        first = Customer.objects.create(name="展会客户A", source_channel="展会", owner=admin, owner_name=admin.username)
        second = Customer.objects.create(name="展会客户B", source_channel="展会", owner=admin, owner_name=admin.username)
        other = Customer.objects.create(name="短视频客户", source_channel="短视频", owner=admin, owner_name=admin.username)
        self.client.force_login(admin)

        response = self.client.post(
            "/customers/bulk/",
            {
                "action": "delete",
                "selection_scope": "filtered",
                "selection_querystring": "scope=all&source_channel=展会",
                "next": "/customers/?scope=all&source_channel=展会",
            },
        )

        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(first.is_recycled)
        self.assertTrue(second.is_recycled)
        self.assertFalse(other.is_recycled)

class InlineRecordEditTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Profile, Customer, ContactLog, Contract):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Contract, ContactLog, Customer, Profile, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_contact_log_list_exposes_inline_edit_cells(self):
        user = User.objects.create_user(username="log-sales")
        customer = Customer.objects.create(name="日志客户", owner=user, owner_name=user.username)
        log = ContactLog.objects.create(customer=customer, created_by=user, follower_name="销售A", summary="初次沟通", result="已报价,待拜访", photo_note="照片说明", minutes_link="contact_audio/2026/06/call.mp3")
        ContactLog.objects.create(customer=customer, created_by=user, follower_name="销售A", summary="照片跟进", photo_file="contact_photos/2026/06/follow.jpg")
        self.client.force_login(user)

        response = self.client.get("/contact-logs/")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn(f'data-inline-url="/contact-logs/{log.pk}/inline-update/"', html)
        self.assertIn('data-edit-field="summary"', html)
        self.assertIn('data-edit-field="method"', html)
        self.assertIn('contactLogInlineOptions', html)
        self.assertIn('<th>音频</th>', html)
        self.assertIn('data-detail-title="跟进音频"', html)
        self.assertIn('data-detail-audio="/media/contact_audio/2026/06/call.mp3"', html)
        self.assertIn('follower_name: "select"', html)
        self.assertIn('result: "multi"', html)
        self.assertIn('data-multi-checkbox="1"', html)
        self.assertIn('input type="checkbox"', html)
        self.assertIn('已报价', html)
        self.assertIn('待拜访', html)
        self.assertNotIn('follower_name: "text"', html)
        self.assertIn('data-detail-title="跟进照片"', html)
        self.assertIn('data-detail-body="照片说明"', html)
        self.assertIn('data-detail-image="/media/contact_photos/2026/06/follow.jpg"', html)
        self.assertIn(f'data-detail-edit-url="/contact-logs/{log.pk}/edit/"', html)
        self.assertNotIn('data-edit-field="photo_note"', html)
        self.assertNotIn('photo_note: "跟进照片"', html)
        self.assertIn('data-edit-trigger>查看详情</button>', html)

    def test_contact_log_inline_update_changes_log(self):
        user = User.objects.create_user(username="log-editor")
        customer = Customer.objects.create(name="日志编辑客户", owner=user, owner_name=user.username)
        log = ContactLog.objects.create(customer=customer, created_by=user, follower_name="销售A", summary="旧内容")
        self.client.force_login(user)

        response = self.client.post(
            f"/contact-logs/{log.pk}/inline-update/",
            data=json.dumps({"field": "summary", "value": "新跟进内容"}),
            content_type="application/json",
        )

        log.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(log.summary, "新跟进内容")

    def test_contract_list_exposes_inline_edit_cells(self):
        user = User.objects.create_superuser(username="contract-admin", password="x")
        customer = Customer.objects.create(name="合同客户")
        contract = Contract.objects.create(customer=customer, signed_by=user, amount="1200.00", attachment_note="附件说明")
        Contract.objects.create(customer=customer, signed_by=user, amount="2600.00", attachment_file="contract_attachments/2026/06/order.pdf")
        self.client.force_login(user)

        response = self.client.get("/contracts/")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn(f'data-inline-url="/contracts/{contract.pk}/inline-update/"', html)
        self.assertIn('data-edit-field="amount"', html)
        self.assertIn('data-edit-field="customer_id"', html)
        self.assertIn('data-edit-field="signed_by_id"', html)
        self.assertIn('contractInlineOptions', html)
        self.assertIn('data-detail-title="合同附件"', html)
        self.assertIn('data-detail-body="附件说明"', html)
        self.assertIn('data-detail-url="/media/contract_attachments/2026/06/order.pdf"', html)
        self.assertIn(f'data-detail-edit-url="/contracts/{contract.pk}/edit/"', html)
        self.assertNotIn('data-edit-field="contract_no"', html)
        self.assertNotIn('data-edit-field="attachment_note"', html)
        self.assertNotIn('data-edit-field="customer_name"', html)
        self.assertNotIn('contract_no: "合同编号"', html)
        self.assertNotIn('attachment_note: "合同附件"', html)

    def test_contract_inline_update_changes_amount_and_customer(self):
        user = User.objects.create_user(username="contract-sales")
        first_customer = Customer.objects.create(name="原客户", owner=user, owner_name=user.username)
        second_customer = Customer.objects.create(name="新客户", owner=user, owner_name=user.username)
        contract = Contract.objects.create(customer=first_customer, signed_by=user, amount="1200.00")
        self.client.force_login(user)

        amount_response = self.client.post(
            f"/contracts/{contract.pk}/inline-update/",
            data=json.dumps({"field": "amount", "value": "1688.50"}),
            content_type="application/json",
        )
        customer_response = self.client.post(
            f"/contracts/{contract.pk}/inline-update/",
            data=json.dumps({"field": "customer_id", "value": str(second_customer.pk)}),
            content_type="application/json",
        )

        contract.refresh_from_db()
        self.assertEqual(amount_response.status_code, 200)
        self.assertEqual(customer_response.status_code, 200)
        self.assertEqual(contract.amount, Decimal("1688.50"))
        self.assertEqual(contract.customer_id, second_customer.pk)
        self.assertEqual(contract.customer_name, "新客户")

    def test_contract_form_hides_manual_contract_no_and_feishu_fields(self):
        form = ContractForm()

        self.assertNotIn("contract_no", form.fields)
        self.assertNotIn("customer_name", form.fields)
        self.assertNotIn("signed_by_name", form.fields)
        self.assertTrue(form.fields["customer"].required)
        self.assertIn("attachment_file", form.fields)
        self.assertEqual(form.fields["attachment_note"].label, "附件说明")
        customer = Customer(name="表单客户", customer_no="NQKH000123")
        signer = User(username="u001", first_name="签约", last_name="销售")
        self.assertEqual(form.fields["customer"].label_from_instance(customer), "NQKH000123 ｜ 表单客户")
        self.assertEqual(form.fields["signed_by"].label_from_instance(signer), "签约 销售")

    def test_contract_no_is_not_inline_editable(self):
        user = User.objects.create_user(username="contract-no-sales")
        customer = Customer.objects.create(name="合同编号客户", owner=user, owner_name=user.username)
        contract = Contract.objects.create(customer=customer, signed_by=user, amount="1200.00")
        original_no = contract.contract_no
        self.client.force_login(user)

        response = self.client.post(
            f"/contracts/{contract.pk}/inline-update/",
            data=json.dumps({"field": "contract_no", "value": "MANUAL-001"}),
            content_type="application/json",
        )

        contract.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(contract.contract_no, original_no)

    def test_contact_log_form_uses_follower_select_and_audio_upload(self):
        user = User.objects.create_user(username="audio-sales", first_name="音频", last_name="销售")
        form = ContactLogForm(initial={"follower_name": user.get_full_name()})

        self.assertIn("audio_file", form.fields)
        self.assertNotIn("minutes_link", form.fields)
        self.assertEqual(form.fields["audio_file"].widget.attrs.get("accept"), "audio/*")
        self.assertIn("photo_file", form.fields)
        self.assertEqual(form.fields["photo_file"].widget.attrs.get("accept"), "image/*")
        self.assertEqual(form.fields["result"].widget.attrs.get("data-dropdown-multiple"), "1")
        self.assertIn(("音频 销售", "音频 销售"), list(form.fields["follower_name"].choices))

    def test_contact_log_form_saves_audio_file_path(self):
        user = User.objects.create_user(username="audio-upload", first_name="音频", last_name="销售")
        media_root = tempfile.mkdtemp(prefix="crm-audio-test-")
        try:
            with override_settings(MEDIA_ROOT=media_root):
                form = ContactLogForm(
                    data={
                        "contact_at": "2026-06-24T10:00",
                        "method": ContactLog.Method.WECHAT,
                        "source": ContactLog.Source.MANUAL,
                        "follower_name": user.get_full_name(),
                        "summary": "上传通话录音",
                        "result": "已沟通",
                        "photo_note": "",
                        "next_contact_at": "",
                    },
                    files={"audio_file": SimpleUploadedFile("call.mp3", b"ID3", content_type="audio/mpeg")},
                )

                self.assertTrue(form.is_valid(), form.errors)
                log = form.save(commit=False)
                self.assertTrue(log.minutes_link.startswith("contact_audio/"))
                self.assertTrue(log.minutes_link.endswith("call.mp3"))
        finally:
            shutil.rmtree(media_root, ignore_errors=True)

class PhonePrefixTests(SimpleTestCase):
    def test_global_phone_prefix_choices_include_south_america(self):
        choices = dict(PHONE_PREFIX_CHOICES)
        self.assertIn("+54", choices)
        self.assertIn("阿根廷", choices["+54"])
        self.assertIn("+591", choices)
        self.assertIn("玻利维亚", choices["+591"])

    def test_region_hint_infers_global_phone_prefix(self):
        self.assertEqual(normalize_phone_number_for_region("91123456789", "阿根廷"), "+54 91123456789")
        self.assertEqual(normalize_phone_number_for_region("71234567", "玻利维亚"), "+591 71234567")


class ImportEofficeContactLogCommandTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (Tag, Customer, ContactLog):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            for model in (ContactLog, Customer, Tag):
                if model._meta.db_table in existing_tables:
                    pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_import_rows_creates_contact_log_and_updates_customer_dates(self):
        customer = Customer.objects.create(
            name="测试客户",
            customer_no="NQKH000100",
            legacy_customer_no="C-NO-001",
        )
        rows = [
            {
                "客户编号": "C-NO-001",
                "沟通记录": "已沟通方案",
                "最后联系时间": "2024-06-01 09:00:00",
                "下次联系时间": "2024-06-10 00:00:00",
                "客户经理": "销售001",
                "客户状态": "已报价,待拜访",
                "客户电话": "13812345678",
            }
        ]
        command = ImportEofficeContactLogCommand()

        stats = command.import_rows(rows)

        customer.refresh_from_db()
        log = ContactLog.objects.get(customer=customer)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(log.source, ContactLog.Source.IMPORT)
        self.assertEqual(log.method, ContactLog.Method.PHONE)
        self.assertEqual(log.summary, "已沟通方案")
        self.assertEqual(log.follower_name, "销售001")
        self.assertEqual(log.result, "已报价,待拜访")
        self.assertEqual(timezone.localtime(log.contact_at).date().isoformat(), "2024-06-01")
        self.assertEqual(timezone.localtime(customer.last_contact_at).date().isoformat(), "2024-06-01")
        self.assertEqual(timezone.localtime(customer.next_contact_at).date().isoformat(), "2024-06-10")

        duplicate_stats = command.import_rows(rows)

        self.assertEqual(duplicate_stats["created"], 0)
        self.assertEqual(duplicate_stats["duplicates"], 1)
        self.assertEqual(ContactLog.objects.filter(customer=customer).count(), 1)

class CustomerTimestampTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if Tag._meta.db_table not in existing_tables:
                schema_editor.create_model(Tag)
            if Customer._meta.db_table not in existing_tables:
                schema_editor.create_model(Customer)

    @classmethod
    def tearDownClass(cls):
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if Customer._meta.db_table in existing_tables:
                pass  # 表由 migrations 管理，测试结束不手动删表
            if Tag._meta.db_table in existing_tables:
                pass  # 表由 migrations 管理，测试结束不手动删表
        super().tearDownClass()

    def test_customer_created_at_is_immutable_on_edit(self):
        customer = Customer.objects.create(name="测试客户", phone="13812345678")
        original_created_at = timezone.make_aware(datetime(2024, 1, 2, 9, 30))
        Customer.objects.filter(pk=customer.pk).update(created_at=original_created_at)

        customer = Customer.objects.get(pk=customer.pk)
        customer.phone = "13912345678"
        customer.created_at = timezone.now()
        customer.save()

        customer.refresh_from_db()
        self.assertEqual(customer.created_at, original_created_at)
        self.assertEqual(customer.phone, "+86 13912345678")

    def test_existing_historical_created_at_is_immutable_on_edit(self):
        customer = Customer.objects.create(name="测试客户", phone="13812345678")
        historical_created_at = timezone.make_aware(datetime(2023, 5, 6, 10, 15))
        Customer.objects.filter(pk=customer.pk).update(historical_created_at=historical_created_at)

        customer = Customer.objects.get(pk=customer.pk)
        customer.historical_created_at = historical_created_at + timedelta(days=7)
        customer.phone = "13912345678"
        customer.save()

        customer.refresh_from_db()
        self.assertEqual(customer.historical_created_at, historical_created_at)

    def test_import_command_matches_customer_number_across_fields(self):
        customer = Customer.objects.create(name="测试客户", customer_no="OA-001")
        command = ImportCustomerCommand()

        found = command._find_customer({"legacy_customer_no": "OA-001"})

        self.assertEqual(found.pk, customer.pk)

    def test_eoffice_core_field_import_can_repair_historical_created_at(self):
        original_historical_created_at = timezone.make_aware(datetime(2023, 5, 6, 10, 15))
        customer = Customer.objects.create(
            name="测试客户",
            customer_no="OA-010",
            historical_created_at=original_historical_created_at,
        )
        command = ImportEofficeCustomerCommand()

        stats = command.import_rows(
            [
                {
                    "客户编号": "OA-010",
                    "客户名称": "测试客户",
                    "地区": "美国",
                    "城市": "Atlanta",
                    "沟通记录": "按 e-office 补齐",
                    "客户状态": "未报价,待拜访",
                    "账号来源": "抖音号账号B",
                    "创建时间": "2024-02-03 08:00:00",
                    "最后联系时间": "2024-02-04 09:00:00",
                    "下次联系时间": "2024-02-10",
                }
            ],
            overwrite=True,
            create_missing=False,
            only_core_fields=True,
        )

        customer.refresh_from_db()
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(timezone.localtime(customer.historical_created_at).date().isoformat(), "2024-02-03")
        self.assertEqual(timezone.localtime(customer.last_contact_at).date().isoformat(), "2024-02-04")
        self.assertEqual(timezone.localtime(customer.next_contact_at).date().isoformat(), "2024-02-10")
        self.assertEqual(customer.region, "美国 Atlanta")
        self.assertEqual(customer.notes, "按 e-office 补齐")
        self.assertEqual(customer.customer_status_text, "未报价,待拜访")
        self.assertEqual(customer.source_channel, "抖音账号B")

    def test_fill_eoffice_account_source_updates_matched_customer(self):
        customer = Customer.objects.create(name="测试客户", customer_no="OA-014")
        command = FillEofficeAccountSourceCommand()

        stats = command.fill_sources(
            [
                {"客户编号": "OA-014", "客户名称": "测试客户", "账号来源": "抖音号账号A"},
                {"客户编号": "OA-404", "客户名称": "缺失客户", "账户来源": "Instagram"},
                {"客户编号": "OA-015", "客户名称": "无来源客户"},
            ],
            overwrite=True,
        )

        customer.refresh_from_db()
        self.assertEqual(stats["seen"], 3)
        self.assertEqual(stats["with_source"], 2)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["missing_customer"], 1)
        self.assertEqual(stats["missing_source"], 1)
        self.assertEqual(customer.source_channel, "抖音账号A")

    def test_eoffice_core_field_import_updates_provided_created_time_without_overwrite_flag(self):
        original_historical_created_at = timezone.make_aware(datetime(2023, 5, 6, 10, 15))
        customer = Customer.objects.create(
            name="测试客户",
            customer_no="OA-012",
            historical_created_at=original_historical_created_at,
        )
        command = ImportEofficeCustomerCommand()

        stats = command.import_rows(
            [
                {
                    "客户编号": "OA-012",
                    "客户名称": "测试客户",
                    "创建时间": "2024-03-04 08:00:00",
                }
            ],
            create_missing=False,
            only_core_fields=True,
        )

        customer.refresh_from_db()
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(timezone.localtime(customer.historical_created_at).date().isoformat(), "2024-03-04")

    def test_web_import_uses_provided_created_time_as_historical_time(self):
        original_historical_created_at = timezone.make_aware(datetime(2023, 5, 6, 10, 15))
        customer = Customer.objects.create(
            name="测试客户",
            customer_no="OA-013",
            historical_created_at=original_historical_created_at,
        )

        stats = _run_customer_import(
            [{"客户编号": "OA-013", "客户名称": "测试客户", "创建时间": "2024-04-05 08:00:00"}],
            {},
            "merge",
            False,
            SimpleNamespace(is_superuser=True),
        )

        customer.refresh_from_db()
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(timezone.localtime(customer.historical_created_at).date().isoformat(), "2024-04-05")

    def test_eoffice_field_report_counts_pending_updates(self):
        Customer.objects.create(name="测试客户", customer_no="OA-011")
        command = ImportEofficeCustomerCommand()

        stats = command.field_report(
            [
                {
                    "客户编号": "OA-011",
                    "客户名称": "测试客户",
                    "客户级别": "重点客户",
                    "客户类型": "贸易商",
                    "客户需求": "8-2A灌封一体机",
                    "客户状态": "未报价",
                    "地区": "美国",
                    "城市": "Atlanta",
                    "沟通记录": "按 e-office 补齐",
                    "创建时间": "2024-02-03 08:00:00",
                    "最后联系时间": "2024-02-04 09:00:00",
                    "下次联系时间": "2024-02-10",
                }
            ],
            only_core_fields=True,
        )

        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["fields"]["grade"]["would_update"], 1)
        self.assertEqual(stats["fields"]["customer_type"]["would_update"], 1)
        self.assertEqual(stats["fields"]["demand"]["would_update"], 1)
        self.assertEqual(stats["fields"]["customer_status_text"]["would_update"], 1)
        self.assertEqual(stats["fields"]["last_contact_at"]["would_update"], 1)
        self.assertEqual(stats["fields"]["next_contact_at"]["would_update"], 1)
        self.assertEqual(stats["fields"]["historical_created_at"]["would_update"], 1)

class SalesProcessP0Tests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="sales", password="x")
        self.other = User.objects.create_user(username="other", password="x")
        self.leader = User.objects.create_user(username="leader", password="x")
        Profile.objects.update_or_create(user=self.leader, defaults={"role": Profile.Role.LEADER, "active": True})

    def test_uncontacted_days_requires_real_last_contact_at(self):
        customer = Customer.objects.create(name="未联系客户", owner=self.owner)
        Customer.objects.filter(pk=customer.pk).update(historical_created_at=timezone.now() - timedelta(days=3))
        customer.refresh_from_db()

        self.assertIsNone(customer.last_contact_at)
        self.assertIsNone(customer.uncontacted_days)

        customer.last_contact_at = timezone.now() - timedelta(days=5)
        customer.save(update_fields=["last_contact_at", "updated_at"])
        customer.refresh_from_db()
        self.assertEqual(customer.uncontacted_days, 5)
    def test_contact_log_updates_customer_summary(self):
        customer = Customer.objects.create(name="测试客户", owner=self.owner)
        contact_at = timezone.now() - timedelta(hours=2)
        log = ContactLog.objects.create(customer=customer, followed_by=self.owner, contact_at=contact_at, content="已沟通", next_action="明天发方案", status_after=Customer.FollowStatus.DEMAND_CONFIRMING)
        update_customer_after_contact(customer, log)
        customer.refresh_from_db()
        self.assertEqual(customer.last_contact_at, contact_at)
        self.assertEqual(customer.next_action, "明天发方案")
        self.assertEqual(customer.follow_status, Customer.FollowStatus.DEMAND_CONFIRMING)

    def test_contact_log_updates_customer_grade_and_status_text(self):
        customer = Customer.objects.create(
            name="状态客户",
            owner=self.owner,
            grade=Customer.Grade.POTENTIAL,
            customer_status_text="未报价",
        )
        log = ContactLog.objects.create(
            customer=customer,
            followed_by=self.owner,
            contact_at=timezone.now(),
            content="已报价，客户意向增强",
            level_after=Customer.Grade.INTENTION,
            status_after="已报价",
        )

        update_customer_after_contact(customer, log)

        customer.refresh_from_db()
        self.assertEqual(customer.grade, Customer.Grade.INTENTION)
        self.assertEqual(customer.customer_status_text, "已报价")
        self.assertEqual(customer.customer_level, Customer.CustomerLevel.INTENTION)
        self.assertEqual(customer.follow_status, Customer.FollowStatus.QUOTED)
    def test_quote_and_payment_reminders_and_deal_skip_public_pool(self):
        customer = Customer.objects.create(name="报价客户", owner=self.owner, last_contact_at=timezone.now() - timedelta(days=20))
        Quote.objects.create(customer=customer, quoted_by=self.owner, status=Quote.Status.SENT, quote_date=timezone.localdate() - timedelta(days=8), total_amount=Decimal("1000"))
        run_daily_rules(now=timezone.now())
        self.assertTrue(TaskReminder.objects.filter(customer=customer, reminder_type=TaskReminder.ReminderType.QUOTE).exists())
        customer.follow_status = Customer.FollowStatus.QUOTING
        customer.save(update_fields=["follow_status"])
        self.assertEqual(release_stale_customers(now=timezone.now(), public_days=1), 0)
        customer.follow_status = Customer.FollowStatus.DEAL
        customer.deal_status = Customer.DealStatus.WON
        customer.status = Customer.Status.DEAL
        customer.is_deal = True
        customer.save(update_fields=["follow_status", "deal_status", "status", "is_deal"])
        run_daily_rules(now=timezone.now())
        self.assertFalse(TaskReminder.objects.filter(customer=customer, reminder_type=TaskReminder.ReminderType.FOLLOW_UP).exists())

    def test_unpaid_contract_creates_payment_reminder_and_amount_is_correct(self):
        customer = Customer.objects.create(name="收款客户", owner=self.owner)
        contract = Contract.objects.create(customer=customer, signed_by=self.owner, contract_amount=Decimal("10000"), amount=Decimal("10000"))
        Payment.objects.create(contract=contract, customer=customer, amount=Decimal("3000"), actual_received_amount=Decimal("3000"))
        self.assertEqual(contract.unpaid_amount, Decimal("7000"))
        run_daily_rules(now=timezone.now())
        self.assertTrue(TaskReminder.objects.filter(customer=customer, contract=contract, reminder_type=TaskReminder.ReminderType.PAYMENT).exists())

    def test_unassigned_and_stale_customers_are_public_pool(self):
        unassigned = Customer.objects.create(name="未分配客户", owner=None, status=Customer.Status.PRIVATE)
        stale = Customer.objects.create(
            name="超期客户",
            owner=self.other,
            last_contact_at=timezone.now() - timedelta(days=31),
            customer_level=Customer.CustomerLevel.NORMAL,
        )
        fresh = Customer.objects.create(name="未超期客户", owner=self.other, last_contact_at=timezone.now())

        public_qs = Customer.objects.filter(public_pool_customer_q())
        self.assertIn(unassigned, public_qs)
        self.assertIn(stale, public_qs)
        self.assertNotIn(fresh, public_qs)
        self.assertTrue(is_public_pool_customer(unassigned))
        self.assertTrue(is_public_pool_customer(stale))
        self.assertIn(stale, customer_queryset_for(self.owner))
    def test_stale_normal_customer_goes_public_and_invalid_goes_recycled(self):
        customer = Customer.objects.create(name="普通客户", owner=self.owner, last_contact_at=timezone.now() - timedelta(days=31), customer_level=Customer.CustomerLevel.NORMAL)
        self.assertEqual(release_stale_customers(now=timezone.now(), public_days=30), 1)
        customer.refresh_from_db()
        self.assertTrue(customer.is_public)
        self.assertIsNone(customer.owner)
        invalid = Customer.objects.create(name="无效客户", owner=self.owner, customer_level=Customer.CustomerLevel.INVALID)
        invalid.refresh_from_db()
        self.assertTrue(invalid.is_recycled)
        self.assertEqual(invalid.status, Customer.Status.INVALID)

    def test_transfer_and_merge_keep_related_records(self):
        source = Customer.objects.create(name="被合并", owner=self.owner)
        target = Customer.objects.create(name="主客户", owner=self.owner)
        log = ContactLog.objects.create(customer=source, content="历史联系")
        quote = Quote.objects.create(customer=source, quoted_by=self.owner, total_amount=Decimal("500"))
        old_owner = source.owner
        source.owner = self.other
        source.save(update_fields=["owner", "updated_at"])
        OperationLog.objects.create(user=self.leader, customer=source, action_type=OperationLog.ActionType.TRANSFER, before_data={"owner": old_owner.username}, after_data={"owner": self.other.username})
        self.assertTrue(OperationLog.objects.filter(customer=source, action_type=OperationLog.ActionType.TRANSFER).exists())
        ContactLog.objects.filter(customer=source).update(customer=target)
        Quote.objects.filter(customer=source).update(customer=target)
        self.assertTrue(ContactLog.objects.filter(pk=log.pk, customer=target).exists())
        self.assertTrue(Quote.objects.filter(pk=quote.pk, customer=target).exists())

    def test_sales_visibility_and_leader_visibility(self):
        mine = Customer.objects.create(name="我的客户", owner=self.owner)
        shared = Customer.objects.create(name="协作客户", owner=self.other)
        shared.co_owners.add(self.owner)
        hidden = Customer.objects.create(name="别人客户", owner=self.other)
        sales_qs = customer_queryset_for(self.owner)
        self.assertIn(mine, sales_qs)
        self.assertIn(shared, sales_qs)
        self.assertNotIn(hidden, sales_qs)
        self.assertEqual(customer_queryset_for(self.leader).count(), 3)
