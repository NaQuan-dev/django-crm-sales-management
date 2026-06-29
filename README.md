# Django CRM Sales Management Template

> **Template only.** This repository is a sanitized public template. It is not a production backup and must not be connected to production data, credentials, Feishu/Lark apps, or customer files without a security review.

This Django CRM template covers customer records, follow-up logs, quotations, contracts/payments, public-pool/recycle-bin workflows, reminders, and sales dashboard views.

The public repository intentionally excludes production databases, import workbooks, uploaded media, logs, real staff accounts, real customer data, deployment keys, and `.env` files.

## Local Setup

1. Copy the environment template:

   ```bash
   cp env.example .env
   ```

2. Replace every placeholder in `.env`, especially secret keys, database passwords, host names, admin credentials, and Feishu/Lark credentials.

3. Start the template stack with Docker:

   ```bash
   docker compose up -d --build
   ```

4. Run migrations and create an administrator if you are not using the container entrypoint:

   ```bash
   python manage.py migrate
   python manage.py createsuperuser
   ```

## Template Safety Notes

- `env.example` and `.env.example` are templates only. Never deploy them as-is.
- `docker-compose.yml` is a development/template compose file. Review domains, storage, network settings, backups, and secrets before production use.
- `CRM_TEMPLATE_RUN_DATA_REPAIR=0` by default. Historical repair/import commands are disabled unless you intentionally enable them.
- Feishu/Lark synchronization is disabled until real credentials and `FEISHU_SYNC_SOURCES_JSON` are provided.
- Do not commit `.env`, databases, import files, uploaded media, logs, or generated archives.

## Directory Layout

- `app/`: Django project code.
- `docs/`: English template documentation.
- `env.example`: placeholder environment variables.
- `docker-compose.yml`: local/template deployment example.
- `TEMPLATE_NOTICE.md`: short reminder that this is a sanitized template.

## Verification

The sanitized template was checked with:

```bash
python manage.py check
python manage.py test crm.tests --noinput
```