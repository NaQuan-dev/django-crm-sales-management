import csv
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import Profile


class Command(BaseCommand):
    help = "从飞书导出包的销售人员管理表创建或更新客户系统销售账号。"

    def add_arguments(self, parser):
        parser.add_argument("export_path", help="飞书客户系统导出目录或压缩包路径")
        parser.add_argument("--default-password", default="", help="可选：给新建账号设置统一临时密码")
        parser.add_argument("--reset-password", action="store_true", help="配合 --default-password，重置已存在账号密码")
        parser.add_argument("--rename-to-employee-no", action="store_true", help="把已有账号用户名改成销售人员表里的员工工号")
        parser.add_argument("--only-username", default="", help="只处理当前用户名匹配的账号，例如 liweihua")
        parser.add_argument("--only-employee-no", default="", help="只处理员工工号匹配的人员，例如 SALES004")
        parser.add_argument("--only-name", default="", help="只处理销售姓名匹配的人员")
        parser.add_argument("--show-accounts", action="store_true", help="在终端显示创建/更新后的登录账号名")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不写入数据库")

    def handle(self, *args, **options):
        source_path = Path(options["export_path"]).expanduser()
        if not source_path.exists():
            raise CommandError(f"找不到导出路径: {source_path}")

        temp_dir = None
        try:
            export_root = source_path
            if source_path.is_file() and source_path.suffix.lower() == ".zip":
                temp_dir = Path(tempfile.mkdtemp(prefix="feishu-crm-export-"))
                with zipfile.ZipFile(source_path) as archive:
                    archive.extractall(temp_dir)
                export_root = self._find_export_root(temp_dir)

            sales_people = self._read_sales_people(export_root)
            stats = self._upsert_sales_people(sales_people, options)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        action = "预演" if options["dry_run"] else "写入"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action}完成：读取 {stats['seen']} 人，新增 {stats['created']} 个账号，更新 {stats['updated']} 个账号，跳过 {stats['skipped']} 人。"
            )
        )
        if options["show_accounts"] and stats["accounts"]:
            self.stdout.write("账号清单：")
            for account in stats["accounts"]:
                self.stdout.write(f"- {account['display_name']} / 用户名: {account['username']}")

    def _find_export_root(self, root):
        if (root / "manifest.json").exists():
            return root
        for child in root.iterdir():
            if child.is_dir() and (child / "manifest.json").exists():
                return child
        return root

    def _read_sales_people(self, export_root):
        table_dirs = [p for p in export_root.iterdir() if p.is_dir() and p.name.startswith("table_销售人员管理_")]
        if not table_dirs:
            raise CommandError("导出目录中没有找到销售人员管理表。")

        people = []
        for table_dir in table_dirs:
            raw_records = self._load_raw_records(table_dir / "records.json")
            csv_path = table_dir / "records.csv"
            if not csv_path.exists():
                continue
            with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
                for row in csv.DictReader(fp):
                    raw_record = raw_records.get(self._text(row, "record_id"), {})
                    sales_user = self._user_value(raw_record.get("销售"))
                    manager_user = self._user_value(raw_record.get("销售经理"))
                    display_name = sales_user["name"] or self._text(row, "销售姓名") or self._text(row, "销售")
                    if not display_name and not sales_user["feishu_open_id"]:
                        continue
                    people.append(
                        {
                            "display_name": display_name,
                            "feishu_open_id": sales_user["feishu_open_id"],
                            "employee_no": self._text(row, "员工工号"),
                            "region": self._text(row, "负责地区"),
                            "manager_name": manager_user["name"],
                            "manager_feishu_open_id": manager_user["feishu_open_id"],
                        }
                    )
        return people

    def _load_raw_records(self, records_path):
        if not records_path.exists():
            return {}
        with records_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        records = data if isinstance(data, list) else data.get("items") or data.get("records") or data.get("data", {}).get("items") or []
        result = {}
        for record in records:
            record_id = str(record.get("record_id") or record.get("id") or "").strip()
            if record_id:
                result[record_id] = record
        return result

    def _upsert_sales_people(self, sales_people, options):
        stats = {"seen": 0, "created": 0, "updated": 0, "skipped": 0, "accounts": []}
        default_password = options["default_password"]
        with transaction.atomic():
            for person in sales_people:
                stats["seen"] += 1
                only_employee_no = str(options["only_employee_no"] or "").strip()
                only_name = str(options["only_name"] or "").strip()
                if only_employee_no and person.get("employee_no") != only_employee_no:
                    stats["skipped"] += 1
                    continue
                if only_name and person.get("display_name") != only_name:
                    stats["skipped"] += 1
                    continue
                user = self._find_existing_user(person)
                only_username = str(options["only_username"] or "").strip()
                if only_username and (not user or user.username != only_username):
                    stats["skipped"] += 1
                    continue
                creating = user is None
                if creating:
                    username = self._unique_username(self._preferred_username(person))
                    user = User(username=username)

                changed = False
                preferred_username = self._preferred_username(person)
                if (
                    not creating
                    and options["rename_to_employee_no"]
                    and person.get("employee_no")
                    and user.username != preferred_username
                ):
                    if User.objects.filter(username=preferred_username).exclude(pk=user.pk).exists():
                        raise CommandError(f"用户名 {preferred_username} 已存在，无法把 {user.username} 改成这个工号。")
                    user.username = preferred_username
                    changed = True
                if person["display_name"] and user.first_name != person["display_name"]:
                    user.first_name = person["display_name"]
                    changed = True
                if creating and default_password:
                    user.set_password(default_password)
                    changed = True
                elif creating:
                    user.set_unusable_password()
                    changed = True
                elif default_password and options["reset_password"]:
                    user.set_password(default_password)
                    changed = True

                if creating or changed:
                    user.is_active = True
                    user.save()

                profile, profile_created = Profile.objects.get_or_create(user=user)
                profile_changed = profile_created
                if profile.role != Profile.Role.SALES:
                    profile.role = Profile.Role.SALES
                    profile_changed = True
                if person["feishu_open_id"] and profile.feishu_open_id != person["feishu_open_id"]:
                    profile.feishu_open_id = person["feishu_open_id"]
                    profile_changed = True
                if not profile.active:
                    profile.active = True
                    profile_changed = True
                if profile_changed:
                    profile.save()

                if creating:
                    stats["created"] += 1
                elif changed or profile_changed:
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
                stats["accounts"].append({"display_name": person["display_name"], "username": user.username})

            if options["dry_run"]:
                transaction.set_rollback(True)
        return stats

    def _find_existing_user(self, person):
        feishu_open_id = person.get("feishu_open_id")
        if feishu_open_id:
            profile = Profile.objects.select_related("user").filter(feishu_open_id=feishu_open_id).first()
            if profile:
                return profile.user
        username = self._preferred_username(person)
        found = User.objects.filter(username=username).first()
        if found:
            return found
        display_name = person.get("display_name")
        if display_name:
            found = User.objects.filter(first_name=display_name).first()
            if found:
                return found
        return None

    def _preferred_username(self, person):
        employee_no = re.sub(r"[^A-Za-z0-9_.@+-]+", "", person.get("employee_no") or "")
        if employee_no:
            return employee_no[:120]
        feishu_open_id = person.get("feishu_open_id") or ""
        if feishu_open_id:
            return f"fs_{feishu_open_id[-12:]}"
        display_name = person.get("display_name") or "sales"
        safe_name = re.sub(r"[^A-Za-z0-9_.@+-]+", "_", display_name).strip("_").lower()
        return safe_name[:80] or "sales"

    def _unique_username(self, username):
        username = username or "sales"
        candidate = username
        index = 2
        while User.objects.filter(username=candidate).exists():
            candidate = f"{username}_{index}"
            index += 1
        return candidate

    def _user_value(self, value):
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            return {
                "name": str(value.get("name") or value.get("text") or value.get("en_name") or value.get("email") or "").strip(),
                "feishu_open_id": str(value.get("open_id") or value.get("id") or value.get("user_id") or "").strip(),
            }
        return {"name": str(value or "").strip(), "feishu_open_id": ""}

    def _text(self, row, key):
        return str(row.get(key) or "").strip()
