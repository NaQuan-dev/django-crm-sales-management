#!/bin/sh
set -e

mkdir -p /app/imports

python manage.py makemigrations crm --noinput
python manage.py migrate --noinput
python manage.py shell <<'PY'
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crm_profile (
            id bigserial PRIMARY KEY,
            role varchar(20) NOT NULL DEFAULT 'sales',
            feishu_open_id varchar(128) NOT NULL DEFAULT '',
            active boolean NOT NULL DEFAULT true,
            user_id integer NOT NULL UNIQUE REFERENCES auth_user(id) DEFERRABLE INITIALLY DEFERRED
        );
    """)
    cursor.execute("ALTER TABLE IF EXISTS crm_profile ADD COLUMN IF NOT EXISTS role varchar(20) NOT NULL DEFAULT 'sales';")
    cursor.execute("ALTER TABLE IF EXISTS crm_profile ADD COLUMN IF NOT EXISTS feishu_open_id varchar(128) NOT NULL DEFAULT '';")
    cursor.execute("ALTER TABLE IF EXISTS crm_profile ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;")
    cursor.execute("ALTER TABLE IF EXISTS crm_contactlog ADD COLUMN IF NOT EXISTS photo_file varchar(300) NOT NULL DEFAULT '';")
    cursor.execute("ALTER TABLE IF EXISTS crm_contract ADD COLUMN IF NOT EXISTS attachment_file varchar(300) NOT NULL DEFAULT '';")
PY
python manage.py shell <<'PY'
from django.contrib.auth.models import User
from crm.models import Profile

for user in User.objects.filter(profile__isnull=True):
    Profile.objects.get_or_create(user=user)
PY
if [ "${1:-}" = "gunicorn" ]; then
  if [ ! -f /app/imports/.customer_no_nqkh_renumber_done ]; then
    python manage.py renumber_customer_numbers > /app/imports/customer_no_nqkh_renumber_report.txt 2>&1
    date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.customer_no_nqkh_renumber_done
  fi
fi
if [ "${1:-}" = "gunicorn" ] && [ -f /app/imports/e-office_客户信息_20260623170146.xlsx ] && [ ! -f /app/imports/.eoffice_account_source_20260623170146_done ]; then
  python manage.py fill_eoffice_account_source /app/imports/e-office_客户信息_20260623170146.xlsx --overwrite > /app/imports/eoffice_account_source_20260623170146_report.txt 2>&1
  date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.eoffice_account_source_20260623170146_done
fi
if [ "${1:-}" = "gunicorn" ] && [ -f /app/imports/e-office_客户信息_20260623170146.xlsx ] && [ ! -f /app/imports/.eoffice_contact_logs_20260623170146_done ]; then
  python manage.py import_eoffice_contact_logs /app/imports/e-office_客户信息_20260623170146.xlsx > /app/imports/eoffice_contact_logs_20260623170146_report.txt 2>&1
  date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.eoffice_contact_logs_20260623170146_done
fi
if [ "${1:-}" = "gunicorn" ] && [ ! -f /app/imports/.reminders_cleared_for_followup_log_done ]; then
  python manage.py clear_reminders > /app/imports/reminders_cleared_for_followup_log_report.txt 2>&1
  date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.reminders_cleared_for_followup_log_done
fi
if [ "${1:-}" = "gunicorn" ] && [ ! -f /app/imports/.blank_number_only_customers_cleanup_20260625_v1_done ]; then
  python manage.py cleanup_blank_number_only_customers --confirm > /app/imports/blank_number_only_customers_cleanup_20260625_v1_report.txt 2>&1
  date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.blank_number_only_customers_cleanup_20260625_v1_done
fi
if [ "${1:-}" = "gunicorn" ]; then
  python manage.py seed_roles
  if [ ! -f /app/imports/.sales_process_upgrade_20260626_v1_done ]; then
    python manage.py generate_missing_numbers > /app/imports/sales_process_upgrade_20260626_v1_report.txt 2>&1
    python manage.py rebuild_payment_summary >> /app/imports/sales_process_upgrade_20260626_v1_report.txt 2>&1
    echo "rebuild_customer_summary skipped during startup; run manually during a maintenance window if needed." >> /app/imports/sales_process_upgrade_20260626_v1_report.txt
    date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.sales_process_upgrade_20260626_v1_done
  fi
  python manage.py create_default_admin

  python manage.py normalize_public_pool_ownership
  python manage.py restore_public_pool_exempt_customers
  python manage.py collectstatic --noinput
fi
if [ "${1:-}" = "python" ] && [ "${2:-}" = "manage.py" ] && [ "${3:-}" = "run_feishu_sync_loop" ] && [ ! -f /app/imports/.feishu_inquiry_resync_20260624_v6_done ]; then
  python -u manage.py reset_feishu_inquiry_sync --confirm --sync-after > /app/imports/feishu_inquiry_resync_20260624_v6_report.txt 2>&1
  date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.feishu_inquiry_resync_20260624_v6_done
fi
if [ "${1:-}" = "python" ] && [ "${2:-}" = "manage.py" ] && [ "${3:-}" = "run_feishu_sync_loop" ] && [ -f /app/imports/legacy_song_customer_info.xlsx ] && [ -f /app/imports/legacy_he_xiaofang_inquiry.xlsx ] && [ ! -f /app/imports/.legacy_sales_customers_20260624_v1_done ]; then
  python -u manage.py import_legacy_sales_customer_sheets --song /app/imports/legacy_song_customer_info.xlsx --he /app/imports/legacy_he_xiaofang_inquiry.xlsx > /app/imports/legacy_sales_customers_20260624_v1_report.txt 2>&1
  date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.legacy_sales_customers_20260624_v1_done
fi
if [ "${1:-}" = "python" ] && [ "${2:-}" = "manage.py" ] && [ "${3:-}" = "run_feishu_sync_loop" ] && [ ! -f /app/imports/.source_channel_normalized_20260625_v1_done ]; then
  python -u manage.py normalize_source_channels > /app/imports/source_channel_normalized_20260625_v1_report.txt 2>&1
  date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.source_channel_normalized_20260625_v1_done
fi
if [ "${1:-}" = "python" ] && [ "${2:-}" = "manage.py" ] && [ "${3:-}" = "run_feishu_sync_loop" ] && [ ! -f /app/imports/.source_detail_restored_20260625_v1_done ]; then
  (
    python -u manage.py restore_detailed_source_channels > /app/imports/source_detail_restored_20260625_v1_report.txt 2>&1
    date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/imports/.source_detail_restored_20260625_v1_done
  ) &
fi

exec "$@"
