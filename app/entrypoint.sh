#!/bin/sh
set -e

mkdir -p /app/imports

echo "Starting Django CRM template. This is a sanitized template build, not a production backup."

python manage.py migrate --noinput

if [ "${1:-}" = "gunicorn" ]; then
  python manage.py seed_roles
  python manage.py create_default_admin

  if [ "${CRM_TEMPLATE_RUN_DATA_REPAIR:-0}" = "1" ]; then
    echo "CRM_TEMPLATE_RUN_DATA_REPAIR=1: running optional template data-repair commands. Review before using with real data."
    python manage.py generate_missing_numbers > /app/imports/template_data_repair_report.txt 2>&1
    python manage.py rebuild_payment_summary >> /app/imports/template_data_repair_report.txt 2>&1
    echo "rebuild_customer_summary skipped during startup; run manually during a maintenance window if needed." >> /app/imports/template_data_repair_report.txt
  else
    echo "Skipping historical data-repair/import commands. Set CRM_TEMPLATE_RUN_DATA_REPAIR=1 only after review."
  fi

  python manage.py collectstatic --noinput
fi

exec "$@"